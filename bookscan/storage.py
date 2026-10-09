"""Immutable capture directories: JPEG + metadata published as one transaction."""
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import time
from PIL import Image


def utc():
    return datetime.now(timezone.utc).isoformat()


def decode(data, expected=(4160, 3120)):
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValueError('Incomplete JPEG markers.')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'JPEG' or image.size != tuple(expected):
            raise ValueError(f'Expected JPEG {expected}; received {image.format} {image.size}.')
        if image.mode not in ('RGB', 'YCbCr'):
            raise ValueError(f'Colour JPEG required; received {image.mode}.')
        image.load()  # Full decode, not just header inspection. Truncated decode stays disabled.
        return image.copy()


def decode_preview(data, max_size=(1280, 960)):
    """Decode a reduced live-analysis image; source JPEG bytes remain untouched."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'JPEG':
                raise ValueError(f'Expected JPEG; received {image.format}.')
            image.draft('RGB', max_size)
            image.load()
            image.thumbnail(max_size, Image.Resampling.BILINEAR)
            return image.convert('RGB')
    except (OSError, SyntaxError) as exc:
        raise ValueError(f'Preview JPEG decode failed: {exc}') from exc


def validate_jpeg_bytes(data, expected=(4160, 3120)):
    """Fully decode and validate capture bytes without retaining a pixel copy."""
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValueError('Incomplete JPEG markers.')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'JPEG' or image.size != tuple(expected):
            raise ValueError(f'Expected JPEG {expected}; received {image.format} {image.size}.')
        if image.mode not in ('RGB', 'YCbCr'):
            raise ValueError(f'Colour JPEG required; received {image.mode}.')
        image.load()
    return True


def inspect_jpeg(path, expected=(4160, 3120), full=False):
    """Validate JPEG identity/header without retaining a decoded pixel buffer."""
    path = Path(path)
    size = path.stat().st_size
    if size < 4:
        raise ValueError('JPEG is too short to contain markers.')
    with path.open('rb') as handle:
        if handle.read(2) != b'\xff\xd8':
            raise ValueError('Missing JPEG start marker.')
        handle.seek(-2, os.SEEK_END)
        if handle.read(2) != b'\xff\xd9':
            raise ValueError('Missing JPEG end marker (truncated or incomplete file).')
    try:
        with Image.open(path) as image:
            if image.format != 'JPEG' or image.size != tuple(expected):
                raise ValueError(f'Expected JPEG {expected}; received {image.format} {image.size}.')
            if image.mode not in ('RGB', 'YCbCr'):
                raise ValueError(f'Colour JPEG required; received {image.mode}.')
            if full:
                image.load()
    except (OSError, SyntaxError) as exc:
        raise ValueError(f'JPEG header/decode validation failed: {exc}') from exc
    return size


def hash_file(path, cancel=None, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise AuditCancelled('Session audit cancelled.')
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class AuditCancelled(Exception):
    pass


def publish_exclusive(source, destination):
    # Linux renameat2 guarantees no replacement even if a non-cooperating writer
    # creates the destination between our collision check and publication.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            # Some host filesystems lack RENAME_NOREPLACE. The session flock
            # serializes application writers; rename cannot replace a populated
            # capture directory. Recheck collisions before the portable rename.
            if Path(destination).exists():
                raise FileExistsError(str(destination))
            os.rename(source, destination)
        else:
            raise OSError(code, os.strerror(code), str(destination))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, data):
    started = time.monotonic()
    with open(path, 'xb') as handle:
        handle.write(data)
        handle.flush()
        write_finished = time.monotonic()
        os.fsync(handle.fileno())
    return {'write_seconds': write_finished-started,
            'fsync_seconds': time.monotonic()-write_finished}


class Session:
    def __init__(self, root, size=(4160, 3120), audit_mode='quick',
                 progress=None, cancel=None):
        self.root = Path(root).resolve()
        self.size = size
        self.root.mkdir(parents=True, exist_ok=True)
        sync_dir(self.root.parent)
        self.lock = open(self.root / '.capture.lock', 'a+b')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.audit_mode = audit_mode
            self.records = self.audit(mode=audit_mode, progress=progress, cancel=cancel)
        except BaseException:
            self.lock.close()
            raise

    def audit(self, mode=None, progress=None, cancel=None):
        mode = mode or getattr(self, 'audit_mode', 'quick')
        if mode not in ('quick', 'full', 'recovery'):
            raise ValueError(f'Unknown audit mode: {mode}')
        records = []
        self.recovery_issues = []
        folders = sorted(self.root.glob('[0-9]' * 6))
        total = len(folders)
        for index, folder in enumerate(folders, 1):
            if cancel is not None and cancel.is_set():
                raise AuditCancelled('Session audit cancelled.')
            try:
                meta_path, image_path = folder / 'capture.json', folder / 'original.jpg'
                if not meta_path.is_file() or not image_path.is_file():
                    raise ValueError('Missing capture metadata or original JPEG.')
                meta = json.loads(meta_path.read_text())
                if meta.get('sequence') != int(folder.name) or tuple(meta.get('dimensions', ())) != tuple(self.size):
                    raise ValueError('Sequence or dimensions disagree with capture metadata.')
                actual_size = inspect_jpeg(image_path, self.size, full=(mode == 'full'))
                if meta.get('bytes') != actual_size:
                    raise ValueError('JPEG file size differs from capture metadata.')
                actual_hash = hash_file(image_path, cancel=cancel)
                if actual_hash != meta.get('sha256'):
                    raise ValueError('JPEG SHA-256 differs from capture metadata.')
            except AuditCancelled:
                raise
            except Exception as exc:
                if mode == 'recovery':
                    self.recovery_issues.append({'path': str(folder), 'issue': str(exc)})
                    if progress:
                        progress(index, total, folder.name, f'problem: {exc}')
                    continue
                raise ValueError(f'Corrupt capture record {folder}: {exc}') from exc
            records.append(meta)
            if progress:
                progress(index, total, folder.name, 'verified')
        if mode == 'recovery':
            for pending in sorted(self.root.glob('.pending-*')):
                self.recovery_issues.append({'path': str(pending), 'issue': 'Unpublished staging directory; preserved for manual inspection.'})
        if mode != 'recovery' and [r['sequence'] for r in records] != list(range(1, len(records) + 1)):
            raise ValueError('Capture sequence has a gap; inspect session before resuming.')
        return records

    def next_sequence(self):
        """Use verified metadata records; never reuse an occupied numeric path."""
        candidate = max((r['sequence'] for r in self.records), default=0) + 1
        if (self.root / f'{candidate:06d}').exists():
            raise FileExistsError(f'Next capture path is occupied and is not verified: {self.root / f"{candidate:06d}"}')
        return candidate

    def accept(self, data, metadata):
        timing = {}
        started = time.monotonic()
        validate_jpeg_bytes(data, self.size)
        timing['input_full_decode_validate_seconds'] = time.monotonic()-started
        sequence = self.next_sequence()
        started = time.monotonic()
        digest = hashlib.sha256(data).hexdigest()
        timing['input_hash_seconds'] = time.monotonic()-started
        record = dict(metadata, sequence=sequence, sha256=digest, bytes=len(data),
                      dimensions=list(self.size), saved_utc=utc(), original='original.jpg')
        # A whole directory rename publishes both files atomically. Staging is retained
        # on failure for diagnosis and never counted as an accepted capture.
        started = time.monotonic()
        stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=self.root))
        timing['staging_directory_create_seconds'] = time.monotonic()-started
        started = time.monotonic()
        original_write = write_file(stage / 'original.jpg', data)
        timing['original_write_seconds'] = time.monotonic()-started
        timing['original_file_write_seconds'] = original_write['write_seconds']
        timing['original_file_fsync_seconds'] = original_write['fsync_seconds']
        started = time.monotonic()
        reread = (stage / 'original.jpg').read_bytes()
        timing['readback_read_seconds'] = time.monotonic()-started
        started = time.monotonic()
        if hashlib.sha256(reread).hexdigest() != digest:
            raise ValueError('Saved JPEG hash differs from camera bytes.')
        timing['readback_hash_seconds'] = time.monotonic()-started
        # The input was fully decoded above; matching read-back hash proves the
        # committed candidate is byte-identical, so another full pixel decode is
        # redundant. Check its JPEG header before publication as an extra guard.
        started = time.monotonic()
        inspect_jpeg(stage / 'original.jpg', self.size, full=False)
        timing['readback_header_seconds'] = time.monotonic()-started
        started = time.monotonic()
        metadata_write = write_file(stage / 'capture.json', (json.dumps(record, indent=2) + '\n').encode())
        timing['metadata_write_seconds'] = time.monotonic()-started
        timing['metadata_file_write_seconds'] = metadata_write['write_seconds']
        timing['metadata_file_fsync_seconds'] = metadata_write['fsync_seconds']
        started = time.monotonic()
        sync_dir(stage)
        timing['staging_directory_fsync_seconds'] = time.monotonic()-started
        final = self.root / f'{sequence:06d}'
        if final.exists():
            raise FileExistsError(f'Capture collision: {final}')
        started = time.monotonic()
        publish_exclusive(stage, final)
        timing['exclusive_publish_seconds'] = time.monotonic()-started
        started = time.monotonic()
        sync_dir(self.root)  # No success signal or sequence advance before this.
        timing['session_directory_fsync_seconds'] = time.monotonic()-started
        self.records.append(record)
        self.last_accept_timing = timing
        return record

    def event(self, event, **details):
        # Decisions/rejections never rewrite captures or advance the capture sequence.
        with open(self.root / 'events.jsonl', 'ab') as handle:
            handle.write((json.dumps(dict(utc=utc(), event=event, **details)) + '\n').encode())
            handle.flush()
            os.fsync(handle.fileno())
        sync_dir(self.root)

    def close(self):
        self.lock.close()
