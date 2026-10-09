"""Positioning-only guides. This module never defines capture-check regions."""
from dataclasses import dataclass, asdict
import json
from pathlib import Path
from PIL import Image, ImageDraw


@dataclass
class Guides:
    fold: float = .50
    top: float = .17
    height: float = .68
    left_width: float = .32
    right_width: float = .32
    single_width: float = .34
    cover_center: float = .50
    cover_top: float = .17
    cover_width: float = .34
    cover_height: float = .68
    margin_left: float = .10
    margin_right: float = .10
    margin_top: float = .08
    margin_bottom: float = .08

    def validate(self):
        import math
        if any(not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError('Guide values must be finite.')
        if not (0 <= self.top < self.top+self.height <= 1 and
                0 < self.left_width <= self.fold and
                0 < self.right_width <= 1-self.fold and
                0 < self.single_width <= 2*min(self.fold, 1-self.fold)):
            raise ValueError('Page outlines must fit within the camera frame.')
        if not (0 < self.cover_width and 0 <= self.cover_center-self.cover_width/2 <
                self.cover_center+self.cover_width/2 <= 1 and
                0 <= self.cover_top < self.cover_top+self.cover_height <= 1):
            raise ValueError('Cover placement must fit within the camera frame.')
        if not (0 <= self.margin_left and 0 <= self.margin_right and
                self.margin_left+self.margin_right < .8 and
                0 <= self.margin_top and 0 <= self.margin_bottom and
                self.margin_top+self.margin_bottom < .8):
            raise ValueError('Margins must leave at least 20% of each page dimension.')
        return self

    @classmethod
    def load(cls, path):
        if not Path(path).exists():return cls()
        settings=json.loads(Path(path).read_text())
        for new,old in [('cover_center','fold'),('cover_top','top'),('cover_width','single_width'),('cover_height','height')]:
            settings.setdefault(new,settings.get(old,getattr(cls(),new)))
        return cls(**settings).validate()

    def rectangles(self, kind):
        if kind == 'cover':
            return [(self.cover_center-self.cover_width/2,self.cover_top,
                     self.cover_center+self.cover_width/2,self.cover_top+self.cover_height)], []
        f, t, bottom = self.fold, self.top, self.top+self.height
        pages = [(f-self.left_width,t,f,bottom),(f,t,f+self.right_width,bottom)] if kind == 'spread' else [(f-self.single_width/2,t,f+self.single_width/2,bottom)]
        content = []
        for a,b,c,d in pages:
            content.append((a+(c-a)*self.margin_left, b+(d-b)*self.margin_top,
                            c-(c-a)*self.margin_right, d-(d-b)*self.margin_bottom))
        return pages, content


def render(image, guides, kind, visible=True, check_regions=()):
    """Return a new preview; original pixels are never modified."""
    preview = image.convert('RGB').copy()
    w,h = preview.size
    scale = lambda box: tuple(round(v*(w if i%2 == 0 else h)) for i,v in enumerate(box))
    if visible:
        pages, contents = guides.rectangles(kind)
        if kind == 'cover':
            # Cover placement is a single rectangle; no fold, content or margins.
            ImageDraw.Draw(preview).rectangle(scale(pages[0]), outline=(180,170,145), width=1)
        else:
            overlay = Image.new('RGBA', (w,h), (0,0,0,0)); draw = ImageDraw.Draw(overlay)
            for page, content in zip(pages, contents):
                a,b,c,d = scale(page); e,f,g,j = scale(content)
                # Disjoint margin bands: muted ochre at 16% opacity, clear content.
                for box in [(a,b,c,f),(a,j,c,d),(a,f,e,j),(g,f,c,j)]:
                    draw.rectangle(box, fill=(164,128,58,40))
            preview = Image.alpha_composite(preview.convert('RGBA'),overlay).convert('RGB')
            draw = ImageDraw.Draw(preview)
            for page, content in zip(pages,contents):
                draw.rectangle(scale(page), outline=(180,170,145), width=1)
                draw.rectangle(scale(content), outline=(75,220,110), width=1)
            x=round(guides.fold*w)
            draw.line((x,round(guides.top*h),x,round((guides.top+guides.height)*h)),fill=(240,55,55),width=1)
    draw=ImageDraw.Draw(preview)
    for box in check_regions:
        a,b,c,d=scale(box)
        for x in range(a,c,10):
            draw.line((x,b,min(x+5,c),b),fill=(70,210,240));draw.line((x,d,min(x+5,c),d),fill=(70,210,240))
        for y in range(b,d,10):
            draw.line((a,y,a,min(y+5,d)),fill=(70,210,240));draw.line((c,y,c,min(y+5,d)),fill=(70,210,240))
    return preview
