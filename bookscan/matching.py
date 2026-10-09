"""Geometric duplicate check: features must align, cover the page and match pixels.

This checks recent originals, not printed page numbers. Repeated artwork and faint
pages can remain ambiguous; manual retry records an intentional override.
"""
import numpy as np
from PIL import Image
try:
    import cv2
except ImportError:
    cv2 = None


def fingerprint(image, config, kind='spread'):
    if cv2 is None:
        return None
    small = image.copy()
    small.thumbnail((1040, 780), Image.Resampling.LANCZOS)
    rgb = np.asarray(small)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    # Pale paper mask excludes the coloured mat. Close over dark printed strokes.
    paper = ((hsv[:, :, 1] < 105) & (gray > 90)).astype(np.uint8)*255
    paper = cv2.morphologyEx(paper, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    roi = np.zeros_like(gray)
    h, w = gray.shape
    regions = []
    for x1, y1, x2, y2 in config.regions(kind):
        roi[int(y1*h):int(y2*h), int(x1*w):int(x2*w)] = 255
        regions.append((int(x1*w), int(y1*h), int(x2*w), int(y2*h)))
    mask = cv2.bitwise_and(paper, roi)
    points, descriptors = cv2.ORB_create(nfeatures=2500, fastThreshold=8).detectAndCompute(gray, mask)
    coords = np.float32([p.pt for p in points]).reshape(-1, 2)
    return dict(gray=gray, mask=mask, coords=coords, descriptors=descriptors, regions=regions)


def same_page(old, new, threshold=.04):
    result = dict(geometric_duplicate=False, inliers=0)
    if old is None or new is None or old['descriptors'] is None or new['descriptors'] is None:
        return result
    if min(len(old['coords']), len(new['coords'])) < 40:
        return result
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(old['descriptors'], new['descriptors'], k=2)
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < .70*pair[1].distance]
    unique = {m.trainIdx: m for m in sorted(good, key=lambda m: m.distance, reverse=True)}
    good = list(unique.values())
    if len(good) < 40:
        return result
    a = np.float32([old['coords'][m.queryIdx] for m in good])
    b = np.float32([new['coords'][m.trainIdx] for m in good])
    transform, inliers = cv2.findHomography(a, b, cv2.RANSAC, 3)
    if transform is None or inliers is None:
        return result
    good_mask = inliers.ravel().astype(bool)
    result['inliers'] = int(good_mask.sum())
    if result['inliers'] < 40 or good_mask.mean() < .65:
        return result
    area = cv2.contourArea(cv2.convexHull(b[good_mask]))
    mask_area = int((new['mask'] > 0).sum())
    coverage = area/max(mask_area, 1)
    result['feature_coverage'] = coverage
    if coverage < .20:
        return result
    h, w = new['gray'].shape
    warped = cv2.warpPerspective(old['gray'], transform, (w, h))
    # Small alignment/sharpness differences should not look like changed letters.
    smooth_old = cv2.GaussianBlur(warped, (5, 5), 0)
    smooth_new = cv2.GaussianBlur(new['gray'], (5, 5), 0)
    old_mask = cv2.warpPerspective(old['mask'], transform, (w, h), flags=cv2.INTER_NEAREST)
    valid = (old_mask > 0) & (new['mask'] > 0)
    feature_area = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(feature_area, cv2.convexHull(b[good_mask]).astype(np.int32), 255)
    comparison = valid & (feature_area > 0)
    overlap = float(valid.sum()/max(mask_area, 1))
    result['paper_overlap'] = overlap
    if overlap < .55:
        return result
    x = warped[comparison].astype(np.float32)
    y = new['gray'][comparison].astype(np.float32)
    delta = float(np.abs((x-x.mean())-(y-y.mean())).mean()/255)
    result['aligned_difference'] = delta
    # Local occlusions must not erase evidence that the remaining printed content
    # is unchanged. Require strong, distributed matches AND agreement of textured
    # tiles in every check region (not blank paper, not just one unchanged page).
    agreements = []
    supports = []
    for x1, y1, x2, y2 in new['regions']:
        coords = b[good_mask]
        supports.append(int(((coords[:, 0] >= x1) & (coords[:, 0] < x2) &
                             (coords[:, 1] >= y1) & (coords[:, 1] < y2)).sum()))
        tiles = []
        for ty in range(y1, y2-31, 32):
            for tx in range(x1, x2-31, 32):
                mask = comparison[ty:ty+32, tx:tx+32]
                if mask.mean() < .8:
                    continue
                a_tile = smooth_old[ty:ty+32, tx:tx+32][mask].astype(np.float32)
                b_tile = smooth_new[ty:ty+32, tx:tx+32][mask].astype(np.float32)
                if min(a_tile.std(), b_tile.std()) < 8:
                    continue
                tiles.append(float(np.abs((a_tile-a_tile.mean())-(b_tile-b_tile.mean())).mean()/255))
        agreements.append(dict(textured_tiles=len(tiles),
                               unchanged_fraction=float(np.mean(np.array(tiles) < threshold)) if tiles else 0))
    robust = (result['inliers'] >= 120 and coverage >= .30 and overlap >= .70
              and all(s >= 30 for s in supports)
              and all(t['textured_tiles'] >= 6 and t['unchanged_fraction'] >= .75 for t in agreements))
    result['region_match_support'] = supports
    result['region_tile_agreement'] = agreements
    result['occlusion_tolerant_duplicate'] = bool(robust)
    result['geometric_duplicate'] = bool(delta < threshold or robust)
    # One strongly unchanged page is sufficient to suspect a repeated spread,
    # not to certify identity. A real partial-page change may need manual review.
    result['suspected_duplicate'] = bool(result['inliers'] >= 120 and coverage >= .30 and overlap >= .70
        and any(s >= 100 and t['textured_tiles'] >= 6 and t['unchanged_fraction'] >= .75
                for s,t in zip(supports,agreements)))
    return result
