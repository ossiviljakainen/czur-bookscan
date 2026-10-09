"""Single-handle Linux V4L2 MJPEG streaming; ABI mirrors linux/videodev2.h."""
import ctypes as C
import errno
import fcntl
import mmap
import os
from pathlib import Path
import select
import time

U32 = C.c_uint32

class Capability(C.Structure):
    _fields_ = [('driver', C.c_char * 16), ('card', C.c_char * 32),
                ('bus_info', C.c_char * 32), ('version', U32),
                ('capabilities', U32), ('device_caps', U32), ('reserved', U32 * 3)]

class Pix(C.Structure):
    _fields_ = [(k, U32) for k in ('width', 'height', 'pixelformat', 'field',
                 'bytesperline', 'sizeimage', 'colorspace', 'priv', 'flags',
                 'ycbcr_enc', 'quantization', 'xfer_func')]

class FormatUnion(C.Union):
    _fields_ = [('pix', Pix), ('raw', C.c_ubyte * 200), ('alignment', C.c_void_p)]

class Format(C.Structure):
    _fields_ = [('type', U32), ('fmt', FormatUnion)]

class Request(C.Structure):
    _fields_ = [('count', U32), ('type', U32), ('memory', U32),
                ('capabilities', U32), ('flags', C.c_ubyte), ('reserved', C.c_ubyte * 3)]

class Timeval(C.Structure):
    _fields_ = [('seconds', C.c_long), ('microseconds', C.c_long)]

class Location(C.Union):
    _fields_ = [('offset', U32), ('userptr', C.c_ulong), ('planes', C.c_void_p),
                ('fd', C.c_int32)]

class Buffer(C.Structure):
    _fields_ = [('index', U32), ('type', U32), ('bytesused', U32), ('flags', U32),
                ('field', U32), ('timestamp', Timeval), ('timecode', C.c_ubyte * 16),
                ('sequence', U32), ('memory', U32), ('m', Location), ('length', U32),
                ('reserved2', U32), ('request_fd', C.c_int32)]

class StreamParm(C.Structure):
    _fields_ = [('type', U32), ('raw', U32 * 50)]

class Control(C.Structure):
    _fields_ = [('id', U32), ('value', C.c_int32)]

class QueryControl(C.Structure):
    _fields_ = [('id', U32), ('type', U32), ('name', C.c_char * 32),
                ('minimum', C.c_int32), ('maximum', C.c_int32), ('step', C.c_int32),
                ('default_value', C.c_int32), ('flags', U32), ('reserved', U32 * 2)]


def ioctl(fd, number, obj, direction=3):
    request = (direction << 30) | (C.sizeof(obj) << 16) | (ord('V') << 8) | number
    fcntl.ioctl(fd, request, obj)
    return obj


def query(fd):
    cap = ioctl(fd, 0, Capability(), 2)
    flags = cap.device_caps if cap.capabilities & 0x80000000 else cap.capabilities
    if not (flags & 1 and flags & 0x04000000):
        raise ValueError('Device is not a streaming image-capture interface (metadata excluded).')
    return {k: getattr(cap, k).decode(errors='replace') for k in ('driver', 'card', 'bus_info')}


def usb_identity(node):
    path = (Path('/sys/class/video4linux') / Path(node).name / 'device').resolve()
    for parent in (path, *path.parents):
        if (parent / 'idVendor').exists():
            def read(name):
                p = parent / name
                return p.read_text().strip() if p.exists() else None
            return dict(vendor=read('idVendor'), product=read('idProduct'),
                        serial=read('serial'), usb_path=str(parent))
    return {}


def discover(device=None):
    candidates, errors = [], []
    nodes = [Path(device)] if device else sorted(Path('/dev').glob('video*'))
    for node in nodes:
        identity = usb_identity(node)
        if (identity.get('vendor'), identity.get('product')) != ('1e4f', '1309'):
            if device:
                raise ValueError(f'{node} is not USB 1e4f:1309; refusing an unrelated camera.')
            continue
        try:
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
            try:
                details = query(fd)
            finally:
                os.close(fd)
            candidates.append(dict(device=str(node), **identity, **details))
        except (OSError, ValueError) as exc:
            errors.append(f'{node}: {exc}')
    if len(candidates) != 1:
        raise RuntimeError('Expected one CZUR image interface; found '
                           f'{len(candidates)}. Connect scanner/check permissions or use --device. '
                           + '; '.join(errors))
    return candidates[0]


class Camera:
    def __init__(self, device, size=(4160, 3120)):
        self.fd = None
        self.maps = []
        self.streaming = False
        self.size = size
        try:
            self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
            self.settings = query(self.fd)
            fmt = Format(type=1)
            ioctl(self.fd, 4, fmt)
            fmt.fmt.pix.width, fmt.fmt.pix.height = size
            fmt.fmt.pix.pixelformat = int.from_bytes(b'MJPG', 'little')
            fmt.fmt.pix.field = 0
            ioctl(self.fd, 5, fmt)
            ioctl(self.fd, 4, fmt)  # Verify on the SAME handle before streaming.
            pix = fmt.fmt.pix
            if (pix.width, pix.height, pix.pixelformat) != (*size, int.from_bytes(b'MJPG', 'little')):
                raise RuntimeError(f'Driver negotiated {pix.width} × {pix.height}, '
                                   f'{pix.pixelformat:#x}; require {size} MJPG.')
            self.settings.update({name: getattr(pix, name) for name, _ in Pix._fields_})
            self.settings['fourcc'] = 'MJPG'
            self.settings['controls'] = self.controls()
            try:
                parm = ioctl(self.fd, 21, StreamParm(type=1))
                self.settings['frame_interval'] = {'numerator': parm.raw[2], 'denominator': parm.raw[3]}
            except OSError as exc:
                self.settings['frame_interval_error'] = str(exc)
            req = ioctl(self.fd, 8, Request(count=4, type=1, memory=1))
            if req.count < 2:
                raise RuntimeError('Insufficient mmap buffers.')
            for i in range(req.count):
                buf = ioctl(self.fd, 9, Buffer(index=i, type=1, memory=1))
                self.maps.append(mmap.mmap(self.fd, buf.length, flags=mmap.MAP_SHARED,
                                 prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=buf.m.offset))
                ioctl(self.fd, 15, buf)
            ioctl(self.fd, 18, C.c_int(1), 1)
            self.streaming = True
        except BaseException:
            self.close()
            raise

    def controls(self):
        result = []
        next_id = 0x80000000
        for _ in range(512):
            try:
                q = ioctl(self.fd, 36, QueryControl(id=next_id))
            except OSError as exc:
                if exc.errno != errno.EINVAL:
                    result.append({'query_error': str(exc)})
                break
            next_id = q.id | 0x80000000
            entry = dict(id=q.id, name=q.name.decode(errors='replace'), type=q.type,
                         flags=q.flags, minimum=q.minimum, maximum=q.maximum,
                         default=q.default_value)
            try:
                entry['value'] = ioctl(self.fd, 27, Control(id=q.id)).value
            except OSError as exc:
                entry['read_error'] = str(exc)
            result.append(entry)
        return result

    def frame(self, timeout=5):
        frame_started = time.monotonic()
        wait_started = frame_started
        if not select.select([self.fd], [], [], timeout)[0]:
            raise TimeoutError('No camera frame for 5 seconds; reconnect scanner and restart.')
        ready_at = time.monotonic()
        dequeue_started = ready_at
        buf = ioctl(self.fd, 17, Buffer(type=1, memory=1))
        try:
            if buf.flags & 0x0040 or not buf.bytesused or buf.bytesused > len(self.maps[buf.index]):
                raise RuntimeError('Driver reported an invalid/error frame.')
            captured = (buf.timestamp.seconds + buf.timestamp.microseconds/1e6
                        if buf.flags & 0xe000 == 0x2000 else None)
            data = self.maps[buf.index][:buf.bytesused]
            copied_at = time.monotonic()
            return (data,
                    dict(driver_sequence=buf.sequence, timestamp_seconds=buf.timestamp.seconds,
                         timestamp_microseconds=buf.timestamp.microseconds,
                         buffer_flags=buf.flags, captured_monotonic=captured, received_monotonic=copied_at,
                         acquisition_timing=dict(camera_wait_seconds=ready_at-wait_started,
                             camera_dequeue_copy_seconds=copied_at-dequeue_started,
                             camera_frame_total_seconds=copied_at-frame_started,
                             transport_format='MJPEG; frame encoding occurs in scanner hardware')))
        finally:
            ioctl(self.fd, 15, buf)

    def close(self):
        if self.fd is not None:
            if self.streaming:
                try:
                    ioctl(self.fd, 19, C.c_int(1), 1)
                except OSError:
                    pass
            for mapping in self.maps:
                mapping.close()
            os.close(self.fd)
            self.fd = None
