"""depth.mp4: Video-Depth-Anything-Small (encoder vits) on work.mp4, colored with inferno.

Vendored code: $ZEUS_EFFECTS_HOME/vendor/Video-Depth-Anything (Apache-2.0, cloned by setup.sh).
Weights:       $ZEUS_EFFECTS_HOME/models/Video-Depth-Anything-Small/video_depth_anything_vits.pth
Runs on torch "mps", else CPU, in fp32: fp16 autocast on MPS makes this model collapse to a
flat output (measured 2026-10-10, torch 2.11), so it is opt-in (ZFX_DEPTH_FP16=1) and checked.
"""

from __future__ import annotations

import gc
import os
import sys
import warnings
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import WORK_FPS, vda_code_dir, vda_weights
from .io import VideoWriter, read_frames
from .progress import get_logger

log = get_logger()

VITS_CONFIG = {"encoder": "vits", "features": 64, "out_channels": [48, 96, 192, 384]}
ATTN_CHUNK = 4      # frames per DINOv2 attention call (bounds memory on 16 GB Macs)
ENCODER_CHUNK = 8   # frames per DINOv2 encoder pass (per-frame model, same result)

# Hand cached Metal buffers back sooner (measured: 6.2 -> 5.5 GB peak footprint, same speed).
# Must be set before the first MPS allocation; harmless if torch already initialised MPS.
os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.25")


class DepthError(RuntimeError):
    pass


def pick_device(preferred: Optional[str] = None) -> str:
    import torch
    if preferred:
        return preferred
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _import_vda():
    code = vda_code_dir()
    if not (code / "video_depth_anything" / "video_depth.py").is_file():
        raise DepthError(f"Video-Depth-Anything code is missing ({code}). Run: bash effects/setup.sh")
    if str(code) not in sys.path:
        sys.path.insert(0, str(code))
    # A stray top-level "utils" module would shadow the vendored one.
    mod = sys.modules.get("utils")
    if mod is not None and not str(getattr(mod, "__file__", "") or "").startswith(str(code)):
        del sys.modules["utils"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from video_depth_anything import video_depth as vd_module  # type: ignore
        from video_depth_anything.dinov2_layers import attention as attn_module  # type: ignore
    _patch_attention(attn_module)
    return vd_module


def _patch_attention(attn_module) -> None:
    """Without xFormers, DINOv2 materializes full attention for 32 frames at once.
    Use torch SDPA in small frame chunks instead (same math, far less memory)."""
    if getattr(attn_module, "XFORMERS_AVAILABLE", False) or getattr(attn_module, "_zfx_patched", False):
        return
    import torch
    import torch.nn.functional as F

    def forward(self, x, attn_bias=None):
        B, N, C = x.shape
        heads = self.num_heads
        outs = []
        for i in range(0, B, ATTN_CHUNK):
            xi = x[i:i + ATTN_CHUNK]
            b = xi.shape[0]
            qkv = self.qkv(xi).reshape(b, N, 3, heads, C // heads).permute(2, 0, 3, 1, 4)
            out = F.scaled_dot_product_attention(qkv[0], qkv[1], qkv[2])
            outs.append(out.transpose(1, 2).reshape(b, N, C))
        x = torch.cat(outs, dim=0) if len(outs) > 1 else outs[0]
        return self.proj_drop(self.proj(x))

    attn_module.Attention.forward = forward
    attn_module.MemEffAttention.forward = forward
    attn_module._zfx_patched = True


def load_model(device: str):
    import torch
    weights = vda_weights()
    if not weights.is_file():
        raise DepthError(f"Video-Depth-Anything-Small weights are missing ({weights}). Run: bash effects/setup.sh")
    vd_module = _import_vda()
    model = vd_module.VideoDepthAnything(**VITS_CONFIG, metric=False)
    state = torch.load(str(weights), map_location="cpu", weights_only=True)
    model.load_state_dict(state, strict=True)
    model = model.to(device).eval()
    _chunk_encoder(model)
    return vd_module, model


def _chunk_encoder(model) -> None:
    """DINOv2 sees frames independently: run it ENCODER_CHUNK frames at a time to cap peak memory."""
    import torch
    encoder = model.pretrained
    original = encoder.get_intermediate_layers

    def chunked(x, *args, **kwargs):
        if x.shape[0] <= ENCODER_CHUNK:
            return original(x, *args, **kwargs)
        parts = [original(x[i:i + ENCODER_CHUNK], *args, **kwargs) for i in range(0, x.shape[0], ENCODER_CHUNK)]

        def cat(items):
            first = items[0]
            if isinstance(first, (tuple, list)):
                return type(first)(cat([it[k] for it in items]) for k in range(len(first)))
            return torch.cat(items, dim=0)

        return cat(parts)

    encoder.get_intermediate_layers = chunked


def _free_mps_cache() -> None:
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


class _TqdmProgress:
    """Stands in for tqdm inside VideoDepthAnything.infer_video_depth to report windows."""

    def __init__(self, report: Callable[[float, str], None], total_frames: int):
        self.report = report
        self.total_frames = total_frames

    def __call__(self, iterable, *args, **kwargs):
        items = list(iterable)
        n = len(items)
        for i, item in enumerate(items):
            if i:
                _free_mps_cache()  # the previous window is done: hand cached GPU memory back
            start = int(item)
            self.report(i / max(n, 1), f"window {i + 1}/{n} (frames {start + 1}-"
                                       f"{min(self.total_frames, start + 32)} of {self.total_frames})")
            yield item
        self.report(1.0, f"{n} windows done")


def colorize(depths: np.ndarray) -> np.ndarray:
    """(N,H,W) float relative depth (bigger = nearer) -> (N,H,W,3) uint8 RGB inferno, one global range."""
    import cv2
    d = np.asarray(depths, dtype=np.float32)
    lo, hi = float(np.nanpercentile(d, 0.5)), float(np.nanpercentile(d, 99.5))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi - lo < 1e-6:
        lo, hi = float(np.nanmin(d)), float(np.nanmax(d)) + 1e-6
    norm = np.clip((d - lo) / (hi - lo), 0.0, 1.0)
    u8 = (norm * 255.0 + 0.5).astype(np.uint8)
    out = np.empty(u8.shape + (3,), dtype=np.uint8)
    for i in range(u8.shape[0]):
        bgr = cv2.applyColorMap(u8[i], cv2.COLORMAP_INFERNO)
        out[i] = bgr[:, :, ::-1]
    return out


def estimate_depth(frames: np.ndarray, report: Callable[[float, str], None], device: Optional[str] = None,
                   input_size: int = 392, fp32: Optional[bool] = None) -> np.ndarray:
    """frames (N,H,W,3) uint8 RGB -> depths (N,H,W) float32."""
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    device = pick_device(device)
    if fp32 is None:
        fp32 = device == "cpu" or os.environ.get("ZFX_DEPTH_FP16", "") != "1"
    log.info("depth: Video-Depth-Anything-Small on %s (%s), %d frames %dx%d, input %d",
             device, "fp32" if fp32 else "fp16", len(frames), frames.shape[2], frames.shape[1], input_size)
    report(0.0, f"loading Video-Depth-Anything-Small on {device}")
    vd_module, model = load_model(device)
    vd_module.tqdm = _TqdmProgress(report, len(frames))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                depths, _ = model.infer_video_depth(frames, WORK_FPS, input_size=input_size, device=device, fp32=fp32)
            except RuntimeError as exc:
                if fp32 or device == "cpu":
                    raise
                log.warning("depth fp16 failed on %s (%s); retrying in fp32", device, exc)
                depths, _ = model.infer_video_depth(frames, WORK_FPS, input_size=input_size, device=device, fp32=True)
            if not fp32 and (not np.isfinite(depths).all() or float(np.std(depths)) < 1e-5):
                log.warning("depth fp16 produced a flat or NaN result; retrying in fp32")
                depths, _ = model.infer_video_depth(frames, WORK_FPS, input_size=input_size, device=device, fp32=True)
            if not np.isfinite(depths).all() or float(np.std(depths)) < 1e-5:
                raise DepthError("The depth model produced a flat result (no depth). Try again, or report this clip.")
    finally:
        del model
        gc.collect()
        _free_mps_cache()
    return np.asarray(depths, dtype=np.float32)


def run_depth(work_path: Path, out_path: Path, report: Callable[[float, str], None],
              device: Optional[str] = None, input_size: int = 392) -> dict:
    """work.mp4 -> depth.mp4 (same frame count/size/fps). Returns stats."""
    frames = read_frames(work_path)
    depths = estimate_depth(frames, report, device=device, input_size=input_size)
    if depths.shape[0] != frames.shape[0]:
        raise DepthError(f"depth produced {depths.shape[0]} frames for {frames.shape[0]} input frames")
    colored = colorize(depths)
    h, w = frames.shape[1:3]
    with VideoWriter(out_path, w, h, WORK_FPS, crf=14) as writer:
        for frame in colored:
            writer.write(frame)
    return {"frames": int(depths.shape[0]), "device": pick_device(device),
            "min": float(depths.min()), "max": float(depths.max())}
