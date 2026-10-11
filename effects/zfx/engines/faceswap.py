"""Face-only swap with FaceFusion 3.x headless (free, local; CoreML on Apple Silicon, CPU fallback).

Installed by `effects/setup-engines.sh --faceswap` into $ZEUS_EFFECTS_HOME/facefusion with its
own Python 3.12 venv ($ZEUS_EFFECTS_HOME/facefusion/.venv). Runs on the untouched source.mp4
(original fps and size); depth/mask/control are not used, but approve + consent still gate it.

Command (flags read from facefusion/program.py of FaceFusion 3.9.1):
    python facefusion.py headless-run
        --source-paths <ref> [<ref2> ...]    (several refs -> FaceFusion averages the face embeddings)
        --target-path source.mp4  --output-path <out>
        --processors face_swapper face_enhancer
        --execution-providers coreml cpu     (cpu only if CoreML fails)
        --face-selector-mode one             (largest face; 'many' = every face)
        --temp-path <job>/.facefusion-tmp  --jobs-path <job>/.facefusion-jobs
        --log-level info
FaceFusion must run with cwd = its checkout (it resolves facefusion.ini and processor modules
relative to the working directory).

Models FaceFusion downloads on first run into facefusion/.assets/models (vendor, license as declared
in FaceFusion's own model metadata; see effects/ENGINES.md):
    hyperswap_1a_256   FaceFusion      ResearchRAIL     (default swapper)
    arcface_w600k_r50  InsightFace     Non-Commercial   (face recognizer, always used)
    gfpgan_1.4         TencentARC      Apache-2.0       (face enhancer)
    yoloface_8n        derronqi        GPL-3.0          (face detector)
    2dfan4             breadbread1984  MIT              (landmarks)
    fan_68_5           FaceFusion      OpenRAIL-M       (landmarks)
    xseg_1             DeepFaceLab     GPL-3.0          (occlusion mask)
    bisenet_resnet_34  yakhyo          MIT              (face parser)
    fairface           dchen236        CC-BY-4.0        (face classifier)
    kim_vocal_2        KimberleyJensen Non-Commercial   (voice extractor, pre-checked)
    nsfw_1/2/3         EraX/Marqo/Freepik  Apache-2.0/Apache-2.0/MIT (content analyser)
FaceFusion code itself: OpenRAIL-AS. => the face-swap output is for personal / research use.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import EngineError

NAME = "faceswap"
LABEL = "Face swap · fast (FaceFusion, local)"
PROCESSORS = ("face_swapper",)
# The face enhancer (GFPGAN) sharpens the swapped face but is ~20x slower on the M2 Pro (measured 2026-10-11, 3 s of
# 1080p30: 33 s without, 697 s with) and barely visible at normal size: opt-in with --enhance.
ENHANCED_PROCESSORS = ("face_swapper", "face_enhancer")
# Optional override of the swapper model, e.g. ghost_2_256 (Apache-2.0) instead of hyperswap_1a_256.
SWAPPER_ENV = "ZEUS_FACESWAP_MODEL"
TIMEOUT_S = 3 * 3600

# tqdm stages FaceFusion prints (stderr, '\r'-separated) and where they sit in our 0..1 range.
STAGE_SPANS = {
    "downloading": (0.00, 0.05),
    "analysing": (0.05, 0.15),
    "extracting": (0.15, 0.20),
    "processing": (0.20, 0.95),
    "merging": (0.95, 0.99),
}
_TQDM = re.compile(r"(downloading|analysing|extracting|processing|merging):\s+(\d+)%\|[^|]*\|\s*([\d.]+[kMG]?)/([\d.]+[kMG]?)")
_FF_LOG = re.compile(r"\[FACEFUSION\.[A-Z_.]+\]\s*(.*)")

Progress = Callable[[str, float, str], None]


# ----------------------------------------------------------------------------- paths
def runtime_home() -> Path:
    env = os.environ.get("ZEUS_EFFECTS_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "YouTube Zeus" / "Effects"


def facefusion_dir() -> Path:
    env = os.environ.get("ZEUS_FACEFUSION_DIR", "").strip()
    return Path(env).expanduser() if env else runtime_home() / "facefusion"


def facefusion_python(ff_dir: Optional[Path] = None) -> Path:
    return (ff_dir or facefusion_dir()) / ".venv" / "bin" / "python"


def status() -> dict:
    ff = facefusion_dir()
    py = facefusion_python(ff)
    if not (ff / "facefusion.py").is_file():
        return {"ready": False, "path": str(ff), "message": "FaceFusion not installed. Run: effects/setup-engines.sh --faceswap"}
    if not py.is_file():
        return {"ready": False, "path": str(ff), "message": "FaceFusion venv missing. Run: effects/setup-engines.sh --faceswap"}
    models = ff / ".assets" / "models"
    have_models = models.is_dir() and any(models.glob("*.onnx"))
    return {"ready": True, "path": str(ff),
            "models_downloaded": have_models,
            "message": "ready" + ("" if have_models else " (models download on first run, ~1.7 GB)")}


# ----------------------------------------------------------------------------- command
def build_command(ff_dir: Path, refs: Sequence[Path], target: Path, output: Path, temp_dir: Path,
                  jobs_dir: Path, providers: Sequence[str] = ("coreml", "cpu"),
                  selector: str = "one", swapper_model: Optional[str] = None,
                  processors: Sequence[str] = PROCESSORS) -> list[str]:
    cmd = [str(facefusion_python(ff_dir)), "facefusion.py", "headless-run",
           "--source-paths", *[str(r) for r in refs],
           "--target-path", str(target),
           "--output-path", str(output),
           "--processors", *processors,
           "--execution-providers", *providers,
           "--face-selector-mode", selector,
           "--temp-path", str(temp_dir),
           "--jobs-path", str(jobs_dir),
           "--log-level", "info"]
    if swapper_model:
        cmd += ["--face-swapper-model", swapper_model]
    return cmd


def _run_streaming(cmd: list[str], cwd: Path, progress: Progress, log_file: Path) -> tuple[int, str]:
    """Run FaceFusion, map its tqdm bars to progress(), keep a full log. Returns (code, tail)."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["SYSTEM_VERSION_COMPAT"] = "0"
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = os.pathsep.join([str(Path(cmd[0]).parent), "/opt/homebrew/bin", "/usr/local/bin",
                                   env.get("PATH", "/usr/bin:/bin")])
    tail: list[str] = []
    last_emit = 0.0
    with open(log_file, "a", encoding="utf-8", errors="replace") as log, \
            subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             bufsize=0) as proc:
        log.write("$ " + " ".join(cmd) + "\n")
        buf = b""
        assert proc.stdout is not None
        start = time.monotonic()
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                m = _TQDM.search(line)
                if m:
                    stage, pct = m.group(1), int(m.group(2))
                    lo, hi = STAGE_SPANS[stage]
                    now = time.monotonic()
                    if now - last_emit >= 0.5 or pct >= 100:
                        last_emit = now
                        detail = f"{m.group(3)}/{m.group(4)}"
                        progress("generate", lo + (hi - lo) * pct / 100.0, f"FaceFusion {stage} {detail}")
                    continue
                log.write(line + "\n")
                tail = (tail + [line])[-12:]
                fm = _FF_LOG.search(line)
                if fm:
                    progress("generate", None, "FaceFusion: " + fm.group(1)[:160])  # type: ignore[arg-type]
            if time.monotonic() - start > TIMEOUT_S:
                proc.kill()
                tail.append(f"timed out after {TIMEOUT_S} s")
                break
        code = proc.wait()
        log.write(f"exit {code}\n")
    return code, "\n".join(tail)


def _progress_wrapper(progress: Optional[Progress]) -> Progress:
    state = {"p": 0.0}

    def report(stage: str, p: Optional[float], message: str = "") -> None:
        if p is not None:
            state["p"] = max(state["p"], float(p))  # monotonic across FaceFusion's stages
        if progress:
            progress(stage, state["p"], message)
    return report  # type: ignore[return-value]


# ----------------------------------------------------------------------------- run
def run(job_dir, refs: Sequence, out_path, progress: Optional[Progress] = None, *,
        target: Optional[Path] = None, selector: str = "one", enhance: bool = False, **_ignored) -> dict:
    """Swap the reference face onto source.mp4 -> out_path (video; FaceFusion's own audio is ignored)."""
    processors = ENHANCED_PROCESSORS if enhance else PROCESSORS
    job_dir = Path(job_dir)
    out_path = Path(out_path)
    report = _progress_wrapper(progress)
    st = status()
    if not st["ready"]:
        raise EngineError(st["message"])
    ff = Path(st["path"])
    target = Path(target) if target else job_dir / "source.mp4"
    if not target.is_file():
        raise EngineError(f"{target.name} is missing in the job folder. Run prepare again.")
    refs = [Path(r).expanduser().resolve() for r in refs]
    if not refs:
        raise EngineError("Face swap needs at least one reference photo (--ref).")

    temp_dir = job_dir / ".facefusion-tmp"
    jobs_dir = job_dir / ".facefusion-jobs"
    temp_dir.mkdir(parents=True, exist_ok=True)
    log_file = job_dir / "facefusion.log"
    tmp_out = out_path.with_name(out_path.stem + ".facefusion" + out_path.suffix)
    tmp_out.unlink(missing_ok=True)
    swapper = os.environ.get(SWAPPER_ENV, "").strip() or None

    t0 = time.monotonic()
    attempts = [("coreml", "cpu"), ("cpu",)]
    used: tuple = ()
    last_tail = ""
    try:
        for providers in attempts:
            report("generate", None, f"FaceFusion starting ({', '.join(providers)})")
            # FaceFusion keeps job state in --jobs-path; a fresh folder per attempt avoids stale ids.
            shutil.rmtree(jobs_dir, ignore_errors=True)
            cmd = build_command(ff, refs, target, tmp_out, temp_dir, jobs_dir, providers, selector, swapper,
                                processors)
            code, last_tail = _run_streaming(cmd, ff, report, log_file)
            if code == 0 and tmp_out.is_file() and tmp_out.stat().st_size > 0:
                used = providers
                break
            tmp_out.unlink(missing_ok=True)
            if "no source face" in last_tail.lower() or "choose an image for the source" in last_tail.lower():
                break  # input problem, CPU would fail the same way
            report("generate", None, f"FaceFusion failed with {providers[0]} (exit {code}); "
                   + ("retrying on CPU" if providers != ("cpu",) else "giving up"))
        if not used:
            hint = ""
            low = last_tail.lower()
            if "no face" in low or "source face" in low or "reference face" in low:
                hint = " No usable face was found: use a sharp, front-facing reference photo."
            elif "nsfw" in low or "content" in low:
                hint = " FaceFusion's content check refused this clip."
            raise EngineError("FaceFusion did not produce a video." + hint
                              + f" Details in {log_file}. Last lines: " + last_tail.replace("\n", " | ")[-400:])
        os.replace(tmp_out, out_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(jobs_dir, ignore_errors=True)
    seconds = round(time.monotonic() - t0, 1)
    report("generate", 1.0, f"FaceFusion done in {seconds:.0f} s")
    return {
        "engine": NAME,
        "seconds": seconds,
        "execution_providers": list(used),
        "processors": list(processors),
        "swapper_model": swapper or "hyperswap_1a_256",
        "face_selector_mode": selector,
        "target": target.name,
        "log": str(log_file),
    }
