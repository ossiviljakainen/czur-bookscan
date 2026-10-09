import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from bookscan.checks import Config,Gate,difference
from bookscan.app import App


class ContinuousStillnessTests(unittest.TestCase):
    def setUp(self):
        self.config=Config();self.gate=Gate(self.config)
        rng=np.random.default_rng(41)
        self.a=rng.integers(40,210,(2,96,128)).astype(np.float32)
        self.b=rng.integers(40,210,(2,96,128)).astype(np.float32)
    def test_default_two_seconds_without_extra_cooldown(self):
        self.assertEqual(self.config.settle_seconds,2.0)
        for t in [0,.4,.8,1.2,1.6,1.99]:self.assertFalse(self.gate.update(self.a,t))
        self.assertTrue(self.gate.update(self.a,2.0))
        self.gate.latch(self.a,accepted=True)
        for t in np.arange(2.1,60,.4):self.assertFalse(self.gate.update(self.a,float(t)))
        for t in [60,60.4,60.8,61.2,61.6,61.99]:self.assertFalse(self.gate.update(self.b,t))
        self.assertTrue(self.gate.update(self.b,62.0))
    def test_motion_resets_even_when_anchor_difference_is_small(self):
        plus=self.a.copy();minus=self.a.copy();plus[:,:48]+=3;minus[:,:48]-=3
        self.assertLess(difference(self.a,plus),self.config.stable_difference)
        self.assertLess(difference(self.a,minus),self.config.stable_difference)
        self.assertGreater(difference(plus,minus),self.config.stable_difference)
        self.assertFalse(self.gate.update(self.a,0));self.assertFalse(self.gate.update(plus,.5))
        self.assertFalse(self.gate.update(minus,1.0));self.assertEqual(self.gate.since,1.0)
        for t in [1.5,2,2.5,2.99]:self.assertFalse(self.gate.update(minus,t))
        self.assertTrue(self.gate.update(minus,3.0))
    def test_frame_gap_resets_continuous_timer(self):
        self.gate.update(self.a,0);self.gate.update(self.a,.5)
        self.assertFalse(self.gate.update(self.a,1.5));self.assertEqual(self.gate.since,1.5)
        for t in [2,2.5,3,3.49]:self.assertFalse(self.gate.update(self.a,t))
        self.assertTrue(self.gate.update(self.a,3.5))
    def test_setting_persists_and_preserves_repeat_latch(self):
        with tempfile.TemporaryDirectory() as temp:
            app=App.__new__(App);app.future=None;app.root=Mock();app.status=Mock()
            app.config=self.config;app.gate=self.gate;app.gate.latch(self.a,accepted=True)
            app.gate.since=12;app.pending={'trigger':'manual'}
            app.session=SimpleNamespace(root=Path(temp),event=Mock())
            app.stillness=Mock();app.stillness.get.return_value='2.4'
            app.apply_stillness()
            self.assertEqual(Config.load(Path(temp)/'check-config.json').settle_seconds,2.4)
            self.assertIsNone(app.gate.since);self.assertIsNone(app.pending)
            np.testing.assert_array_equal(app.gate.accepted,self.a)
            np.testing.assert_array_equal(app.gate.blocked,self.a)
            for t in [20,20.4,20.8,21.2,21.6,22.4]:self.assertFalse(app.gate.update(self.a,t))
