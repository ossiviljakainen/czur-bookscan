"""Conservative, configurable heuristics. No claim of semantic page recognition."""
from dataclasses import dataclass, field, asdict
import json
import os
import tempfile
from pathlib import Path
import numpy as np
from PIL import Image


@dataclass
class Config:
    settle_seconds: float = 2.0
    stable_difference: float = 0.007
    turn_difference: float = 0.035
    duplicate_difference: float = 0.018
    geometric_duplicate_difference: float = 0.04
    changed_tiles: float = 0.35
    blank_std: float = 5.0
    minimum_sharpness: float = 0.0  # Off until calibrated against faint printing.
    max_frame_gap: float = 0.8
    holder_pause: bool = True
    holder_core_inset: float = .30
    holder_yellow_fraction: float = .001
    rois: list = field(default_factory=lambda: [[0.12, 0.18, 0.46, 0.82],
                                               [0.54, 0.18, 0.88, 0.82]])
    mode_rois: dict = field(default_factory=lambda: {
        'cover': [[.36, .25, .64, .75]],
        'single page': [[.36, .25, .64, .75]],
        'intentional blank': [[.36, .25, .64, .75]]})

    def regions(self, kind='spread'):
        return self.rois if kind == 'spread' else self.mode_rois[kind]

    def save(self, path):
        """Atomically persist user settings; never touch capture directories."""
        path=Path(path)
        fd,temp=tempfile.mkstemp(prefix='.check-config-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as handle:
                handle.write(json.dumps(asdict(self),indent=2)+'\n');handle.flush();os.fsync(handle.fileno())
            os.replace(temp,path)
            from .storage import sync_dir
            sync_dir(path.parent)
        finally:
            if os.path.exists(temp):os.unlink(temp)

    @classmethod
    def load(cls, path=None):
        config = cls(**json.loads(Path(path).read_text())) if path else cls()
        for name, value in asdict(config).items():
            if name not in ('rois', 'mode_rois') and (not np.isfinite(value) or value < 0):
                raise ValueError(f'Invalid {name}: {value}')
        if config.settle_seconds <= 0 or config.max_frame_gap <= 0:
            raise ValueError('Settling time and maximum frame gap must be positive.')
        if not 0 <= config.holder_core_inset < .5 or not 0 < config.holder_yellow_fraction < .1:
            raise ValueError('Require holder inset in [0,.5) and yellow fraction in (0,.1).')
        if not 0 < config.stable_difference < config.duplicate_difference < config.turn_difference < 1:
            raise ValueError('Require 0 < stable_difference < duplicate_difference < turn_difference < 1.')
        if not 0 < config.changed_tiles <= 1 or not config.rois:
            raise ValueError('Require changed_tiles in (0,1] and at least one interior region.')
        if set(config.mode_rois) != {'cover', 'single page', 'intentional blank'}:
            raise ValueError('Check regions required for cover, single page and intentional blank.')
        if any(len(regions) != 1 for regions in config.mode_rois.values()):
            raise ValueError('Non-spread modes require one check rectangle.')
        for x1, y1, x2, y2 in config.rois + sum(config.mode_rois.values(), []):
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise ValueError('Regions must be ordered, normalized rectangles within the frame.')
        return config


def signature(image, config, kind='spread'):
    w, h = image.size
    return np.stack([np.asarray(image.crop((int(x1*w), int(y1*h), int(x2*w), int(y2*h)))
                     .convert('L').resize((128, 96), Image.Resampling.BILINEAR), dtype=np.float32)
                     for x1, y1, x2, y2 in config.regions(kind)])


def difference(a, b):
    # Remove mean lighting changes independently in each page region.
    a = a - a.mean(axis=(1, 2), keepdims=True)
    b = b - b.mean(axis=(1, 2), keepdims=True)
    return float(np.abs(a-b).mean()/255)


def duplicate_distance(a, b):
    # Small pose tolerance, no wrap-around pixels.
    return min(difference(a[:, 2:-2, 2:-2],
                          b[:, 2+dy:b.shape[1]-2+dy, 2+dx:b.shape[2]-2+dx])
               for dy in (-2, 0, 2) for dx in (-2, 0, 2))


def tile_change(a, b, threshold):
    a = a - a.mean(axis=(1, 2), keepdims=True)
    b = b - b.mean(axis=(1, 2), keepdims=True)
    tiles = np.abs(a-b).reshape(len(a), 6, 16, 8, 16).mean(axis=(2, 4))/255
    return float((tiles > threshold).mean())


def quality(sig, config, kind='spread'):
    std = sig.std(axis=(1, 2))
    laplacian = (4*sig[:, 1:-1, 1:-1] - sig[:, :-2, 1:-1] - sig[:, 2:, 1:-1]
                 - sig[:, 1:-1, :-2] - sig[:, 1:-1, 2:])
    sharpness = float(laplacian.var())
    metrics = dict(region_std=std.tolist(), sharpness=sharpness)
    reasons = []
    if kind != 'intentional blank' and np.any(std < config.blank_std):
        reasons.append('Possible blank/low-contrast region; choose Intentional blank or record an override.')
    if kind != 'intentional blank' and config.minimum_sharpness and sharpness < config.minimum_sharpness:
        reasons.append('Sharpness below configured minimum.')
    return metrics, reasons


class Gate:
    """Accept -> latch -> observe movement -> stable distinct interior -> rearm.

    Rejection latches its attempted image too, avoiding automatic retry/alarm loops.
    Manual retry deliberately resets only the settling phase.
    """
    def __init__(self, config):
        self.config = config
        self.accepted = None
        self.blocked = None
        self.moved = False
        self.anchor = None
        self.since = None
        self.last_time = None
        self.previous = None
        self.samples = 0
        self.state = 'waiting'

    def latch(self, sig, accepted=False):
        self.blocked = sig.copy()
        if accepted:
            self.accepted = sig.copy()
        self.moved = False
        self.reset_settling()
        self.state = 'wait for page turn' if accepted else 'rejected; correct page or Retry'

    def reset_settling(self):
        self.anchor = None
        self.since = None
        self.samples = 0
        self.last_time = None
        self.previous = None

    def retry(self):
        self.blocked = None
        self.reset_settling()
        self.state = 'waiting'

    def update(self, sig, now):
        c = self.config
        gap = self.last_time is not None and now - self.last_time > c.max_frame_gap
        motion = self.previous is not None and difference(sig,self.previous) > c.stable_difference
        self.previous = sig.copy()
        self.last_time = now
        if self.blocked is not None:
            changed = difference(sig, self.blocked) > c.turn_difference
            broad = tile_change(sig, self.blocked, c.turn_difference) >= c.changed_tiles
            if not (changed and broad):
                self.anchor = None
                self.since = None
                self.samples = 0
                return False
            self.moved = True
        if gap or motion or self.anchor is None or difference(sig, self.anchor) > c.stable_difference:
            self.anchor = sig.copy()
            self.since = now
            self.samples = 1
            if self.blocked is None or self.moved:
                self.state = 'settling'
            return False
        self.samples += 1
        if now - self.since < c.settle_seconds or self.samples < 3:
            return False
        if self.accepted is not None and duplicate_distance(sig, self.accepted) < c.duplicate_difference:
            self.state = 'same page; wait for page turn'
            return False
        self.blocked = None
        self.state = 'ready'
        return True
