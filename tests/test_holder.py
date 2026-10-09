import unittest
from PIL import Image,ImageDraw
from bookscan.checks import Config
from bookscan.holder import parked_holder
from bookscan.matching import cv2


@unittest.skipIf(cv2 is None,'OpenCV required')
class HolderTests(unittest.TestCase):
    def frame(self,x):
        im=Image.new('RGB',(1040,780),'white')
        ImageDraw.Draw(im).rectangle((x,340,x+65,400),fill=(240,200,25))
        return im

    def test_yellow_holder_in_core_pauses_but_margin_does_not(self):
        self.assertTrue(parked_holder(self.frame(275),Config())['paused'])
        self.assertFalse(parked_holder(self.frame(145),Config())['paused'])

    def test_can_disable_detector_for_matching_artwork(self):
        self.assertFalse(parked_holder(self.frame(275),Config(holder_pause=False))['paused'])

    def test_small_yellow_mark_is_not_pause(self):
        im=Image.new('RGB',(1040,780),'white');ImageDraw.Draw(im).rectangle((300,370,308,378),fill=(240,200,25))
        self.assertFalse(parked_holder(im,Config())['paused'])

class HolderCaptureBarrierTests(unittest.TestCase):
    def test_manual_trigger_discarded_while_parked(self):
        from bookscan.app import App
        from unittest.mock import Mock
        from types import SimpleNamespace
        app=App.__new__(App);app.fatal=False;app.future=None;app.pause=SimpleNamespace(get=lambda:False)
        app.holder_parked=True;app.pending=None;app.status=Mock()
        app.trigger()
        self.assertIsNone(app.pending)
        self.assertIn('PAUSED',app.status.set.call_args.args[0])

    @unittest.skipIf(cv2 is None,'OpenCV required')
    def test_attempt_cannot_override_parked_holder(self):
        import io
        import numpy as np
        from bookscan.app import App
        from bookscan.checks import signature,Gate
        from unittest.mock import Mock
        im=Image.new('RGB',(4160,3120),'white')
        ImageDraw.Draw(im).rectangle((1100,1360,1360,1600),fill=(240,200,25))
        out=io.BytesIO();im.save(out,'JPEG')
        app=App.__new__(App);app.config=Config();app.gate=Gate(app.config)
        sig=signature(im,app.config);app.gate.latch(sig,accepted=True);accepted=app.gate.accepted.copy()
        app.status=Mock();app.pool=Mock();app.pending={}
        app.attempt(out.getvalue(),{}, {},sig,dict(kind='spread',trigger='manual',override_reason='force'))
        app.pool.submit.assert_not_called();self.assertIsNone(app.pending)
        np.testing.assert_array_equal(accepted,app.gate.accepted)
