"""Make it me: one job, no assistant in the loop (contract: effects/MAKE-IT-ME.md).

    zeus_effects.py makeme --input <clip|url> [--start S --duration D] (--ref-pack DIR | --self-video FILE | --ref IMG…)
                           [--voice FILE] [--quality natural|fast] [--hat remove|keep] [--no-voice] --consent [--job DIR]
                           [--hair "short dark hair"] [--seed N] [--person NAME] [--cookies FILE]

Steps, in this order, each reported as JSON lines {"stage": <step>, "step": <step>, "progress": 0..1 within the step,
"overall": 0..1 of the job, "message": …} (the first line is {"stage": "plan", "steps": […], "job": "<dir>"}):

    prepare   download/trim, depth, people mask, face mesh (mode depth_mesh; the mesh feeds the hat step); with
              --parts also the parts map (zfx.parts: parts.mp4, parts.npz, parts.json, control-parts.mp4,
              preview-parts.jpg, about half a minute more)
    refs      the reference frames (face pack / self-video -> <job>/me.c101face / photos) into <job>/refs/, and the
              voice sample (<job>/me-voice.m4a: Demucs vocals, silences trimmed, normalized) when there is no --voice
    approve   automatic: the user consented and chose the clip (preview.jpg stays for the record)
    faceswap  FaceFusion, quality natural by default -> final.mp4, kept as final-swap.mp4
    hat       only with --hat remove: zfx.engines.hat (BiSeNet hat mask -> VACE hair -> composite) -> final.mp4;
              skipped with a message when no hat is found
    voice     Chatterbox voice conversion + EQ/loudness match + remix -> final-voice.mp4 (skipped without a sample)
    final     compare.mp4 (original left, result right, the result's sound), the AI comment, job.json `makeme`

Resumable: every finished step leaves <job>/.makeme/<step>.json with a hash of its inputs and the files it wrote;
running makeme again with the same --job skips the steps whose hash still matches and whose files exist (a changed
input re-runs that step and, through the hashes, everything after it). `zeus_effects.py cancel --job DIR` stops a
running job (its pid and process group are in job.json `makeme`); the job's status becomes "cancelled".
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

STEPS = ("prepare", "refs", "approve", "faceswap", "hat", "voice", "final")
WEIGHTS = {"prepare": 0.10, "refs": 0.05, "approve": 0.01, "faceswap": 0.14, "hat": 0.60, "voice": 0.08, "final": 0.02}
STATE_DIR = ".makeme"
PREPARE_MODE = "depth_mesh"
QUALITIES = ("natural", "fast")
HAT_CHOICES = ("remove", "keep")
DEFAULT_URL_DURATION = 5.0
MAX_PHOTOS = 4
CONSENT_TEXT = "the clip, the face and the voice are mine or of people who agreed"
AI_COMMENT_SWAP = "faceswap"
AI_COMMENT_HAIR = "faceswap natural, vace hair"
VERSION = 1

Progress = Callable[[str, float, str], None]


class MakeMeError(RuntimeError):
    """Plain-English refusal or failure; the CLI prints it as the JSON error line."""

    def __init__(self, message: str, step: str = "makeme"):
        super().__init__(message)
        self.step = step


class Cancelled(MakeMeError):
    """The job was stopped (SIGTERM / cancel)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def file_digest(path) -> str:
    """SHA-256 of a file ("-" when it does not exist)."""
    p = Path(path)
    if not p.is_file():
        return "-"
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------- options
@dataclass
class Options:
    input: str
    start: float = 0.0
    duration: Optional[float] = None
    ref_pack: Optional[str] = None
    self_video: Optional[str] = None
    refs: list = field(default_factory=list)
    voice: Optional[str] = None
    no_voice: bool = False
    quality: str = "natural"
    hat: str = "keep"
    consent: bool = False
    job: Optional[str] = None
    hair: Optional[str] = None
    seed: Optional[int] = None
    person: Optional[str] = None
    cookies: Optional[str] = None
    enhance: bool = False
    swapjob: Optional[str] = None     # the shared-folder job this run serves (zeus_effects.py watch)
    parts: bool = False               # --parts: the prepare step also builds the parts map (effects/PARTS.md)

    @classmethod
    def from_args(cls, args) -> "Options":
        g = lambda k, d=None: getattr(args, k, d)  # noqa: E731
        return cls(input=(g("input") or "").strip(), start=float(g("start") or 0.0), duration=g("duration"),
                   ref_pack=g("ref_pack"), self_video=g("self_video"), refs=list(g("ref") or []), voice=g("voice"),
                   no_voice=bool(g("no_voice")), quality=(g("quality") or "natural").lower(),
                   hat=(g("hat") or "keep").lower(), consent=bool(g("consent")), job=g("job"), hair=g("hair"),
                   seed=g("seed"), person=g("person"), cookies=g("cookies"), enhance=bool(g("enhance")),
                   swapjob=g("swapjob"), parts=bool(g("parts")))

    @property
    def me_source(self) -> str:
        if self.ref_pack:
            return "face-pack"
        if self.self_video:
            return "self-video"
        return "photos"

    @property
    def wants_voice(self) -> bool:
        return (not self.no_voice) and bool(self.voice or self.self_video)

    def public(self) -> dict:
        return {"input": self.input, "start": self.start, "duration": self.duration, "me": self.me_source,
                "ref_pack": self.ref_pack, "self_video": self.self_video, "refs": list(self.refs), "voice": self.voice,
                "voice_on": self.wants_voice, "quality": self.quality, "hat": self.hat, "consent": self.consent,
                "hair": self.hair, "seed": self.seed, "person": self.person, "enhance": self.enhance,
                "swapjob": self.swapjob, "parts": self.parts}


def validate(opts: Options) -> None:
    if not opts.consent:
        raise MakeMeError(f"Refusing without --consent: confirm that {CONSENT_TEXT}.", "consent")
    if not opts.input:
        raise MakeMeError("--input is required (a video file or a YouTube link).", "start")
    sources = sum(1 for v in (opts.ref_pack, opts.self_video, opts.refs) if v)
    if sources == 0:
        raise MakeMeError("Say who you are: --ref-pack <Name>.c101face, --self-video FILE, or --ref photo(s).", "refs")
    if sources > 1:
        raise MakeMeError("Choose one of --ref-pack, --self-video and --ref photos.", "refs")
    if opts.quality not in QUALITIES:
        raise MakeMeError(f"--quality must be one of {', '.join(QUALITIES)}.", "start")
    if opts.hat not in HAT_CHOICES:
        raise MakeMeError(f"--hat must be one of {', '.join(HAT_CHOICES)}.", "start")
    if len(opts.refs) > MAX_PHOTOS:
        raise MakeMeError(f"Use at most {MAX_PHOTOS} reference photos.", "refs")
    for name, value in (("--self-video", opts.self_video), ("--voice", opts.voice)):
        if value and not Path(value).expanduser().is_file():
            raise MakeMeError(f"{name} file not found: {value}", "refs")
    for p in opts.refs:
        if not Path(p).expanduser().is_file():
            raise MakeMeError(f"Reference photo not found: {p}", "refs")
    if opts.ref_pack and not Path(opts.ref_pack).expanduser().exists():
        raise MakeMeError(f"Face pack not found: {opts.ref_pack}", "refs")


def plan_steps(opts: Options) -> list[str]:
    """The steps this job runs (hat only with --hat remove, voice only when there is a sample to take)."""
    steps = ["prepare", "refs", "approve", "faceswap"]
    if opts.hat == "remove":
        steps.append("hat")
    if opts.wants_voice:
        steps.append("voice")
    steps.append("final")
    return steps


# ----------------------------------------------------------------------------- markers (resume)
def state_dir(job_dir) -> Path:
    return Path(job_dir) / STATE_DIR


def marker_path(job_dir, step: str) -> Path:
    return state_dir(job_dir) / f"{step}.json"


def read_marker(job_dir, step: str) -> Optional[dict]:
    p = marker_path(job_dir, step)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def is_done(job_dir, step: str, hash_value: str) -> Optional[dict]:
    """The marker when the step finished with the same inputs and its files still exist, else None."""
    marker = read_marker(job_dir, step)
    if not marker or marker.get("hash") != hash_value:
        return None
    job_dir = Path(job_dir)
    for rel in marker.get("outputs") or []:
        p = Path(rel)
        if not (p if p.is_absolute() else job_dir / p).exists():
            return None
    return marker


def mark_done(job_dir, step: str, hash_value: str, outputs: Sequence, info: Optional[dict] = None,
              seconds: Optional[float] = None, skipped: bool = False) -> dict:
    job_dir = Path(job_dir)
    rels = []
    for o in outputs:
        p = Path(o)
        try:
            rels.append(str(p.resolve().relative_to(job_dir.resolve())))
        except ValueError:
            rels.append(str(p))
    marker = {"step": step, "hash": hash_value, "finished": _now_iso(), "outputs": rels, "skipped": skipped,
              "seconds": seconds, "info": _jsonable(info or {})}
    d = state_dir(job_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = marker_path(job_dir, step).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marker, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, marker_path(job_dir, step))
    return marker


def clear_markers(job_dir, steps: Sequence[str] = STEPS) -> None:
    for s in steps:
        marker_path(job_dir, s).unlink(missing_ok=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


# ----------------------------------------------------------------------------- progress per step
class StepReporter:
    """Progress of one step: update(stage, p, message) -> one JSON line with the step, its progress and the overall.

    Accepts everything the sub-commands do with a zfx.progress.Progress (update/__call__, span, log, warn, done,
    error, .stage, .finished, .logger); `done` keeps the result instead of ending the stream, `error` raises."""

    def __init__(self, parent, step: str, lo: float, hi: float, min_interval: float = 0.0):
        self.parent = parent
        self.step = step
        self.lo, self.hi = lo, hi
        self.stage = step
        self.finished = False
        self.failed = False
        self.result: Optional[dict] = None
        self.min_interval = min_interval
        self._last = 0.0
        self.last_progress = 0.0
        self.last_message = ""

    @property
    def logger(self):
        return getattr(self.parent, "logger", None) or _null_logger()

    def update(self, stage: str, progress: Optional[float], message: str = "", **extra: Any) -> None:
        p = extra.get("overall")
        if p is None:
            p = progress
        if p is None:
            p = self.last_progress
        p = max(0.0, min(1.0, float(p)))
        self.last_progress = p
        if message:
            self.last_message = message
        now = time.monotonic()
        if self.min_interval and p < 1.0 and now - self._last < self.min_interval:
            return
        self._last = now
        obj = {"stage": self.step, "step": self.step, "progress": round(p, 4),
               "overall": round(self.lo + (self.hi - self.lo) * p, 4), "message": message}
        if stage and stage != self.step:
            obj["substage"] = stage
        self.parent.emit(obj)
        if message and hasattr(self.parent, "logger"):
            self.parent.logger.debug("[%s %3.0f%%] %s", self.step, p * 100, message)

    __call__ = update

    def span(self, stage: str, lo: float, hi: float):
        def report(fraction: float, message: str = "") -> None:
            self.update(stage, lo + (hi - lo) * max(0.0, min(1.0, fraction)), message)
        return report

    def log(self, message: str, level: int = 20) -> None:
        if hasattr(self.parent, "log"):
            self.parent.log(message, level)

    info = log

    def warn(self, message: str) -> None:
        if hasattr(self.parent, "warn"):
            self.parent.warn(message)

    def emit(self, obj: dict) -> None:
        self.parent.emit(obj)

    def done(self, job: Any = None, result: Optional[dict] = None) -> None:
        self.finished = True
        self.result = result or {}

    def error(self, message: str, stage: Optional[str] = None) -> None:
        self.finished = True
        self.failed = True
        raise MakeMeError(message, self.step)


def _null_logger():
    import logging
    return logging.getLogger("zeus_effects.makeme")


# ----------------------------------------------------------------------------- the pipeline core (testable)
@dataclass
class Step:
    """One step: inputs(ctx) -> hash of what it depends on; run(ctx, reporter) -> (outputs, info, skipped);
    restore(ctx, marker) puts what later steps need back into ctx when the step is skipped as already done;
    check(ctx) -> extra condition for "done" (e.g. the approval still verifies)."""
    name: str
    inputs: Callable[[Any], str]
    run: Callable[[Any, StepReporter], tuple]
    restore: Optional[Callable[[Any, dict], None]] = None
    check: Optional[Callable[[Any], bool]] = None


@dataclass
class Context:
    job_dir: Path
    opts: Options
    progress: Any
    state: dict = field(default_factory=dict)
    steps: dict = field(default_factory=dict)        # name -> record for job.json
    warnings: list = field(default_factory=list)
    plan: list = field(default_factory=list)
    started: str = field(default_factory=_now_iso)

    def save(self, status: str = "running", extra: Optional[dict] = None) -> None:
        """Merge the `makeme` section into job.json (never touches the other keys)."""
        path = self.job_dir / "job.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        section = dict(data.get("makeme") or {})
        section.update({"version": VERSION, "status": status, "started": section.get("started") or self.started,
                        "updated": _now_iso(), "options": self.opts.public(), "plan": list(self.plan),
                        "steps": _jsonable(self.steps), "warnings": list(self.warnings),
                        "timings": {k: v.get("seconds") for k, v in self.steps.items() if v.get("seconds") is not None},
                        "pid": os.getpid(), "pgid": _pgid(),
                        "consent": self.opts.consent, "consent_text": CONSENT_TEXT})
        if self.opts.swapjob:
            section["swapjob"] = self.opts.swapjob
        if status in ("running", "done"):      # an earlier attempt's failure is history once the job runs again
            for stale in ("error", "failed_step", "cancelled_by", "finished"):
                section.pop(stale, None)
        section.update(_jsonable(extra or {}))
        if status in ("done", "failed", "cancelled"):
            section["finished"] = _now_iso()
        data["makeme"] = section
        data["updated"] = _now_iso()
        try:
            from .jobs import atomic_write_json
            atomic_write_json(path, data)
        except Exception:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(tmp, path)


def _pgid() -> Optional[int]:
    try:
        return os.getpgid(0)
    except OSError:
        return None


def run_pipeline(ctx: Context, steps: Sequence[Step], weights: Optional[dict] = None) -> dict:
    """Run the steps in order, skipping the ones already done for the same inputs. Returns ctx.steps."""
    weights = weights or WEIGHTS
    names = [s.name for s in steps]
    ctx.plan = names
    total_w = sum(weights.get(n, 0.05) for n in names) or 1.0
    acc = 0.0
    for n in names:
        ctx.steps.setdefault(n, {"status": "pending"})
    ctx.progress.emit({"stage": "plan", "step": "plan", "progress": 0.0, "overall": 0.0, "steps": names,
                       "job": str(ctx.job_dir), "message": "Make it me: " + " › ".join(names)})
    ctx.save("running")
    for step in steps:
        w = weights.get(step.name, 0.05) / total_w
        reporter = StepReporter(ctx.progress, step.name, acc, acc + w)
        t0 = time.monotonic()
        try:
            h = step.inputs(ctx)
        except MakeMeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MakeMeError(f"{step.name}: {type(exc).__name__}: {exc}", step.name) from exc
        marker = is_done(ctx.job_dir, step.name, h)
        if marker is not None and (step.check is None or step.check(ctx)):
            if step.restore:
                step.restore(ctx, marker)
            ctx.steps[step.name] = {"status": "skipped" if marker.get("skipped") else "done", "resumed": True,
                                    "seconds": marker.get("seconds"), "finished": marker.get("finished"),
                                    "message": (marker.get("info") or {}).get("message") or "already done",
                                    "outputs": marker.get("outputs")}
            reporter.update(step.name, 1.0, f"{step.name}: already done, kept")
            acc += w
            ctx.save("running")
            continue
        ctx.steps[step.name] = {"status": "running", "started": _now_iso()}
        ctx.save("running")
        reporter.update(step.name, 0.0, f"{step.name}: starting")
        try:
            outputs, info, skipped = step.run(ctx, reporter)
        except Cancelled:
            ctx.steps[step.name].update(status="cancelled", seconds=round(time.monotonic() - t0, 1))
            raise
        except MakeMeError as exc:
            ctx.steps[step.name].update(status="failed", error=str(exc), seconds=round(time.monotonic() - t0, 1))
            raise
        except KeyboardInterrupt:
            ctx.steps[step.name].update(status="cancelled", seconds=round(time.monotonic() - t0, 1))
            raise Cancelled("Cancelled.", step.name)
        except Exception as exc:  # noqa: BLE001
            ctx.steps[step.name].update(status="failed", error=f"{type(exc).__name__}: {exc}",
                                        seconds=round(time.monotonic() - t0, 1))
            raise MakeMeError(_plain(exc), step.name) from exc
        seconds = round(time.monotonic() - t0, 1)
        info = dict(info or {})
        if reporter.last_message and "message" not in info:
            info["message"] = reporter.last_message
        mark_done(ctx.job_dir, step.name, h, outputs, info, seconds, skipped)
        ctx.steps[step.name] = {"status": "skipped" if skipped else "done", "seconds": seconds,
                                "started": ctx.steps[step.name].get("started"), "finished": _now_iso(),
                                "message": info.get("message"), "outputs": [str(o) for o in outputs],
                                **({"info": _jsonable({k: v for k, v in info.items() if k != "message"})}
                                   if info else {})}
        reporter.update(step.name, 1.0, info.get("message") or f"{step.name}: done in {seconds:.0f} s")
        acc += w
        ctx.save("running")
    return ctx.steps


def _plain(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    if isinstance(exc, MemoryError) or "out of memory" in msg.lower():
        return "Not enough memory for this step. Close other heavy apps or use a shorter clip. (" + msg[:200] + ")"
    return msg if len(msg) < 600 else msg[:600] + "..."


# ----------------------------------------------------------------------------- the real steps
def _effects_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _tool(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", shutil.which(name)):
        if cand and Path(cand).is_file():
            return cand
    raise MakeMeError(f"{name} is not installed (brew install ffmpeg).", "final")


def resolve_duration(opts: Options) -> float:
    from . import MAX_DURATION
    from . import io as zio
    if opts.duration is not None:
        return float(opts.duration)
    if zio.is_url(opts.input):
        return DEFAULT_URL_DURATION
    try:
        length = float(zio.probe(Path(opts.input).expanduser())["duration"] or 0.0)
    except Exception:  # noqa: BLE001
        return DEFAULT_URL_DURATION
    rest = length - float(opts.start or 0.0)
    if rest <= 0.5:
        raise MakeMeError(f"--start {opts.start:g} s is past the end of the video ({length:.1f} s).", "prepare")
    return round(min(MAX_DURATION, rest), 3)


def _job_dir(opts: Options) -> Path:
    from .jobs import jobs_dir, new_job_id
    if opts.job:
        return Path(opts.job).expanduser().resolve()
    return (jobs_dir() / new_job_id(opts.input)).resolve()


# -- prepare -------------------------------------------------------------------------------------------------------
PARTS_FILES = ("parts.mp4", "parts.npz", "parts.json", "control-parts.mp4", "preview-parts.jpg")


def _prepare_inputs(ctx: Context) -> str:
    o = ctx.opts
    src = o.input if "://" in o.input else file_digest(Path(o.input).expanduser())
    # "parts" only enters the hash when it is on, so jobs made before the option keep their prepare marker
    return digest("prepare", src, o.start, ctx.state["duration"], PREPARE_MODE, *(["parts"] if o.parts else []))


def _prepare_run(ctx: Context, reporter: StepReporter) -> tuple:
    import zeus_effects  # the CLI module (effects/zeus_effects.py), on sys.path when run through it
    o = ctx.opts
    ns = SimpleNamespace(input=o.input, start=o.start, duration=ctx.state["duration"], mode=PREPARE_MODE,
                         job=str(ctx.job_dir), cookies=o.cookies, device=None, depth_size=zeus_effects.DEPTH_INPUT_SIZE,
                         mask_quality="accurate", min_person_ratio=0.25, motion=False,
                         parts=o.parts, things="", gaze=False, things_over_person=False)
    try:
        zeus_effects.cmd_prepare(ns, reporter)
    except zeus_effects.CLIError as exc:
        raise MakeMeError(str(exc), "prepare") from exc
    result = reporter.result or {}
    outputs = [ctx.job_dir / n for n in ("source.mp4", "work.mp4", "mask.mp4", "control.mp4", "face-landmarks.json",
                                         "preview.jpg", "job.json")]
    if o.parts:
        outputs += [ctx.job_dir / n for n in PARTS_FILES]
    for w in result.get("warnings") or []:
        ctx.warnings.append(f"prepare: {w}")
    parts = result.get("parts") if o.parts else None
    info = {"frames": result.get("frames"), "fps": result.get("fps"), "size": result.get("size"),
            "duration": result.get("duration"), "has_audio": result.get("has_audio"), "timings": result.get("timings"),
            "message": f"prepared {result.get('duration')} s, {result.get('frames')} frames at 16 fps"
                       + (" with the parts map" if o.parts else "")}
    if parts:
        info["parts"] = {k: parts.get(k) for k in ("classes", "body_frames", "face_frames", "face_detail_frames",
                                                   "things", "gaze", "warnings")}
    return [p for p in outputs if p.exists()], info, False


# -- refs ----------------------------------------------------------------------------------------------------------
def _refs_inputs(ctx: Context) -> str:
    o = ctx.opts
    if o.ref_pack:
        from .facepack import load_pack, manifest_sha256
        return digest("refs", "pack", manifest_sha256(load_pack(o.ref_pack)))
    if o.self_video:
        return digest("refs", "self", file_digest(Path(o.self_video).expanduser()), o.consent, o.person or "",
                      o.wants_voice and not o.voice)
    return digest("refs", "photos", *[file_digest(Path(p).expanduser()) for p in o.refs])


def _refs_run(ctx: Context, reporter: StepReporter) -> tuple:
    from . import refs as R
    o = ctx.opts
    outputs: list = []
    sample = None
    sample_info: Optional[dict] = None
    try:
        if o.ref_pack:
            copies, record = R.refs_from_pack(o.ref_pack, ctx.job_dir)
            pack_dir = Path(record["pack"])
            reporter.update("refs", 0.9, f"face pack {record['name']}: {len(copies)} frames")
        elif o.self_video:
            copies, record = R.refs_from_self_video(o.self_video, ctx.job_dir, consent=o.consent, person=o.person,
                                                    report=lambda s, p, m="": reporter.update(s, 0.6 * float(p or 0), m))
            pack_dir = Path(record["pack"])
            outputs.append(pack_dir / "manifest.json")
            if o.wants_voice and not o.voice:
                sample_path = ctx.job_dir / R.VOICE_NAME
                sample_info = R.voice_from_video(o.self_video, sample_path, state_dir(ctx.job_dir) / "voice-sample",
                                                 report=lambda s, p, m="": reporter.update(s, 0.6 + 0.4 * float(p or 0), m))
                if sample_info and sample_info.get("ok"):
                    sample = sample_path
                    outputs.append(sample_path)
                    for w in sample_info.get("warnings") or []:
                        ctx.warnings.append(f"voice sample: {w}")
                else:
                    ctx.warnings.append((sample_info or {}).get("reason") or "No voice sample from the self-video.")
        else:
            copies, record = R.refs_from_photos(o.refs, ctx.job_dir)
            pack_dir = None
    except R.RefsError as exc:
        raise MakeMeError(str(exc), "refs") from exc
    if o.voice:
        sample = Path(o.voice).expanduser().resolve()
    outputs += list(copies)
    ctx.state.update(refs=[str(c) for c in copies], pack_dir=str(pack_dir) if pack_dir else None,
                     voice_sample=str(sample) if sample else None, refs_record=record)
    picked = ", ".join(p.get("slot") or p.get("file", "") for p in (record.get("picked") or [])) if record else ""
    message = f"{len(copies)} reference frame(s) from the {record.get('source', 'photos')}" + (f" ({picked})" if picked else "")
    if sample:
        message += "; voice sample ready"
    elif o.wants_voice:
        message += "; no voice sample"
    info = {"refs": ctx.state["refs"], "pack_dir": ctx.state["pack_dir"], "voice_sample": ctx.state["voice_sample"],
            "record": record, "voice_sample_info": sample_info, "message": message}
    return outputs, info, False


def _refs_restore(ctx: Context, marker: dict) -> None:
    info = marker.get("info") or {}
    ctx.state.update(refs=list(info.get("refs") or []), pack_dir=info.get("pack_dir"),
                     voice_sample=info.get("voice_sample"), refs_record=info.get("record"))
    if ctx.opts.voice:
        ctx.state["voice_sample"] = str(Path(ctx.opts.voice).expanduser().resolve())


# -- approve -------------------------------------------------------------------------------------------------------
def _approve_inputs(ctx: Context) -> str:
    m = read_marker(ctx.job_dir, "prepare") or {}
    return digest("approve", m.get("hash"), m.get("finished"))


def _approve_check(ctx: Context) -> bool:
    from .jobs import Job
    from .review import is_approved
    try:
        return bool(is_approved(Job.load(ctx.job_dir))[0])
    except Exception:  # noqa: BLE001
        return False


def _approve_run(ctx: Context, reporter: StepReporter) -> tuple:
    from .jobs import Job
    from .review import ReviewError, approve
    try:
        review = approve(Job.load(ctx.job_dir))
    except ReviewError as exc:
        raise MakeMeError(str(exc), "approve") from exc
    info = {"automatic": True, "reason": "the user consented and chose the clip; preview.jpg is kept for the record",
            "files": len(review.get("hashes") or {}), "approved_at": review.get("approved_at"),
            "message": f"approved automatically ({len(review.get('hashes') or {})} files hashed)"}
    return [ctx.job_dir / "job.json"], info, False


# -- faceswap ------------------------------------------------------------------------------------------------------
def _faceswap_inputs(ctx: Context) -> str:
    o = ctx.opts
    refs = ctx.state.get("refs") or []
    return digest("faceswap", file_digest(ctx.job_dir / "source.mp4"), *[file_digest(r) for r in refs], o.quality,
                  o.enhance)


def _faceswap_run(ctx: Context, reporter: StepReporter) -> tuple:
    from . import generate
    o = ctx.opts
    pack_dir = ctx.state.get("pack_dir")
    ns = SimpleNamespace(engine="faceswap", consent=True, ref=[] if pack_dir else list(ctx.state.get("refs") or []),
                         ref_pack=pack_dir, prompt="", frames=None, steps=None, size=None, seed=None,
                         enhance=o.enhance, quality=o.quality, upscale=None)
    try:
        result = generate.run_generate(ns, ctx.job_dir, reporter)
    except generate.GenerateError as exc:
        raise MakeMeError(str(exc), "faceswap") from exc
    final = ctx.job_dir / "final.mp4"
    swap = ctx.job_dir / "final-swap.mp4"
    shutil.copyfile(final, swap)
    ctx.state["swap"] = str(swap)
    info = {"seconds": result.get("seconds"), "engine_info": result.get("info"), "mux": result.get("mux"),
            "generation": result.get("generation"), "swap": swap.name,
            "message": f"face swapped ({o.quality}) in {result.get('seconds', 0):.0f} s"}
    return [final, swap], info, False


def _faceswap_restore(ctx: Context, marker: dict) -> None:
    ctx.state["swap"] = str(ctx.job_dir / "final-swap.mp4")


# -- hat -----------------------------------------------------------------------------------------------------------
def _hat_inputs(ctx: Context) -> str:
    from .engines import hat as H
    o = ctx.opts
    return digest("hat", file_digest(ctx.job_dir / "final-swap.mp4"), H.hair_prompt(o.hair),
                  o.seed if o.seed is not None else H.DEFAULT_SEED, H.VACE_FRAMES, H.VACE_STEPS, H.VACE_SIZE,
                  file_digest((ctx.state.get("refs") or ["-"])[0]))


def _hat_run(ctx: Context, reporter: StepReporter) -> tuple:
    from .engines import EngineError
    from .engines import hat as H
    o = ctx.opts
    swap = ctx.job_dir / "final-swap.mp4"
    final = ctx.job_dir / "final.mp4"
    refs = ctx.state.get("refs") or []
    if not refs:
        raise MakeMeError("No reference frame for the hat step.", "hat")
    audio = ctx.job_dir / "audio.m4a"
    try:
        record = H.run(ctx.job_dir, [refs[0]], ctx.job_dir / H.HAT_DIR / "final.mp4", reporter, base=swap,
                       landmarks=ctx.job_dir / "face-landmarks.json", audio=audio if audio.is_file() else None,
                       hair=o.hair, seed=o.seed, engine_label=AI_COMMENT_HAIR)
    except EngineError as exc:
        raise MakeMeError(str(exc), "hat") from exc
    if record.get("skipped"):
        if file_digest(final) != file_digest(swap):
            shutil.copyfile(swap, final)
        ctx.state["hat"] = {"found": False}
        record["message"] = record.get("reason")
        return [final], record, True
    shutil.copyfile(ctx.job_dir / H.HAT_DIR / "final.mp4", final)
    ctx.state["hat"] = {"found": True}
    cover = (record.get("mask") or {}).get("mean_cover")
    record["message"] = (f"hat removed (mask {cover * 100:.1f} % of the frame) in {record.get('seconds', 0):.0f} s"
                         if cover is not None else "hat removed")
    return [final, ctx.job_dir / H.HAT_DIR / "final.mp4"], record, False


def _hat_restore(ctx: Context, marker: dict) -> None:
    ctx.state["hat"] = {"found": not marker.get("skipped")}


# -- voice ---------------------------------------------------------------------------------------------------------
def _voice_inputs(ctx: Context) -> str:
    sample = ctx.state.get("voice_sample")
    return digest("voice", file_digest(ctx.job_dir / "final.mp4"), file_digest(sample) if sample else "-",
                  "chatterbox", "keep_music", "match", "auto")


def _voice_run(ctx: Context, reporter: StepReporter) -> tuple:
    from . import voice as V
    from .jobs import Job
    sample = ctx.state.get("voice_sample")
    if not sample or not Path(sample).is_file():
        return [], {"message": "no voice sample: the voice step is skipped (the result keeps the original voice)"}, True
    st = V.voice_status()
    if not st["ready"]:
        raise MakeMeError("The voice engine is not installed (" + str(st["message"]) + "): run effects/setup-voice.sh, "
                          "or --no-voice.", "voice")
    ns = SimpleNamespace(voice=[sample], consent=True, engine="chatterbox", keep_music=True, grade="match",
                         deess="auto", check="auto", device="auto", steps=None, seed=0)
    try:
        result = V.run_voice(ns, Job.load(ctx.job_dir), reporter)
    except V.VoiceError as exc:
        raise MakeMeError(str(exc), "voice") from exc
    for w in result.get("warnings") or []:
        ctx.warnings.append(f"voice: {w}")
    ctx.state["final_voice"] = result.get("final_voice")
    info = {"final_voice": result.get("final_voice"), "audio_voice": result.get("audio_voice"),
            "seconds": result.get("seconds"), "reference_speech_seconds": result.get("reference_speech_seconds"),
            "f0": result.get("f0"), "loudness_lufs": result.get("loudness_lufs"), "timing": result.get("timing"),
            "transcript": result.get("transcript"),
            "message": f"voice changed in {result.get('seconds', 0):.0f} s"}
    return [Path(result["final_voice"]), Path(result["audio_voice"])], info, False


def _voice_restore(ctx: Context, marker: dict) -> None:
    info = marker.get("info") or {}
    if info.get("final_voice") and Path(info["final_voice"]).is_file():
        ctx.state["final_voice"] = info["final_voice"]


# -- final ---------------------------------------------------------------------------------------------------------
def _final_inputs(ctx: Context) -> str:
    return digest("final", file_digest(ctx.job_dir / "final.mp4"), file_digest(ctx.job_dir / "final-voice.mp4")
                  if ctx.state.get("final_voice") else "-")


def result_file(ctx: Context) -> Path:
    fv = ctx.state.get("final_voice")
    if fv and Path(fv).is_file():
        return Path(fv)
    return ctx.job_dir / "final.mp4"


def video_comment(path: Path) -> Optional[str]:
    try:
        out = subprocess.run([_tool("ffprobe"), "-v", "error", "-show_entries", "format_tags=comment", "-of",
                              "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=60).stdout
        return out.strip() or None
    except Exception:  # noqa: BLE001
        return None


def make_compare(source: Path, result: Path, out: Path, comment: Optional[str] = None) -> dict:
    """compare.mp4: the original on the left, the result on the right (each at half width), the result's sound."""
    from . import io as zio
    info = zio.probe(result)
    w, h = int(info["width"]), int(info["height"])
    half = max(2, (w // 2) - (w // 2) % 2)
    hh = h - h % 2
    rate = info.get("fps_rational") or "30/1"
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    graph = (f"[0:v]scale={half}:{hh}:flags=lanczos,setsar=1,setpts=PTS-STARTPTS[l];"
             f"[1:v]scale={half}:{hh}:flags=lanczos,setsar=1,setpts=PTS-STARTPTS[r];"
             f"[l][r]hstack=inputs=2:shortest=1,format=yuv420p[v]")
    cmd = [_tool("ffmpeg"), "-y", "-v", "error", "-i", str(source), "-i", str(result), "-filter_complex", graph,
           "-map", "[v]", "-r", rate]
    if info.get("has_audio"):
        cmd += ["-map", "1:a:0", "-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-map_metadata", "-1",
            "-metadata", f"comment={(comment or 'AI-generated with YouTube Zeus Effects') + ' - side by side'}",
            "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise MakeMeError("compare.mp4 failed: " + (proc.stderr.strip().splitlines() or ["?"])[-1][:300], "final")
    os.replace(tmp, out)
    return {"file": out.name, "width": half * 2, "height": hh, "left": source.name, "right": result.name,
            "audio": "result" if info.get("has_audio") else None}


def _final_run(ctx: Context, reporter: StepReporter) -> tuple:
    source = ctx.job_dir / "source.mp4"
    result = result_file(ctx)
    if not result.is_file():
        raise MakeMeError("final.mp4 is missing: the face swap did not finish.", "final")
    reporter.update("final", 0.1, "side by side: original left, result right")
    comment = video_comment(result)
    compare = ctx.job_dir / "compare.mp4"
    cmp_info = make_compare(source, result, compare, comment)
    ctx.state.update(result=str(result), comment=comment, compare=str(compare))
    info = {"result": result.name, "compare": compare.name, "comment": comment, "compare_info": cmp_info,
            "message": f"{result.name} and compare.mp4 ready"}
    return [compare], info, False


def _final_restore(ctx: Context, marker: dict) -> None:
    info = marker.get("info") or {}
    ctx.state.update(result=str(ctx.job_dir / info["result"]) if info.get("result") else str(result_file(ctx)),
                     comment=info.get("comment"), compare=str(ctx.job_dir / "compare.mp4"))


def build_steps(opts: Options) -> list[Step]:
    table = {
        "prepare": Step("prepare", _prepare_inputs, _prepare_run),
        "refs": Step("refs", _refs_inputs, _refs_run, _refs_restore),
        "approve": Step("approve", _approve_inputs, _approve_run, check=_approve_check),
        "faceswap": Step("faceswap", _faceswap_inputs, _faceswap_run, _faceswap_restore),
        "hat": Step("hat", _hat_inputs, _hat_run, _hat_restore),
        "voice": Step("voice", _voice_inputs, _voice_run, _voice_restore),
        "final": Step("final", _final_inputs, _final_run, _final_restore),
    }
    return [table[n] for n in plan_steps(opts)]


# ----------------------------------------------------------------------------- process group / cancel
def own_process_group() -> Optional[int]:
    """Put this process in its own group when it is not interactive (so `cancel` can stop the whole job without
    touching the app or the watcher that started it). Returns the group id."""
    try:
        if not sys.stdin.isatty():
            os.setpgid(0, 0)
    except OSError:
        pass
    return _pgid()


def install_cancel_handler() -> None:
    def on_term(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)
    except (ValueError, OSError):
        pass


def descendants(pid: int) -> list[int]:
    found: list[int] = []
    todo = [pid]
    while todo:
        p = todo.pop()
        try:
            out = subprocess.run(["/usr/bin/pgrep", "-P", str(p)], capture_output=True, text=True, timeout=10).stdout
        except Exception:  # noqa: BLE001
            out = ""
        for line in out.split():
            try:
                c = int(line)
            except ValueError:
                continue
            if c not in found:
                found.append(c)
                todo.append(c)
    return found


def cancel_job(job_dir, grace: float = 5.0) -> dict:
    """Stop a running makeme job: SIGTERM to its process group and tree, SIGKILL to what is left after `grace` s."""
    job_dir = Path(job_dir)
    path = job_dir / "job.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MakeMeError(f"Could not read {path}: {exc}", "cancel") from exc
    section = data.get("makeme") or {}
    pid, pgid = section.get("pid"), section.get("pgid")
    status = section.get("status")
    if not pid or status not in ("running", None):
        return {"cancelled": False, "status": status, "message": f"The job is not running (status {status})."}
    targets: list[int] = []
    if _exists(int(pid)):
        targets = [int(pid)] + descendants(int(pid))
    my_pgid = _pgid()
    killed_group = False
    if pgid and int(pgid) not in (my_pgid, 0, 1) and _exists(int(pid)):
        try:
            os.killpg(int(pgid), signal.SIGTERM)
            killed_group = True
        except OSError:
            pass
    for t in targets:
        try:
            os.kill(t, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_exists(t) for t in targets):
        time.sleep(0.2)
    for t in targets:
        if _exists(t):
            try:
                os.kill(t, signal.SIGKILL)
            except OSError:
                pass
    if killed_group and pgid:
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except OSError:
            pass
    section.update(status="cancelled", finished=_now_iso(), updated=_now_iso(), cancelled_by="cancel")
    for name, rec in (section.get("steps") or {}).items():
        if isinstance(rec, dict) and rec.get("status") == "running":
            rec["status"] = "cancelled"
    data["makeme"] = section
    from .jobs import atomic_write_json
    atomic_write_json(path, data)
    return {"cancelled": True, "pid": pid, "pgid": pgid, "processes": len(targets), "status": "cancelled",
            "message": f"Stopped the job ({len(targets)} process{'es' if len(targets) != 1 else ''})."}


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ----------------------------------------------------------------------------- entry point
def run_makeme(args, progress) -> dict:
    """The `makeme` command. Returns the result dict for the final `done` line; raises MakeMeError."""
    opts = Options.from_args(args)
    validate(opts)
    install_cancel_handler()
    own_process_group()
    job_dir = _job_dir(opts)
    job_dir.mkdir(parents=True, exist_ok=True)
    ctx = Context(job_dir=job_dir, opts=opts, progress=progress)
    ctx.state["duration"] = resolve_duration(opts)
    steps = build_steps(opts)
    t0 = time.monotonic()
    try:
        run_pipeline(ctx, steps)
    except Cancelled as exc:
        ctx.save("cancelled", {"error": str(exc)})
        raise
    except KeyboardInterrupt:
        ctx.save("cancelled", {"error": "Cancelled."})
        raise Cancelled("Cancelled.", progress.stage if getattr(progress, "stage", None) else "makeme")
    except MakeMeError as exc:
        ctx.save("failed", {"error": str(exc), "failed_step": exc.step})
        raise
    result = result_file(ctx)
    summary = {
        "id": job_dir.name,
        "job": str(job_dir),
        "result": str(result),
        "final": str(job_dir / "final.mp4"),
        "final_voice": ctx.state.get("final_voice"),
        "final_swap": str(job_dir / "final-swap.mp4") if (job_dir / "final-swap.mp4").is_file() else None,
        "compare": ctx.state.get("compare"),
        "comment": ctx.state.get("comment"),
        "me": opts.me_source,
        "pack": ctx.state.get("pack_dir"),
        "refs": ctx.state.get("refs"),
        "voice_sample": ctx.state.get("voice_sample"),
        "hat": ctx.state.get("hat"),
        "steps": {k: {kk: vv for kk, vv in v.items() if kk in ("status", "seconds", "message", "resumed")}
                  for k, v in ctx.steps.items()},
        "timings": {k: v.get("seconds") for k, v in ctx.steps.items() if v.get("seconds") is not None},
        "seconds": round(time.monotonic() - t0, 1),
        "warnings": list(ctx.warnings),
    }
    ctx.save("done", {"result": result.name, "compare": "compare.mp4", "comment": ctx.state.get("comment"),
                      "seconds": summary["seconds"]})
    return summary
