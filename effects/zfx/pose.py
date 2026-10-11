"""Full-body motion capture on work.mp4: MediaPipe Pose Landmarker (heavy) + Hand Landmarker + the face mesh.

Every frame of work.mp4 (16 fps) gets the person's whole movement, saved as data assets in the job folder:

  motion.json  the documented motion asset (format "zeus-motion", version 1, see MOTION_SCHEMA below):
               33 body landmarks (normalized image x,y,z + visibility) and the same 33 as world landmarks
               (metres, origin between the hips), 21 points per hand (left/right), the 478-point face mesh
               when present, smoothed (One-Euro filter) and raw.
  motion.bvh   a standard BVH body skeleton solved from the world landmarks (zfx.bvh).
  pose.mp4     an OpenPose-style coloured skeleton on black (body limbs, hands, a dotted face outline), same
               frames and size as work.mp4: a pose control video for VACE.

Models (official MediaPipe .task files, Apache-2.0, from Google's model storage; setup.sh downloads them, and
ensure_model() fetches a missing one on first use): pose_landmarker_heavy.task (~30 MB), hand_landmarker.task
(~8 MB). Everything runs on this Mac (CPU, XNNPACK).

A small person in a big frame (a presenter in a corner) is too small for MediaPipe's person and palm detectors,
so, like the face mesh, the landmarkers first run on one fixed crop around the main person of mask.mp4 and fall
back to the full frame when that finds the body in fewer frames.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import (HAND_LANDMARKER_URL, POSE_LANDMARKER_URL, WORK_FPS, __version__, hand_landmarker_model,
               pose_landmarker_model)
from .io import VideoWriter, iter_frames, probe
from .jobs import atomic_write_text, now_iso
from .progress import get_logger

log = get_logger()

MOTION_FORMAT = "zeus-motion"
MOTION_VERSION = 1

# --------------------------------------------------------------------------- landmark names
BODY_NAMES = (
    "nose", "left_eye_inner", "left_eye", "left_eye_outer", "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right", "left_shoulder", "right_shoulder", "left_elbow",
    "right_elbow", "left_wrist", "right_wrist", "left_pinky", "right_pinky", "left_index", "right_index",
    "left_thumb", "right_thumb", "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle",
    "left_heel", "right_heel", "left_foot_index", "right_foot_index",
)
B = {name: i for i, name in enumerate(BODY_NAMES)}

HAND_NAMES = (
    "wrist", "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip",
    "index_finger_mcp", "index_finger_pip", "index_finger_dip", "index_finger_tip",
    "middle_finger_mcp", "middle_finger_pip", "middle_finger_dip", "middle_finger_tip",
    "ring_finger_mcp", "ring_finger_pip", "ring_finger_dip", "ring_finger_tip",
    "pinky_mcp", "pinky_pip", "pinky_dip", "pinky_tip",
)

# MediaPipe's own connections (for consumers that draw or rig the landmarks).
BODY_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8), (9, 10), (11, 12), (11, 13), (13, 15),
    (15, 17), (15, 19), (15, 21), (17, 19), (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28), (27, 29), (28, 30), (29, 31),
    (30, 32), (27, 31), (28, 32),
)
HAND_EDGES = (
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16), (0, 17), (17, 18), (18, 19), (19, 20),
)

# Body parts for the summary ("which limbs were tracked"): a part counts as tracked in a frame when every one of
# its landmarks has a visibility of at least VISIBLE.
LIMB_GROUPS = {
    "head": ("nose", "left_eye", "right_eye", "left_ear", "right_ear", "mouth_left", "mouth_right"),
    "torso": ("left_shoulder", "right_shoulder", "left_hip", "right_hip"),
    "left_arm": ("left_shoulder", "left_elbow", "left_wrist"),
    "right_arm": ("right_shoulder", "right_elbow", "right_wrist"),
    "left_hand": ("left_wrist", "left_pinky", "left_index", "left_thumb"),
    "right_hand": ("right_wrist", "right_pinky", "right_index", "right_thumb"),
    "left_leg": ("left_hip", "left_knee", "left_ankle"),
    "right_leg": ("right_hip", "right_knee", "right_ankle"),
    "left_foot": ("left_ankle", "left_heel", "left_foot_index"),
    "right_foot": ("right_ankle", "right_heel", "right_foot_index"),
}
VISIBLE = 0.5

# A common MediaPipe face mesh -> 68-point (iBUG/dlib, OpenPose face) subset: jaw 17, brows 5+5, nose 9,
# eyes 6+6, mouth 12+8. pose.mp4 draws these as white dots, like OpenPose/DWPose face keypoints.
FACE68 = (
    162, 234, 93, 58, 172, 136, 149, 148, 152, 377, 378, 365, 397, 288, 323, 454, 389,
    71, 63, 105, 66, 107, 336, 296, 334, 293, 301,
    168, 197, 5, 4, 75, 97, 2, 326, 305,
    33, 160, 158, 133, 153, 144, 362, 385, 387, 263, 373, 380,
    61, 39, 37, 0, 267, 269, 291, 405, 314, 17, 84, 181, 78, 82, 13, 312, 308, 317, 14, 87,
)

# --------------------------------------------------------------------------- smoothing
# One-Euro filter (Casiez, Roussel, Vogel, CHI 2012): a low-pass filter whose cutoff rises with the speed, so
# still poses lose their jitter and fast moves keep little lag. Units: cutoffs in Hz, beta per (unit/second).
SMOOTHING = {
    "method": "one_euro",
    "image": {"min_cutoff": 1.2, "beta": 4.0, "d_cutoff": 1.0},    # normalized image coordinates (x, y, z)
    "world": {"min_cutoff": 1.2, "beta": 1.5, "d_cutoff": 1.0},    # metres (body and hand world landmarks)
    "applies_to": "x, y, z of every landmark; visibility and scores stay raw",
    "reset": "after every frame without a detection (no smoothing across gaps)",
    "raw_kept": True,
}


class OneEuroFilter:
    """Vectorised One-Euro filter: every element of the array is its own signal."""

    def __init__(self, fps: float, min_cutoff: float = 1.0, beta: float = 0.0, d_cutoff: float = 1.0):
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.dt = 1.0 / float(fps)
        self.min_cutoff, self.beta, self.d_cutoff = float(min_cutoff), float(beta), float(d_cutoff)
        self.reset()

    def reset(self) -> None:
        self.x_prev: Optional[np.ndarray] = None
        self.dx_prev: Optional[np.ndarray] = None

    def _alpha(self, cutoff):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / self.dt)

    def __call__(self, x) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if self.x_prev is None or self.x_prev.shape != x.shape:
            self.x_prev, self.dx_prev = x.copy(), np.zeros_like(x)
            return x.copy()
        dx = (x - self.x_prev) / self.dt
        a_d = self._alpha(self.d_cutoff)
        dx_hat = a_d * dx + (1.0 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * np.abs(dx_hat)
        a = self._alpha(cutoff)
        x_hat = a * x + (1.0 - a) * self.x_prev
        self.x_prev, self.dx_prev = x_hat, dx_hat
        return x_hat


def smooth_track(frames: Sequence, fps: float, params: dict, dims: int = 3) -> list:
    """Smooth the first `dims` values of every point of a per-frame track (None = not detected).
    Other columns (visibility) are copied. Restarts after every gap. Returns new arrays (float64) or None."""
    filt = OneEuroFilter(fps, params["min_cutoff"], params["beta"], params["d_cutoff"])
    out: list = []
    for frame in frames:
        if frame is None:
            filt.reset()
            out.append(None)
            continue
        arr = np.array(frame, dtype=np.float64)
        arr[:, :dims] = filt(arr[:, :dims])
        out.append(arr)
    return out


# --------------------------------------------------------------------------- models
class PoseError(RuntimeError):
    pass


def ensure_model(path: Path, url: str, min_bytes: int = 1_000_000,
                 report: Optional[Callable[[float, str], None]] = None) -> Path:
    """The model file, downloaded once (atomically, via .part) from Google's official MediaPipe storage when
    missing. ZEUS_EFFECTS_NO_DOWNLOAD=1 forbids the download (tests, offline use)."""
    path = Path(path)
    if path.is_file() and path.stat().st_size >= min_bytes:
        return path
    if os.environ.get("ZEUS_EFFECTS_NO_DOWNLOAD", "").strip() in ("1", "true", "yes"):
        raise PoseError(f"MediaPipe {path.name} is missing ({path}). Run: bash effects/setup.sh")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    if report:
        report(0.0, f"downloading {path.name} (first use)")
    log.info("downloading %s from %s", path.name, url)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        if part.stat().st_size < min_bytes:
            raise PoseError(f"downloaded {path.name} is too small ({part.stat().st_size} bytes)")
        os.replace(part, path)
    except PoseError:
        raise
    except Exception as exc:
        try:
            part.unlink()
        except OSError:
            pass
        raise PoseError(f"Could not download MediaPipe {path.name} ({exc}). Run: bash effects/setup.sh") from exc
    return path


def ensure_models(report: Optional[Callable[[float, str], None]] = None) -> tuple[Path, Path]:
    return (ensure_model(pose_landmarker_model(), POSE_LANDMARKER_URL, 10_000_000, report),
            ensure_model(hand_landmarker_model(), HAND_LANDMARKER_URL, 2_000_000, report))


# --------------------------------------------------------------------------- detection
def person_crop(mask_path: Optional[Path], width: int, height: int,
                margin: float = 0.2) -> Optional[tuple[int, int, int, int]]:
    """One fixed crop (x0, y0, w, h) around the main person of every mask frame (union of boxes + margin).
    None without a mask/person, or when the crop would cover most of the frame anyway."""
    if mask_path is None or not Path(mask_path).is_file():
        return None
    import cv2
    boxes = []
    for m in iter_frames(mask_path, gray=True):
        count, _labels, stats, _ = cv2.connectedComponentsWithStats((m > 127).astype(np.uint8), connectivity=8)
        if count < 2:
            continue
        main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[main, cv2.CC_STAT_AREA] < 50:
            continue
        x, y, w, h = (int(stats[main, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH,
                                                     cv2.CC_STAT_HEIGHT))
        boxes.append((x, y, x + w, y + h))
    if not boxes:
        return None
    arr = np.array(boxes)
    x0, y0, x1, y1 = arr[:, 0].min(), arr[:, 1].min(), arr[:, 2].max(), arr[:, 3].max()
    mx, my = margin * (x1 - x0), margin * (y1 - y0)
    x0, y0 = int(max(0, x0 - mx)), int(max(0, y0 - my))
    x1, y1 = int(min(width, x1 + mx)), int(min(height, y1 + my))
    if (x1 - x0) < 32 or (y1 - y0) < 32:
        return None
    if (x1 - x0) * (y1 - y0) > 0.7 * width * height:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _to_full(points, crop, w: int, h: int, with_vis: bool) -> list:
    """Landmarks of a crop -> normalized coordinates of the full frame; z keeps the x scale (like the mesh)."""
    x0, y0, cw, ch = crop
    out = []
    for p in points:
        row = [(x0 + float(p.x) * cw) / w, (y0 + float(p.y) * ch) / h, float(p.z) * cw / w]
        if with_vis:
            row.append(float(getattr(p, "visibility", None) or 0.0))
        out.append(row)
    return out


def _world(points, with_vis: bool) -> list:
    out = []
    for p in points:
        row = [float(p.x), float(p.y), float(p.z)]
        if with_vis:
            row.append(float(getattr(p, "visibility", None) or 0.0))
        out.append(row)
    return out


def _views(work_path: Path, crop: Optional[tuple[int, int, int, int]]):
    """(frame index, mp.Image of the crop or full frame, crop rect, w, h) for every frame."""
    import mediapipe as mp
    for i, frame in enumerate(iter_frames(work_path)):
        h, w = frame.shape[:2]
        rect = crop or (0, 0, w, h)
        x0, y0, cw, ch = rect
        view = np.ascontiguousarray(frame[y0:y0 + ch, x0:x0 + cw])
        yield i, mp.Image(image_format=mp.ImageFormat.SRGB, data=view), rect, w, h


def run_pose(work_path: Path, crop, total: int, report: Callable[[float, str], None],
             label: str = "body") -> tuple[list, list, int]:
    """Pose Landmarker (VIDEO mode) -> (image frames, world frames, frames with a body)."""
    from mediapipe.tasks.python import BaseOptions, vision
    options = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(pose_landmarker_model())),
        running_mode=vision.RunningMode.VIDEO,
        num_poses=1,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    image_frames: list = []
    world_frames: list = []
    found = 0
    with vision.PoseLandmarker.create_from_options(options) as landmarker:
        for i, image, rect, w, h in _views(work_path, crop):
            result = landmarker.detect_for_video(image, int(round(i * 1000.0 / WORK_FPS)))
            poses = getattr(result, "pose_landmarks", None) or []
            worlds = getattr(result, "pose_world_landmarks", None) or []
            if poses and worlds:
                found += 1
                image_frames.append(_to_full(poses[0], rect, w, h, True))
                world_frames.append(_world(worlds[0], True))
            else:
                image_frames.append(None)
                world_frames.append(None)
            if total and (i % 4 == 3 or i + 1 == total):
                report((i + 1) / total, f"{label}: frame {i + 1}/{total}, body in {found}")
    return image_frames, world_frames, found


def run_hands(work_path: Path, crop, total: int, report: Callable[[float, str], None]) -> list:
    """Hand Landmarker (VIDEO mode, 2 hands) -> per frame a list of
    {"label": MediaPipe handedness, "score", "image": 21x3, "world": 21x3}."""
    from mediapipe.tasks.python import BaseOptions, vision
    options = vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(hand_landmarker_model())),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=2,
        min_hand_detection_confidence=0.4,
        min_hand_presence_confidence=0.4,
        min_tracking_confidence=0.4,
    )
    frames: list = []
    seen = 0
    with vision.HandLandmarker.create_from_options(options) as landmarker:
        for i, image, rect, w, h in _views(work_path, crop):
            result = landmarker.detect_for_video(image, int(round(i * 1000.0 / WORK_FPS)))
            hands = []
            lms = getattr(result, "hand_landmarks", None) or []
            worlds = getattr(result, "hand_world_landmarks", None) or []
            sides = getattr(result, "handedness", None) or []
            for k, lm in enumerate(lms):
                cat = sides[k][0] if k < len(sides) and sides[k] else None
                hands.append({
                    "label": (getattr(cat, "category_name", None) or "").lower() or None,
                    "score": float(getattr(cat, "score", 0.0) or 0.0),
                    "image": _to_full(lm, rect, w, h, False),
                    "world": _world(worlds[k], False) if k < len(worlds) else None,
                })
            seen += 1 if hands else 0
            frames.append(hands)
            if total and (i % 4 == 3 or i + 1 == total):
                report((i + 1) / total, f"hands: frame {i + 1}/{total}, hands in {seen}")
    return frames


def _aspect_dist(a, b, w: int, h: int) -> float:
    return math.hypot((a[0] - b[0]) * w, (a[1] - b[1]) * h)


def assign_hands(hand_frames: Sequence, body_frames: Sequence, w: int, h: int) -> dict:
    """Detected hands -> the person's own left/right hand.

    With a body in the frame, each hand goes to the nearer body wrist (the person's own left/right, the best of
    the two pairings); a hand farther than 1.2x the shoulder width from that wrist is someone else's and dropped.
    Without a body, MediaPipe's handedness is used swapped: it assumes a mirrored selfie image, work.mp4 is not.
    Returns {"left": [hand|None per frame], "right": [...]}."""
    out = {"left": [], "right": []}
    for i, hands in enumerate(hand_frames):
        body = body_frames[i] if i < len(body_frames) else None
        pick = {"left": None, "right": None}
        hands = list(hands or [])
        if body is not None and hands:
            lw, rw = body[B["left_wrist"]], body[B["right_wrist"]]
            sw = max(_aspect_dist(body[B["left_shoulder"]], body[B["right_shoulder"]], w, h), 8.0)
            limit = 1.2 * sw
            if len(hands) == 1:
                d_l = _aspect_dist(hands[0]["image"][0], lw, w, h)
                d_r = _aspect_dist(hands[0]["image"][0], rw, w, h)
                side, d = ("left", d_l) if d_l <= d_r else ("right", d_r)
                if d <= limit:
                    pick[side] = hands[0]
            else:
                a, b = hands[0], hands[1]
                straight = _aspect_dist(a["image"][0], lw, w, h) + _aspect_dist(b["image"][0], rw, w, h)
                crossed = _aspect_dist(a["image"][0], rw, w, h) + _aspect_dist(b["image"][0], lw, w, h)
                left, right = (a, b) if straight <= crossed else (b, a)
                if _aspect_dist(left["image"][0], lw, w, h) <= limit:
                    pick["left"] = left
                if _aspect_dist(right["image"][0], rw, w, h) <= limit:
                    pick["right"] = right
        elif hands:
            for hand in sorted(hands, key=lambda x: -x["score"]):
                side = {"left": "right", "right": "left"}.get(hand["label"] or "")
                if side and pick[side] is None:
                    pick[side] = hand
        for side in ("left", "right"):
            out[side].append(pick[side])
    return out


# --------------------------------------------------------------------------- the motion asset
def _round_track(frames: Sequence, decimals: Sequence[int]) -> list:
    out = []
    for frame in frames:
        if frame is None:
            out.append(None)
            continue
        out.append([[round(float(v), decimals[min(k, len(decimals) - 1)]) for k, v in enumerate(p)]
                    for p in frame])
    return out


IMG_DEC = (5, 5, 5, 3)     # x, y, z, visibility
WORLD_DEC = (4, 4, 4, 3)   # metres (0.1 mm), visibility


def _visibility_stats(body: Sequence) -> dict:
    frames = [np.asarray(f) for f in body if f is not None]
    if not frames:
        return {"mean": None, "per_landmark": None, "limbs": {k: 0.0 for k in LIMB_GROUPS}}
    vis = np.stack([f[:, 3] for f in frames])                  # (F, 33)
    limbs = {}
    for name, members in LIMB_GROUPS.items():
        idx = [B[m] for m in members]
        limbs[name] = round(float((vis[:, idx].min(axis=1) >= VISIBLE).mean()), 3)
    return {"mean": round(float(vis.mean()), 3),
            "per_landmark": {BODY_NAMES[k]: round(float(vis[:, k].mean()), 3) for k in range(len(BODY_NAMES))},
            "limbs": limbs}


def build_motion(width: int, height: int, fps: float, body_image: Sequence, body_world: Sequence,
                 hands: Optional[dict] = None, face: Optional[dict] = None, source: Optional[dict] = None,
                 search: Optional[dict] = None, models: Optional[dict] = None,
                 smoothing: Optional[dict] = None) -> dict:
    """Raw detections -> the motion.json dict (smoothing applied here; raw values kept)."""
    smoothing = smoothing or SMOOTHING
    n = len(body_image)
    hands = hands or {"left": [None] * n, "right": [None] * n}

    def hand_track(side: str, key: str):
        return [None if hnd is None or hnd.get(key) is None else hnd[key] for hnd in hands[side]]

    hands_out = {}
    for side in ("left", "right"):
        img_raw, world_raw = hand_track(side, "image"), hand_track(side, "world")
        hands_out[side] = {
            "image": _round_track(smooth_track(img_raw, fps, smoothing["image"]), IMG_DEC),
            "world": _round_track(smooth_track(world_raw, fps, smoothing["world"]), WORLD_DEC),
            "image_raw": _round_track(img_raw, IMG_DEC),
            "world_raw": _round_track(world_raw, WORLD_DEC),
            "score": [None if hnd is None else round(float(hnd["score"]), 3) for hnd in hands[side]],
            "frames_found": sum(1 for hnd in hands[side] if hnd is not None),
        }

    face_out = None
    face_frames = (face or {}).get("frames")
    if face_frames:
        face_frames = list(face_frames)[:n] + [None] * max(0, n - len(face_frames))
        face_out = {
            "points": int((face or {}).get("points") or 478),
            "model": (face or {}).get("model"),
            "file": "face-landmarks.json",
            "image": _round_track(smooth_track(face_frames, fps, smoothing["image"]), IMG_DEC),
            "image_raw": _round_track(face_frames, IMG_DEC),
            "frames_found": sum(1 for f in face_frames if f is not None),
            "openpose68": list(FACE68),
        }

    body_found = sum(1 for f in body_image if f is not None)
    stats = {
        "frames": n,
        "body_frames": body_found,
        "left_hand_frames": hands_out["left"]["frames_found"],
        "right_hand_frames": hands_out["right"]["frames_found"],
        "face_frames": face_out["frames_found"] if face_out else 0,
        "visibility": _visibility_stats(body_image),
    }
    return {
        "format": MOTION_FORMAT,
        "version": MOTION_VERSION,
        "generator": f"YouTube Zeus Effects {__version__} (zfx.pose)",
        "created": now_iso(),
        "source": source or {},
        "fps": fps,
        "frames": n,
        "width": int(width),
        "height": int(height),
        "time": [round(i / float(fps), 4) for i in range(n)],
        "coordinates": COORDINATES,
        "models": models or {},
        "search": search or {},
        "smoothing": smoothing,
        "names": {"body": list(BODY_NAMES), "hand": list(HAND_NAMES),
                  "face": "478 MediaPipe face mesh points (index = position); openpose68 lists a 68-point subset"},
        "edges": {"body": [list(e) for e in BODY_EDGES], "hand": [list(e) for e in HAND_EDGES]},
        "limb_groups": {k: list(v) for k, v in LIMB_GROUPS.items()},
        "body": {
            "image": _round_track(smooth_track(body_image, fps, smoothing["image"]), IMG_DEC),
            "world": _round_track(smooth_track(body_world, fps, smoothing["world"]), WORLD_DEC),
            "image_raw": _round_track(body_image, IMG_DEC),
            "world_raw": _round_track(body_world, WORLD_DEC),
        },
        "hands": hands_out,
        "face": face_out,
        "stats": stats,
    }


COORDINATES = {
    "image": "x, y normalized to [0, 1] of the frame (x right, y down); z = MediaPipe relative depth in the x "
             "scale (smaller = nearer the camera; body: origin at the hips, hand: at the wrist, face: at the "
             "head centre); 4th value of body points = visibility 0..1",
    "world": "metres, camera-aligned axes (x right, y down, z away from the camera); body: origin at the centre "
             "between the hips; hands: origin at the hand's centre; 4th value of body points = visibility 0..1",
    "frame_index": "time = index / fps; index i is frame i of work.mp4 and pose.mp4",
}

MOTION_SCHEMA = """motion.json (format "zeus-motion", version 1)
  format, version, generator, created
  source        {job, input, start, duration, video: "work.mp4", source_fps}
  fps, frames, width, height      work.mp4 timing and size (16 fps)
  time          [seconds per frame]
  coordinates   what image/world values mean (see COORDINATES)
  models        {pose, hands, face} .task file names
  search        {pose: "person_crop"|"full_frame", crop: [x, y, w, h]|null}
  smoothing     One-Euro parameters (image, world), applies_to, reset, raw_kept
  names         {body: 33 names, hand: 21 names, face: text}
  edges         {body: MediaPipe pairs, hand: pairs}
  limb_groups   {part: [body landmark names]}
  body          {image, world, image_raw, world_raw}: per frame null or 33 x [x, y, z, visibility]
  hands         {left|right: {image, world, image_raw, world_raw: per frame null or 21 x [x, y, z],
                 score: per frame null or 0..1, frames_found}}  (the person's own left/right)
  face          null or {points: 478, model, file, image, image_raw: per frame null or 478 x [x, y, z],
                 frames_found, openpose68: [68 indices]}
  stats         {frames, body_frames, left_hand_frames, right_hand_frames, face_frames,
                 visibility: {mean, per_landmark{name: mean}, limbs{part: fraction of body frames tracked}}}
"""


def _dump(obj, indent: int = 0) -> str:
    """JSON with dicts indented and every per-frame list written one frame per line (readable, diff-friendly)."""
    pad, pad2 = "  " * indent, "  " * (indent + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [f"{pad2}{json.dumps(k)}: {_dump(v, indent + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(obj, list) and len(obj) > 1 and any(isinstance(x, list) for x in obj) \
            and all(x is None or isinstance(x, list) for x in obj) \
            and any(isinstance(x, list) and x and isinstance(x[0], list) for x in obj):
        rows = [pad2 + json.dumps(x, separators=(",", ":")) for x in obj]
        return "[\n" + ",\n".join(rows) + "\n" + pad + "]"
    return json.dumps(obj, separators=(", ", ": ") if not isinstance(obj, list) else (",", ":"),
                      ensure_ascii=False)


def write_motion(data: dict, path: Path) -> Path:
    text = _dump(data) + "\n"
    json.loads(text)  # sanity
    atomic_write_text(path, text)
    return Path(path)


def load_motion(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != MOTION_FORMAT or int(data.get("version") or 0) != MOTION_VERSION:
        raise PoseError(f"{Path(path).name} is not a zeus-motion version {MOTION_VERSION} file.")
    return data


def as_array(frame) -> Optional[np.ndarray]:
    return None if frame is None else np.asarray(frame, dtype=np.float64)


def detect_motion(work_path: Path, report: Callable[[float, str], None], mask_path: Optional[Path] = None,
                  face: Optional[dict] = None, source: Optional[dict] = None) -> dict:
    """Body + hands (+ the face mesh dict of zfx.mesh when given) on every frame of work.mp4 -> motion dict."""
    t0 = time.monotonic()
    pose_model, hand_model = ensure_models(lambda f, m: report(0.0, m))
    info = probe(work_path)
    total = info.get("frames") or 0
    w, h = info["width"], info["height"]
    report(0.0, "MediaPipe Pose Landmarker (heavy, 33 points) + Hand Landmarker (21 points per hand)")
    crop = person_crop(mask_path, w, h)
    timings = {}
    if crop:
        img, world, found = run_pose(work_path, crop, total, lambda f, m: report(0.4 * f, m), "body (person crop)")
        used = "person_crop"
        if found < (total or len(img)):
            img2, world2, found2 = run_pose(work_path, None, total, lambda f, m: report(0.4 + 0.2 * f, m),
                                            "body (full frame)")
            if found2 > found:
                img, world, found, used = img2, world2, found2, "full_frame"
    else:
        img, world, found = run_pose(work_path, None, total, lambda f, m: report(0.6 * f, m), "body")
        used = "full_frame"
    timings["body"] = round(time.monotonic() - t0, 2)
    t1 = time.monotonic()
    hand_crop = crop if used == "person_crop" else None
    raw_hands = run_hands(work_path, hand_crop, total, lambda f, m: report(0.6 + 0.35 * f, m))
    hands = assign_hands(raw_hands, img, w, h)
    timings["hands"] = round(time.monotonic() - t1, 2)
    motion = build_motion(
        w, h, WORK_FPS, img, world, hands, face, source,
        search={"pose": used, "crop": list(crop) if crop and used == "person_crop" else None,
                "hands": "person_crop" if hand_crop else "full_frame"},
        models={"pose": pose_model.name, "hands": hand_model.name,
                "face": (face or {}).get("model") if face and face.get("frames") else None})
    motion["timings"] = timings
    report(1.0, f"body in {found}/{len(img)} frames, hands L {motion['stats']['left_hand_frames']} "
                f"R {motion['stats']['right_hand_frames']}")
    return motion


# --------------------------------------------------------------------------- drawing (OpenPose / DWPose style)
# OpenPose COCO-18 keypoints from MediaPipe: 0 nose, 1 neck (mid shoulders), 2-4 right arm, 5-7 left arm,
# 8-10 right leg, 11-13 left leg, 14 right eye, 15 left eye, 16 right ear, 17 left ear.
OPENPOSE18 = (B["nose"], None, B["right_shoulder"], B["right_elbow"], B["right_wrist"], B["left_shoulder"],
              B["left_elbow"], B["left_wrist"], B["right_hip"], B["right_knee"], B["right_ankle"], B["left_hip"],
              B["left_knee"], B["left_ankle"], B["right_eye"], B["left_eye"], B["right_ear"], B["left_ear"])
OPENPOSE_LIMBS = ((1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10), (1, 11), (11, 12),
                  (12, 13), (1, 0), (0, 14), (14, 16), (0, 15), (15, 17))
OPENPOSE_COLORS = ((255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0),
                   (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
                   (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85))
# Feet (BODY_25 adds them): ankle -> toes and ankle -> heel, in the colour of that leg's lower limb.
FEET = ((B["right_ankle"], B["right_foot_index"], 9), (B["right_ankle"], B["right_heel"], 9),
        (B["left_ankle"], B["left_foot_index"], 12), (B["left_ankle"], B["left_heel"], 12))


def _hsv_color(k: int, n: int) -> tuple[int, int, int]:
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(k / float(n), 1.0, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


HAND_COLORS = tuple(_hsv_color(k, len(HAND_EDGES)) for k in range(len(HAND_EDGES)))


def openpose18(body, vis: float = VISIBLE) -> list:
    """33 MediaPipe points -> 18 OpenPose points [(x, y) normalized] or None where not visible."""
    out = []
    for idx in OPENPOSE18:
        if idx is None:   # neck = middle of the shoulders
            ls, rs = body[B["left_shoulder"]], body[B["right_shoulder"]]
            ok = min(ls[3], rs[3]) >= vis
            out.append(((ls[0] + rs[0]) / 2.0, (ls[1] + rs[1]) / 2.0) if ok else None)
        else:
            p = body[idx]
            out.append((p[0], p[1]) if p[3] >= vis else None)
    return out


def draw_pose(height: int, width: int, body=None, left_hand=None, right_hand=None, face=None,
              canvas: Optional[np.ndarray] = None, vis: float = VISIBLE) -> np.ndarray:
    """OpenPose/DWPose-style skeleton (RGB uint8, black background unless `canvas` is given).

    Body: COCO-18 limbs as filled ellipses in the OpenPose colours at 60 %, joints as full-colour dots, plus the
    feet; hands: 20 rainbow bones + blue dots; face: 68 white dots (from the face mesh, else the pose's eyes and
    mouth corners). Sizes follow DWPose (stick 4 px, dots 4 px) and shrink for small people."""
    import cv2
    out = np.zeros((height, width, 3), np.uint8) if canvas is None else np.ascontiguousarray(canvas).copy()
    layer = np.zeros_like(out)
    body = as_array(body)

    def px(p):
        return int(round(float(p[0]) * width)), int(round(float(p[1]) * height))

    scale = 1.0
    if body is not None:
        ls, rs = body[B["left_shoulder"]], body[B["right_shoulder"]]
        sw = math.hypot((ls[0] - rs[0]) * width, (ls[1] - rs[1]) * height)
        if min(ls[3], rs[3]) >= vis and sw > 1:
            scale = float(np.clip(sw / 90.0, 0.35, 1.0))
    stick = max(1, int(round(4 * scale)))
    dot = max(1, int(round(4 * scale)))

    if body is not None:
        kp = openpose18(body, vis)
        for k, (a, b) in enumerate(OPENPOSE_LIMBS):
            if kp[a] is None or kp[b] is None:
                continue
            (x1, y1), (x2, y2) = px(kp[a]), px(kp[b])
            length = math.hypot(x2 - x1, y2 - y1)
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            poly = cv2.ellipse2Poly(((x1 + x2) // 2, (y1 + y2) // 2), (max(1, int(length / 2)), stick),
                                    int(angle), 0, 360, 1)
            cv2.fillConvexPoly(layer, poly, OPENPOSE_COLORS[k])
        for a, b, color in FEET:
            if body[a][3] >= vis and body[b][3] >= vis:
                cv2.line(layer, px(body[a]), px(body[b]), OPENPOSE_COLORS[color], max(1, stick), cv2.LINE_AA)
        layer = (layer.astype(np.float32) * 0.6).astype(np.uint8)
        for k, p in enumerate(kp):
            if p is not None:
                cv2.circle(layer, px(p), dot, OPENPOSE_COLORS[k], -1, cv2.LINE_AA)

    for hand in (left_hand, right_hand):
        hand = as_array(hand)
        if hand is None or len(hand) < 21:
            continue
        pts = [px(p) for p in hand]
        span = max(math.hypot(pts[0][0] - pts[9][0], pts[0][1] - pts[9][1]), 1.0)  # wrist -> middle knuckle
        hdot = max(1, min(4, int(round(span / 16.0))))
        hline = max(1, min(2, int(round(span / 12.0))))
        for k, (a, b) in enumerate(HAND_EDGES):
            cv2.line(layer, pts[a], pts[b], HAND_COLORS[k], hline, cv2.LINE_AA)
        for p in pts:
            cv2.circle(layer, p, hdot, (0, 0, 255), -1, cv2.LINE_AA)

    face = as_array(face)
    if face is not None and len(face) >= 468:
        pts = [px(face[i]) for i in FACE68]
        span = max(math.hypot(pts[0][0] - pts[16][0], pts[0][1] - pts[16][1]), 1.0)   # jaw width
        fdot = max(1, min(3, int(round(span / 40.0))))
        for p in pts:
            cv2.circle(layer, p, fdot, (255, 255, 255), -1, cv2.LINE_AA)
    elif body is not None:
        for name in ("left_eye_inner", "left_eye_outer", "right_eye_inner", "right_eye_outer",
                     "mouth_left", "mouth_right"):
            p = body[B[name]]
            if p[3] >= vis:
                cv2.circle(layer, px(p), max(1, dot // 2), (255, 255, 255), -1, cv2.LINE_AA)

    if canvas is None:
        return layer
    drawn = layer.any(axis=2)
    out[drawn] = layer[drawn]
    return out


def frame_parts(motion: dict, i: int) -> dict:
    """Smoothed body/hands/face of frame i of a motion dict (None where not detected)."""
    def at(track):
        return track[i] if track is not None and i < len(track) else None
    face = motion.get("face") or {}
    return {"body": at(motion["body"]["image"]), "left_hand": at(motion["hands"]["left"]["image"]),
            "right_hand": at(motion["hands"]["right"]["image"]), "face": at(face.get("image"))}


def write_pose_video(motion: dict, out_path: Path, report: Callable[[float, str], None]) -> dict:
    """pose.mp4: the skeleton on black for every frame of the motion (same size/frames as work.mp4)."""
    w, h, n = int(motion["width"]), int(motion["height"]), int(motion["frames"])
    drawn = 0
    with VideoWriter(out_path, w, h, WORK_FPS, crf=14) as writer:
        for i in range(n):
            parts = frame_parts(motion, i)
            frame = draw_pose(h, w, **parts)
            drawn += 1 if parts["body"] is not None else 0
            writer.write(frame)
            if i % 8 == 7 or i + 1 == n:
                report((i + 1) / n, f"pose.mp4 frame {i + 1}/{n}")
    return {"frames": n, "frames_with_body": drawn}


# --------------------------------------------------------------------------- summary (CLI `motion`)
def summarize(motion: dict) -> dict:
    stats = motion.get("stats") or {}
    n = int(motion.get("frames") or 0)
    vis = stats.get("visibility") or {}
    limbs = vis.get("limbs") or {}

    def rate(k):
        return round(k / n, 3) if n else 0.0

    return {
        "format": motion.get("format"),
        "version": motion.get("version"),
        "frames": n,
        "fps": motion.get("fps"),
        "size": [motion.get("width"), motion.get("height")],
        "duration": round(n / float(motion.get("fps") or WORK_FPS), 3) if n else 0.0,
        "body_frames": stats.get("body_frames", 0),
        "body_rate": rate(stats.get("body_frames", 0)),
        "left_hand_frames": stats.get("left_hand_frames", 0),
        "right_hand_frames": stats.get("right_hand_frames", 0),
        "hand_rate": rate(max(stats.get("left_hand_frames", 0), stats.get("right_hand_frames", 0))),
        "face_frames": stats.get("face_frames", 0),
        "average_visibility": vis.get("mean"),
        "limbs_tracked": {k: v for k, v in limbs.items() if v >= 0.5},
        "limbs_fraction": limbs,
        "search": motion.get("search"),
        "models": motion.get("models"),
        "smoothing": {k: motion.get("smoothing", {}).get(k) for k in ("method", "image", "world")},
    }
