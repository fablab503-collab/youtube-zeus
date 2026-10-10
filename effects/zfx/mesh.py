"""Face mesh: MediaPipe Face Landmarker (tasks API, VIDEO mode, 478 points) on work.mp4.

face-landmarks.json:
  {"version": 1, "model": "face_landmarker.task", "fps": 16, "width": W, "height": H,
   "points": 478, "faces_found": n, "frames": [ [[x,y,z] x 478] | null, ... ]}
x, y normalized to [0,1] of the frame, z relative depth (MediaPipe scale).

The tesselation mesh is drawn in white lines on top of control (depth_mesh) or the
original work frames (mesh).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import WORK_FPS, face_landmarker_model
from .io import iter_frames, probe
from .jobs import atomic_write_text
from .progress import get_logger

log = get_logger()

NUM_POINTS = 478


class MeshError(RuntimeError):
    pass


def tesselation() -> np.ndarray:
    """(K, 2) int array of landmark index pairs for the face tesselation."""
    from mediapipe.tasks.python.vision.face_landmarker import FaceLandmarksConnections as C
    conns = getattr(C, "FACE_LANDMARKS_TESSELATION", None) or getattr(C, "FACEMESH_TESSELATION", None)
    if conns is None:  # very old mediapipe
        from mediapipe.solutions.face_mesh_connections import FACEMESH_TESSELATION  # type: ignore
        conns = list(FACEMESH_TESSELATION)
    pairs = [(c.start, c.end) if hasattr(c, "start") else tuple(c) for c in conns]
    return np.array(pairs, dtype=np.int32)


CROP_SIDE = 512  # pixels fed to the landmarker for the head crop


def head_crop(mask_path: Optional[Path], width: int, height: int) -> Optional[tuple[int, int, int, int]]:
    """One fixed square crop (x0, y0, side_w, side_h) around the main person's head, from the people masks.

    MediaPipe's face detector runs at 128x128 and misses small faces (a presenter in a corner),
    so the landmarker gets an upscaled crop of the head region. One crop for the whole clip keeps
    VIDEO-mode tracking stable. None when there is no mask / no person.
    """
    if mask_path is None or not Path(mask_path).is_file():
        return None
    import cv2
    boxes = []
    for m in iter_frames(mask_path, gray=True):
        count, labels, stats, _ = cv2.connectedComponentsWithStats((m > 127).astype(np.uint8), connectivity=8)
        if count < 2:
            continue
        main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))  # the biggest person
        if stats[main, cv2.CC_STAT_AREA] < 50:
            continue
        ys, xs = np.nonzero(labels == main)
        top, bottom = int(ys.min()), int(ys.max())
        person_h = bottom - top + 1
        band = ys <= top + max(8, int(0.12 * person_h))      # the top of the person: the head, not the shoulders
        x0, x1 = int(xs[band].min()), int(xs[band].max())
        head_w = max(x1 - x0 + 1, 16)
        boxes.append((x0, top, x1, min(bottom, top + int(1.4 * head_w))))
    if not boxes:
        return None
    arr = np.array(boxes)
    x0, y0 = arr[:, 0].min(), arr[:, 1].min()
    x1, y1 = arr[:, 2].max(), arr[:, 3].max()
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    side = max(x1 - x0, y1 - y0) * 1.5
    side = int(min(max(side, 96), min(width, height)))
    left = int(round(min(max(cx - side / 2.0, 0), width - side)))
    top = int(round(min(max(cy - side / 2.0, 0), height - side)))
    return left, top, side, side


def _run_landmarker(work_path: Path, crop: Optional[tuple[int, int, int, int]], total: int,
                    report: Callable[[float, str], None], num_faces: int, label: str) -> tuple[list, int]:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    options = vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(face_landmarker_model())),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=num_faces,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    frames: list = []
    found = 0
    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        for i, frame in enumerate(iter_frames(work_path)):
            h, w = frame.shape[:2]
            if crop:
                x0, y0, cw, ch = crop
                view = cv2.resize(frame[y0:y0 + ch, x0:x0 + cw], (CROP_SIDE, CROP_SIDE),
                                  interpolation=cv2.INTER_CUBIC)
            else:
                x0, y0, cw, ch = 0, 0, w, h
                view = frame
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(view))
            result = landmarker.detect_for_video(image, int(round(i * 1000.0 / WORK_FPS)))
            faces = getattr(result, "face_landmarks", None) or []
            if faces:
                found += 1
                # back to normalized coordinates of the full frame; z keeps the x scale
                frames.append([[round((x0 + float(p.x) * cw) / w, 5), round((y0 + float(p.y) * ch) / h, 5),
                                round(float(p.z) * cw / w, 5)] for p in faces[0]])
            else:
                frames.append(None)
            if total:
                report((i + 1) / total, f"{label}: frame {i + 1}/{total}, face in {found}")
    return frames, found


def detect_landmarks(work_path: Path, report: Callable[[float, str], None], num_faces: int = 1,
                     mask_path: Optional[Path] = None) -> dict:
    """Face Landmarker over every frame of work.mp4 -> landmarks dict (see module doc).

    Tries the full frame and, when people masks are available, an upscaled head crop;
    keeps whichever finds the face in more frames.
    """
    model = face_landmarker_model()
    if not model.is_file():
        raise MeshError(f"MediaPipe face_landmarker.task is missing ({model}). Run: bash effects/setup.sh")
    info = probe(work_path)
    total = info.get("frames") or 0
    w, h = info["width"], info["height"]
    report(0.0, "MediaPipe Face Landmarker (478 points)")
    crop = head_crop(mask_path, w, h)
    if crop:
        frames, found = _run_landmarker(work_path, crop, total, lambda f, m: report(0.5 * f, m), num_faces,
                                        "head crop")
        used = "head_crop"
        if found < (total or len(frames)):
            full, full_found = _run_landmarker(work_path, None, total, lambda f, m: report(0.5 + 0.5 * f, m),
                                               num_faces, "full frame")
            if full_found > found:
                frames, found, used = full, full_found, "full_frame"
    else:
        frames, found = _run_landmarker(work_path, None, total, report, num_faces, "full frame")
        used = "full_frame"
    return {
        "version": 1,
        "model": model.name,
        "fps": WORK_FPS,
        "width": w,
        "height": h,
        "points": NUM_POINTS,
        "faces_found": found,
        "search": used,
        "crop": list(crop) if crop and used == "head_crop" else None,
        "frames": frames,
    }


def write_landmarks(data: dict, path: Path) -> Path:
    # One frame per line keeps the file readable and diff-friendly.
    head = {k: v for k, v in data.items() if k != "frames"}
    lines = ",\n".join("  " + json.dumps(f, separators=(",", ":")) for f in data["frames"])
    text = json.dumps(head, indent=2)[:-2] + ',\n  "frames": [\n' + lines + "\n  ]\n}\n"
    json.loads(text)  # sanity
    atomic_write_text(path, text)
    return path


def load_landmarks(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def draw_mesh(frame: np.ndarray, points: Optional[Sequence[Sequence[float]]], edges: Optional[np.ndarray] = None,
              color=(255, 255, 255), thickness: int = 1) -> np.ndarray:
    """Draw the tesselation (white lines) on a copy of an RGB frame. points: normalized [x,y,(z)]."""
    import cv2
    out = np.ascontiguousarray(frame).copy()
    if not points:
        return out
    h, w = out.shape[:2]
    pts = np.asarray(points, dtype=np.float32)[:, :2] * np.array([w, h], dtype=np.float32)
    edges = tesselation() if edges is None else edges
    edges = edges[(edges[:, 0] < len(pts)) & (edges[:, 1] < len(pts))]
    segs = np.round(pts[edges]).astype(np.int32)  # (K, 2, 2)
    cv2.polylines(out, list(segs.reshape(-1, 2, 1, 2)), False, color, thickness, cv2.LINE_AA)
    return out
