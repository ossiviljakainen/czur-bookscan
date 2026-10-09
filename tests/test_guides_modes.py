import unittest
import numpy as np
from PIL import Image, ImageDraw
from bookscan.checks import Config, signature, quality
from bookscan.guides import Guides, render


class GuidesModesTests(unittest.TestCase):
    def test_spread_physical_pages_meet_at_adjustable_fold(self):
        guides=Guides(fold=.55,left_width=.25,right_width=.3,height=.6,margin_left=.05).validate()
        pages,content=guides.rectangles('spread')
        self.assertEqual(pages[0][2],pages[1][0])
        self.assertEqual(pages[0][2],.55)
        self.assertGreater(content[0][0],pages[0][0])
        self.assertLess(content[1][2],pages[1][2])

    def test_preview_only_clear_content_and_translucent_margin(self):
        image=Image.new('RGB',(1000,750),(240,240,240));before=image.tobytes()
        guides=Guides();overlay=render(image,guides,'spread')
        self.assertEqual(image.tobytes(),before)
        self.assertEqual(overlay.getpixel((300,300)),(240,240,240))
        margin=overlay.getpixel((190,300))
        self.assertTrue(all(v>200 for v in margin))
        self.assertNotEqual(margin,(240,240,240))
        self.assertEqual(render(image,guides,'spread',False).tobytes(),before)

    def test_guide_changes_do_not_change_check_signature(self):
        image=Image.new('RGB',(1000,750),'white');ImageDraw.Draw(image).text((400,300),'cover',fill='black')
        config=Config();before=signature(image,config,'cover')
        render(image,Guides(fold=.6,left_width=.2,right_width=.25),'cover')
        np.testing.assert_array_equal(before,signature(image,config,'cover'))

    def test_central_cover_not_rejected_for_flat_spread_background(self):
        image=Image.new('RGB',(1000,750),'black');draw=ImageDraw.Draw(image)
        draw.rectangle((340,150,660,600),fill='white')
        for y in range(200,540,20):draw.rectangle((375,y,625,y+7),fill='black')
        config=Config()
        self.assertFalse(quality(signature(image,config,'cover'),config,'cover')[1])
        self.assertEqual(signature(image,config,'cover').shape[0],1)
        self.assertEqual(signature(image,config,'spread').shape[0],2)

    def test_modes_keep_independent_regions_and_blank_skips_print_checks(self):
        config=Config(minimum_sharpness=1000)
        config.mode_rois['cover']=[[.2,.2,.4,.8]]
        self.assertEqual(config.regions('single page'),[[.36,.25,.64,.75]])
        sig=signature(Image.new('RGB',(1000,750),'white'),config,'intentional blank')
        self.assertFalse(quality(sig,config,'intentional blank')[1])
        self.assertTrue(quality(sig,config,'single page')[1])

    def test_invalid_guide_extents_and_margins_rejected(self):
        for guides in [Guides(fold=.1),Guides(height=.95),Guides(margin_left=.6,margin_right=.3)]:
            with self.assertRaises(ValueError):guides.validate()
