import unittest
from unittest.mock import Mock
from types import SimpleNamespace
from pathlib import Path
import io
import numpy as np
from PIL import Image
from bookscan.app import App
from bookscan.checks import Config,Gate,signature
from bookscan.guides import Guides


def blank_jpeg():
 im=Image.new('RGB',(4160,3120),'white');out=io.BytesIO();im.save(out,'JPEG');return out.getvalue(),im


class ManualOverrideTests(unittest.TestCase):
 def app(self):
  app=App.__new__(App);app.config=Config(holder_pause=False);app.guides=Guides();app.gate=Gate(app.config)
  app.identity={};app.history=[];app.fingerprints=[];app.last_rejection=None
  app.volume=SimpleNamespace(get=lambda:.1);app.pitch=SimpleNamespace(get=lambda:1)
  app.mute=SimpleNamespace(get=lambda:True);app.wait_tone=SimpleNamespace(get=lambda:False)
  app.status=Mock();app.audio=Mock();app.pool=Mock();app.session=Mock();app.session.last_accept_timing={};app.future=None;app.latest_frame_timing={}
  return app

 def options(self,trigger='manual',reason=''):
  return dict(kind='cover',trigger=trigger,override_reason=reason,printed_pages='',requested_monotonic=1)

 def verify(self,app,data,im,options):
  sig=signature(im,app.config,'cover')
  snapshot=dict(config=app.config,guides={},device={},history=[],fingerprints=[],audio_config={},last_rejection=app.last_rejection)
  return app.verify_and_save(data,{}, {},sig,options,im,{'paused':False},snapshot,{},1)

 def test_manual_override_records_current_and_previous_rejection(self):
  data,im=blank_jpeg();app=self.app();sig=signature(im,app.config,'cover')
  result=self.verify(app,data,im,self.options())
  self.assertFalse(result['accepted']);prior=result['rejection']
  app.last_rejection=prior
  result=self.verify(app,data,im,self.options(reason='Sparse cover is complete and readable'))
  record=app.session.accept.call_args.args[1]
  self.assertEqual(record['overridden_rejection'],prior)
  self.assertTrue(record['check_warnings']);self.assertTrue(record['override_reason'])
  self.assertFalse(record['guides_affect_checks']);self.assertTrue(result['accepted'])

 def test_standing_override_cannot_bypass_automatic_rejection(self):
  data,im=blank_jpeg();app=self.app()
  result=self.verify(app,data,im,self.options('automatic','standing reason'))
  app.session.accept.assert_not_called();self.assertFalse(result['accepted'])

 def test_override_cannot_bypass_decode_or_required_dimensions(self):
  from bookscan.storage import validate_jpeg_bytes
  out=io.BytesIO();Image.new('RGB',(400,300),'white').save(out,'JPEG')
  for data in (b'badjpeg',out.getvalue()):
   with self.assertRaises(ValueError):validate_jpeg_bytes(data)
