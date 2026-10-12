"""Parts map of a prepared job (zfx.parts): one tone per body part, head detail, gaze, named things, depth bands.

  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m zfx.parts_cli --job DIR [--things "microphone, laptop, cap"] [--gaze]
        [--no-things] [--things-over-person] [--things-fps 2] [--things-threshold 0.4]
        [--things-template "{}"] [--things-no-zoom] [--no-face-detail] [--no-preview] [--device mps|cpu] [-v]

Run it from the effects/ folder (or with effects/ on PYTHONPATH). Needs a job prepared in a depth mode (work.mp4,
mask.mp4, depth.mp4); the pose and the face mesh are taken from motion.json / face-landmarks.json or computed.

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

from zfx.io import MediaError  # noqa: E402
from zfx.jobs import Job, JobError  # noqa: E402
from zfx.parts import Backends, PartsError, run_parts, summary_lines  # noqa: E402
from zfx.progress import Progress, setup_logging  # noqa: E402
from zfx.things import TEMPLATE, THINGS_FPS, THRESHOLD, ThingsError, parse_things  # noqa: E402

STAGE_WEIGHTS = {"load": 0.06, "landmarks": 0.10, "faceparse": 0.24, "things": 0.22, "parts": 0.16,
                 "render": 0.18, "preview": 0.04}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zfx.parts_cli",
                                description="Parts map of a prepared Zeus Effects job: a tone per body part, "
                                            "face detail, gaze, named things and depth bands.")
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
    p.add_argument("-v", "--verbose", action="store_true", help="debug logs on stderr")
    return p


def main(argv: Optional[list] = None, backends: Optional[Backends] = None, stream=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    progress = Progress(stream)
    offsets, acc = {}, 0.0
    for stage, weight in STAGE_WEIGHTS.items():
        offsets[stage] = acc
        acc += weight

    def say(stage: str, frac: float, message: str = "") -> None:
        frac = max(0.0, min(1.0, float(frac)))
        overall = (offsets.get(stage, 0.0) + STAGE_WEIGHTS.get(stage, 0.0) * frac) / acc
        progress.update(stage, frac, message, overall=round(overall, 4))

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


if __name__ == "__main__":
    sys.exit(main())
