"""Zeus Effects: free, local-only character-replacement prep for YouTube Zeus.

Keep *how* people move (depth, masks, face mesh), replace *who* they are with the
user's reference photo, put the original soundtrack back. See effects/SPEC.md.

Runtime folder: $ZEUS_EFFECTS_HOME (default
"~/Library/Application Support/YouTube Zeus/Effects"); nothing heavy lives in the repo.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "YouTube Zeus" / "Effects"

WORK_FPS = 16          # what the models see
WORK_MAX_SIDE = 832    # long side of work.mp4
MAX_DURATION = 10.0    # seconds per job
MODES = ("depth", "depth_mesh", "mesh")
AI_COMMENT = "AI-generated with YouTube Zeus Effects ({engine})"


def effects_home() -> Path:
    """Runtime folder ($ZEUS_EFFECTS_HOME overrides the default)."""
    env = os.environ.get("ZEUS_EFFECTS_HOME", "").strip()
    return Path(env).expanduser() if env else DEFAULT_HOME


def models_dir() -> Path:
    return effects_home() / "models"


def vendor_dir() -> Path:
    return effects_home() / "vendor"


def jobs_dir() -> Path:
    return effects_home() / "Jobs"


def log_path() -> Path:
    return effects_home() / "effects.log"


# Weights and vendored code (downloaded by setup.sh, never by the engine itself).
def vda_code_dir() -> Path:
    return vendor_dir() / "Video-Depth-Anything"


def vda_weights() -> Path:
    return models_dir() / "Video-Depth-Anything-Small" / "video_depth_anything_vits.pth"


def face_landmarker_model() -> Path:
    return models_dir() / "face_landmarker.task"


def vace_dir() -> Path:
    return models_dir() / "Wan2.1-VACE-1.3B-diffusers"


def facefusion_dir() -> Path:
    return effects_home() / "facefusion"
