import ctypes as C
from dataclasses import asdict
import errno
import hashlib
import io
import json
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from bookscan import camera
from bookscan.checks import Config, Gate, signature, quality, difference, duplicate_distance
from bookscan.storage import Session, decode, AuditCancelled


def jpeg(size=(80, 60), colour=True):
    image = Image.new('RGB' if colour else 'L', size, (180, 120, 80) if colour else 180)
    output = io.BytesIO()
    image.save(output, 'JPEG')
    return output.getvalue()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'session'
        self.session = Session(self.root, (80, 60))
        self.data = jpeg()

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def test_exact_original_and_resume(self):
        first = self.session.accept(self.data, dict(trigger='manual', printed_pages='12–13'))
        self.assertEqual((self.root/'000001/original.jpg').read_bytes(), self.data)
        self.assertEqual(first['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(json.loads((self.root/'000001/capture.json').read_text()), first)
        self.session.close()
        self.session = Session(self.root, (80, 60))
        self.assertEqual(self.session.accept(self.data, {})['sequence'], 2)

    def test_second_writer_excluded(self):
        with self.assertRaises(BlockingIOError):
            Session(self.root, (80, 60))

    def test_bad_decode_dimensions_and_colour_do_not_advance(self):
        for data in (self.data[:-10], b'not a jpeg', jpeg((81, 60)), jpeg(colour=False)):
            with self.assertRaises(ValueError):
                self.session.accept(data, {})
        self.assertEqual(self.session.records, [])
        self.assertFalse((self.root/'000001').exists())

    def test_write_failure_never_advances(self):
        with patch('bookscan.storage.write_file', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.session.accept(self.data, {})
        self.assertEqual(self.session.records, [])
        self.assertFalse((self.root/'000001').exists())

    def test_readback_corruption_rejected(self):
        original_read = Path.read_bytes
        def corrupt(path):
            data = original_read(path)
            return jpeg((81, 60)) if path.name == 'original.jpg' else data
        with patch.object(Path, 'read_bytes', corrupt):
            with self.assertRaises(ValueError):
                self.session.accept(self.data, {})
        self.assertFalse((self.root/'000001').exists())
        self.assertEqual(len(self.session.records), 0)

    def test_collision_preserves_existing_files(self):
        (self.root/'000001').mkdir()
        (self.root/'000001/important').write_text('preserve')
        with self.assertRaises(FileExistsError):
            self.session.accept(self.data, {})
        self.assertEqual((self.root/'000001/important').read_text(), 'preserve')
        self.assertEqual(len(self.session.records), 0)

    def test_final_fsync_failure_requires_restart_without_success(self):
        from bookscan.storage import sync_dir
        def fail_final(path):
            if path == self.root:
                raise OSError('sync failure')
            sync_dir(path)
        with patch('bookscan.storage.sync_dir', side_effect=fail_final):
            with self.assertRaises(OSError):
                self.session.accept(self.data, {})
        self.assertEqual(self.session.records, [])
        self.assertTrue((self.root/'000001/original.jpg').exists())
        self.session.close()
        self.session = Session(self.root, (80, 60))
        self.assertEqual(len(self.session.records), 1)

    def test_resume_detects_tampering_and_gaps(self):
        self.session.accept(self.data, {})
        (self.root/'000001/original.jpg').write_bytes(jpeg((81, 60)))
        with self.assertRaises(ValueError):
            self.session.audit()
        (self.root/'000001/original.jpg').write_bytes(self.data)
        (self.root/'000001').rename(self.root/'000002')
        with self.assertRaises(ValueError):
            self.session.audit()

    def test_pending_and_rejections_are_not_captures(self):
        (self.root/'.pending-crash').mkdir()
        self.session.event('rejected_attempt', reason='blur')
        self.assertEqual(self.session.audit(), [])
        self.assertEqual(len(self.session.records), 0)

    def _make_verified_record(self, sequence, data=None):
        data = data or self.data
        folder = self.root / f'{sequence:06d}'
        folder.mkdir()
        (folder/'original.jpg').write_bytes(data)
        record = dict(sequence=sequence, dimensions=list(self.session.size), bytes=len(data),
                      sha256=hashlib.sha256(data).hexdigest(), original='original.jpg')
        (folder/'capture.json').write_text(json.dumps(record))
        return record

    def test_quick_resume_audit_does_not_decode_old_images_and_preserves_them(self):
        large_size=(4160,3120)
        large_data=jpeg(large_size)
        self.session.close()
        self.session=Session(self.root,large_size)
        before=[]
        for sequence in range(1,33):
            self._make_verified_record(sequence,large_data)
            before.append(hashlib.sha256((self.root/f'{sequence:06d}/original.jpg').read_bytes()).hexdigest())
        self.session.close()
        with patch('PIL.Image.Image.load', side_effect=AssertionError('old image fully decoded')):
            self.session=Session(self.root,large_size,audit_mode='quick')
        after=[hashlib.sha256((self.root/f'{sequence:06d}/original.jpg').read_bytes()).hexdigest() for sequence in range(1,33)]
        self.assertEqual(before,after)
        self.assertEqual(self.session.next_sequence(),33)
        self.assertEqual(self.session.accept(large_data,{})['sequence'],33)

    def test_truncated_jpeg_is_reported_without_rewriting_valid_old_capture(self):
        self._make_verified_record(1)
        good=(self.root/'000001/original.jpg').read_bytes()
        self._make_verified_record(2,self.data[:-2])
        self.session.close()
        with self.assertRaisesRegex(ValueError,'end marker|Corrupt capture'):
            Session(self.root,(80,60),audit_mode='quick')
        self.assertEqual((self.root/'000001/original.jpg').read_bytes(),good)
        self.assertEqual((self.root/'000002/original.jpg').read_bytes(),self.data[:-2])

    def test_missing_original_is_reported(self):
        self._make_verified_record(1)
        (self.root/'000001/original.jpg').unlink()
        self.session.close()
        with self.assertRaisesRegex(ValueError,'Missing capture metadata or original JPEG'):
            Session(self.root,(80,60),audit_mode='quick')

    def test_recovery_audit_skips_incomplete_number_without_overwrite(self):
        self._make_verified_record(1)
        (self.root/'000002').mkdir()
        (self.root/'000002/original.jpg').write_bytes(b'incomplete')
        self._make_verified_record(3)
        self.session.close()
        self.session=Session(self.root,(80,60),audit_mode='recovery')
        self.assertEqual([r['sequence'] for r in self.session.records],[1,3])
        self.assertEqual(self.session.accept(self.data,{})['sequence'],4)
        self.assertEqual((self.root/'000002/original.jpg').read_bytes(),b'incomplete')

    def test_audit_cancellation_leaves_originals_unchanged(self):
        self._make_verified_record(1)
        before=(self.root/'000001/original.jpg').read_bytes()
        cancel=threading.Event();cancel.set()
        self.session.close()
        with self.assertRaises(AuditCancelled):
            Session(self.root,(80,60),cancel=cancel)
        self.assertEqual((self.root/'000001/original.jpg').read_bytes(),before)

    def test_full_audit_decodes_without_returning_pixel_copy(self):
        self._make_verified_record(1)
        self.session.close()
        with patch('PIL.Image.Image.copy', side_effect=AssertionError('audit copied decoded pixels')):
            self.session=Session(self.root,(80,60),audit_mode='full')
        self.assertEqual(len(self.session.records),1)

    def test_camera_absence_is_reported_without_blocking_session_open(self):
        from bookscan.app import Reader
        self.session.accept(self.data,{})
        with patch('bookscan.app.discover',side_effect=RuntimeError('scanner absent')):
            reader=Reader(None)
            kind,detail=reader.messages.get(timeout=2)
            self.assertEqual(kind,'error')
            self.assertIn('scanner absent',detail)
            self.assertEqual(len(self.session.records),1)
            reader.close()


class GateTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(settle_seconds=1)
        self.gate = Gate(self.config)
        rng = np.random.default_rng(10)
        self.a = rng.integers(40, 220, (2, 96, 128)).astype(np.float32)
        self.b = rng.integers(40, 220, (2, 96, 128)).astype(np.float32)

    def settle(self, sig, start=0):
        results = [self.gate.update(sig, start+t) for t in (0, .4, .8, 1.2)]
        return results[-1]

    def test_stable_acceptance_then_static_latch(self):
        self.assertTrue(self.settle(self.a))
        self.gate.latch(self.a, accepted=True)
        for t in range(2, 10):
            self.assertFalse(self.gate.update(self.a, t))

    def test_real_turn_then_new_still_page_rearms(self):
        self.gate.latch(self.a, accepted=True)
        self.assertTrue(self.settle(self.b))

    def test_hand_motion_then_return_to_same_page_stays_latched(self):
        self.gate.latch(self.a, accepted=True)
        self.gate.update(self.b, 0)
        self.assertFalse(self.settle(self.a, .4))

    def test_local_motion_and_lighting_do_not_rearm(self):
        self.gate.latch(self.a, accepted=True)
        changed = self.a.copy()
        changed[:, :16, :16] = 255
        self.assertFalse(self.settle(changed))
        self.assertFalse(self.settle(self.a+15, 2))
        self.assertLess(difference(self.a, self.a+15), .0001)

    def test_slow_drift_never_settles(self):
        for i in range(12):
            sig = self.a.copy()
            sig[:, :48] += i*4
            self.assertFalse(self.gate.update(sig, i*.25))

    def test_missing_frames_do_not_count_as_stability(self):
        for t in (0, 2, 4, 6):
            self.assertFalse(self.gate.update(self.a, t))

    def test_rejection_does_not_auto_repeat(self):
        self.gate.latch(self.a)
        self.assertFalse(self.settle(self.a))
        self.gate.retry()
        self.assertTrue(self.settle(self.a, 2))

    def test_pose_tolerant_duplicate(self):
        moved = np.roll(self.a, (2, -2), axis=(1, 2))
        self.assertLess(duplicate_distance(self.a, moved), self.config.duplicate_difference)

    def test_blanks_need_explicit_kind_and_sharpness_is_optional(self):
        blank = np.full_like(self.a, 220)
        self.assertTrue(quality(blank, self.config)[1])
        self.assertFalse(quality(blank, self.config, 'intentional blank')[1])
        self.assertFalse(quality(self.a, self.config, 'cover')[1])

    def test_outside_regions_ignored(self):
        image = Image.new('RGB', (400, 300), 'white')
        first = signature(image, self.config)
        image.paste('black', (0, 0, 400, 40))
        self.assertEqual(difference(first, signature(image, self.config)), 0)

    def test_bad_config_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'config.json'
            for changes in ({'rois': [[.8, .2, .1, .9]]}, {'settle_seconds': 0},
                            {'changed_tiles': 2}, {'stable_difference': .5}):
                path.write_text(json.dumps(dict(asdict(self.config), **changes)))
                with self.assertRaises(ValueError):
                    Config.load(path)


class CameraTests(unittest.TestCase):
    def test_device_caps_exclude_metadata_even_with_aggregate_capture_flag(self):
        def fake(fd, number, obj, direction=3):
            obj.capabilities = 0x80000001 | 0x04000000
            obj.device_caps = 0x00800000 | 0x04000000
            return obj
        with patch('bookscan.camera.ioctl', side_effect=fake):
            with self.assertRaises(ValueError):
                camera.query(10)

    def test_same_handle_negotiation_stream_and_original_bytes(self):
        calls = []
        data = jpeg()
        memory = bytearray(4096)
        memory[:len(data)] = data
        class Mapping:
            def __len__(self): return len(memory)
            def __getitem__(self, key): return memory[key]
            def close(self): pass
        def fake(fd, number, obj, direction=3):
            self.assertEqual(fd, 42)
            calls.append(number)
            if number == 0:
                obj.capabilities = 1 | 0x04000000
                obj.card = b'ET13S'
            elif number == 4:
                obj.fmt.pix.width, obj.fmt.pix.height = 4160, 3120
                obj.fmt.pix.pixelformat = int.from_bytes(b'MJPG', 'little')
            elif number == 36:
                raise OSError(errno.EINVAL, 'no more controls')
            elif number == 9:
                obj.length = 4096
            elif number == 17:
                obj.bytesused = len(data)
                obj.sequence = 123
            return obj
        with patch('bookscan.camera.os.open', return_value=42) as opening, \
             patch('bookscan.camera.os.close') as closing, \
             patch('bookscan.camera.ioctl', side_effect=fake), \
             patch('bookscan.camera.mmap.mmap', return_value=Mapping()), \
             patch('bookscan.camera.select.select', return_value=([42], [], [])):
            capture = camera.Camera('/dev/video7')
            received, meta = capture.frame()
            self.assertEqual(received, data)
            self.assertEqual(meta['driver_sequence'], 123)
            self.assertLess(calls.index(5), calls.index(18))
            self.assertEqual(calls.count(4), 2)
            self.assertEqual(opening.call_count, 1)
            capture.close()
            closing.assert_called_once_with(42)
            self.assertIn(19, calls)

    def test_dimension_fallback_fails_before_streaming(self):
        calls = []
        def fake(fd, number, obj, direction=3):
            calls.append(number)
            if number == 0:
                obj.capabilities = 1 | 0x04000000
            if number == 4:
                obj.fmt.pix.width, obj.fmt.pix.height = 1920, 1080
            return obj
        with patch('bookscan.camera.os.open', return_value=42), \
             patch('bookscan.camera.os.close') as closing, \
             patch('bookscan.camera.ioctl', side_effect=fake):
            with self.assertRaises(RuntimeError):
                camera.Camera('/dev/video7')
            self.assertNotIn(18, calls)
            closing.assert_called_once_with(42)


if __name__ == '__main__':
    unittest.main()
