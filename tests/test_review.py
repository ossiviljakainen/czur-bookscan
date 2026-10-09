import hashlib,io,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from bookscan.storage import Session
from bookscan.review import Review,caption,load_original
from bookscan.guides import Guides,render


class SavedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.session=Session(self.root);self.review=Review(self.root)
        out=io.BytesIO();Image.new('RGB',(4160,3120),(205,195,180)).save(out,'JPEG');self.data=out.getvalue()
    def tearDown(self):
        self.review.pool.shutdown(wait=True,cancel_futures=True);self.session.close();self.temp.cleanup()
    def finish(self):
        for _,future,_ in self.review.jobs:future.result(timeout=5)
        self.review.poll()
    def test_thumbnail_is_read_from_saved_bytes_and_keeps_original_unchanged(self):
        record=self.session.accept(self.data,dict(kind='cover'));self.review.submit(record);self.finish()
        self.assertEqual(self.review.items[0][0]['sequence'],1)
        self.assertEqual(self.review.items[0][1].size,(130,98))
        self.assertEqual((self.root/'000001/original.jpg').read_bytes(),self.data)
    def test_worker_does_not_block_submit_or_capture_save(self):
        record=self.session.accept(self.data,dict(kind='spread'));started=threading.Event();release=threading.Event()
        real=self.review.thumbnail;worker=[]
        def slow(record,queued=None):
            worker.append(threading.get_ident());started.set();release.wait(5);return real(record,queued)
        try:
            with patch.object(self.review,'thumbnail',side_effect=slow):
                self.review.submit(record);self.assertTrue(started.wait(2))
                # The independent save path is available while thumbnail work is held.
                second=self.session.accept(self.data,dict(kind='spread'))
                self.assertEqual(second['sequence'],2);self.assertNotEqual(worker[0],threading.get_ident())
                self.assertFalse(self.review.poll())
        finally:release.set()
        self.finish()
    def test_last_three_and_reopen_verified_records(self):
        for i in range(4):
            record=self.session.accept(self.data,dict(kind='spread',retry_of=i if i else None,override_reason='intentional retry' if i else '',check_warnings=['duplicate'] if i else []))
            self.review.submit(record)
        self.finish();self.assertEqual([r['sequence'] for r,_ in self.review.items],[2,3,4])
        self.session.close();self.session=Session(self.root)
        self.review.items=[]
        for record in self.session.records[-3:]:self.review.submit(record)
        self.finish();self.assertEqual([r['sequence'] for r,_ in self.review.items],[2,3,4])
        self.assertIn('Retry of 000003',caption(self.review.items[-1][0]))
        self.assertIn('Heuristic override',caption(self.review.items[-1][0]))
    def test_failed_review_read_preserves_last_success(self):
        record=self.session.accept(self.data,dict(kind='cover'));self.review.submit(record);self.finish()
        second=self.session.accept(self.data,dict(kind='cover'))
        (self.root/'000002/original.jpg').write_bytes(b'corrupt test fixture')
        self.review.submit(second)
        try:self.review.jobs[0][1].result(timeout=5)
        except ValueError:pass
        self.review.poll()
        self.assertEqual(self.review.items[-1][0]['sequence'],1);self.assertIn('changed',self.review.error)


class CoverGuideTests(unittest.TestCase):
    def test_cover_only_draws_placement_border(self):
        original=Image.new('RGB',(1000,750),'white');guides=Guides()
        preview=render(original,guides,'cover')
        pages,contents=guides.rectangles('cover');self.assertEqual(len(pages),1);self.assertEqual(contents,[])
        # All interior pixels including former fold and margin/content positions stay unchanged.
        self.assertEqual(preview.crop((331,129,669,636)).tobytes(),original.crop((331,129,669,636)).tobytes())
        self.assertEqual(original.getpixel((330,200)),(255,255,255))
        self.assertEqual(preview.getpixel((330,200)),(180,170,145))
    def test_cover_geometry_adjustment_is_independent_of_spread(self):
        guides=Guides();before=guides.rectangles('spread')
        guides.cover_center=.78;guides.cover_width=.20;guides.cover_top=.12;guides.cover_height=.45;guides.validate()
        self.assertEqual(guides.rectangles('spread'),before)
        self.assertAlmostEqual(guides.rectangles('cover')[0][0][0],.68)
