"""Parts map of a prepared job (zfx.parts): one tone per body part, head detail, gaze, named things, depth bands.

  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m zfx.parts_cli --job DIR [--things "microphone, laptop, cap"] [--gaze]
        [--no-things] [--things-over-person] [--things-fps 2] [--things-threshold 0.4]
        [--things-template "{}"] [--things-no-zoom] [--no-face-detail] [--no-preview] [--device mps|cpu] [-v]

The same command is `zeus_effects.py parts …` (and `zeus effects parts …` from the app): `add_arguments` puts the
options on that sub-parser and `run` does the work with its Progress. Run it from the effects/ folder (or with
effects/ on PYTHONPATH). Needs a job prepared in a depth mode (work.mp4, mask.mp4, depth.mp4); the pose and the
face mesh are taken from motion.json / face-landmarks.json or computed.

stdout: JSON lines like every Zeus Effects command: {"stage","progress","message","overall"}, then
{"done":true,"job":"<dir>","result":{...}} or {"error":"...","stage":"..."} (exit 1). The summary (classes found,
coverage, things found per frame, gaze) goes to stderr and effects.log. Writes parts.mp4, parts.npz, parts.json,
control-parts.mp4 and preview-parts.jpg into the job folder and records them in job.json (the approved
artifacts are never touched). See effects/PARTS.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# The parts step's own stages, in order, with their share of the whole step (measured on an M2 Pro).
STAGE_WEIGHTS = {"load": 0.06, "landmarks": 0.10, "faceparse": 0.24, "things": 0.22, "parts": 0.16,
                 "render": 0.18, "preview": 0.04}
_TOTAL_WEIGHT = sum(STAGE_WEIGHTS.values())
_OFFSETS: dict = {}
_acc = 0.0
for _stage, _weight in STAGE_WEIGHTS.items():
    _OFFSETS[_stage] = _acc
    _acc += _weight


def overall_fraction(stage: str, fraction: float) -> float:
    """0..1 of the whole parts step when `stage` (load, landmarks, faceparse, things, parts, render, preview) is at
    `fraction`; an unknown stage keeps the fraction as it is."""
    frac = max(0.0, min(1.0, float(fraction or 0.0)))
    if stage not in STAGE_WEIGHTS:
        return frac
    return round((_OFFSETS[stage] + STAGE_WEIGHTS[stage] * frac) / _TOTAL_WEIGHT, 4)


def add_arguments(p: argparse.ArgumentParser, job: bool = True) -> argparse.ArgumentParser:
    """The parts options on a parser (the standalone command, or the `parts` sub-command of zeus_effects.py)."""
    from zfx.things import TEMPLATE, THINGS_FPS, THRESHOLD
    if job:
        p.add_argument("--job", required=True, help="job folder (or a job id under Jobs/)")
    p.add_argument("--things", default="",
                   help='things to find, comma-separated ("microphone, laptop, cap"); one tone each (CLIPSeg)')
    p.add_argument("--no-things", action="store_true", help="ignore --things (no CLIPSeg run)")
    p.add_argument("--gaze", action="store_true",
                   help="gaze direction and blinks from the irises (face mesh with 478 points), drawn in the eyes")
    p.add_argument("--things-over-person", action="store_true",
                   help="let named things override the body tones (default: things stay outside the person mask)")
    p.add_argument("--things-fps", type=float, default=THINGS_FPS,
                   help=f"frames per second sent to CLIPSeg (default {THINGS_FPS:g}; the rest is interpolated)")
    p.add_argument("--things-threshold", type=float, default=THRESHOLD,
                   help=f"CLIPSeg probability above which a pixel is the thing (default {THRESHOLD:g})")
    p.add_argument("--things-template", default=TEMPLATE,
                   help='prompt template around each name (default "{}"; try "a photo of a {}")')
    p.add_argument("--things-no-zoom", action="store_true",
                   help="no second CLIPSeg pass on a crop around the person (default: on when the person is small)")
    p.add_argument("--no-face-detail", action="store_true", help="skip the BiSeNet face parser (coarse head only)")
    p.add_argument("--no-preview", action="store_true", help="do not write preview-parts.jpg")
    p.add_argument("--device", choices=("mps", "cpu"), default=None, help="where CLIPSeg runs (default: mps)")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zfx.parts_cli",
                                description="Parts map of a prepared Zeus Effects job: a tone per body part, "
                                            "face detail, gaze, named things and depth bands.")
    add_arguments(p)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logs on stderr")
    return p


def run(args, progress, backends=None) -> int:
    """The parts command with parsed `args` (add_arguments) and a zfx.progress.Progress: 0 when done, 1 on an
    error (one JSON error line, like the other commands), 130 when cancelled."""
    from zfx.io import MediaError
    from zfx.jobs import Job, JobError
    from zfx.parts import PartsError, run_parts, summary_lines
    from zfx.things import ThingsError, parse_things

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    def say(stage: str, frac: float, message: str = "") -> None:
        progress.update(stage, max(0.0, min(1.0, float(frac))), message, overall=overall_fraction(stage, frac))

    job = None
    try:
        job = Job.load(args.job)
        names = [] if args.no_things else parse_things(args.things)
        if args.things_threshold <= 0 or args.things_threshold >= 1:
            raise PartsError("--things-threshold must be between 0 and 1.")
        if args.things_fps <= 0:
            raise PartsError("--things-fps must be positive.")
        progress.log(f"parts of job {job.dir} (things: {', '.join(names) or 'none'}, gaze: {args.gaze})")
        result = run_parts(job, things=names, gaze=args.gaze, over_person=args.things_over_person,
                           things_fps=args.things_fps, things_threshold=args.things_threshold,
                           things_template=args.things_template, things_zoom=not args.things_no_zoom,
                           face_detail=not args.no_face_detail,
                           device=args.device, report=say, backends=backends, preview=not args.no_preview)
    except (JobError, PartsError, ThingsError, MediaError) as exc:
        progress.error(str(exc), progress.stage if progress.stage != "start" else "parts")
        return 1
    except KeyboardInterrupt:
        progress.error("Cancelled.", progress.stage)
        return 130
    except Exception as exc:  # one plain JSON error line, like the other commands
        progress.logger.exception("parts failed")
        msg = str(exc).strip() or type(exc).__name__
        if isinstance(exc, MemoryError) or "out of memory" in msg.lower():
            msg = "Not enough memory for this step. Close other heavy apps or use a shorter clip. (" + msg[:200] + ")"
        progress.error(msg if len(msg) < 600 else msg[:600] + "...", progress.stage)
        return 1
    for line in summary_lines(result):
        progress.log(line)
    progress.done(job.dir, result)
    return 0


def main(argv: Optional[list] = None, backends=None, stream=None) -> int:
    from zfx.progress import Progress, setup_logging
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    return run(args, Progress(stream), backends=backends)


if __name__ == "__main__":
    sys.exit(main())
