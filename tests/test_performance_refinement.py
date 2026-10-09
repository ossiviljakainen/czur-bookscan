import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image
from bookscan.app import default_review_count
from bookscan.audio import Audio
from bookscan.checks import Config, Gate
from bookscan.guides import Guides, render
from bookscan.storage import Session, AuditCancelled


def make_jpeg(size=(80,60), color=(200,190,170)):
    out=io.BytesIO();Image.new('RGB',size,color).save(out,'JPEG');return out.getvalue()


class PerformanceRefinementTests(unittest.TestCase):
    def test_quick_resume_does_not_full_decode_large_existing_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'session';session=Session(root,(80,60))
            data=make_jpeg()
            for _ in range(250):session.accept(data,{})
            session.close()
            with patch.object(Image.Image,'load',side_effect=AssertionError('unexpected pixel decode')):
                resumed=Session(root,(80,60))
            self.assertEqual(len(resumed.records),250);self.assertEqual(resumed.next_sequence(),251)
            resumed.close()

    def test_quick_resume_reports_truncated_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'session';session=Session(root,(80,60));record=session.accept(make_jpeg(),{});session.close()
            path=root/'000001/original.jpg';path.write_bytes(path.read_bytes()[:-2])
            with self.assertRaisesRegex(ValueError,'truncated|end marker|hash'):
                Session(root,(80,60))

    def test_cancellation_leaves_existing_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'session';session=Session(root,(80,60));session.accept(make_jpeg(),{});session.close()
            before={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file() and path.name!='.capture.lock'}
            cancel=threading.Event();cancel.set()
            with self.assertRaises(AuditCancelled):Session(root,(80,60),cancel=cancel)
            after={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file() and path.name!='.capture.lock'}
            self.assertEqual(before,after)

    def test_short_stillness_requires_three_observations_and_motion_resets(self):
        gate=Gate(Config(settle_seconds=.05));sig=np.zeros((2,96,128),np.float32)
        self.assertFalse(gate.update(sig,0.0));self.assertFalse(gate.update(sig,.04))
        self.assertTrue(gate.update(sig,.08))
        gate=Gate(Config(settle_seconds=.05));self.assertFalse(gate.update(sig,0.0))
        moved=sig.copy();moved[:,:,40:80]=50
        self.assertFalse(gate.update(moved,.04));self.assertEqual(gate.since,.04)
        self.assertFalse(gate.update(moved,.08));self.assertTrue(gate.update(moved,.10))

    def test_three_thumbnail_default_and_existing_session_preference(self):
        self.assertEqual(default_review_count({},0),'3')
        self.assertEqual(default_review_count({},219),'1')
        self.assertEqual(default_review_count({'count':1},0),'1')
        self.assertEqual(default_review_count({'count':'3'},219),'3')

    def test_cyan_check_overlay_does_not_change_source_or_page_mode_geometry(self):
        source=Image.new('RGB',(300,200),'white');original=source.tobytes();guides=Guides()
        box=[(.2,.2,.8,.8)];shown=render(source,guides,'spread',visible=False,check_regions=box)
        cyan=sum(1 for pixel in shown.getdata() if pixel==(70,210,240))
        self.assertGreater(cyan,0);self.assertEqual(source.tobytes(),original)
        self.assertEqual(shown.size,source.size)

    def test_audio_waiting_tone_is_audible_level_and_backend_failures_are_reported(self):
        audio=Audio(volume=.25);audio.volume=.25
        pcm,_,_=audio.waiting_pcm(elapsed=.3);samples=np.frombuffer(pcm,dtype='<i2')
        self.assertGreater(float(np.max(np.abs(samples))),1000)
        self.assertEqual(len(pcm),audio.CHUNK_SAMPLES*2)
        audio.close()
        class FailedProcess:
            returncode=1
            def __init__(self):
                import io
                self.stdin=io.BytesIO();self.stderr=io.BytesIO(b'no sink')
            def poll(self):return 1
        audio=Audio();audio.available=True
        try:
            with patch('bookscan.audio.subprocess.Popen',return_value=FailedProcess()):
                audio.test_waiting();event=audio.diagnostics.get(timeout=2)
                if event['event']=='started':event=audio.diagnostics.get(timeout=2)
            self.assertEqual(event['event'],'failed');self.assertIn('no sink',audio.error)
        finally:audio.close()

    def test_all_continuous_tone_variants_have_smooth_chunk_boundaries(self):
        audio=Audio(volume=.25)
        try:
            for variant in audio.WAIT_VARIANTS:
                phase=mod_phase=elapsed=0.0;chunks=[]
                for _ in range(16):
                    pcm,phase,mod_phase=audio.waiting_pcm(variant=variant,phase=phase,
                        modulation_phase=mod_phase,elapsed=elapsed)
                    chunks.append(np.frombuffer(pcm,dtype='<i2'))
                    elapsed+=audio.CHUNK_SAMPLES/audio.RATE
                joins=[abs(int(left[-1])-int(right[0])) for left,right in zip(chunks,chunks[1:])]
                self.assertLess(max(joins),1200,variant)
                levels=[float(np.sqrt(np.mean(chunk.astype(float)**2))) for chunk in chunks[5:]]
                if variant == 'gentle modulation':
                    self.assertLess((max(levels)-min(levels))/np.mean(levels),.08,variant)
                phase=mod_phase=elapsed=0.0;long=[]
                for _ in range(40):
                    pcm,phase,mod_phase=audio.waiting_pcm(variant=variant,phase=phase,
                        modulation_phase=mod_phase,elapsed=elapsed)
                    long.append(np.frombuffer(pcm,dtype='<i2'));elapsed+=audio.CHUNK_SAMPLES/audio.RATE
                signal=np.concatenate(long)[4000:].astype(float)
                frequencies=np.fft.rfftfreq(len(signal),1/audio.RATE);spectrum=np.abs(np.fft.rfft(signal))
                def peak_near(target):
                    region=np.flatnonzero(np.abs(frequencies-target)<2.5)
                    return frequencies[region[np.argmax(spectrum[region])]]
                if variant=='pure sine':self.assertAlmostEqual(peak_near(240),240,delta=1.5)
                elif variant=='two-tone':
                    self.assertAlmostEqual(peak_near(220),220,delta=1.5)
                    self.assertAlmostEqual(peak_near(260),260,delta=1.5)
                else:
                    self.assertAlmostEqual(peak_near(240),240,delta=1.5)
                    self.assertGreater(spectrum[np.argmin(abs(frequencies-237.5))],0)
        finally:audio.close()

    def test_waiting_stream_stops_when_state_ends_and_success_alert_still_plays(self):
        class FakePipe(io.BytesIO):
            def __init__(self,owner):super().__init__();self.owner=owner
            def write(self,data):
                written=super().write(data);self.owner.wrote.set();return written
            def close(self):super().close();self.owner.finished=True
        class FakeProcess:
            def __init__(self):
                self.wrote=threading.Event();self.finished=False;self.returncode=None
                self.stdin=FakePipe(self);self.stderr=io.BytesIO();self.command_data=None
            def poll(self):return self.returncode if self.finished else None
            def wait(self,timeout=None):self.finished=True;self.returncode=0;return 0
            def communicate(self,data,timeout=None):
                self.command_data=data;self.finished=True;self.returncode=0;return (b'',b'')
        made=[]
        def fake_popen(*_args,**_kwargs):
            process=FakeProcess();made.append(process);return process
        audio=Audio(volume=.25);audio.available=True;audio.waiting=True
        try:
            with patch('bookscan.audio.subprocess.Popen',side_effect=fake_popen):
                deadline=time.monotonic()+2
                while not made and time.monotonic()<deadline:time.sleep(.005)
                self.assertTrue(made);self.assertTrue(made[0].wrote.wait(1))
                audio.waiting=False
                stopped=audio.diagnostics.get(timeout=2)
                if stopped['event']=='started':stopped=audio.diagnostics.get(timeout=2)
                self.assertEqual(stopped['event'],'played');self.assertEqual(stopped['name'],'waiting')
                audio.signal('accepted')
                accepted=audio.diagnostics.get(timeout=2)
                self.assertEqual(accepted['event'],'played');self.assertEqual(accepted['name'],'accepted')
            self.assertEqual(made[1].command_data,audio.pcm('accepted'))
        finally:audio.close()

    def test_audio_diagnostic_log_is_separate_from_capture_records(self):
        with tempfile.TemporaryDirectory() as directory:
            audio=Audio();audio.log_path=Path(directory)/'audio-diagnostics.jsonl'
            try:
                audio.log_diagnostic(dict(event='played',name='test_waiting',queue_delay_seconds=.01))
                audio.log_diagnostic(dict(event='played',name='waiting',queue_delay_seconds=.01))
                rows=[json.loads(line) for line in audio.log_path.read_text().splitlines()]
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['name'],'test_waiting')
                self.assertIn('utc',rows[0])
            finally:audio.close()
