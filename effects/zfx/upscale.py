"""Upscale VACE's generated crops before they are composited at the source resolution.

Real-ESRGAN ncnn-vulkan 0.2.5.0 (BSD-3-Clause, xinntao; official macOS build = universal x86_64 + arm64 binary,
Vulkan through a bundled MoltenVK, so it runs natively on Apple Silicon without Rosetta), installed by
`effects/setup-engines.sh --upscaler` into $ZEUS_EFFECTS_HOME/tools/realesrgan/ (ZEUS_REALESRGAN_DIR overrides):
    realesrgan-ncnn-vulkan, models/realesr-animevideov3-x2|x3|x4.{param,bin}, models/realesrgan-x4plus.{param,bin}
The models folder is passed with -m (the binary looks for a folder literally named `models`).

Modes (generate --upscale):
    fast     realesr-animevideov3 at x2/x3/x4, the smallest factor >= the needed one (M2 Pro: 16 frames 640x368
             in 3.2 s at x2, 4.4 s at x4, 0.15-0.24 GB)
    best     realesrgan-x4plus at x4 (M2 Pro: 6.7 s per 640x368 frame, 1.5 GB)
    lanczos  no model: cv2 Lanczos
The result is then resized to the crop's size in source pixels (INTER_AREA down, Lanczos up). Anything that goes
wrong with the binary falls back to Lanczos and says why in the returned info.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

MODES = ("fast", "best", "lanczos")
DEFAULT_MODE = "fast"
BINARY = "realesrgan-ncnn-vulkan"
MIN_RATIO = 1.15     # below this the model is not worth running: plain resize
LICENSE = "BSD-3-Clause (Real-ESRGAN ncnn-vulkan, xinntao)"


def tool_dir() -> Path:
    env = os.environ.get("ZEUS_REALESRGAN_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    home = os.environ.get("ZEUS_EFFECTS_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / "Library" / "Application Support" / "YouTube Zeus" / "Effects"
    return base / "tools" / "realesrgan"


def binary() -> Optional[Path]:
    p = tool_dir() / BINARY
    return p if p.is_file() and os.access(p, os.X_OK) else None


def model_for(mode: str, ratio: float) -> Optional[tuple[str, int]]:
    """(model name, scale) for a needed enlargement `ratio`, or None for a plain resize."""
    if mode == "lanczos" or ratio < MIN_RATIO:
        return None
    if mode == "best":
        return "realesrgan-x4plus", 4
    scale = 2 if ratio <= 2.0 else (3 if ratio <= 3.0 else 4)
    return "realesr-animevideov3", scale


def status() -> dict:
    b = binary()
    models = tool_dir() / "models"
    have = sorted(p.stem for p in models.glob("*.param")) if models.is_dir() else []
    return {"ready": bool(b) and "realesr-animevideov3-x2" in have, "binary": str(b) if b else None,
            "models": have, "license": LICENSE}


def resize(img: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    import cv2
    h, w = img.shape[:2]
    if (w, h) == (out_w, out_h):
        return img
    interp = cv2.INTER_AREA if out_w * out_h < w * h else cv2.INTER_LANCZOS4
    return cv2.resize(img, (int(out_w), int(out_h)), interpolation=interp)


def run_realesrgan(in_dir: Path, out_dir: Path, model: str, scale: int, timeout: float = 3600) -> None:
    b = binary()
    if b is None:
        raise RuntimeError(f"{BINARY} not installed (effects/setup-engines.sh --upscaler)")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(b), "-i", str(in_dir), "-o", str(out_dir), "-n", model, "-s", str(scale),
           "-m", str(tool_dir() / "models"), "-f", "png"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"{BINARY} exit {proc.returncode}: {' | '.join(tail)[:300]}")


def upscale_dir(in_dir: Path, out_dir: Path, ratio: float, mode: str = DEFAULT_MODE) -> dict:
    """Upscale every PNG of in_dir into out_dir (same names) by the model the mode and the needed `ratio` call for.
    out_dir gets the model's output size (gen size x scale); callers resize to the exact crop size with resize().
    Returns {mode, model, scale, seconds, fallback?}; on any failure out_dir holds copies of the inputs and
    info["fallback"] says why (the caller's resize() then does a Lanczos upscale)."""
    mode = mode if mode in MODES else DEFAULT_MODE
    pick = model_for(mode, ratio)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    info: dict = {"mode": mode, "ratio": round(float(ratio), 3)}
    files = sorted(in_dir.glob("*.png"))
    if pick is not None and binary() is None:
        info["fallback"] = "Real-ESRGAN not installed (effects/setup-engines.sh --upscaler): Lanczos"
        pick = None
    if pick is not None:
        model, scale = pick
        try:
            run_realesrgan(in_dir, out_dir, model, scale)
            missing = [f.name for f in files if not (out_dir / f.name).is_file()]
            if missing:
                raise RuntimeError(f"no output for {len(missing)} frame(s), e.g. {missing[0]}")
            info.update(model=model, scale=scale)
        except Exception as exc:   # noqa: BLE001 - any failure: plain resize instead
            info["fallback"] = f"Real-ESRGAN failed ({exc}): Lanczos"
            pick = None
    if pick is None:
        for f in files:
            shutil.copyfile(f, out_dir / f.name)
        info.update(model="lanczos", scale=1)
    info["seconds"] = round(time.monotonic() - t0, 1)
    return info


def upscale_frames(frames: Sequence[np.ndarray], out_w: int, out_h: int, mode: str = DEFAULT_MODE) -> tuple[list, dict]:
    """In-memory convenience: RGB uint8 frames -> frames at out_w x out_h (model, then exact resize)."""
    import cv2
    h, w = frames[0].shape[:2]
    ratio = max(out_w / float(w), out_h / float(h))
    with tempfile.TemporaryDirectory(prefix="zfx-upscale-") as tmp:
        src, dst = Path(tmp) / "in", Path(tmp) / "out"
        src.mkdir()
        for i, f in enumerate(frames):
            cv2.imwrite(str(src / f"{i:05d}.png"), np.ascontiguousarray(f[:, :, ::-1]))
        info = upscale_dir(src, dst, ratio, mode)
        out = [resize(cv2.imread(str(dst / f"{i:05d}.png"), cv2.IMREAD_COLOR)[:, :, ::-1], out_w, out_h)
               for i in range(len(frames))]
    return out, info
