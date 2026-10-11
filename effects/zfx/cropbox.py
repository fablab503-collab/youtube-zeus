"""Crop boxes for vace: generate the person, not the whole frame (pure numpy + cv2, no model).

VACE at 360p on a 1080p or 4K frame spends most of its 640x368 pixels on background that is pasted back from the
original anyway; a presenter covering 6 % of the frame got ~100x150 generated pixels. So each window of frames gets
one box around the person:

    union of the window's mask bounding boxes (one box per window: no jitter inside a window)
    + a margin of 12 % of its size on every side the person does not leave the frame through
    -> grown (never shrunk) to the generation aspect, portrait 368x640 or landscape 640x368, whichever fits the
       frame with less stretch (then: the smaller box)
    -> shifted inside the frame; if the frame is too small for that aspect it is clamped, and the generation size
       follows the box instead (adapt_gen: multiples of 16, same pixel budget), so the person is never stretched
       by more than aspect_error (|log| of the aspect ratios, undone when the result is scaled back)
    -> snapped to even source pixels (ffmpeg crops yuv420 at even offsets).

A box covering most of the frame (>= FULL_FRAME_SHARE of its area), or a window without any mask pixel, uses the
whole frame and the clip's own orientation (the behaviour before crops).
Boxes are in SOURCE pixels [x0, y0, x1, y1) (end exclusive). Pixel centres map linearly between a box and the
canvas it is resized to (crop_affine), the same convention as ffmpeg's crop + scale and cv2.resize.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

MARGIN = 0.12
FULL_FRAME_SHARE = 0.80
MIN_SHORT_SHARE = 0.15       # a box's short side is at least 15 % of the frame's short side (context for VACE)
ADAPT_ABOVE = 0.03           # |log aspect error| above this: generate at the box's own aspect (adapt_gen)
MAX_STRETCH = 0.15           # still above this after adapting (extreme shapes): whole frame instead


def mask_bbox(mask: np.ndarray, thresh: int = 127) -> Optional[tuple[int, int, int, int]]:
    """(x0, y0, x1, y1) of the pixels above thresh, end exclusive; None when empty."""
    on = np.asarray(mask) > thresh
    rows, cols = np.any(on, axis=1), np.any(on, axis=0)
    if not rows.any():
        return None
    y0, y1 = np.argmax(rows), len(rows) - np.argmax(rows[::-1])
    x0, x1 = np.argmax(cols), len(cols) - np.argmax(cols[::-1])
    return int(x0), int(y0), int(x1), int(y1)


def union_bbox(boxes: Sequence) -> Optional[tuple]:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def scale_box(box, sx: float, sy: float) -> tuple[float, float, float, float]:
    return box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy


def _fit_span(lo: float, hi: float, size: float, limit: float) -> tuple[float, float]:
    """A span of `size` centred on [lo, hi), shifted to lie inside [0, limit)."""
    size = min(size, limit)
    start = (lo + hi) / 2.0 - size / 2.0
    start = min(max(start, 0.0), limit - size)
    return start, start + size


def plan_box(bbox, frame_w: int, frame_h: int, gen_sizes: Sequence[tuple[int, int]] = ((640, 368), (368, 640)),
             margin: float = MARGIN, full_frame_share: float = FULL_FRAME_SHARE,
             min_short_share: float = MIN_SHORT_SHARE) -> dict:
    """Crop box for one window: {box [x0,y0,x1,y1] (source px), gen [w,h], full_frame, aspect_error}.

    bbox: union of the window's mask boxes in source pixels, or None (no person: whole frame)."""
    landscape = [g for g in gen_sizes if g[0] >= g[1]] or list(gen_sizes)
    portrait = [g for g in gen_sizes if g[0] < g[1]] or list(gen_sizes)
    clip_gen = landscape[0] if frame_w >= frame_h else portrait[0]
    whole = {"box": [0, 0, int(frame_w), int(frame_h)], "gen": list(clip_gen), "full_frame": True,
             "aspect_error": round(abs(math.log((frame_w / frame_h) / (clip_gen[0] / clip_gen[1]))), 4)}
    if bbox is None:
        return whole
    x0, y0, x1, y1 = (float(v) for v in bbox)
    bw, bh = max(1.0, x1 - x0), max(1.0, y1 - y0)
    # margin on every side the person does not leave the frame through (past the frame it would only be clamped)
    x0 = x0 - margin * bw if x0 > 1 else 0.0
    x1 = x1 + margin * bw if x1 < frame_w - 1 else float(frame_w)
    y0 = y0 - margin * bh if y0 > 1 else 0.0
    y1 = y1 + margin * bh if y1 < frame_h - 1 else float(frame_h)
    x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(float(frame_w), x1), min(float(frame_h), y1)
    bw, bh = x1 - x0, y1 - y0
    min_short = min_short_share * min(frame_w, frame_h)
    best = None
    for gen in gen_sizes:   # worth cropping; the smallest box that keeps the aspect, else the least stretch
        cand = _fit_box(x0, y0, x1, y1, frame_w, frame_h, gen, min_short)
        if cand["area"] >= full_frame_share * frame_w * frame_h:
            continue
        fits = cand["aspect_error"] <= ADAPT_ABOVE
        key = (0, cand["area"]) if fits else (1, cand["aspect_error"])
        if best is None or key < best[0]:
            best = (key, cand)
    if best is None:
        return whole
    cand = best[1]
    cand.pop("area")
    if cand["aspect_error"] > ADAPT_ABOVE:
        # the frame cannot hold the standard aspect around this person: generate at the box's own aspect instead
        # (multiples of 16, no more pixels than the standard size) rather than stretching the person
        bx0, by0, bx1, by1 = cand["box"]
        cand["gen"] = list(adapt_gen((bx1 - bx0) / float(by1 - by0), gen_sizes[0]))
        cand["aspect_error"] = round(abs(math.log(((bx1 - bx0) / (by1 - by0)) / (cand["gen"][0] / cand["gen"][1]))), 4)
        if cand["aspect_error"] > MAX_STRETCH:
            return whole
    return cand


def adapt_gen(aspect: float, standard: Sequence[int] = (640, 368)) -> tuple[int, int]:
    """Generation size (multiples of 16) with about the standard size's pixel count at `aspect` (w/h), its long
    side no longer than the standard's long side."""
    area = float(standard[0] * standard[1])
    longest = max(standard)
    w, h = math.sqrt(area * aspect), math.sqrt(area / aspect)
    k = min(1.0, longest / max(w, h))
    w, h = w * k, h * k
    return max(16, int(round(w / 16.0)) * 16), max(16, int(round(h / 16.0)) * 16)


def _fit_box(x0: float, y0: float, x1: float, y1: float, frame_w: int, frame_h: int, gen: Sequence[int],
             min_short: float) -> dict:
    """The [x0,y0,x1,y1) span grown to gen's aspect (and min_short), kept inside the frame, snapped to even pixels."""
    bw, bh = x1 - x0, y1 - y0
    aspect = gen[0] / gen[1]
    want_w, want_h = max(bw, bh * aspect), max(bh, bw / aspect)
    if min(want_w, want_h) < min_short:
        k = min_short / min(want_w, want_h)
        want_w, want_h = want_w * k, want_h * k
    # does it fit? keep the aspect while possible, then stretch rather than cut the person
    if want_w > frame_w:
        want_w = float(frame_w)
        want_h = max(bh, min(float(frame_h), want_w / aspect))
    if want_h > frame_h:
        want_h = float(frame_h)
        want_w = max(min(bw, float(frame_w)), min(float(frame_w), want_h * aspect))
    bx0, bx1 = _fit_span(x0, x1, want_w, frame_w)
    by0, by1 = _fit_span(y0, y1, want_h, frame_h)
    # even source pixels, end exclusive, inside the frame
    ex0, ey0 = int(math.floor(bx0 / 2.0)) * 2, int(math.floor(by0 / 2.0)) * 2
    ex1, ey1 = min(int(frame_w), int(math.ceil(bx1 / 2.0)) * 2), min(int(frame_h), int(math.ceil(by1 / 2.0)) * 2)
    ex1 = max(ex1, ex0 + 2)
    ey1 = max(ey1, ey0 + 2)
    err = abs(math.log(((ex1 - ex0) / (ey1 - ey0)) / aspect))
    return {"box": [ex0, ey0, ex1, ey1], "gen": list(gen), "full_frame": False, "aspect_error": round(err, 4),
            "area": (ex1 - ex0) * (ey1 - ey0)}


MAIN_BLOB_RATIO = 0.5        # a blob this big (share of the frame's largest) steers the box wherever it is ...
NEAR_SHARE = 0.3             # ... a smaller one only when it is within 30 % of the main person's size of them


def person_bbox(mask: np.ndarray, ratio: float = MAIN_BLOB_RATIO, near: float = NEAR_SHARE,
                thresh: int = 127) -> Optional[tuple]:
    """Bounding box of the frame's main person(s): the largest mask blob, the blobs at least `ratio` of it, and
    smaller blobs close to those (a hand the mask cut off from the arm). A small blob elsewhere (a face on a screen in
    the background: prepare's --min-person-ratio keeps blobs down to 25 %) would stretch the box across the frame for
    a few pixels, so it is left outside the box: not regenerated, its original pixels stay."""
    import cv2
    on = (np.asarray(mask) > thresh).astype(np.uint8)
    count, _labels, stats, _ = cv2.connectedComponentsWithStats(on, connectivity=8)
    if count <= 1:
        return None
    blobs = [(int(st[cv2.CC_STAT_AREA]), (int(st[cv2.CC_STAT_LEFT]), int(st[cv2.CC_STAT_TOP]),
                                          int(st[cv2.CC_STAT_LEFT] + st[cv2.CC_STAT_WIDTH]),
                                          int(st[cv2.CC_STAT_TOP] + st[cv2.CC_STAT_HEIGHT]))) for st in stats[1:]]
    largest = max(a for a, _ in blobs)
    main = union_bbox([b for a, b in blobs if a >= ratio * largest])
    mw, mh = main[2] - main[0], main[3] - main[1]
    reach = (main[0] - near * mw, main[1] - near * mh, main[2] + near * mw, main[3] + near * mh)
    close = [b for a, b in blobs if b[0] < reach[2] and b[2] > reach[0] and b[1] < reach[3] and b[3] > reach[1]]
    return union_bbox([main] + close)


def plan_window_boxes(masks: np.ndarray, windows: Sequence[tuple[int, int]], src_w: int, src_h: int,
                      gen_sizes: Sequence[tuple[int, int]] = ((640, 368), (368, 640)), crop: bool = True,
                      blob_ratio: float = MAIN_BLOB_RATIO, **kw) -> list[dict]:
    """One plan_box per window. masks: (N, H, W) uint8 at work size (255 = person); windows [start, end) may run
    past N (padded windows reuse the last frame). Each plan records the share of the window's mask pixels that
    fall outside its box (outside_mask_share: stray blobs left as they are)."""
    n, mh, mw = masks.shape
    sx, sy = src_w / float(mw), src_h / float(mh)
    plans = []
    for start, end in windows:
        idx = sorted({min(i, n - 1) for i in range(start, end)})
        union = union_bbox([person_bbox(masks[i], blob_ratio) for i in idx])
        plan = plan_box(scale_box(union, sx, sy) if (union and crop) else None, src_w, src_h, gen_sizes, **kw)
        if not crop:
            plan["full_frame"] = True
        plan["window"] = [int(start), int(end)]
        plan["mask_bbox"] = [round(v, 1) for v in scale_box(union, sx, sy)] if union else None
        x0, y0, x1, y1 = scale_box(plan["box"], 1.0 / sx, 1.0 / sy)
        inside = outside = 0
        for i in idx:
            on = masks[i] > 127
            total = int(on.sum())
            kept = int(on[int(y0):int(math.ceil(y1)), int(x0):int(math.ceil(x1))].sum())
            inside += kept
            outside += total - kept
        plan["outside_mask_share"] = round(outside / max(1, inside + outside), 4)
        plans.append(plan)
    return plans


# ----------------------------------------------------------------------------- resampling between boxes
def crop_affine(box, out_w: int, out_h: int) -> np.ndarray:
    """2x3 matrix taking canvas pixel (u, v) of an out_w x out_h canvas to frame coordinates of `box`
    (pixel centres aligned: u = -0.5 is the box's left edge)."""
    x0, y0, x1, y1 = (float(v) for v in box)
    sx, sy = (x1 - x0) / out_w, (y1 - y0) / out_h
    return np.array([[sx, 0.0, x0 + 0.5 * sx - 0.5], [0.0, sy, y0 + 0.5 * sy - 0.5]], np.float64)


def crop_resize(frame: np.ndarray, box, out_w: int, out_h: int, interp: Optional[int] = None) -> np.ndarray:
    """The `box` of `frame` (box in this frame's pixels, may be fractional) resampled to out_w x out_h."""
    import cv2
    interp = cv2.INTER_LINEAR if interp is None else interp
    m = crop_affine(box, out_w, out_h)
    return cv2.warpAffine(frame, m, (int(out_w), int(out_h)), flags=interp | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REPLICATE)


def reproject(patch: np.ndarray, box_from, box_to, out_w: int, out_h: int, fill: np.ndarray) -> np.ndarray:
    """`patch` (a canvas of box_from) drawn into a canvas of box_to (out_w x out_h) over `fill`; the parts of box_to
    outside box_from keep `fill`. Boxes in the same (source) pixel space."""
    import cv2
    a = crop_affine(box_from, patch.shape[1], patch.shape[0])
    b = crop_affine(box_to, out_w, out_h)
    # canvas_to (u, v) -> frame -> canvas_from: inverse of a applied after b
    ainv = cv2.invertAffineTransform(a)
    m = ainv[:, :2] @ b[:, :2]
    t = ainv[:, :2] @ b[:, 2] + ainv[:, 2]
    full = np.hstack([m, t[:, None]])
    out = np.array(fill, copy=True)
    cv2.warpAffine(patch, full, (int(out_w), int(out_h)), dst=out, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                   borderMode=cv2.BORDER_TRANSPARENT)
    return out


def paste(canvas: np.ndarray, patch: np.ndarray, box) -> tuple[np.ndarray, np.ndarray]:
    """(image, coverage) of the canvas size: `patch` resampled into `box` (canvas pixels, may be fractional),
    coverage 1 inside the box, 0 outside (soft at fractional edges)."""
    import cv2
    h, w = canvas.shape[:2]
    a = crop_affine(box, patch.shape[1], patch.shape[0])
    ainv = cv2.invertAffineTransform(a)   # frame -> patch canvas
    img = cv2.warpAffine(patch.astype(np.float32), ainv, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    ones = np.ones(patch.shape[:2], np.float32)
    cov = cv2.warpAffine(ones, ainv, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return img, cov


def frame_weights(windows: Sequence[tuple[int, int]], total: int) -> list[list[tuple[int, float]]]:
    """For each frame, [(window index, weight)] with weights summing to 1: a later window ramps in linearly over
    its overlap with the frames already covered (same ramp as vace.blend_weights)."""
    cover: list[list[tuple[int, float]]] = [[] for _ in range(total)]
    prev_end = 0
    for wi, (start, end) in enumerate(windows):
        valid = min(end, total) - start
        overlap = max(0, prev_end - start) if wi > 0 else 0
        for j in range(max(0, valid)):
            i = start + j
            if j < overlap:
                a = (j + 1) / float(overlap + 1)
                cover[i] = [(k, w * (1.0 - a)) for k, w in cover[i]] + [(wi, a)]
            else:
                cover[i] = [(wi, 1.0)]
        prev_end = start + valid
    return cover
