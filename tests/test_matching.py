import unittest
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from bookscan.checks import Config
from bookscan.matching import cv2, fingerprint, same_page


@unittest.skipIf(cv2 is None, 'OpenCV missing: use project virtual environment')
class MatchingTests(unittest.TestCase):
    def page(self, seed):
        image = Image.new('RGB', (1040, 780), (35, 125, 140))
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 50, 850, 730), fill=(240, 235, 225))
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 17)
        rng = np.random.default_rng(seed)
        for i in range(27):
            line = ''.join(rng.choice(list('abcdefghijklmnpqrstuv 123456789'), 44))
            draw.text((240, 70+i*23), line, font=font, fill=(35, 35, 35))
        return image

    def test_same_page_large_pose_change(self):
        a = self.page(1)
        transform = cv2.getRotationMatrix2D((520, 390), 9, .9)
        transform[:, 2] += [-70, 12]
        b = Image.fromarray(cv2.warpAffine(np.asarray(a), transform, (1040, 780),
                                         borderValue=(35, 125, 140)))
        config = Config(rois=[[.1, .08, .9, .92]])
        result = same_page(fingerprint(a, config), fingerprint(b, config))
        self.assertTrue(result['geometric_duplicate'], result)

    def test_similar_layout_different_print_is_not_duplicate(self):
        config = Config(rois=[[.1, .08, .9, .92]])
        result = same_page(fingerprint(self.page(1), config), fingerprint(self.page(2), config))
        self.assertFalse(result['geometric_duplicate'], result)

    def test_featureless_blank_is_ambiguous_not_geometric_duplicate(self):
        config = Config()
        blank = fingerprint(Image.new('RGB', (1040, 780), 'white'), config)
        self.assertFalse(same_page(blank, blank)['geometric_duplicate'])

    def test_moved_finger_or_holder_does_not_make_new_page(self):
        a = self.page(1)
        b = a.copy()
        draw = ImageDraw.Draw(b)
        draw.ellipse((280,250,440,470),fill=(190,135,110))
        draw.rectangle((500,400,565,465),fill=(240,200,25))
        config=Config(rois=[[.1,.08,.9,.92]])
        result=same_page(fingerprint(a,config),fingerprint(b,config))
        self.assertTrue(result['geometric_duplicate'] or result.get('suspected_duplicate'),result)
