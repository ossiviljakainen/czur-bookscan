"""Calibratable pause signal for the trial's distinctive yellow page holder.

Independent of positioning guides. Uses narrow horizontal cores of check regions;
not generic finger recognition or guaranteed holder/artwork discrimination.
"""
import numpy as np
from PIL import Image
from .matching import cv2


def parked_holder(image, config, kind='spread'):
    result = dict(paused=False, detector='yellow-holder-core-v2', regions=[])
    if not config.holder_pause:
        return result
    if cv2 is None:
        result['unavailable'] = True
        return result
    small=image.copy();small.thumbnail((1040,780),Image.Resampling.BILINEAR)
    rgb=np.asarray(small);h,w=rgb.shape[:2]
    hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
    yellow=cv2.inRange(hsv,np.array([18,110,90]),np.array([42,255,255])) > 0
    for x1,y1,x2,y2 in config.regions(kind):
        inset=(x2-x1)*config.holder_core_inset
        a,b,c,d=int((x1+inset)*w),int(y1*h),int((x2-inset)*w),int(y2*h)
        fraction=float(yellow[b:d,a:c].sum()/(w*h))
        result['regions'].append(dict(core=[x1+inset,y1,x2-inset,y2],yellow_frame_fraction=fraction))
        if fraction >= config.holder_yellow_fraction:
            result['paused']=True
    return result
