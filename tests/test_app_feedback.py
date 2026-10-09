import queue
import time
import unittest
from types import SimpleNamespace
from concurrent.futures import Future
from unittest.mock import Mock
import numpy as np
from bookscan.app import App
from bookscan.checks import Config, Gate


class FeedbackTests(unittest.TestCase):
    def app(self, result=None, error=None):
        app = App.__new__(App)
        app.config = Config()
        app.gate = Gate(app.config)
        app.saving_sig = np.ones((2,96,128),np.float32)*100
        app.saving_fingerprint = None
        app.saving_preview = None
        app.history, app.fingerprints, app.original_previews = [], [], []
        app.fatal = app.camera_failed = False
        app.pending = None
        app.counter = Mock()
        app.status = Mock()
        app.label = Mock()
        app.reason = Mock()
        app.root = Mock()
        app.pause = SimpleNamespace(get=lambda:False)
        app.auto = Mock()
        app.auto.get.return_value = False
        app.mute = SimpleNamespace(get=lambda:True)
        app.volume = SimpleNamespace(get=lambda:.25)
        app.pitch = SimpleNamespace(get=lambda:1)
        app.wait_tone = SimpleNamespace(get=lambda:True)
        app.audio = Mock(error=None)
        app.audio.diagnostics=queue.Queue()
        app.last_audio_error = None
        app.sound_status = Mock()
        app.last_frame = time.monotonic()
        app.reader = SimpleNamespace(messages=queue.Queue(),frames=queue.Queue())
        app.session = SimpleNamespace(records=[dict(sequence=1)])
        app.review = Mock()
        app.review.poll.return_value = False
        app.future = Future()
        if error: app.future.set_exception(error)
        elif result: app.future.set_result(dict(accepted=True,record=dict(sequence=1),timing={},fingerprint=None))
        app.last_tick_started=None;app.save_completed_monotonic=None
        app.debug_timing=SimpleNamespace(get=lambda:False)
        app.saving_sig=np.ones((2,96,128),np.float32)*100
        app.review.jobs=[]
        app.review.submit=Mock(side_effect=lambda record:app.review.jobs.append((record,None,time.monotonic())))
        return app

    def test_blip_only_after_save_future_succeeds(self):
        app = self.app(result=dict(sequence=1))
        app.tick()
        app.audio.signal.assert_called_once_with('accepted')
        self.assertFalse(app.fatal)
        self.assertEqual(len(app.history),1)
        app.review.submit.assert_called_once_with(dict(sequence=1))

    def test_disk_error_pauses_and_never_blips(self):
        app = self.app(error=OSError('disk full'))
        app.tick()
        app.audio.signal.assert_called_once_with('rejected')
        self.assertTrue(app.fatal)
        self.assertEqual(len(app.history),0)
        app.counter.set.assert_not_called()
        app.review.submit.assert_not_called()

    def test_no_blip_while_save_is_pending(self):
        app = self.app()
        app.tick()
        app.audio.signal.assert_not_called()
        app.review.submit.assert_not_called()

    def test_camera_failure_visible_and_auto_disabled(self):
        app = self.app()
        app.future = None
        app.reader.messages.put(('error','device disconnected'))
        app.tick()
        self.assertFalse(app.fatal)
        self.assertTrue(app.camera_failed)
        app.audio.signal.assert_not_called()
        app.auto.set.assert_called_with(False)
