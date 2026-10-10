"""Shared helpers for the Zeus Effects tests (no model weights needed)."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EFFECTS = Path(__file__).resolve().parents[1]
if str(EFFECTS) not in sys.path:
    sys.path.insert(0, str(EFFECTS))

# Tests never touch the real runtime folder (jobs, effects.log): one throwaway home per run.
_SESSION_HOME = tempfile.mkdtemp(prefix="zfx-tests-home-")
os.environ["ZEUS_EFFECTS_HOME"] = _SESSION_HOME
atexit.register(shutil.rmtree, _SESSION_HOME, True)

CLI = EFFECTS / "zeus_effects.py"


def ffmpeg() -> str:
    for cand in ("/opt/homebrew/bin/ffmpeg", shutil.which("ffmpeg")):
        if cand and Path(cand).exists():
            return cand
    raise RuntimeError("ffmpeg not found")


def make_clip(path: Path, seconds: float = 2.0, size: str = "1280x720", rate: int = 30,
              audio: bool = True) -> Path:
    """testsrc video + 440 Hz sine audio (H.264/AAC mp4)."""
    cmd = [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    return path


class TempHome:
    """Point ZEUS_EFFECTS_HOME at an empty temp folder for the duration of a test."""

    def __enter__(self) -> Path:
        self.dir = Path(tempfile.mkdtemp(prefix="zfx-test-"))
        self.old = os.environ.get("ZEUS_EFFECTS_HOME")
        os.environ["ZEUS_EFFECTS_HOME"] = str(self.dir)
        return self.dir

    def __exit__(self, *exc) -> None:
        if self.old is None:
            os.environ.pop("ZEUS_EFFECTS_HOME", None)
        else:
            os.environ["ZEUS_EFFECTS_HOME"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
