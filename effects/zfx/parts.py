"""Parts map: every region of every frame gets its own tone, so a replacement model can map each part of the
person (and the things around them) instead of one silhouette.

For a prepared job (work.mp4 at 16 fps, mask.mp4, depth.mp4; motion.json / face-landmarks.json when present,
otherwise the pose and face mesh are computed with zfx.pose / zfx.mesh) this builds, per frame, an index map
with the fixed legend LEGEND below:

  body parts   from the 33 MediaPipe pose landmarks: polygons and capsules (head, neck from the chin line to the
               shoulders, torso from the shoulders to the hips, upper arms, forearms, hands - the 21 hand points
               when present -, thighs, shins, feet) intersected with the person mask; person pixels no part
               reached take the nearest part (distance transform), pixels farther than 1.5 shoulder widths stay
               "body-other"
  head detail  BiSeNet (FaceFusion's bisenet_resnet_34.onnx, CelebAMask-HQ classes) on a head crop of every
               frame, run once for the whole clip in FaceFusion's venv (faceparse_worker.py): face skin, brows,
               eyes, nose, mouth, ears, glasses, hair, hat, neck and cloth override the coarse head / neck /
               torso / body-other pixels (never arms or hands, so a hand in front of the face keeps its tone)
  gaze         (--gaze) MediaPipe face landmarks 468-477 (the irises): per eye the iris offset inside the eye
               box -> left / right / up / down / centre + a 2-D vector (image terms: "right" = the picture's
               right), blink from the eye aspect ratio; drawn as a dot plus a short arrow in the eyes region
  things       names from the user's prompt ("microphone, laptop, cap") through CLIPSeg (zfx.things), one tone
               each, kept outside the person unless asked (--things-over-person)
  background   three depth bands (far / mid / near, tertiles of the background depth of the clip)

Temporal smoothing: a majority vote over 3 frames (the previous and next frames outvote the current one), the
gaze marker is drawn afterwards. Rendered edges are feathered 2 px.

Outputs in the job folder:
  parts.mp4           the map rendered with the fixed palette: one hue per class, lightness by depth
  parts.npz           index (N, H, W) uint8 with the legend ids, plus ids/names/things/fps/size
  parts.json          legend (id -> name -> colour), the things prompt, per-frame stats (pixels per class,
                      gaze, blink, things), clip stats, timings (format "zeus-parts", version 1)
  control-parts.mp4   a control video for VACE: the original frame outside the person, inside the person the
                      depth luminance tinted by the part hue ("depth with tones"), named things tinted too
  preview-parts.jpg   contact sheet: 4 frames x (work | depth | mask | parts | control-parts) + the legend
"""

from __future__ import annotations

import colorsys
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import WORK_FPS, __version__, facefusion_dir
from .io import MediaError, VideoWriter, iter_frames, probe
from .jobs import Job, atomic_write_text, now_iso
from .pose import B, HAND_EDGES, VISIBLE, as_array
from .progress import get_logger
from .things import (MAX_THINGS, TEMPLATE, THINGS_FPS, THRESHOLD, ThingsError, detect_things, things_probabilities,
                     things_regions)

log = get_logger()

PARTS_FORMAT = "zeus-parts"
PARTS_VERSION = 1

# --------------------------------------------------------------------------- legend
# (id, name, group, hue in degrees, saturation). The ids never change: a consumer can rely on them. The rendered
# colour is HSV(hue, saturation, value) with the value from the depth (V_FLOOR at the farthest, 1 at the nearest);
# the legend lists the colour at value 1. Left/right = the person's own left/right (MediaPipe's convention).
LEGEND = (
    (0, "background-far", "background", 230.0, 0.55),
    (1, "background-mid", "background", 215.0, 0.40),
    (2, "background-near", "background", 200.0, 0.25),
    (3, "body-other", "body", 0.0, 0.0),
    (4, "head", "head", 25.0, 0.50),
    (5, "face-skin", "face", 30.0, 0.28),
    (6, "brows", "face", 35.0, 0.95),
    (7, "eyes", "face", 58.0, 0.95),
    (8, "nose", "face", 12.0, 0.55),
    (9, "mouth", "face", 350.0, 0.90),
    (10, "ears", "head", 18.0, 0.70),
    (11, "hair", "head", 275.0, 0.70),
    (12, "hat", "head", 175.0, 0.90),
    (13, "glasses", "face", 305.0, 0.85),
    (14, "neck", "body", 38.0, 0.50),
    (15, "torso", "body", 0.0, 0.85),
    (16, "left-upper-arm", "arms", 130.0, 0.90),
    (17, "right-upper-arm", "arms", 70.0, 0.90),
    (18, "left-forearm", "arms", 145.0, 0.70),
    (19, "right-forearm", "arms", 85.0, 0.75),
    (20, "left-hand", "arms", 160.0, 0.95),
    (21, "right-hand", "arms", 100.0, 0.95),
    (22, "left-thigh", "legs", 320.0, 0.85),
    (23, "right-thigh", "legs", 255.0, 0.85),
    (24, "left-shin", "legs", 330.0, 0.65),
    (25, "right-shin", "legs", 265.0, 0.65),
    (26, "left-foot", "legs", 340.0, 0.95),
    (27, "right-foot", "legs", 240.0, 0.95),
    (28, "gaze", "face", 190.0, 1.00),
)
(BG_FAR, BG_MID, BG_NEAR, BODY_OTHER, HEAD, FACE_SKIN, BROWS, EYES, NOSE, MOUTH, EARS, HAIR, HAT, GLASSES, NECK,
 TORSO, L_UPPER_ARM, R_UPPER_ARM, L_FOREARM, R_FOREARM, L_HAND, R_HAND, L_THIGH, R_THIGH, L_SHIN, R_SHIN, L_FOOT,
 R_FOOT, GAZE) = range(29)
assert tuple(e[0] for e in LEGEND) == tuple(range(29))
THING_ID_BASE = 40                 # named things: 40 + k (k = position in the prompt), at most MAX_THINGS
NAME_OF = {e[0]: e[1] for e in LEGEND}
ID_OF = {e[1]: e[0] for e in LEGEND}
V_FLOOR = 0.30                     # value (lightness) at the farthest depth; 1.0 at the nearest
TINT_SATURATION = 0.55             # control-parts.mp4: saturation of the hue laid over the depth luminance
FEATHER_PX = 2
FACEPARSE_OVERRIDABLE = (BODY_OTHER, HEAD, NECK, TORSO)
FACEPARSE_SIZE = 512
# BiSeNet (CelebAMask-HQ) class -> legend id. 0 (background) never overrides.
BISENET_TO_PART = {1: FACE_SKIN, 2: BROWS, 3: BROWS, 4: EYES, 5: EYES, 6: GLASSES, 7: EARS, 8: EARS, 9: EARS,
                   10: NOSE, 11: MOUTH, 12: MOUTH, 13: MOUTH, 14: NECK, 15: NECK, 16: TORSO, 17: HAIR, 18: HAT}
BISENET_LUT = np.zeros(256, np.uint8)
for _k, _v in BISENET_TO_PART.items():
    BISENET_LUT[_k] = _v

# Capsule radii as a fraction of the body unit (the shoulder width in pixels).
RADII = {"upper_arm": 0.16, "forearm": 0.13, "hand": 0.14, "thigh": 0.22, "shin": 0.16, "foot": 0.12}
NEAREST_LIMIT_UNITS = 1.5          # person pixels farther than this x unit from every part stay body-other
NEAREST_LIMIT_MIN_PX = 24.0


class PartsError(RuntimeError):
    """Plain-English problem of the parts step."""


def thing_hue(k: int) -> float:
    """Hue of the k-th named thing: golden-angle steps from magenta (distinct from the body hues)."""
    return round((300.0 + 137.508 * k) % 360.0, 2)


def _rgb(hue: float, sat: float, val: float = 1.0) -> list[int]:
    r, g, b = colorsys.hsv_to_rgb((hue % 360.0) / 360.0, float(sat), float(val))
    return [int(round(255 * r)), int(round(255 * g)), int(round(255 * b))]


def _hex(rgb: Sequence[int]) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in rgb)


def legend(things: Sequence[str] = ()) -> list[dict]:
    """The fixed legend plus one entry per named thing: [{id, name, group, hue, saturation, color, hex}]."""
    if len(things) > MAX_THINGS:
        raise PartsError(f"At most {MAX_THINGS} things can be named ({len(things)} given).")
    out = []
    for cid, name, group, hue, sat in LEGEND:
        color = _rgb(hue, sat)
        out.append({"id": cid, "name": name, "group": group, "hue": hue, "saturation": sat, "color": color,
                    "hex": _hex(color)})
    for k, name in enumerate(things):
        hue = thing_hue(k)
        color = _rgb(hue, 1.0)
        out.append({"id": THING_ID_BASE + k, "name": f"thing:{name}", "group": "things", "hue": hue,
                    "saturation": 1.0, "color": color, "hex": _hex(color), "prompt": str(name)})
    return out


def class_table(things: Sequence[str] = ()) -> tuple[np.ndarray, np.ndarray]:
    """(hue[256], saturation[256]) float32 lookup tables for rendering (unused ids: hue 0, saturation 0)."""
    hue = np.zeros(256, np.float32)
    sat = np.zeros(256, np.float32)
    for entry in legend(things):
        hue[entry["id"]] = entry["hue"]
        sat[entry["id"]] = entry["saturation"]
    return hue, sat


def names_table(things: Sequence[str] = ()) -> dict[int, str]:
    return {entry["id"]: entry["name"] for entry in legend(things)}


# --------------------------------------------------------------------------- rendering
def feather_edges(rgb: np.ndarray, index: np.ndarray, feather_px: int = FEATHER_PX,
                  within: Optional[np.ndarray] = None) -> np.ndarray:
    """Blend the rendered colours across class boundaries (about feather_px wide). `within` limits the feathering
    to boundaries whose whole neighbourhood lies inside that mask (control: keep the person's outline hard)."""
    if feather_px <= 0:
        return rgb
    import cv2
    edge = np.zeros(index.shape, bool)
    edge[:, 1:] |= index[:, 1:] != index[:, :-1]
    edge[:, :-1] |= index[:, 1:] != index[:, :-1]
    edge[1:, :] |= index[1:, :] != index[:-1, :]
    edge[:-1, :] |= index[1:, :] != index[:-1, :]
    if not edge.any():
        return rgb
    k = 2 * feather_px + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    zone = cv2.dilate(edge.astype(np.uint8), kernel).astype(bool)
    if within is not None:
        inner = cv2.erode(within.astype(np.uint8), kernel).astype(bool)
        zone &= inner
        if not zone.any():
            return rgb
    blurred = cv2.GaussianBlur(rgb, (k + 2, k + 2) if (k + 2) % 2 else (k + 1, k + 1), feather_px * 0.6)
    out = rgb.copy()
    out[zone] = blurred[zone]
    return out


def render_map(index: np.ndarray, depth01: np.ndarray, hue: np.ndarray, sat: np.ndarray,
               feather_px: int = FEATHER_PX) -> np.ndarray:
    """Index map -> RGB uint8: hue and saturation per class, value V_FLOOR..1 from the depth (near = bright);
    the gaze marker is always at full value."""
    import cv2
    h = hue[index]
    s = sat[index]
    v = (V_FLOOR + (1.0 - V_FLOOR) * np.clip(depth01, 0.0, 1.0)).astype(np.float32)
    v[index == GAZE] = 1.0
    hsv = np.dstack([h, s, v]).astype(np.float32)
    rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
    out = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return feather_edges(out, index, feather_px)


def render_control(work: np.ndarray, index: np.ndarray, depth01: np.ndarray, region: np.ndarray, hue: np.ndarray,
                   sat: np.ndarray, feather_px: int = FEATHER_PX, tint: float = TINT_SATURATION) -> np.ndarray:
    """control-parts frame: the original outside `region` (person + things), inside it the depth luminance tinted
    by the class hue (saturation = tint x the class saturation, so body-other stays plain grey depth)."""
    import cv2
    h = hue[index]
    s = (sat[index] * tint).astype(np.float32)
    v = np.clip(depth01, 0.0, 1.0).astype(np.float32)
    marker = index == GAZE
    s[marker] = 0.0
    v[marker] = 1.0
    hsv = np.dstack([h, s, v]).astype(np.float32)
    tinted = np.clip(cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    out = np.where(region[:, :, None], tinted, work)
    return feather_edges(out, index, feather_px, within=region)


# --------------------------------------------------------------------------- depth
def depth_luma(depth_rgb: np.ndarray) -> np.ndarray:
    """Coloured depth frame (inferno, near = bright) -> float32 depth in [0, 1] (luminance)."""
    d = np.asarray(depth_rgb)
    if d.ndim == 2:
        return d.astype(np.float32) / 255.0
    return (d.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)) / 255.0


def band_thresholds(depth01_frames: Sequence[np.ndarray], person_masks: Sequence[np.ndarray]) -> tuple[float, float]:
    """Tertiles of the background depth over the given frames -> (far|mid, mid|near) thresholds."""
    values = []
    for d, m in zip(depth01_frames, person_masks):
        bg = np.asarray(d)[~np.asarray(m, bool)]
        if bg.size:
            values.append(bg[:: max(1, bg.size // 20000)])
    if not values:
        return 1.0 / 3.0, 2.0 / 3.0
    sample = np.concatenate(values)
    t1, t2 = float(np.percentile(sample, 100.0 / 3.0)), float(np.percentile(sample, 200.0 / 3.0))
    if t2 - t1 < 1e-3:
        t1, t2 = max(0.0, t1 - 0.05), min(1.0, t2 + 0.05)
    return round(t1, 4), round(t2, 4)


def depth_bands(depth01: np.ndarray, thresholds: tuple[float, float], blur_px: int = 3) -> np.ndarray:
    """Depth -> uint8 band ids: BG_FAR below thresholds[0], BG_MID up to thresholds[1], BG_NEAR above."""
    import cv2
    d = np.asarray(depth01, np.float32)
    if blur_px > 0:
        d = cv2.GaussianBlur(d, (0, 0), blur_px)
    bands = np.full(d.shape, BG_FAR, np.uint8)
    bands[d >= thresholds[0]] = BG_MID
    bands[d >= thresholds[1]] = BG_NEAR
    return bands


# --------------------------------------------------------------------------- body parts from the landmarks
def _pt(p, w: int, h: int) -> tuple[float, float]:
    return float(p[0]) * w, float(p[1]) * h


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _capsule(canvas: np.ndarray, a, b, r: float, value: int) -> None:
    import cv2
    ai = (int(round(a[0])), int(round(a[1])))
    bi = (int(round(b[0])), int(round(b[1])))
    r = max(2, int(round(r)))
    cv2.line(canvas, ai, bi, int(value), 2 * r)
    cv2.circle(canvas, ai, r, int(value), -1)
    cv2.circle(canvas, bi, r, int(value), -1)


def _polygon(canvas: np.ndarray, pts, value: int) -> None:
    import cv2
    arr = np.round(np.asarray(pts, np.float64)).astype(np.int32).reshape(-1, 1, 2)
    cv2.fillPoly(canvas, [arr], int(value))


def _ellipse(canvas: np.ndarray, centre, rx: float, ry: float, value: int) -> None:
    import cv2
    cv2.ellipse(canvas, (int(round(centre[0])), int(round(centre[1]))), (max(1, int(round(rx))),
                max(1, int(round(ry)))), 0, 0, 360, int(value), -1)


def face_box(face, w: int, h: int) -> Optional[tuple[float, float, float, float]]:
    """Bounding box (x0, y0, x1, y1) in pixels of the face mesh points."""
    f = as_array(face)
    if f is None or len(f) < 68:
        return None
    xs, ys = f[:, 0] * w, f[:, 1] * h
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def body_unit(body: Optional[np.ndarray], fbox, w: int, h: int) -> Optional[float]:
    """The size unit of the person in pixels: the shoulder width, else 1.4 x the hip width, else 2.2 x the face
    width, else 3 x the ear distance; None when nothing usable is in the frame."""
    if body is not None:
        def ok(*names):
            return all(body[B[n]][3] >= VISIBLE for n in names)
        if ok("left_shoulder", "right_shoulder"):
            d = _dist(_pt(body[B["left_shoulder"]], w, h), _pt(body[B["right_shoulder"]], w, h))
            if d >= 4:
                return d
        if ok("left_hip", "right_hip"):
            d = _dist(_pt(body[B["left_hip"]], w, h), _pt(body[B["right_hip"]], w, h))
            if d >= 4:
                return 1.4 * d
    if fbox is not None and fbox[2] - fbox[0] >= 4:
        return 2.2 * (fbox[2] - fbox[0])
    if body is not None and body[B["left_ear"]][3] >= VISIBLE and body[B["right_ear"]][3] >= VISIBLE:
        d = _dist(_pt(body[B["left_ear"]], w, h), _pt(body[B["right_ear"]], w, h))
        if d >= 4:
            return 3.0 * d
    return None


def rasterize_body(h: int, w: int, body=None, left_hand=None, right_hand=None, face=None
                   ) -> tuple[np.ndarray, Optional[float]]:
    """Coarse part ids (uint8 HxW, 0 = unassigned) from the landmarks of one frame, and the body unit (px).

    body: 33 x [x, y, z, visibility] normalized; hands: 21 x [x, y, z]; face: 478 x [x, y, z]. Draw order (later
    wins): torso, thighs, shins, feet, neck, head, upper arms, forearms, hands. Points under VISIBLE are skipped;
    without visible hips the torso runs 1.7 units down from the shoulders."""
    canvas = np.zeros((h, w), np.uint8)
    body = as_array(body)
    fbox = face_box(face, w, h)
    unit = body_unit(body, fbox, w, h)
    if unit is None:
        return canvas, None

    def pt(name):
        return _pt(body[B[name]], w, h)

    def ok(*names):
        return body is not None and all(body[B[n]][3] >= VISIBLE for n in names)

    # torso
    if ok("left_shoulder", "right_shoulder"):
        ls, rs = pt("left_shoulder"), pt("right_shoulder")
        if ok("left_hip", "right_hip"):
            lh, rh = pt("left_hip"), pt("right_hip")
        else:
            lh, rh = (ls[0], ls[1] + 1.7 * unit), (rs[0], rs[1] + 1.7 * unit)
        _polygon(canvas, [ls, rs, rh, lh], TORSO)
    # legs
    for side, thigh, shin, foot in (("left", L_THIGH, L_SHIN, L_FOOT), ("right", R_THIGH, R_SHIN, R_FOOT)):
        if ok(f"{side}_hip", f"{side}_knee"):
            _capsule(canvas, pt(f"{side}_hip"), pt(f"{side}_knee"), RADII["thigh"] * unit, thigh)
        if ok(f"{side}_knee", f"{side}_ankle"):
            _capsule(canvas, pt(f"{side}_knee"), pt(f"{side}_ankle"), RADII["shin"] * unit, shin)
        if ok(f"{side}_ankle", f"{side}_foot_index"):
            _capsule(canvas, pt(f"{side}_ankle"), pt(f"{side}_foot_index"), RADII["foot"] * unit, foot)
            if ok(f"{side}_heel"):
                _capsule(canvas, pt(f"{side}_ankle"), pt(f"{side}_heel"), RADII["foot"] * unit, foot)
    # neck: from the chin line to the shoulders
    chin = None
    if fbox is not None:
        f = as_array(face)
        chin = _pt(f[152], w, h) if len(f) > 152 else ((fbox[0] + fbox[2]) / 2.0, fbox[3])
    elif ok("mouth_left", "mouth_right"):
        ml, mr = pt("mouth_left"), pt("mouth_right")
        chin = ((ml[0] + mr[0]) / 2.0, (ml[1] + mr[1]) / 2.0 + 0.22 * unit)
    if chin is not None and ok("left_shoulder", "right_shoulder"):
        ls, rs = pt("left_shoulder"), pt("right_shoulder")
        mid = ((ls[0] + rs[0]) / 2.0, (ls[1] + rs[1]) / 2.0)
        hw = 0.20 * unit
        top = chin[1] - 0.08 * unit
        lsi = (ls[0] + 0.35 * (mid[0] - ls[0]), ls[1] + 0.35 * (mid[1] - ls[1]))
        rsi = (rs[0] + 0.35 * (mid[0] - rs[0]), rs[1] + 0.35 * (mid[1] - rs[1]))
        _polygon(canvas, [(chin[0] - hw, top), (chin[0] + hw, top), rsi, lsi]
                 if ls[0] < rs[0] else [(chin[0] + hw, top), (chin[0] - hw, top), rsi, lsi], NECK)
    # head
    if fbox is not None:
        fw, fh = fbox[2] - fbox[0], fbox[3] - fbox[1]
        cx, cy = (fbox[0] + fbox[2]) / 2.0, (fbox[1] + fbox[3]) / 2.0
        _ellipse(canvas, (cx, cy - 0.12 * fh), 0.72 * fw, 0.80 * fh, HEAD)
    elif body is not None:
        head_pts = [pt(n) for n in ("nose", "left_eye", "right_eye", "left_ear", "right_ear") if ok(n)]
        if head_pts:
            d = None
            if ok("left_ear", "right_ear"):
                d = _dist(pt("left_ear"), pt("right_ear"))
            elif ok("left_eye", "right_eye"):
                d = 2.6 * _dist(pt("left_eye"), pt("right_eye"))
            d = max(d or 0.0, 0.45 * unit)
            cx = sum(p[0] for p in head_pts) / len(head_pts)
            cy = sum(p[1] for p in head_pts) / len(head_pts)
            _ellipse(canvas, (cx, cy - 0.15 * d), 0.80 * d, 1.0 * d, HEAD)
    # arms and hands
    for side, upper, fore, hand_id, hand in (("left", L_UPPER_ARM, L_FOREARM, L_HAND, left_hand),
                                             ("right", R_UPPER_ARM, R_FOREARM, R_HAND, right_hand)):
        if ok(f"{side}_shoulder", f"{side}_elbow"):
            _capsule(canvas, pt(f"{side}_shoulder"), pt(f"{side}_elbow"), RADII["upper_arm"] * unit, upper)
        if ok(f"{side}_elbow", f"{side}_wrist"):
            _capsule(canvas, pt(f"{side}_elbow"), pt(f"{side}_wrist"), RADII["forearm"] * unit, fore)
        hnd = as_array(hand)
        if hnd is not None and len(hnd) >= 21:
            _draw_hand(canvas, hnd, w, h, hand_id, unit)
        elif ok(f"{side}_wrist", f"{side}_index", f"{side}_pinky"):
            wr, ix, pk = pt(f"{side}_wrist"), pt(f"{side}_index"), pt(f"{side}_pinky")
            tip = ((ix[0] + pk[0]) / 2.0, (ix[1] + pk[1]) / 2.0)
            ext = (wr[0] + 1.25 * (tip[0] - wr[0]), wr[1] + 1.25 * (tip[1] - wr[1]))
            _capsule(canvas, wr, ext, RADII["hand"] * unit, hand_id)
    return canvas, unit


def _draw_hand(canvas: np.ndarray, hand: np.ndarray, w: int, h: int, value: int, unit: float) -> None:
    """The 21 hand points: their convex hull plus fat bones (the fingers stick out of the hull)."""
    import cv2
    pts = np.array([_pt(p, w, h) for p in hand[:21]], np.float32)
    span = max(_dist(pts[0], pts[9]), 1.0)                       # wrist -> middle knuckle
    r = max(2, int(round(min(0.14 * span, 0.08 * unit) + 1)))
    hull = cv2.convexHull(np.round(pts).astype(np.int32))
    cv2.fillConvexPoly(canvas, hull, int(value))
    for a, b in HAND_EDGES:
        _capsule(canvas, pts[a], pts[b], r, value)


def nearest_fill(index: np.ndarray, person: np.ndarray, limit_px: float,
                 fill_value: int = BODY_OTHER) -> np.ndarray:
    """Person pixels without a part take the part of the nearest assigned pixel (Euclidean distance transform)
    when it is within limit_px, else `fill_value`. Pixels outside `person` become 0."""
    import cv2
    out = np.asarray(index, np.uint8).copy()
    person = np.asarray(person, bool)
    out[~person] = 0
    todo = person & (out == 0)
    if not todo.any():
        return out
    seeds = out != 0
    if not seeds.any() or limit_px <= 0:
        out[todo] = fill_value
        return out
    src = (~seeds).astype(np.uint8)                              # zero = a seed (distance 0)
    dist, labels = cv2.distanceTransformWithLabels(src, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lut = np.zeros(int(labels.max()) + 1, np.uint8)
    lut[labels[seeds]] = out[seeds]
    near = todo & (dist <= float(limit_px))
    out[near] = lut[labels[near]]
    out[todo & ~near] = fill_value
    return out


# --------------------------------------------------------------------------- head detail (BiSeNet)
def bisenet_model() -> Path:
    return facefusion_dir() / ".assets" / "models" / "bisenet_resnet_34.onnx"


def facefusion_python() -> Path:
    return facefusion_dir() / ".venv" / "bin" / "python"


def faceparse_available() -> tuple[bool, str]:
    if not facefusion_python().is_file():
        return False, f"FaceFusion's venv is missing ({facefusion_python()}): run effects/setup.sh --faceswap"
    if not bisenet_model().is_file():
        return False, f"FaceFusion's face parser is missing ({bisenet_model()})"
    return True, "ok"


def head_box(face, body, w: int, h: int) -> Optional[tuple[int, int, int, int]]:
    """The head crop (x0, y0, x1, y1) for BiSeNet: the face box widened 0.6 x its width on each side, 1.0 x its
    height upwards (hair, hat) and 0.25 x downwards (neck), like CelebAMask-HQ's framing; from the pose's head
    points when there is no face mesh. None without a head in the frame."""
    fbox = face_box(face, w, h)
    if fbox is None:
        b = as_array(body)
        if b is None:
            return None
        names = [n for n in ("nose", "left_eye", "right_eye", "left_ear", "right_ear") if b[B[n]][3] >= VISIBLE]
        if len(names) < 3:
            return None
        xs = [b[B[n]][0] * w for n in names]
        ys = [b[B[n]][1] * h for n in names]
        fw = max(max(xs) - min(xs), 8.0) * 1.3
        fh = fw * 1.15
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        fbox = (cx - fw / 2.0, cy - 0.5 * fh, cx + fw / 2.0, cy + 0.7 * fh)
    x0, y0, x1, y1 = fbox
    fw, fh = max(x1 - x0, 8.0), max(y1 - y0, 8.0)
    bx0, bx1 = int(max(0, math.floor(x0 - 0.6 * fw))), int(min(w, math.ceil(x1 + 0.6 * fw)))
    by0, by1 = int(max(0, math.floor(y0 - 1.0 * fh))), int(min(h, math.ceil(y1 + 0.25 * fh)))
    if bx1 - bx0 < 16 or by1 - by0 < 16:
        return None
    return bx0, by0, bx1, by1


def run_faceparse_worker(crops: np.ndarray, report: Callable[[float, str], None]) -> tuple[np.ndarray, str]:
    """BiSeNet over (K, 512, 512, 3) crops in FaceFusion's venv -> ((K, 512, 512) uint8 classes, provider)."""
    ok, why = faceparse_available()
    if not ok:
        raise PartsError(why)
    worker = Path(__file__).with_name("faceparse_worker.py")
    provider = "?"
    with tempfile.TemporaryDirectory(prefix="zfx-parts-") as tmp:
        src, dst = Path(tmp) / "crops.npy", Path(tmp) / "classes.npy"
        np.save(src, np.ascontiguousarray(crops, dtype=np.uint8))
        cmd = [str(facefusion_python()), "-I", str(worker), str(bisenet_model()), str(src), str(dst)]
        if os.environ.get("ZFX_FACEPARSE_CPU", "") == "1":
            cmd.append("--cpu")
        log.debug("run: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "i" in msg and "n" in msg:
                report(msg["i"] / max(1, msg["n"]), f"face parser: crop {msg['i']}/{msg['n']}")
            elif msg.get("warning"):
                log.warning("face parser: %s", msg["warning"])
            elif msg.get("done"):
                provider = str(msg.get("provider"))
        err = (proc.stderr.read() if proc.stderr else "").strip()
        code = proc.wait()
        if code != 0 or not dst.is_file():
            tail = " | ".join(err.splitlines()[-4:])
            raise PartsError(f"The face parser failed (exit {code}): {tail[-400:] or 'no output'}")
        classes = np.load(dst)
    return classes, provider


def face_parse_frames(frames: np.ndarray, boxes: Sequence[Optional[tuple[int, int, int, int]]],
                      report: Callable[[float, str], None], backend=None) -> tuple[list, dict]:
    """Per frame the (512, 512) BiSeNet class map of its head crop (None without a box). `backend` (tests):
    callable (K, 512, 512, 3) uint8 -> (K, 512, 512) uint8."""
    import cv2
    todo = [i for i, b in enumerate(boxes) if b is not None]
    out: list = [None] * len(boxes)
    info = {"frames": len(todo), "provider": None}
    if not todo:
        return out, info
    crops = np.zeros((len(todo), FACEPARSE_SIZE, FACEPARSE_SIZE, 3), np.uint8)
    for k, i in enumerate(todo):
        x0, y0, x1, y1 = boxes[i]
        crops[k] = cv2.resize(frames[i][y0:y1, x0:x1], (FACEPARSE_SIZE, FACEPARSE_SIZE),
                              interpolation=cv2.INTER_LINEAR)
    report(0.0, f"BiSeNet face parser on {len(todo)} head crops")
    if backend is not None:
        classes = np.asarray(backend(crops), np.uint8)
        info["provider"] = getattr(backend, "name", "backend")
    else:
        classes, info["provider"] = run_faceparse_worker(crops, report)
    for k, i in enumerate(todo):
        out[i] = classes[k]
    return out, info


def apply_faceparse(index: np.ndarray, classes: Optional[np.ndarray], box, person: np.ndarray) -> np.ndarray:
    """Paste the BiSeNet classes of the head crop into the index map where they are allowed to override."""
    import cv2
    if classes is None or box is None:
        return index
    x0, y0, x1, y1 = box
    region = cv2.resize(classes, (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST)
    mapped = BISENET_LUT[region]
    sub = index[y0:y1, x0:x1]
    allowed = person[y0:y1, x0:x1] & np.isin(sub, FACEPARSE_OVERRIDABLE) & (mapped != 0)
    sub[allowed] = mapped[allowed]
    return index


# --------------------------------------------------------------------------- gaze
RIGHT_EYE = (33, 160, 158, 133, 153, 144)     # outer corner, upper lid x2, inner corner, lower lid x2
LEFT_EYE = (362, 385, 387, 263, 373, 380)     # inner corner, upper lid x2, outer corner, lower lid x2
RIGHT_IRIS, LEFT_IRIS = 468, 473
GAZE_H_THRESHOLD, GAZE_V_THRESHOLD = 0.22, 0.30   # fraction of the eye's half width / height
# Blink: the eye aspect ratio (EAR, Soukupová & Čech 2016) drops under BLINK_RATIO x its local median (the
# BLINK_WINDOW frames around, 1 s at 16 fps) and under BLINK_MAX. Narrowed eyes (a smile, looking down) lower
# the baseline for seconds and are not blinks; "openness" (EAR / the clip's 90th percentile) shows them.
BLINK_RATIO, BLINK_WINDOW, BLINK_MAX = 0.7, 8, 0.24


def eye_metrics(face_px: np.ndarray, eye: Sequence[int], iris: int) -> dict:
    p = [face_px[i] for i in eye]
    corner_a, corner_b = p[0], p[3]
    width = max(_dist(corner_a, corner_b), 1e-3)
    top = ((p[1][0] + p[2][0]) / 2.0, (p[1][1] + p[2][1]) / 2.0)
    bottom = ((p[4][0] + p[5][0]) / 2.0, (p[4][1] + p[5][1]) / 2.0)
    centre = ((corner_a[0] + corner_b[0]) / 2.0, (top[1] + bottom[1]) / 2.0)
    half_w = width / 2.0
    half_h = max(_dist(top, bottom) / 2.0, 0.35 * half_w * 0.5, 0.5)
    ear = (_dist(p[1], p[5]) + _dist(p[2], p[4])) / (2.0 * width)
    ix, iy = float(face_px[iris][0]), float(face_px[iris][1])
    dx = float(np.clip((ix - centre[0]) / half_w, -1.5, 1.5))
    dy = float(np.clip((iy - centre[1]) / half_h, -1.5, 1.5))
    xs = [q[0] for q in p] + [ix]
    ys = [q[1] for q in p] + [iy]
    return {"dx": round(dx, 3), "dy": round(dy, 3), "ear": round(float(ear), 3), "iris": [round(ix, 1), round(iy, 1)],
            "half_w": round(half_w, 2), "half_h": round(half_h, 2),
            "box": [int(math.floor(min(xs))), int(math.floor(min(ys))), int(math.ceil(max(xs))), int(math.ceil(max(ys)))]}


def gaze_direction(dx: float, dy: float, h_thr: float = GAZE_H_THRESHOLD, v_thr: float = GAZE_V_THRESHOLD) -> str:
    """'centre', else the dominant axis: 'left'/'right' (the picture's left/right) or 'up'/'down'."""
    if abs(dx) < h_thr and abs(dy) < v_thr:
        return "centre"
    if abs(dx) / h_thr >= abs(dy) / v_thr:
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def frame_gaze(face, w: int, h: int) -> Optional[dict]:
    """Per-eye metrics of one frame from the 478-point mesh (None without the irises). Blink and openness are
    decided later over the whole clip (see blink_flags)."""
    f = as_array(face)
    if f is None or len(f) < 478:
        return None
    px = f[:, :2] * np.array([w, h], np.float64)
    left = eye_metrics(px, LEFT_EYE, LEFT_IRIS)
    right = eye_metrics(px, RIGHT_EYE, RIGHT_IRIS)
    dx = (left["dx"] + right["dx"]) / 2.0
    dy = (left["dy"] + right["dy"]) / 2.0
    for eye in (left, right):
        eye["direction"] = gaze_direction(eye["dx"], eye["dy"])
    return {"left": left, "right": right, "vector": [round(dx, 3), round(dy, 3)],
            "direction": gaze_direction(dx, dy), "ear": round((left["ear"] + right["ear"]) / 2.0, 3)}


def blink_flags(ears: Sequence[Optional[float]], ratio: float = BLINK_RATIO, window: int = BLINK_WINDOW,
                max_ear: float = BLINK_MAX) -> tuple[list[Optional[bool]], list[Optional[float]]]:
    """Per frame: blink (EAR under ratio x the median EAR of the +-window frames around, and under max_ear),
    and openness (EAR / the clip's 90th percentile, 0..1). None where there is no EAR."""
    n = len(ears)
    valid = [e for e in ears if e is not None and math.isfinite(e)]
    if not valid:
        return [None] * n, [None] * n
    p90 = max(float(np.percentile(valid, 90)), 1e-6)
    blinks: list = [None] * n
    openness: list = [None] * n
    for i, e in enumerate(ears):
        if e is None or not math.isfinite(e):
            continue
        around = [x for x in ears[max(0, i - window):i + window + 1] if x is not None and math.isfinite(x)]
        local = float(np.median(around)) if around else e
        blinks[i] = bool(e < ratio * local and e < max_ear)
        openness[i] = round(float(np.clip(e / p90, 0.0, 1.0)), 3)
    return blinks, openness


def draw_gaze(index: np.ndarray, gaze: Optional[dict], person: np.ndarray) -> None:
    """A dot on each iris and a short arrow in the gaze direction, as the GAZE tone, inside the eye boxes."""
    import cv2
    if not gaze or gaze.get("blink"):
        return
    h, w = index.shape
    for eye in (gaze["left"], gaze["right"]):
        r = max(1, int(round(0.30 * eye["half_w"])))
        layer = np.zeros((h, w), np.uint8)
        ix, iy = int(round(eye["iris"][0])), int(round(eye["iris"][1]))
        cv2.circle(layer, (ix, iy), r, 1, -1)
        if gaze["direction"] != "centre":
            ex = ix + eye["dx"] * eye["half_w"] * 0.9
            ey = iy + eye["dy"] * eye["half_h"] * 0.9
            cv2.line(layer, (ix, iy), (int(round(ex)), int(round(ey))), 1, max(1, r // 2 + 1))
        x0, y0, x1, y1 = eye["box"]
        pad = 2
        zone = np.zeros((h, w), bool)
        zone[max(0, y0 - pad):min(h, y1 + pad + 1), max(0, x0 - pad):min(w, x1 + pad + 1)] = True
        index[(layer > 0) & zone & person] = GAZE


# --------------------------------------------------------------------------- temporal
def temporal_majority(prev: Optional[np.ndarray], cur: np.ndarray, nxt: Optional[np.ndarray]) -> np.ndarray:
    """Majority of 3 frames for a label map: where the neighbours agree with each other they outvote the frame."""
    if prev is None or nxt is None:
        return cur
    return np.where(prev == nxt, prev, cur)


# --------------------------------------------------------------------------- the clip
class Backends:
    """Optional stand-ins (tests): things.segment(frames, prompts) -> probabilities; face_parse(crops) -> classes."""

    def __init__(self, things=None, face_parse=None):
        self.things = things
        self.face_parse = face_parse


def _load_landmarks(job: Job, work_path: Path, mask_path: Path, n: int, say: Callable[[str, float, str], None],
                    face_needed: bool) -> dict:
    """Per frame body / left_hand / right_hand / face tracks (lists of length n, None where missing) from
    motion.json and face-landmarks.json; the pose and the face mesh are computed (and saved) when missing."""
    from .mesh import detect_landmarks, load_landmarks, write_landmarks
    from .pose import detect_motion, load_motion, write_motion

    def pad(track):
        track = list(track or [])[:n]
        return track + [None] * (n - len(track))

    body = hands_l = hands_r = face = [None] * n
    sources: dict = {}
    motion_path = job.dir / "motion.json"
    face_path = job.dir / "face-landmarks.json"
    if motion_path.is_file():
        try:
            motion = load_motion(motion_path)
            body = pad(motion["body"]["image"])
            hands_l = pad(motion["hands"]["left"]["image"])
            hands_r = pad(motion["hands"]["right"]["image"])
            if motion.get("face") and motion["face"].get("image"):
                face = pad(motion["face"]["image"])
            sources["motion"] = motion_path.name
        except Exception as exc:
            log.warning("could not use %s (%s)", motion_path.name, exc)
    if all(f is None for f in face) and face_path.is_file():
        try:
            face = pad(load_landmarks(face_path).get("frames"))
            sources["face"] = face_path.name
        except Exception as exc:
            log.warning("could not use %s (%s)", face_path.name, exc)
    if all(f is None for f in face) and face_needed:
        say("landmarks", 0.0, "face mesh (MediaPipe Face Landmarker)")
        landmarks = detect_landmarks(work_path, lambda f, m: say("landmarks", 0.5 * f, m), mask_path=mask_path)
        face = pad(landmarks["frames"])
        if not face_path.is_file():
            write_landmarks(landmarks, face_path)
            job.set_artifact("landmarks", face_path, save=False)
        sources["face"] = face_path.name + " (computed)"
        landmarks_dict = landmarks
    else:
        landmarks_dict = None
    if all(b is None for b in body) and "motion" not in sources:
        say("landmarks", 0.5, "whole-body pose (MediaPipe Pose + Hand Landmarker)")
        face_arg = landmarks_dict
        if face_arg is None and any(f is not None for f in face):
            face_arg = {"frames": face, "points": 478, "model": "face_landmarker.task"}
        motion = detect_motion(work_path, lambda f, m: say("landmarks", 0.5 + 0.5 * f, m), mask_path=mask_path,
                               face=face_arg, source={"job": job.id, "video": "work.mp4"})
        body = pad(motion["body"]["image"])
        hands_l = pad(motion["hands"]["left"]["image"])
        hands_r = pad(motion["hands"]["right"]["image"])
        if not motion_path.is_file():
            write_motion(motion, motion_path)
            job.set_artifact("motion", motion_path, save=False)
        sources["motion"] = motion_path.name + " (computed)"
    return {"body": body, "left_hand": hands_l, "right_hand": hands_r, "face": face, "sources": sources}


def _read_clip(work_path: Path, mask_path: Path, depth_path: Path, say) -> dict:
    info = probe(work_path)
    w, h = int(info["width"]), int(info["height"])
    say("load", 0.05, "reading work.mp4")
    work = np.stack(list(iter_frames(work_path)), axis=0)
    say("load", 0.45, "reading mask.mp4")
    masks = np.stack([m > 127 for m in iter_frames(mask_path, size=(w, h), gray=True)], axis=0)
    say("load", 0.7, "reading depth.mp4")
    from .composite import sample_indices
    keep = set(sample_indices(len(work), 4))      # the coloured depth of the preview frames only
    depth_rgb: dict = {}
    depth = []
    for i, d in enumerate(iter_frames(depth_path, size=(w, h))):
        depth.append(depth_luma(d))
        if i in keep:
            depth_rgb[i] = d
    depth = np.stack(depth, axis=0).astype(np.float32)
    n = min(len(work), len(masks), len(depth))
    if n == 0:
        raise PartsError("work.mp4, mask.mp4 or depth.mp4 has no frames.")
    if not (len(work) == len(masks) == len(depth)):
        log.warning("parts: frame counts differ (work %d, mask %d, depth %d); using %d", len(work), len(masks),
                    len(depth), n)
    return {"w": w, "h": h, "n": n, "work": work[:n], "masks": masks[:n], "depth": depth[:n],
            "depth_rgb": depth_rgb}


def run_parts(job: Job, things: Sequence[str] = (), gaze: bool = False, over_person: bool = False,
              things_fps: float = THINGS_FPS, things_threshold: float = THRESHOLD, things_template: str = TEMPLATE,
              things_zoom: bool = True, face_detail: bool = True, device: Optional[str] = None,
              report: Optional[Callable[[str, float, str], None]] = None, backends: Optional[Backends] = None,
              preview: bool = True) -> dict:
    """Build parts.mp4, parts.npz, parts.json, control-parts.mp4 and preview-parts.jpg for a prepared job.
    report(stage, fraction, message); stages: load, landmarks, faceparse, things, parts, render, preview."""
    import cv2
    from .composite import contact_sheet, sample_indices
    say = report or (lambda stage, frac, msg: None)
    backends = backends or Backends()
    things = list(things)
    if len(things) > MAX_THINGS:
        raise PartsError(f"At most {MAX_THINGS} things can be named ({len(things)} given).")
    t_all = time.monotonic()
    timings: dict = {}
    warnings: list[str] = []
    paths = {name: job.path(name) for name in ("work", "mask", "depth")}
    missing = [p.name for p in paths.values() if not p.is_file()]
    if missing:
        raise PartsError(f"Job {job.id} is missing {', '.join(missing)}: prepare it with a depth mode first "
                         "(depth, depth_mesh or depth_pose).")

    # 1. the clip
    t0 = time.monotonic()
    clip = _read_clip(paths["work"], paths["mask"], paths["depth"], say)
    w, h, n = clip["w"], clip["h"], clip["n"]
    work, masks, depth = clip["work"], clip["masks"], clip["depth"]
    timings["load"] = round(time.monotonic() - t0, 2)
    say("load", 1.0, f"{n} frames {w}x{h}")

    # 2. landmarks
    t0 = time.monotonic()
    say("landmarks", 0.0, "pose, hands and face from the job")
    tracks = _load_landmarks(job, paths["work"], paths["mask"], n, say, face_needed=True)
    body_frames = sum(1 for b in tracks["body"] if b is not None)
    face_frames = sum(1 for f in tracks["face"] if f is not None)
    timings["landmarks"] = round(time.monotonic() - t0, 2)
    say("landmarks", 1.0, f"body in {body_frames}/{n} frames, face in {face_frames}/{n}")
    if body_frames == 0:
        warnings.append("No body found in any frame: the person is body-other only (plus the face detail).")

    # 3. head detail
    t0 = time.monotonic()
    boxes = [head_box(tracks["face"][i], tracks["body"][i], w, h) for i in range(n)]
    classes: list = [None] * n
    faceparse_info: dict = {"frames": 0, "provider": None, "model": None}
    if face_detail:
        available, why = (True, "ok") if backends.face_parse is not None else faceparse_available()
        if available:
            try:
                classes, faceparse_info = face_parse_frames(work, boxes, lambda f, m: say("faceparse", f, m),
                                                            backend=backends.face_parse)
                faceparse_info["model"] = bisenet_model().name if backends.face_parse is None else "fake"
            except PartsError as exc:
                warnings.append(f"Face detail skipped: {exc}")
                classes = [None] * n
        else:
            warnings.append(f"Face detail skipped: {why}")
    timings["faceparse"] = round(time.monotonic() - t0, 2)
    say("faceparse", 1.0, f"face detail in {faceparse_info.get('frames', 0)}/{n} frames")

    # 4. things
    t0 = time.monotonic()
    things_info: dict = {"names": things, "prompt": ", ".join(things), "found": {}}
    probs = None
    if things:
        found = detect_things(work, things, lambda f, m: say("things", f, m), backend=backends.things,
                              fps=things_fps, template=things_template, device=device, masks=masks,
                              zoom=things_zoom)
        probs = found["probs"]
        things_info.update({"prompts": found["prompts"], "sampled_frames": found["sampled"],
                            "sample_fps": things_fps, "threshold": things_threshold, "template": things_template,
                            "model": found["model"], "revision": found["revision"], "device": found["device"],
                            "map_size": found["size"], "zoom_box": found["zoom"], "over_person": bool(over_person),
                            "blobs": "above the threshold, with a pixel above threshold + 0.1, at least 0.02 % "
                                     "of the frame; the most likely thing wins a pixel"})
        max_prob = found["max_prob"]
    timings["things"] = round(time.monotonic() - t0, 2)
    say("things", 1.0, f"{len(things)} thing(s)" if things else "no things named")

    # 5. the index map per frame
    t0 = time.monotonic()
    picks16 = sample_indices(n, 16)
    thresholds = band_thresholds([depth[i] for i in picks16], [masks[i] for i in picks16])
    raw = np.zeros((n, h, w), np.uint8)
    units: list = [None] * n
    things_anywhere = np.zeros((n, len(things)), bool)
    things_present = np.zeros((n, len(things)), bool)
    for i in range(n):
        person = masks[i]
        coarse, unit = rasterize_body(h, w, tracks["body"][i], tracks["left_hand"][i], tracks["right_hand"][i],
                                      tracks["face"][i])
        units[i] = unit
        limit = max(NEAREST_LIMIT_MIN_PX, NEAREST_LIMIT_UNITS * unit) if unit else 0.0
        filled = nearest_fill(coarse, person, limit)
        index = depth_bands(depth[i], thresholds)
        index[person] = filled[person]
        apply_faceparse(index, classes[i], boxes[i], person)
        if probs is not None and probs.shape[1]:
            p = things_probabilities(probs[i], w, h)
            regions = things_regions(p, things_threshold)
            best = p.argmax(axis=0)
            for k in range(len(things)):
                hit = regions[k] & (best == k)
                things_anywhere[i, k] = bool(hit.any())
                sel = hit if over_person else (hit & ~person)
                things_present[i, k] = bool(sel.any())
                index[sel] = THING_ID_BASE + k
        raw[i] = index
        if i % 8 == 7 or i + 1 == n:
            say("parts", (i + 1) / n, f"parts: frame {i + 1}/{n}")
    index_all = np.zeros_like(raw)
    for i in range(n):
        index_all[i] = temporal_majority(raw[i - 1] if i > 0 else None, raw[i], raw[i + 1] if i + 1 < n else None)
    del raw
    # gaze (after the smoothing, so a moving marker is not voted away)
    gazes: list = [None] * n
    if gaze:
        gazes = [frame_gaze(tracks["face"][i], w, h) for i in range(n)]
        if all(g is None for g in gazes):
            warnings.append("Gaze unavailable: the face mesh has no iris points (478 needed) or no face was found.")
        else:
            blinks, openness = blink_flags([g["ear"] if g else None for g in gazes])
            for i, g in enumerate(gazes):
                if g:
                    g["blink"] = bool(blinks[i])
                    g["openness"] = openness[i]
                    draw_gaze(index_all[i], g, masks[i])
    timings["parts"] = round(time.monotonic() - t0, 2)

    # 6. render + stats
    t0 = time.monotonic()
    hue, sat = class_table(things)
    names = names_table(things)
    picks = sample_indices(n, 4)
    samples: list = []
    frames_out: list = []
    counts_all = np.zeros((n, 256), np.int64)
    parts_path = job.dir / "parts.mp4"
    control_path = job.dir / "control-parts.mp4"
    with VideoWriter(parts_path, w, h, WORK_FPS, crf=14) as pw, VideoWriter(control_path, w, h, WORK_FPS,
                                                                            crf=14) as cw:
        for i in range(n):
            index = index_all[i]
            person = masks[i]
            rendered = render_map(index, depth[i], hue, sat)
            region = person | (index >= THING_ID_BASE)
            control = render_control(work[i], index, depth[i], region, hue, sat)
            pw.write(rendered)
            cw.write(control)
            counts = np.bincount(index.ravel(), minlength=256)
            counts_all[i] = counts
            area = float(w * h)
            entry = {
                "i": i, "t": round(i / float(WORK_FPS), 4),
                "body": tracks["body"][i] is not None, "face": tracks["face"][i] is not None,
                "face_detail": classes[i] is not None, "unit_px": round(units[i], 1) if units[i] else None,
                "person": round(float(person.mean()), 4),
                "pixels": {names.get(c, str(c)): int(counts[c]) for c in np.flatnonzero(counts)},
            }
            if things:
                entry["things"] = {things[k]: {"present": bool(things_present[i, k]),
                                               "anywhere": bool(things_anywhere[i, k]),
                                               "max_prob": round(float(max_prob[i, k]), 3),
                                               "pixels": int(counts[THING_ID_BASE + k])} for k in range(len(things))}
            if gaze:
                entry["gaze"] = gazes[i]
                entry["blink"] = bool(gazes[i]["blink"]) if gazes[i] else None
            frames_out.append(entry)
            if i in picks:
                drgb = clip["depth_rgb"].get(i)
                if drgb is None:
                    drgb = np.repeat((depth[i] * 255).astype(np.uint8)[:, :, None], 3, axis=2)
                samples.append([work[i], drgb, (person.astype(np.uint8) * 255), rendered, control])
            if i % 8 == 7 or i + 1 == n:
                say("render", (i + 1) / n, f"parts.mp4 + control-parts.mp4 frame {i + 1}/{n}")
    timings["render"] = round(time.monotonic() - t0, 2)

    # 7. index file
    npz_path = job.dir / "parts.npz"
    ids = np.array(sorted(names), np.int32)
    np.savez_compressed(npz_path, index=index_all, ids=ids, names=np.array([names[int(k)] for k in ids]),
                        things=np.array(list(things)), fps=np.array(WORK_FPS), size=np.array([w, h]),
                        format=np.array(f"{PARTS_FORMAT}/{PARTS_VERSION}"))

    # 8. preview
    preview_path = job.dir / "preview-parts.jpg"
    if preview and samples:
        t0 = time.monotonic()
        sheet = contact_sheet(samples, ["work", "depth", "mask", "parts", "control-parts"], sorted(picks))
        present_ids = [int(c) for c in np.flatnonzero(counts_all.sum(axis=0))]
        sheet = np.vstack([sheet, legend_strip(sheet.shape[1], [e for e in legend(things) if e["id"] in present_ids])])
        tmp = preview_path.with_name(preview_path.stem + ".partial.jpg")
        if not cv2.imwrite(str(tmp), sheet[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise PartsError(f"could not write {preview_path.name}")
        tmp.replace(preview_path)
        timings["preview"] = round(time.monotonic() - t0, 2)
        say("preview", 1.0, "preview-parts.jpg")

    # 9. stats + parts.json
    area = float(w * h)
    mean_cov = counts_all.mean(axis=0) / area
    frames_with = (counts_all > 0).sum(axis=0)
    classes_stats = {names[c]: {"frames": int(frames_with[c]), "mean_coverage": round(float(mean_cov[c]), 5)}
                     for c in sorted(names) if frames_with[c] > 0}
    for k, name in enumerate(things):
        things_info["found"][name] = {
            "frames": int(things_present[:, k].sum()), "rate": round(float(things_present[:, k].mean()), 3),
            "frames_anywhere": int(things_anywhere[:, k].sum()),
            "rate_anywhere": round(float(things_anywhere[:, k].mean()), 3),
            "mean_max_prob": round(float(max_prob[:, k].mean()), 3) if things else None,
            "mean_coverage": round(float(mean_cov[THING_ID_BASE + k]), 5)}
        if things_anywhere[:, k].any() and not things_present[:, k].any() and not over_person:
            warnings.append(f"'{name}' was only found on the person (kept out of the map; "
                            "use --things-over-person to let it override the body tones).")
        elif not things_anywhere[:, k].any():
            warnings.append(f"'{name}' was not found in any frame (CLIPSeg below {things_threshold:g}).")
    gaze_stats = None
    if gaze and any(gazes):
        directions: dict = {}
        for g in gazes:
            if g:
                directions[g["direction"]] = directions.get(g["direction"], 0) + 1
        ears = [g["ear"] for g in gazes if g]
        gaze_stats = {"frames": sum(1 for g in gazes if g), "directions": directions,
                      "blink_frames": sum(1 for g in gazes if g and g["blink"]),
                      "blink_rule": f"EAR < {BLINK_RATIO} x the median EAR of the +-{BLINK_WINDOW} frames around "
                                    f"and < {BLINK_MAX}",
                      "ear_median": round(float(np.median(ears)), 3), "ear_p90": round(float(np.percentile(ears, 90)), 3),
                      "mean_openness": round(float(np.mean([g["openness"] for g in gazes if g])), 3),
                      "mean_vector": [round(float(np.mean([g["vector"][0] for g in gazes if g])), 3),
                                      round(float(np.mean([g["vector"][1] for g in gazes if g])), 3)],
                      "convention": "dx > 0 = the picture's right (the person's left), dy > 0 = down; units = "
                                    "fraction of the eye's half width / height (the iris inside the eye box: the "
                                    "vertical part is weak on small faces, the lids follow the iris); left/right "
                                    "eye = the person's own; openness = EAR / the clip's 90th percentile"}
    person_cov = float(masks.mean())
    timings["total"] = round(time.monotonic() - t_all, 2)
    data = {
        "format": PARTS_FORMAT, "version": PARTS_VERSION,
        "generator": f"YouTube Zeus Effects {__version__} (zfx.parts)", "created": now_iso(),
        "job": job.id, "video": "work.mp4", "fps": WORK_FPS, "frames": n, "width": w, "height": h,
        "legend": legend(things),
        "conventions": {"left_right": "the person's own left/right (MediaPipe)", "index": "parts.npz index "
                        "(N, H, W) uint8 = legend id per pixel; things = 40 + position in the prompt",
                        "render": f"parts.mp4 = HSV(hue, saturation, {V_FLOOR} + {1 - V_FLOOR:.2f} x depth), "
                                  f"edges feathered {FEATHER_PX} px; control-parts.mp4 = original outside the "
                                  f"person and things, depth luminance tinted (saturation x {TINT_SATURATION}) "
                                  "inside, person outline hard"},
        "sources": {"mask": "mask.mp4", "depth": "depth.mp4", **tracks["sources"],
                    "face_parser": faceparse_info.get("model"), "face_parser_provider": faceparse_info.get("provider")},
        "depth_bands": {"thresholds": list(thresholds), "meaning": "depth luminance (0 far .. 1 near) below the first "
                        "= background-far, below the second = background-mid, else background-near"},
        "smoothing": {"temporal": "majority of 3 frames (gaze drawn afterwards)", "feather_px": FEATHER_PX,
                      "things": f"linear in time between frames sampled at {things_fps:g} fps"},
        "things": things_info,
        "gaze": gaze_stats if gaze else None,
        "stats": {"person_coverage": round(person_cov, 4), "body_frames": body_frames, "face_frames": face_frames,
                  "face_detail_frames": int(faceparse_info.get("frames", 0)), "classes": classes_stats},
        "timings": timings,
        "warnings": warnings,
        "frames": frames_out,
    }
    json_path = job.dir / "parts.json"
    atomic_write_text(json_path, json.dumps(data, ensure_ascii=False, separators=(",", ":"), indent=None)
                      .replace('},{"i":', '},\n{"i":') + "\n")
    # job.json: the new artifacts (never touching the approved ones)
    job.set_artifact("parts", parts_path, save=False)
    job.set_artifact("parts_index", npz_path, save=False)
    job.set_artifact("parts_json", json_path, save=False)
    job.set_artifact("control_parts", control_path, save=False)
    if preview_path.is_file():
        job.set_artifact("preview_parts", preview_path, save=False)
    summary = {
        "id": job.id, "frames": n, "size": [w, h], "fps": WORK_FPS,
        "person_coverage": round(person_cov, 4), "body_frames": body_frames, "face_frames": face_frames,
        "face_detail_frames": int(faceparse_info.get("frames", 0)),
        "classes": {k: round(v["mean_coverage"] * 100.0, 3) for k, v in classes_stats.items()},
        "things": things_info["found"], "things_prompt": things_info["prompt"],
        "gaze": gaze_stats, "timings": timings, "warnings": warnings,
        "files": {"parts": str(parts_path), "parts_index": str(npz_path), "parts_json": str(json_path),
                  "control_parts": str(control_path),
                  "preview_parts": str(preview_path) if preview_path.is_file() else None},
    }
    job["parts_info"] = {k: summary[k] for k in ("frames", "person_coverage", "body_frames", "face_frames",
                                                 "face_detail_frames", "classes", "things", "things_prompt",
                                                 "gaze", "timings", "warnings")}
    job.save()
    return summary


def legend_strip(width: int, entries: Sequence[dict], swatch: int = 14, row_h: int = 24) -> np.ndarray:
    """A dark strip with colour swatches and names (for preview-parts.jpg)."""
    import cv2
    font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1
    items = []
    for e in entries:
        (tw, _), _ = cv2.getTextSize(e["name"], font, scale, thick)
        items.append((e, swatch + 6 + tw + 16))
    rows: list[list] = [[]]
    x = 8
    for e, iw in items:
        if x + iw > width - 8 and rows[-1]:
            rows.append([])
            x = 8
        rows[-1].append((e, x))
        x += iw
    strip = np.full((row_h * len(rows) + 6, width, 3), 24, np.uint8)
    for r, row in enumerate(rows):
        y = 4 + r * row_h
        for e, x in row:
            color = tuple(int(c) for c in e["color"])
            cv2.rectangle(strip, (x, y + 3), (x + swatch, y + 3 + swatch), color, -1)
            cv2.putText(strip, e["name"], (x + swatch + 6, y + 3 + swatch - 2), font, scale, (235, 235, 235),
                        thick, cv2.LINE_AA)
    return strip


def summary_lines(result: dict) -> list[str]:
    """Human summary of run_parts' result (one line per item)."""
    n = result.get("frames") or 0
    lines = [f"Parts of job {result.get('id')}: {n} frames {result.get('size', ['?', '?'])[0]}x"
             f"{result.get('size', ['?', '?'])[1]} at {result.get('fps')} fps, person {result.get('person_coverage', 0) * 100:.1f} % "
             f"of the frame, body in {result.get('body_frames')}/{n}, face in {result.get('face_frames')}/{n}, "
             f"face detail in {result.get('face_detail_frames')}/{n}"]
    classes = result.get("classes") or {}
    if classes:
        lines.append("  classes (mean % of the frame): " + ", ".join(f"{k} {v:.2f}" for k, v in classes.items()))
    things = result.get("things") or {}
    if things:
        lines.append("  things: " + "; ".join(
            f"{k} {v['frames']}/{n} frames ({v['rate'] * 100:.0f} %" + (f", on the person {v['frames_anywhere']}" if
                                                                           v["frames_anywhere"] != v["frames"] else "")
            + f", mean max prob {v['mean_max_prob']:.2f})" for k, v in things.items()))
    gz = result.get("gaze")
    if gz:
        lines.append("  gaze: " + ", ".join(f"{k} {v}" for k, v in sorted(gz["directions"].items(), key=lambda kv: -kv[1]))
                     + f"; blinks {gz['blink_frames']} frames; mean openness {gz['mean_openness']:.2f} "
                     f"(EAR median {gz['ear_median']})")
    files = result.get("files") or {}
    lines.append("  files: " + ", ".join(Path(v).name for v in files.values() if v))
    t = result.get("timings") or {}
    lines.append("  timings (s): " + ", ".join(f"{k} {v}" for k, v in t.items()))
    for wmsg in result.get("warnings") or []:
        lines.append(f"  warning: {wmsg}")
    return lines
