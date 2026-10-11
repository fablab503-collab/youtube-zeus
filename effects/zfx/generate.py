"""generate: consent + review gates, engine dispatch, final mux with the untouched original audio.

    run_generate(args, job_dir, progress) -> result dict
        args: engine ('vace' | 'faceswap'), ref (list of photo paths), prompt, consent (bool),
              frames, steps, size ('480p' | '360p'), seed
        progress: callable(stage, progress 0..1, message) (zfx.progress.Progress works as is)

Order: refuse without consent -> refuse unless the review approval still matches the prepared
files -> engine writes result-raw.mp4 (video only) -> final.mp4 = result-raw retimed to the
source fps/duration and scaled to the source size + audio.m4a stream-copied untouched, with
metadata comment "AI-generated with YouTube Zeus Effects (<engine>)" -> record in job.json.
Raises GenerateError (plain English) on refusal or failure; the CLI turns it into the JSON error line.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Optional

from .engines import ENGINES, EngineError, get_engine

CONSENT_TEXT = "Tick: the photos show me or people who agreed"
AI_COMMENT = "AI-generated with YouTube Zeus Effects ({engine})"
REF_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
MAX_REFS = 4

Progress = Callable[[str, float, str], None]


class GenerateError(RuntimeError):
    """Plain-English refusal or failure of a generation."""


# ----------------------------------------------------------------------------- small utils
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tool(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", shutil.which(name)):
        if cand and Path(cand).is_file():
            return cand
    raise GenerateError(f"{name} is not installed (brew install ffmpeg).")


def _progress(progress: Optional[Progress]) -> Progress:
    if progress is not None:
        if callable(progress):
            return progress
        if hasattr(progress, "update"):
            return progress.update  # type: ignore[return-value]
    try:
        from .progress import Progress as _P
        return _P()
    except Exception:
        def emit(stage: str, p: float, message: str = "") -> None:
            print(json.dumps({"stage": stage, "progress": round(float(p or 0), 4), "message": message}), flush=True)
        return emit


# ----------------------------------------------------------------------------- job.json access
def _job_module():
    try:
        from . import jobs
        return jobs
    except Exception:
        return None


def resolve_job_dir(job_dir) -> Path:
    """Accepts a folder path, a bare job id, or a zfx.jobs.Job (what zeus_effects.py passes)."""
    if hasattr(job_dir, "dir") and hasattr(job_dir, "data"):
        job_dir = job_dir.dir
    jobs = _job_module()
    if jobs is not None and hasattr(jobs, "Job"):
        try:
            return jobs.Job.load(job_dir).dir
        except Exception as exc:
            raise GenerateError(str(exc)) from exc
    p = Path(job_dir).expanduser()
    if not (p / "job.json").is_file():
        raise GenerateError(f"No job.json in {p}. Run prepare first.")
    return p.resolve()


def load_job(job_dir: Path) -> dict:
    jobs = _job_module()
    if jobs is not None and hasattr(jobs, "Job"):
        return jobs.Job.load(job_dir).data
    if jobs is not None and hasattr(jobs, "load_job"):
        return jobs.load_job(job_dir)
    return json.loads((Path(job_dir) / "job.json").read_text(encoding="utf-8"))


def _atomic_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".job.json.", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def update_job(job_dir: Path, fn: Callable[[dict], None]) -> dict:
    """Reload job.json, apply fn(data) in place, save atomically."""
    jobs = _job_module()
    if jobs is not None and hasattr(jobs, "Job"):
        job = jobs.Job.load(job_dir)
        fn(job.data)
        job.save()
        return job.data
    data = load_job(job_dir)
    fn(data)
    data["updated"] = _now_iso()
    if jobs is not None and hasattr(jobs, "save_job"):
        jobs.save_job(job_dir, data)
    else:
        _atomic_json(Path(job_dir) / "job.json", data)
    return data


# ----------------------------------------------------------------------------- gates
ARTIFACT_FILES = {"source": "source.mp4", "audio": "audio.m4a", "work": "work.mp4", "depth": "depth.mp4",
                  "mask": "mask.mp4", "control": "control.mp4", "landmarks": "face-landmarks.json",
                  "preview": "preview.jpg"}


def _fallback_verify(job_dir: Path) -> tuple[bool, str]:
    """Same contract as zfx.review: approved, and every approved file has the same SHA-256."""
    try:
        data = load_job(job_dir)
    except Exception as exc:
        return False, f"Could not read job.json: {exc}"
    review = data.get("review") or {}
    hashes = review.get("hashes") or {}
    if not review.get("approved_at") or not hashes:
        return False, "This job is not approved yet. Review preview.jpg and the videos, then run approve."
    changed = []
    for name, digest in hashes.items():
        rel = (data.get("artifacts") or {}).get(name) or ARTIFACT_FILES.get(name) or name
        p = Path(rel) if Path(rel).is_absolute() else Path(job_dir) / rel
        if not p.is_file():
            changed.append(f"{p.name} is missing")
        elif sha256_file(p) != digest:
            changed.append(f"{p.name} changed")
    if changed:
        return False, ("Prepared files changed after approval (" + "; ".join(changed)
                       + "). Review again and run approve before generating.")
    return True, "approved and unchanged"


def check_review(job_dir: Path) -> tuple[bool, str]:
    """(ok, message) from zfx.review when present, else the local fallback."""
    try:
        from . import review
    except Exception:
        return _fallback_verify(job_dir)
    jobs = _job_module()
    target: Any = jobs.Job.load(job_dir) if jobs is not None and hasattr(jobs, "Job") else job_dir
    try:
        if hasattr(review, "is_approved"):
            ok, msg = review.is_approved(target)
            return bool(ok), str(msg)
        if hasattr(review, "verify"):
            result = review.verify(target)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
                return result[0], str(result[1])
            return True, "approved and unchanged"
    except Exception as exc:  # ReviewError and friends: plain-English message
        return False, str(exc)
    return _fallback_verify(job_dir)


def prepare_refs(refs, job_dir: Path) -> list[Path]:
    """Validate the reference photos and copy them into <job>/refs/ (HEIC -> JPEG with sips)."""
    refs = [r for r in (refs or []) if r]
    if not refs:
        raise GenerateError("Add at least one reference photo (--ref).")
    if len(refs) > MAX_REFS:
        raise GenerateError(f"Use at most {MAX_REFS} reference photos.")
    out_dir = job_dir / "refs"
    out_dir.mkdir(exist_ok=True)
    copies = []
    for i, ref in enumerate(refs, 1):
        src = Path(ref).expanduser()
        if not src.is_file():
            raise GenerateError(f"Reference photo not found: {src}")
        ext = src.suffix.lower()
        if ext not in REF_EXTENSIONS:
            raise GenerateError(f"{src.name} is not a photo (use JPEG, PNG, HEIC or WebP).")
        if ext in (".heic", ".heif"):
            dst = out_dir / f"ref-{i}.jpg"
            proc = subprocess.run(["/usr/bin/sips", "-s", "format", "jpeg", str(src), "--out", str(dst)],
                                  capture_output=True, text=True)
            if proc.returncode != 0 or not dst.is_file():
                raise GenerateError(f"Could not convert {src.name} to JPEG.")
        else:
            dst = out_dir / f"ref-{i}{'.jpg' if ext == '.jpeg' else ext}"
            if src.resolve() != dst.resolve():
                shutil.copyfile(src, dst)
        copies.append(dst)
    return copies


# ----------------------------------------------------------------------------- mux
def probe_video(path: Path) -> dict:
    out = subprocess.run([_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise GenerateError(f"Could not read {Path(path).name}: {out.stderr.strip()[-200:]}")
    info = json.loads(out.stdout or "{}")
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if v is None:
        raise GenerateError(f"No video stream in {Path(path).name}.")
    rate = v.get("avg_frame_rate") if v.get("avg_frame_rate") not in (None, "0/0") else v.get("r_frame_rate")
    try:
        fps = Fraction(rate) if rate and rate != "0/0" else Fraction(0)
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    duration = float(v.get("duration") or info.get("format", {}).get("duration") or 0.0)
    frames = int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None
    if frames is None and fps:
        frames = round(duration * float(fps))
    if (not duration) and frames and fps:
        duration = frames / float(fps)
    def _float(x) -> float:
        try:
            return float(x or 0.0)
        except ValueError:
            return 0.0

    fmt = info.get("format", {})
    container = _float(fmt.get("duration"))
    return {"width": int(v.get("width") or 0), "height": int(v.get("height") or 0), "fps": fps,
            "duration": duration, "container_duration": container or duration, "frames": frames,
            "video_start": _float(v.get("start_time")) - _float(fmt.get("start_time")),
            "codec": v.get("codec_name"), "has_audio": a is not None}


def mux_final(raw: Path, source: Path, audio: Optional[Path], out: Path, engine: str,
              mask: Optional[Path] = None) -> dict:
    """final = raw video at the source fps and size, on the source's clock, + original audio (stream copy).

    With `mask` (vace): the generated pixels are kept only inside the feathered person mask, over the original
    full-resolution frames, so everything outside the person stays sharp (VACE only redraws the mask anyway, but at
    360p for the whole frame).

    The raw video is never time-stretched: it is resampled to the source fps, then padded (last frame held) or trimmed
    to the source *file* duration, the clock the audio follows. Stretching it to the video stream's duration made lips
    drift up to ~0.1 s by the end of a 3 s clip (found 2026-10-11). vace: work.mp4 is padded so frame i is time i/16
    from the start of the file, so its output starts at 0. faceswap: FaceFusion's frame 0 is the source's first video
    frame, which YouTube files show ~33 ms after the audio starts, so its output starts at that same offset."""
    raw_i, src_i = probe_video(raw), probe_video(source)
    offset = max(0.0, src_i["video_start"]) if engine == "faceswap" else 0.0
    src_fps = src_i["fps"] or Fraction(30)
    src_dur = src_i["container_duration"] or (raw_i["duration"] or 0.0)
    raw_dur = (raw_i["frames"] / float(raw_i["fps"])) if raw_i["frames"] and raw_i["fps"] else raw_i["duration"]
    if not raw_dur or not src_dur:
        raise GenerateError("Could not read the clip durations for the final mux.")
    factor = 1.0
    w, h = src_i["width"] - src_i["width"] % 2, src_i["height"] - src_i["height"] % 2
    composite = mask is not None and Path(mask).is_file()
    same = (not composite and abs(raw_dur - src_dur) <= 1.5 / float(src_fps) and raw_i["fps"] == src_fps
            and (raw_i["width"], raw_i["height"]) == (w, h) and raw_i["codec"] == "h264")
    comment = AI_COMMENT.format(engine=engine)
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    cmd = [_tool("ffmpeg"), "-y", "-v", "error"]
    if offset > 1e-4:
        cmd += ["-itsoffset", f"{offset:.6f}"]
    cmd += ["-i", str(raw)]
    audio_src = None
    if audio and Path(audio).is_file():
        audio_src = Path(audio)
    elif src_i["has_audio"]:
        audio_src = source  # no audio.m4a: copy the source's own audio stream
    if audio_src is not None:
        cmd += ["-i", str(audio_src)]
    rate = f"{src_fps.numerator}/{src_fps.denominator}"
    hold = f"tpad=stop_mode=clone:stop_duration=1,trim=end={src_dur:.6f}"
    if composite:
        si = 2 if audio_src is not None else 1
        cmd += ["-i", str(source), "-i", str(mask)]
        lead = max(0.0, src_i["video_start"])   # the source's own video starts that late on the file's clock
        sigma = max(2.0, w / 320.0)              # feather ~6 px at 1080p
        graph = (f"[0:v]setpts=PTS-STARTPTS,fps={rate},{hold},scale={w}:{h}:flags=lanczos,format=gbrp[gen];"
                 f"[{si + 1}:v]setpts=PTS-STARTPTS,fps={rate},{hold},scale={w}:{h}:flags=bilinear,"
                 f"format=gray,gblur=sigma={sigma:.1f},format=gbrp[m];"
                 f"[{si}:v]setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration={lead:.6f},fps={rate},{hold},"
                 f"scale={w}:{h},format=gbrp[base];"
                 f"[base][gen][m]maskedmerge,format=yuv420p[v]")
        cmd += ["-filter_complex", graph, "-map", "[v]"]
    else:
        cmd += ["-map", "0:v:0"]
    if audio_src is not None:
        cmd += ["-map", "1:a:0", "-c:a", "copy"]
    if composite:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "17"]
    elif same:
        cmd += ["-c:v", "copy"]
    else:
        vf = (f"setpts=PTS-STARTPTS+{offset:.6f}/TB,fps={src_fps.numerator}/{src_fps.denominator},"
              f"tpad=stop_mode=clone:stop_duration=1,trim=end={src_dur:.6f},"
              f"scale={w}:{h}:flags=lanczos,format=yuv420p")
        cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "17"]
    cmd += ["-map_metadata", "-1", "-metadata", f"comment={comment}", "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise GenerateError("Final mux failed: " + (proc.stderr.strip().splitlines() or ["?"])[-1][:300])
    os.replace(tmp, out)
    return {"retime_factor": round(factor, 5), "fps": f"{src_fps.numerator}/{src_fps.denominator}",
            "duration": round(src_dur, 3), "video_offset": round(offset, 4), "width": w, "height": h,
            "video_stream_copy": same, "composited_in_mask": composite,
            "audio": audio_src.name if audio_src else None, "comment": comment}


# ----------------------------------------------------------------------------- entry point
def run_generate(args, job_dir, progress: Optional[Progress] = None) -> dict:
    report = _progress(progress)
    engine_name = (getattr(args, "engine", "") or "").strip().lower()
    if engine_name not in ENGINES:
        raise GenerateError(f"Unknown engine '{engine_name}'. Choose one of: {', '.join(ENGINES)}.")
    if not getattr(args, "consent", False):
        raise GenerateError(f"Generation refused: consent is missing. {CONSENT_TEXT} (--consent).")
    job_path = resolve_job_dir(job_dir)
    ok, why = check_review(job_path)
    if not ok:
        raise GenerateError(f"Generation refused: {why}")
    refs = prepare_refs(getattr(args, "ref", None), job_path)

    engine = get_engine(engine_name)
    st = engine.status()
    if not st.get("ready"):
        raise GenerateError(st.get("message") or f"Engine {engine_name} is not installed.")

    source = job_path / "source.mp4"
    if not source.is_file():
        raise GenerateError("source.mp4 is missing in the job folder. Run prepare again.")
    audio = job_path / "audio.m4a"
    raw = job_path / "result-raw.mp4"
    final = job_path / "final.mp4"
    previous_status = load_job(job_path).get("status")
    update_job(job_path, lambda d: d.update(status="generating"))

    options = {
        "prompt": getattr(args, "prompt", None) or "",
        "frames": getattr(args, "frames", None),
        "steps": getattr(args, "steps", None),
        "size": getattr(args, "size", None),
        "seed": getattr(args, "seed", None),
        "enhance": bool(getattr(args, "enhance", False)),
    }
    t0 = time.monotonic()
    report("generate", 0.0, f"starting {engine.LABEL}")
    try:
        info = engine.run(job_path, refs, raw, report, **options)
        report("mux", 0.0, "putting the original audio back")
        mask = job_path / "mask.mp4"
        mux = mux_final(raw, source, audio if audio.is_file() else None, final, engine_name,
                        mask=mask if engine_name == "vace" and mask.is_file() else None)
        report("mux", 1.0, "final.mp4 ready")
    except (EngineError, GenerateError) as exc:
        update_job(job_path, lambda d: d.update(status=previous_status or "approved", last_error=str(exc)))
        raise GenerateError(str(exc)) from exc
    except Exception as exc:
        update_job(job_path, lambda d: d.update(status=previous_status or "approved",
                                                last_error=f"{type(exc).__name__}: {exc}"))
        raise GenerateError(f"{engine_name} failed: {type(exc).__name__}: {exc}") from exc
    seconds = round(time.monotonic() - t0, 1)

    record = {
        "engine": engine_name,
        "created": _now_iso(),
        "seconds": seconds,
        "consent": True,
        "consent_text": CONSENT_TEXT,
        "refs": [str(r.relative_to(job_path)) for r in refs],
        "ref_sha256": [sha256_file(r) for r in refs],
        "options": {k: v for k, v in options.items() if v not in (None, "")},
        "result_raw": raw.name,
        "final": final.name,
        "final_sha256": sha256_file(final),
        "engine_info": info,
        "mux": mux,
    }

    def save(d: dict) -> None:
        d.setdefault("generations", []).append(record)
        d.setdefault("artifacts", {}).update({"result_raw": raw.name, "final": final.name})
        d["status"] = "generated"
        d.pop("last_error", None)
    update_job(job_path, save)
    return {"engine": engine_name, "final": str(final), "result_raw": str(raw), "seconds": seconds,
            "generation": len(load_job(job_path).get("generations") or []), "info": info, "mux": mux}
