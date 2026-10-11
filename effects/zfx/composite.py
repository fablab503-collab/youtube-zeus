"""control.mp4 and preview.jpg.

control (mode depth)       colored depth inside the people mask over the original work.mp4
                           background, feathered edge
control (mode depth_mesh)  the same + white face mesh
control (mode mesh)        original work.mp4 + white face mesh
control (mode pose)        the OpenPose-style skeleton (body, hands, face dots) on black (= pose.mp4)
control (mode depth_pose)  grey depth inside the people mask (hard edge, like vace's gray control) over the
                           original background, the skeleton drawn on top inside the mask
preview.jpg                contact sheet: 4 frames x (work | depth | mask | pose | control); no depth column in
                           mode pose (no depth is computed), a pose column whenever motion was tracked
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import WORK_FPS
from .io import VideoWriter, iter_frames, probe
from .progress import get_logger

log = get_logger()

FEATHER_PX = 6
COLUMNS = ("work", "depth", "mask", "control")


def feather(mask: np.ndarray, feather_px: int = FEATHER_PX) -> np.ndarray:
    """uint8/float mask -> float32 alpha in [0,1] with a soft edge of ~feather_px."""
    import cv2
    m = np.asarray(mask)
    if m.ndim == 3:
        m = m[:, :, 0]
    alpha = m.astype(np.float32) / (255.0 if m.dtype == np.uint8 or m.max() > 1.0 else 1.0)
    if feather_px > 0:
        k = 2 * feather_px + 1
        alpha = cv2.GaussianBlur(alpha, (k, k), feather_px / 2.0)
    return np.clip(alpha, 0.0, 1.0)


def composite_frame(work: np.ndarray, depth: np.ndarray, mask: np.ndarray, feather_px: int = FEATHER_PX) -> np.ndarray:
    """Depth inside the mask, original outside, feathered. All HxW(x3) uint8."""
    if work.shape[:2] != depth.shape[:2] or work.shape[:2] != np.asarray(mask).shape[:2]:
        raise ValueError(f"size mismatch: work {work.shape}, depth {depth.shape}, mask {np.asarray(mask).shape}")
    alpha = feather(mask, feather_px)[:, :, None]
    out = depth.astype(np.float32) * alpha + work.astype(np.float32) * (1.0 - alpha)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def gray_depth_frame(work: np.ndarray, depth: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Depth luminance (near = bright) inside the binary mask, the original outside, hard edge.

    VACE was trained with grey depth; a feathered colour halo past the mask would be kept as a glow (see
    engines/vace.py gray_control, which does the same for the depth modes at generation time)."""
    if work.shape[:2] != depth.shape[:2] or work.shape[:2] != np.asarray(mask).shape[:2]:
        raise ValueError(f"size mismatch: work {work.shape}, depth {depth.shape}, mask {np.asarray(mask).shape}")
    luma = depth.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    m = np.asarray(mask)
    alpha = ((m[:, :, 0] if m.ndim == 3 else m) > 127).astype(np.float32)[:, :, None]
    out = work.astype(np.float32) * (1.0 - alpha) + luma[:, :, None] * alpha
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def overlay_inside(base: np.ndarray, layer: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Non-black pixels of `layer` replace `base` inside the mask (the skeleton stays on the person)."""
    m = np.asarray(mask)
    inside = (m[:, :, 0] if m.ndim == 3 else m) > 127
    drawn = layer.any(axis=2) & inside
    out = base.copy()
    out[drawn] = layer[drawn]
    return out


def control_frame(mode: str, work: np.ndarray, depth: Optional[np.ndarray], mask: Optional[np.ndarray],
                  points=None, edges=None, pose: Optional[np.ndarray] = None) -> np.ndarray:
    """pose: the skeleton frame on black (zfx.pose.draw_pose) for the pose modes."""
    if mode == "pose":
        return pose if pose is not None else np.zeros_like(work)
    if mode == "depth_pose":
        base = gray_depth_frame(work, depth, mask)
        return base if pose is None else overlay_inside(base, pose, mask)
    if mode == "mesh":
        base = work
    else:
        base = composite_frame(work, depth, mask)
    if mode in ("mesh", "depth_mesh"):
        from .mesh import draw_mesh
        base = draw_mesh(base, points, edges)
    return base


def sample_indices(total: int, count: int = 4) -> list[int]:
    if total <= 0:
        return []
    if total <= count:
        return list(range(total))
    return sorted({int(round(x)) for x in np.linspace(0, total - 1, count)})


def build_control(work_path: Path, depth_path: Optional[Path], mask_path: Path, out_path: Path, mode: str,
                  report: Callable[[float, str], None], landmarks: Optional[dict] = None,
                  preview_path: Optional[Path] = None, motion: Optional[dict] = None) -> dict:
    """Writes control.mp4 (and preview.jpg when preview_path is given).

    motion: the zfx.pose motion dict (pose modes, or any mode with --motion): its skeleton is drawn for the
    control (pose modes) and as the preview's pose column. depth_path may be None in mode pose."""
    info = probe(work_path)
    w, h = info["width"], info["height"]
    total = info.get("frames") or 0
    if not total:
        from .io import count_frames
        total = count_frames(work_path)
    picks = set(sample_indices(total))
    samples: list[list[np.ndarray]] = []
    frames_pts = (landmarks or {}).get("frames") or []
    edges = None
    if mode in ("mesh", "depth_mesh"):
        from .mesh import tesselation
        edges = tesselation()

    use_depth = mode != "pose"
    if use_depth and (depth_path is None or not Path(depth_path).is_file()):
        raise ValueError(f"mode {mode} needs depth.mp4")
    columns = ["work"] + (["depth"] if use_depth else []) + ["mask"]
    if motion is not None and mode != "pose":
        columns.append("pose")
    columns.append("control" if mode != "pose" else "control = pose")

    def no_depth():
        while True:
            yield None

    work_it = iter_frames(work_path)
    depth_it = iter_frames(depth_path, size=(w, h)) if use_depth else no_depth()
    mask_it = iter_frames(mask_path, size=(w, h), gray=True)
    count = 0
    with VideoWriter(out_path, w, h, WORK_FPS, crf=14) as writer:
        for i, (work, depth, mask) in enumerate(zip(work_it, depth_it, mask_it)):
            mask = np.where(mask > 127, 255, 0).astype(np.uint8)  # undo codec ringing
            points = frames_pts[i] if i < len(frames_pts) else None
            skeleton = None
            if motion is not None:
                from .pose import draw_pose, frame_parts
                skeleton = draw_pose(h, w, **frame_parts(motion, i)) if i < int(motion.get("frames") or 0) \
                    else np.zeros_like(work)
            control = control_frame(mode, work, depth, mask, points, edges, skeleton)
            writer.write(control)
            if i in picks:
                row = [work.copy()] + ([depth.copy()] if use_depth else []) + [mask.copy()]
                if motion is not None and mode != "pose":
                    row.append(skeleton)
                samples.append(row + [control])
            count += 1
            if total and (i % 4 == 3 or i + 1 == total):
                report((i + 1) / total, f"frame {i + 1}/{total}")
    if total and count != total:
        log.warning("control: %d frames written, work has %d (depth/mask shorter?)", count, total)
    result = {"frames": count, "mode": mode, "preview_columns": columns}
    if preview_path is not None and samples:
        write_preview(samples, preview_path, [sorted(picks)[k] for k in range(len(samples))], labels=columns)
        result["preview"] = str(preview_path)
    return result


def _label(img: np.ndarray, text: str, corner: str = "tl") -> None:
    import cv2
    font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    x = 6 if corner == "tl" else img.shape[1] - tw - 6
    y = 6
    cv2.rectangle(img, (x - 4, y - 2), (x + tw + 4, y + th + base + 2), (0, 0, 0), -1)
    cv2.putText(img, text, (x, y + th), font, scale, (255, 255, 255), thick, cv2.LINE_AA)


def contact_sheet(rows: Sequence[Sequence[np.ndarray]], labels: Sequence[str] = COLUMNS,
                  frame_numbers: Optional[Sequence[int]] = None, tile_width: int = 320, gap: int = 4) -> np.ndarray:
    """rows of RGB/gray tiles -> one RGB image (rows x columns), labels on the first row."""
    import cv2
    if not rows:
        raise ValueError("contact sheet needs at least one row")
    h0, w0 = rows[0][0].shape[:2]
    tile_h = max(2, int(round(h0 * tile_width / float(w0))))
    ncols = max(len(r) for r in rows)
    sheet = np.full((len(rows) * tile_h + (len(rows) + 1) * gap, ncols * tile_width + (ncols + 1) * gap, 3),
                    24, dtype=np.uint8)
    for r, row in enumerate(rows):
        for c, tile in enumerate(row):
            t = np.asarray(tile)
            if t.ndim == 2:
                t = np.repeat(t[:, :, None], 3, axis=2)
            t = cv2.resize(t, (tile_width, tile_h), interpolation=cv2.INTER_AREA)
            if r == 0 and c < len(labels):
                _label(t, labels[c])
            if c == 0 and frame_numbers is not None and r < len(frame_numbers):
                _label(t, f"#{frame_numbers[r]}", "tr")
            y, x = gap + r * (tile_h + gap), gap + c * (tile_width + gap)
            sheet[y:y + tile_h, x:x + tile_width] = t
    return sheet


def write_preview(rows: Sequence[Sequence[np.ndarray]], path: Path, frame_numbers: Optional[Sequence[int]] = None,
                  quality: int = 90, labels: Sequence[str] = COLUMNS) -> Path:
    import cv2
    sheet = contact_sheet(rows, labels, frame_numbers)
    tmp = Path(path).with_name(Path(path).stem + ".partial.jpg")
    if not cv2.imwrite(str(tmp), sheet[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality]):
        raise OSError(f"could not write {path}")
    tmp.replace(path)
    return Path(path)
