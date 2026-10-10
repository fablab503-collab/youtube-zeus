"""Generation engines for Zeus Effects (free, local only; no API keys, no cloud).

vace      Wan2.1-VACE-1.3B (diffusers WanVACEPipeline on MPS): full character replacement, slow.
faceswap  FaceFusion 3.x headless (CoreML, CPU fallback): face only, fast.

Each engine module exposes:
    NAME, LABEL
    status() -> {"ready": bool, "message": str, ...}     (never downloads; used by doctor)
    run(job_dir, refs, out_path, progress, **options) -> dict   (writes out_path, video only)

Heavy imports (torch, diffusers) happen inside run(), so importing this package is cheap.
Weights / checkouts are installed by effects/setup-engines.sh (--vace / --faceswap).
"""

from __future__ import annotations

import importlib
from types import ModuleType

ENGINES = ("vace", "faceswap")
LABELS = {
    "vace": "Full replace · slow (Wan2.1-VACE-1.3B, local)",
    "faceswap": "Face swap · fast (FaceFusion, local)",
}


class EngineError(RuntimeError):
    """Plain-English engine problem (missing weights, engine crashed, out of memory...)."""


def get_engine(name: str) -> ModuleType:
    key = (name or "").strip().lower()
    if key not in ENGINES:
        raise EngineError(f"Unknown engine '{name}'. Choose one of: {', '.join(ENGINES)}.")
    return importlib.import_module(f"{__name__}.{key}")


def status_all() -> dict:
    """{engine: status dict} for doctor. Never raises."""
    out = {}
    for name in ENGINES:
        try:
            out[name] = get_engine(name).status()
        except Exception as exc:  # pragma: no cover - defensive
            out[name] = {"ready": False, "message": f"{type(exc).__name__}: {exc}"}
    return out
