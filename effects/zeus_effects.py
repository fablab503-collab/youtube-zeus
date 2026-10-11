#!/usr/bin/env python3
"""Zeus Effects CLI: free, local-only character replacement prep and generation.

Run with the runtime venv:
  "$ZEUS_EFFECTS_HOME/.venv/bin/python" effects/zeus_effects.py <command> ...

  doctor   [--json]
  prepare  --input FILE|URL [--start S] [--duration D<=10] --mode depth|depth_mesh|mesh|pose|depth_pose
           [--job DIR] [--cookies FILE] [--motion]
  approve  --job DIR
  generate --job DIR --engine vace|faceswap (--ref IMG [--ref IMG2] | --ref-pack DIR.c101face | both)
           [--prompt TEXT] [--consent] [--frames N] [--steps N] [--size 480p|360p] [--seed N]
  facepack --pack DIR.c101face [--max-refs N] [--json]   (what generate would pick from a face pack)
  motion   --job DIR [--json]   (the job's motion capture: frames with a body, hands, visibility, limbs)
  status   --job DIR [--json]
  list     [--json]

stdout: JSON lines {"stage","progress","message"}; last line {"done":true,"job","result"} or
{"error","stage"} (exit 1). --json on doctor/status/list prints one such "done" line whose
`result` is the report (the same keys are also repeated at the top level for direct decoding).
Human logs: stderr and $ZEUS_EFFECTS_HOME/effects.log. See effects/SPEC.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import zfx  # noqa: E402
from zfx import MAX_DURATION, MESH_MODES, MODES, POSE_MODES  # noqa: E402
from zfx.progress import Progress, setup_logging  # noqa: E402


class CLIError(Exception):
    def __init__(self, message: str, stage: str = "start"):
        super().__init__(message)
        self.stage = stage


# ---------------------------------------------------------------------------
# helpers
def _emit_report(progress: Progress, report: dict, job=None) -> None:
    obj = dict(report)
    obj.update({"done": True, "job": str(job) if job else None, "result": report})
    progress.emit(obj)
    progress.finished = True


def _tool_version(path: str | None, args: list[str]) -> str | None:
    if not path:
        return None
    try:
        out = subprocess.run([path] + args, capture_output=True, text=True, timeout=20).stdout.strip()
        return out.splitlines()[0] if out else None
    except Exception:
        return None


def _file_info(path: Path) -> dict:
    present = path.exists()
    size = None
    if present and path.is_file():
        size = path.stat().st_size
    return {"path": str(path), "present": present, "bytes": size}


def _memory() -> dict:
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"total_gb": round(vm.total / 2**30, 1), "available_gb": round(vm.available / 2**30, 1)}
    except Exception:
        pass
    try:
        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout)
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        page = 16384
        pages = {}
        for line in vm.splitlines():
            if "page size of" in line:
                page = int(line.split("page size of")[1].split()[0])
            elif ":" in line:
                k, v = line.split(":", 1)
                try:
                    pages[k.strip()] = int(v.strip().rstrip("."))
                except ValueError:
                    pass
        avail = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
                 + pages.get("Pages speculative", 0) + pages.get("Pages purgeable", 0)) * page
        return {"total_gb": round(total / 2**30, 1), "available_gb": round(avail / 2**30, 1)}
    except Exception:
        return {"total_gb": None, "available_gb": None}


# ---------------------------------------------------------------------------
# doctor
def doctor_report() -> dict:
    from zfx import (effects_home, face_landmarker_model, facefusion_dir, hand_landmarker_model,
                     pose_landmarker_model, vace_dir, vda_code_dir, vda_weights)
    from zfx.io import which

    home = effects_home()
    report: dict = {"version": zfx.__version__, "home": str(home), "platform": platform.platform()}

    py_ok = sys.version_info >= (3, 10)
    report["python"] = {"version": platform.python_version(), "executable": sys.executable,
                        "venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix), "ok": py_ok}

    torch_info = {"version": None, "mps": False, "ok": False}
    try:
        import torch
        torch_info = {"version": torch.__version__, "mps": bool(torch.backends.mps.is_available()),
                      "mps_built": bool(torch.backends.mps.is_built()), "ok": True}
    except Exception as exc:
        torch_info["detail"] = f"{type(exc).__name__}: {exc}"
    report["torch"] = torch_info

    ffmpeg, ffprobe, ytdlp = which("ffmpeg"), which("ffprobe"), which("yt-dlp")
    report["ffmpeg"] = {"path": ffmpeg, "version": _tool_version(ffmpeg, ["-version"]), "ok": bool(ffmpeg)}
    report["ffprobe"] = {"path": ffprobe, "ok": bool(ffprobe)}
    report["yt_dlp"] = {"path": ytdlp, "version": _tool_version(ytdlp, ["--version"]), "ok": bool(ytdlp)}

    try:
        from zfx.masks import vision_available
        ok, detail = vision_available()
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    report["vision"] = {"ok": ok, "detail": detail}

    try:
        import mediapipe
        report["mediapipe"] = {"version": getattr(mediapipe, "__version__", "?"), "ok": True}
    except Exception as exc:
        report["mediapipe"] = {"version": None, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    try:
        import cv2
        report["opencv"] = {"version": cv2.__version__, "ok": True}
    except Exception as exc:
        report["opencv"] = {"version": None, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    vace = vace_dir()
    ff = facefusion_dir()
    weights = {
        "vda": _file_info(vda_weights()),
        "vda_code": _file_info(vda_code_dir() / "video_depth_anything" / "video_depth.py"),
        "face_landmarker": _file_info(face_landmarker_model()),
        "pose_landmarker": _file_info(pose_landmarker_model()),
        "hand_landmarker": _file_info(hand_landmarker_model()),
        "vace": {"path": str(vace), "present": (vace / "model_index.json").is_file()},
        "facefusion": {"path": str(ff), "present": (ff / "facefusion.py").is_file(),
                       "venv": (ff / ".venv" / "bin" / "python").exists()},
    }
    report["weights"] = weights

    prepare_ok = all([py_ok, torch_info.get("ok"), report["ffmpeg"]["ok"], report["ffprobe"]["ok"], ok,
                      report["mediapipe"]["ok"], report["opencv"]["ok"], weights["vda"]["present"],
                      weights["vda_code"]["present"], weights["face_landmarker"]["present"]])
    report["engines"] = {
        "prepare": prepare_ok,
        # pose modes; the two .task files are also fetched on first use (Google's official MediaPipe storage)
        "pose": bool(prepare_ok and weights["pose_landmarker"]["present"] and weights["hand_landmarker"]["present"]),
        "download": bool(ytdlp),
        "vace": bool(weights["vace"]["present"] and torch_info.get("ok")),
        "faceswap": bool(weights["facefusion"]["present"] and weights["facefusion"]["venv"]),
    }
    report["memory"] = _memory()
    try:
        probe_dir = home if home.exists() else Path.home()
        du = shutil.disk_usage(probe_dir)
        report["disk"] = {"path": str(probe_dir), "free_gb": round(du.free / 2**30, 1),
                          "total_gb": round(du.total / 2**30, 1)}
    except OSError:
        report["disk"] = {"path": str(home), "free_gb": None, "total_gb": None}
    report["ok"] = prepare_ok
    return report


def cmd_doctor(args, progress: Progress) -> int:
    report = doctor_report()
    if args.json:
        _emit_report(progress, report)
        return 0
    yes = lambda b: "ok " if b else "NO "  # noqa: E731
    lines = [
        f"Zeus Effects {report['version']}  home: {report['home']}",
        f"  {yes(report['python']['ok'])} python {report['python']['version']} ({report['python']['executable']})",
        f"  {yes(report['torch']['ok'])} torch {report['torch']['version']}  mps: {report['torch']['mps']}",
        f"  {yes(report['ffmpeg']['ok'])} ffmpeg {report['ffmpeg']['path']}",
        f"  {yes(report['yt_dlp']['ok'])} yt-dlp {report['yt_dlp']['version']} {report['yt_dlp']['path']}",
        f"  {yes(report['vision']['ok'])} Apple Vision (pyobjc) {report['vision']['detail']}",
        f"  {yes(report['mediapipe']['ok'])} mediapipe {report['mediapipe']['version']}",
    ]
    for name, info in report["weights"].items():
        lines.append(f"  {yes(info['present'])} {name}: {info['path']}")
    mem, disk = report["memory"], report["disk"]
    lines.append(f"  RAM available {mem['available_gb']} / {mem['total_gb']} GB, disk free {disk['free_gb']} GB")
    lines.append("  engines: " + ", ".join(f"{k} {'ready' if v else 'not installed'}"
                                            for k, v in report["engines"].items()))
    print("\n".join(lines))
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# prepare
# 392 measured on an M2 Pro 16 GB (64 frames 832x468): 10.6 s, 6.2 GB peak footprint;
# 518 (the model's native size): 31 s, 9.8 GB peak, too much next to other GPU work.
DEPTH_INPUT_SIZE = 392

STAGE_WEIGHTS = {"download": 0.10, "trim": 0.04, "normalize": 0.04, "depth": 0.37, "masks": 0.20,
                 "mesh": 0.10, "pose": 0.25, "composite": 0.15}


def cmd_prepare(args, progress: Progress) -> int:
    from zfx import io as zio
    from zfx.jobs import Job
    from zfx.progress import Timer

    if args.mode not in MODES:
        raise CLIError(f"--mode must be one of {', '.join(MODES)}.", "start")
    try:
        start = zio.sanitize_seconds(args.start, "--start", 0.0)
        duration = zio.sanitize_seconds(args.duration, "--duration", 0.5, MAX_DURATION)
    except zio.MediaError as exc:
        raise CLIError(str(exc), "start")
    input_ref = (args.input or "").strip()
    if not input_ref:
        raise CLIError("--input is required (a video file or a YouTube URL).", "start")
    if not zio.is_url(input_ref):
        p = Path(input_ref).expanduser()
        if not p.is_file():
            raise CLIError(f"Input file not found: {p}", "start")
        input_ref = str(p.resolve())

    want_motion = args.mode in POSE_MODES or bool(getattr(args, "motion", False))
    want_mesh = args.mode in MESH_MODES or want_motion     # the face is part of the motion asset
    want_depth = args.mode != "pose"
    stages = [s for s in STAGE_WEIGHTS
              if (s != "mesh" or want_mesh) and (s != "pose" or want_motion) and (s != "depth" or want_depth)
              and (s != "download" or zio.is_url(input_ref))]
    total_w = sum(STAGE_WEIGHTS[s] for s in stages)
    offsets, acc = {}, 0.0
    for s in stages:
        offsets[s] = acc
        acc += STAGE_WEIGHTS[s]

    def say(stage: str, frac: float, message: str = "") -> None:
        overall = (offsets.get(stage, 0.0) + STAGE_WEIGHTS.get(stage, 0.0) * max(0.0, min(1.0, frac))) / total_w
        progress.update(stage, frac, message, overall=round(overall, 4))

    job = Job.create(input_ref, start, duration, args.mode, args.job)
    progress.log(f"prepare job {job.dir} ({args.mode}, {input_ref} @ {start:g}s for {duration:g}s)")
    say("download" if zio.is_url(input_ref) else "trim", 0.0, f"job {job.id}")
    stage = "download"
    warnings_out: list[str] = []
    try:
        cookies = args.cookies
        if not cookies and zio.is_url(input_ref):
            default = Path.home() / "Library" / "Application Support" / "YouTube Zeus" / "youtube-cookies.txt"
            cookies = str(default) if default.is_file() else None
        info = zio.prepare_input(input_ref, start, duration, job.dir, cookies, say)
        for k, v in info["timings"].items():
            job.set_timing(k, v, save=False)
        src, work = info["source"], info["work"]
        job["source_info"] = src
        job["fps"] = src["fps"]
        job["source_fps"] = src["fps_rational"]
        job["work_fps"] = zfx.WORK_FPS
        job["size"] = [work["width"], work["height"]]
        job["source_size"] = [src["width"], src["height"]]
        job["frames"] = work["frames"]
        job["duration"] = round(min(duration, src["duration"]), 3)
        job["has_audio"] = bool(info["audio"])
        job.set_artifact("source", job.dir / "source.mp4", save=False)
        if info["audio"]:
            job.set_artifact("audio", job.dir / "audio.m4a", save=False)
        else:
            warnings_out.append("The clip has no audio track.")
        job.set_artifact("work", job.dir / "work.mp4", save=False)
        job.save()

        if want_depth:
            stage = "depth"
            from zfx.depth import run_depth
            with Timer() as t:
                say("depth", 0.0, "Video-Depth-Anything-Small")
                dstats = run_depth(job.dir / "work.mp4", job.dir / "depth.mp4", lambda f, m: say("depth", f, m),
                                   device=args.device, input_size=args.depth_size)
            job.set_artifact("depth", job.dir / "depth.mp4", save=False)
            job["depth_info"] = dstats
            job.set_timing("depth", t.seconds)
            say("depth", 1.0, f"depth.mp4 in {t.seconds:.1f} s on {dstats['device']}")

        stage = "masks"
        from zfx.masks import run_masks
        with Timer() as t:
            mstats = run_masks(job.dir / "work.mp4", job.dir / "mask.mp4", lambda f, m: say("masks", f, m),
                               quality=args.mask_quality, min_person_ratio=args.min_person_ratio)
        job.set_artifact("mask", job.dir / "mask.mp4", save=False)
        job["mask_info"] = mstats
        job.set_timing("masks", t.seconds)
        if mstats["person_frames"] == 0:
            warnings_out.append("No person found in any frame (mask is empty).")
        say("masks", 1.0, f"mask.mp4 in {t.seconds:.1f} s")

        landmarks = None
        if want_mesh:
            stage = "mesh"
            from zfx.mesh import detect_landmarks, write_landmarks
            with Timer() as t:
                landmarks = detect_landmarks(job.dir / "work.mp4", lambda f, m: say("mesh", f, m),
                                             mask_path=job.dir / "mask.mp4")
                write_landmarks(landmarks, job.dir / "face-landmarks.json")
            job.set_artifact("landmarks", job.dir / "face-landmarks.json", save=False)
            job["mesh_info"] = {"faces_found": landmarks["faces_found"], "frames": len(landmarks["frames"]),
                                "search": landmarks.get("search"), "crop": landmarks.get("crop")}
            job.set_timing("mesh", t.seconds)
            if landmarks["faces_found"] == 0:
                warnings_out.append("No face found for the mesh (control has no mesh lines)." if args.mode in
                                    ("mesh", "depth_mesh") else "No face found (motion.json has no face).")
            say("mesh", 1.0, f"face in {landmarks['faces_found']}/{len(landmarks['frames'])} frames, "
                             f"{t.seconds:.1f} s")

        motion = None
        if want_motion:
            stage = "pose"
            from zfx.bvh import BVHError, write_bvh
            from zfx.pose import detect_motion, summarize, write_motion, write_pose_video
            with Timer() as t:
                source_ref = {"job": job.id, "input": input_ref, "start": start, "duration": job.get("duration"),
                              "video": "work.mp4", "source_fps": job.get("source_fps")}
                motion = detect_motion(job.dir / "work.mp4", lambda f, m: say("pose", 0.85 * f, m),
                                       mask_path=job.dir / "mask.mp4", face=landmarks, source=source_ref)
                write_motion(motion, job.dir / "motion.json")
                job.set_artifact("motion", job.dir / "motion.json", save=False)
                pstats = write_pose_video(motion, job.dir / "pose.mp4", lambda f, m: say("pose", 0.85 + 0.1 * f, m))
                job.set_artifact("pose", job.dir / "pose.mp4", save=False)
                bvh_info = None
                try:
                    bvh_info = write_bvh(motion, job.dir / "motion.bvh")
                    job.set_artifact("bvh", job.dir / "motion.bvh", save=False)
                except BVHError as exc:
                    warnings_out.append(str(exc))
            summary = summarize(motion)
            job["motion_info"] = {k: summary[k] for k in ("frames", "body_frames", "left_hand_frames",
                                                          "right_hand_frames", "face_frames", "average_visibility",
                                                          "limbs_tracked", "search")}
            job["motion_info"]["pose_video"] = pstats
            if bvh_info:
                job["motion_info"]["bvh"] = {k: bvh_info[k] for k in ("frames", "joints", "channels",
                                                                        "held_frames", "root")}
            job.set_timing("pose", t.seconds)
            if summary["body_frames"] == 0:
                warnings_out.append("No body found in any frame (motion.json is empty, pose.mp4 is black).")
            say("pose", 1.0, f"body in {summary['body_frames']}/{summary['frames']} frames, hands L "
                             f"{summary['left_hand_frames']} R {summary['right_hand_frames']}, {t.seconds:.1f} s")

        stage = "composite"
        from zfx.composite import build_control
        with Timer() as t:
            cstats = build_control(job.dir / "work.mp4", job.dir / "depth.mp4" if want_depth else None,
                                   job.dir / "mask.mp4", job.dir / "control.mp4", args.mode,
                                   lambda f, m: say("composite", f, m), landmarks=landmarks,
                                   preview_path=job.dir / "preview.jpg", motion=motion)
        job.set_artifact("control", job.dir / "control.mp4", save=False)
        if (job.dir / "preview.jpg").is_file():
            job.set_artifact("preview", job.dir / "preview.jpg", save=False)
        job.set_timing("composite", t.seconds, save=False)
        job["composite_info"] = cstats
        job["review"] = None
        job["warnings"] = warnings_out
        job["error"] = None
        job["status"] = "prepared"
        job.set_timing("total", sum(v for k, v in job["timings"].items() if k != "total"), save=False)
        job.save()
        say("composite", 1.0, f"control.mp4 + preview.jpg in {t.seconds:.1f} s")
    except Exception as exc:
        if stage == "download":  # io covers download, trim and normalize
            stage = progress.stage if progress.stage in ("download", "trim", "normalize") else stage
        job["status"] = "failed"
        job["error"] = str(exc)
        job["failed_stage"] = stage
        try:
            job.save()
        except Exception:
            pass
        if isinstance(exc, CLIError):
            raise
        progress.logger.exception("prepare failed at %s", stage)
        raise CLIError(_plain(exc), stage)

    result = {
        "id": job.id,
        "mode": job.mode,
        "frames": job.get("frames"),
        "fps": job.get("fps"),
        "work_fps": zfx.WORK_FPS,
        "size": job.get("size"),
        "duration": job.get("duration"),
        "has_audio": job.get("has_audio"),
        "artifacts": {name: str(job.path(name)) for name in job["artifacts"]},
        "timings": job.get("timings"),
        "warnings": warnings_out,
        "next": "review preview.jpg, then approve --job",
    }
    if job.get("motion_info"):
        result["motion"] = job["motion_info"]
    progress.done(job.dir, result)
    return 0


def _plain(exc: BaseException) -> str:
    msg = str(exc).strip() or type(exc).__name__
    if isinstance(exc, MemoryError) or "out of memory" in msg.lower() or "MPS backend out of memory" in msg:
        return "Not enough memory for this step. Close other heavy apps or use a shorter clip. (" + msg[:200] + ")"
    return msg if len(msg) < 600 else msg[:600] + "..."


# ---------------------------------------------------------------------------
# approve / generate / status / list
def cmd_approve(args, progress: Progress) -> int:
    from zfx.jobs import Job, JobError
    from zfx.review import ReviewError, approve
    try:
        job = Job.load(args.job)
        progress.update("approve", 0.1, "hashing prepared files")
        review = approve(job)
    except (JobError, ReviewError) as exc:
        raise CLIError(str(exc), "approve")
    progress.update("approve", 1.0, f"approved {len(review['hashes'])} files")
    progress.done(job.dir, {"id": job.id, "status": job.get("status"), "review": review})
    return 0


def cmd_generate(args, progress: Progress) -> int:
    from zfx.jobs import Job, JobError
    from zfx.review import ReviewError, verify
    try:
        job = Job.load(args.job)
    except JobError as exc:
        raise CLIError(str(exc), "generate")
    if not args.consent:
        raise CLIError("Refusing to generate without --consent: confirm the reference photos show you "
                       "or people who agreed to this.", "consent")
    try:
        verify(job)
    except ReviewError as exc:
        raise CLIError(str(exc), "review")
    refs = []
    for ref in args.ref or []:
        p = Path(ref).expanduser()
        if not p.is_file():
            raise CLIError(f"Reference photo not found: {p}", "generate")
        refs.append(str(p.resolve()))
    if args.ref_pack:
        from zfx.facepack import FacePackError, load_pack
        try:
            args.ref_pack = str(load_pack(args.ref_pack).dir)   # refuses early: no consent, paths leaving the pack
        except FacePackError as exc:
            raise CLIError(str(exc), "ref-pack")
    if not refs and not args.ref_pack:
        raise CLIError("Add at least one reference photo with --ref, or a face pack with --ref-pack.", "generate")
    args.ref = refs
    try:
        from zfx.generate import run_generate
    except ImportError as exc:
        raise CLIError(f"The generation engines are not installed in this copy (zfx.generate: {exc}).", "generate")
    progress.update("generate", 0.0, f"engine {args.engine}")
    result = run_generate(args, job, progress)
    if getattr(progress, "finished", False):
        return 1 if getattr(progress, "failed", False) else 0
    progress.done(job.dir, result if isinstance(result, dict) else {"result": result})
    return 0


def cmd_facepack(args, progress: Progress) -> int:
    """Validate a Cinema 101 face pack and show which frames generate --ref-pack would use."""
    from zfx.facepack import MAX_REFS, FacePackError, describe, load_pack, pick_refs
    try:
        pack = load_pack(args.pack)
        picks = pick_refs(pack, max_refs=args.max_refs if args.max_refs is not None else MAX_REFS)
    except FacePackError as exc:
        raise CLIError(str(exc), "facepack")
    report = describe(pack, picks)
    if not picks:
        raise CLIError(f"No usable frame in the face pack {pack.name} ({report['frames']} listed, "
                       f"{report['present']} present, {report['blurry']} too blurry).", "facepack")
    if args.json:
        _emit_report(progress, report)
        return 0
    who = f" of {report['person']}" if report["person"] else ""
    lines = [f"Face pack {report['name']}{who} (version {report['version']}, created {report['created'] or '?'})",
             f"  {report['frames']} frames listed, {report['present']} present, {report['usable']} usable, "
             f"{report['blurry']} under the sharpness floor ({report['min_sharpness']:.0%} of the sharpest)"]
    for item in report["skipped"]:
        lines.append(f"  skipped {item.get('file') or '?'}: {item['reason']}")
    lines.append(f"  generate --ref-pack would use {len(picks)}:")
    for i, pick in enumerate(picks, 1):
        lines.append(f"  {i}. {pick['file']}  {pick['why']}")
    print("\n".join(lines))
    progress.finished = True
    return 0


def motion_report(job) -> dict:
    """Summary of a job's motion capture (motion.json, motion.bvh, pose.mp4) for agents."""
    from zfx.bvh import BVHError, describe_bvh
    from zfx.pose import PoseError, load_motion, summarize
    path = job.artifact("motion") or (job.dir / "motion.json" if (job.dir / "motion.json").is_file() else None)
    if path is None:
        raise CLIError(f"Job {job.id} has no motion capture. Prepare it with --mode pose or depth_pose "
                       "(or --motion with any mode).", "motion")
    try:
        motion = load_motion(path)
    except (PoseError, OSError, ValueError) as exc:
        raise CLIError(f"Could not read {path.name}: {exc}", "motion")
    report = summarize(motion)
    report["job"] = job.id
    report["files"] = {"motion": str(path)}
    for name, file in (("pose", "pose.mp4"), ("bvh", "motion.bvh")):
        p = job.artifact(name) or (job.dir / file if (job.dir / file).is_file() else None)
        report["files"][name] = str(p) if p else None
    if report["files"]["bvh"]:
        try:
            report["bvh"] = describe_bvh(Path(report["files"]["bvh"]))
        except (BVHError, OSError, ValueError) as exc:
            report["bvh"] = {"error": str(exc)}
    return report


def cmd_motion(args, progress: Progress) -> int:
    from zfx.jobs import Job, JobError
    try:
        job = Job.load(args.job)
    except JobError as exc:
        raise CLIError(str(exc), "motion")
    report = motion_report(job)
    if args.json:
        _emit_report(progress, report, job.dir)
        return 0
    n = report["frames"] or 0

    def pct(k):
        return f"{k}/{n} ({(100.0 * k / n) if n else 0:.0f} %)"

    vis = report["average_visibility"]
    limbs = report["limbs_fraction"] or {}
    lines = [f"Motion of job {report['job']}: {n} frames at {report['fps']} fps "
             f"({report['duration']} s, {report['size'][0]}x{report['size'][1]})",
             f"  body        {pct(report['body_frames'])}",
             f"  left hand   {pct(report['left_hand_frames'])}",
             f"  right hand  {pct(report['right_hand_frames'])}",
             f"  face        {pct(report['face_frames'])}",
             f"  average visibility {vis if vis is not None else '-'}",
             "  limbs tracked (share of body frames with every point visible): "
             + (", ".join(f"{k} {v:.0%}" for k, v in limbs.items() if v > 0) or "none")]
    if report.get("bvh") and "joints" in report["bvh"]:
        b = report["bvh"]
        lines.append(f"  motion.bvh  {len(b['joints'])} joints, {b['channels']} channels, {b['frames']} frames "
                     f"at {b['fps']} fps")
    for name, path in report["files"].items():
        lines.append(f"  {name:6}  {path or '-'}")
    print("\n".join(lines))
    progress.finished = True
    return 0


def cmd_status(args, progress: Progress) -> int:
    from zfx.jobs import Job, JobError
    try:
        job = Job.load(args.job)
    except JobError as exc:
        raise CLIError(str(exc), "status")
    if args.json:
        _emit_report(progress, job.data, job.dir)
    else:
        print(json.dumps(job.data, indent=2, ensure_ascii=False))
        progress.finished = True
    return 0


def cmd_list(args, progress: Progress) -> int:
    from zfx.jobs import list_jobs
    jobs = [j.summary() for j in list_jobs()]
    if args.json:
        _emit_report(progress, {"jobs": jobs, "count": len(jobs)})
    else:
        if not jobs:
            print("No jobs yet.")
        for j in jobs:
            print(f"{j['created'] or '?':25}  {j['status'] or '?':9}  {j['mode'] or '?':10}  "
                  f"{'approved' if j['approved'] else '        '}  {j['dir']}")
        progress.finished = True
    return 0


def cmd_voice(args, progress: Progress) -> int:
    """Change who speaks in the job's audio, same words/timing/emotion (zfx/voice.py): final-voice.mp4."""
    if not args.consent:
        raise CLIError("Refusing to change the voice without --consent: confirm the voice is yours or of someone "
                       "who agreed to this.", "consent")
    try:
        from zfx.voice import VoiceError, run_voice
    except ImportError as exc:
        raise CLIError(f"The voice step is not available in this copy (zfx.voice: {exc}).", "voice")
    try:
        result = run_voice(args, args.job, progress)
    except VoiceError as exc:
        raise CLIError(str(exc), progress.stage if progress.stage not in ("start", None) else "voice")
    progress.done(Path(result["final_voice"]).parent, result)
    return 0


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zeus_effects", description="YouTube Zeus Effects (free, local only).")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logs on stderr")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check tools, weights, memory and disk (never downloads)")
    d.add_argument("--json", action="store_true")

    pr = sub.add_parser("prepare", help="download/trim, normalize, depth, masks, mesh, pose, composite")
    pr.add_argument("--input", required=True, help="video file or YouTube URL")
    pr.add_argument("--start", type=float, default=0.0, help="start second (default 0)")
    pr.add_argument("--duration", type=float, default=5.0, help=f"seconds, at most {MAX_DURATION:g} (default 5)")
    pr.add_argument("--mode", required=True, choices=MODES)
    pr.add_argument("--job", help="job folder (default: Jobs/<new id>)")
    pr.add_argument("--cookies", help="cookies.txt for yt-dlp (default: the Zeus youtube-cookies.txt if present)")
    pr.add_argument("--device", choices=("mps", "cpu"), help=argparse.SUPPRESS)
    pr.add_argument("--depth-size", type=int, default=DEPTH_INPUT_SIZE,
                    help=f"depth model input (short side, multiple of 14; default {DEPTH_INPUT_SIZE}, 518 = sharper "
                         "but ~3x slower and ~10 GB peak)")
    pr.add_argument("--mask-quality", choices=("accurate", "balanced", "fast"), default="accurate",
                    help=argparse.SUPPRESS)
    pr.add_argument("--min-person-ratio", type=float, default=0.25,
                    help="drop mask blobs smaller than this x the largest person, e.g. faces on screens "
                         "(default 0.25; 0 keeps every blob)")
    pr.add_argument("--motion", action="store_true",
                    help="also save the whole-body motion capture (motion.json, motion.bvh, pose.mp4) in the "
                         "depth/mesh modes (always on in pose and depth_pose)")

    a = sub.add_parser("approve", help="record the review (SHA-256 of every prepared artifact)")
    a.add_argument("--job", required=True)

    g = sub.add_parser("generate", help="run an engine on an approved job (needs --consent)")
    g.add_argument("--job", required=True)
    g.add_argument("--engine", required=True, choices=("vace", "faceswap"))
    g.add_argument("--ref", action="append", default=None, help="reference photo (repeat for more)")
    g.add_argument("--ref-pack", metavar="DIR", default=None,
                   help="a Cinema 101 face pack (.c101face folder, effects/FACE-PACK.md): its sharpest frontal, "
                        "±20°, ±40° and smile frames are used (up to 5; 2 for vace). Combines with --ref.")
    g.add_argument("--prompt", default="", help="extra prompt text (vace)")
    g.add_argument("--consent", action="store_true",
                   help="the reference photos show me or people who agreed")
    g.add_argument("--frames", type=int, default=None,
                   help="vace: frames per window (4k+1, default 33); the whole clip runs in overlapping windows")
    g.add_argument("--steps", type=int, default=None, help="diffusion steps (engine default)")
    g.add_argument("--size", choices=("480p", "360p"), default=None,
                   help="vace size (engine default; see effects/ENGINES.md for times on a 16 GB Mac)")
    g.add_argument("--seed", type=int, default=None)
    g.add_argument("--upscale", choices=("fast", "best", "lanczos"), default=None,
                   help="vace: how the generated crop is upscaled to the source resolution (fast = Real-ESRGAN "
                        "animevideov3, best = realesrgan-x4plus, lanczos = no model; default fast)")
    g.add_argument("--enhance", action="store_true",
                   help="faceswap: also sharpen the face (GFPGAN), about 20x slower")
    g.add_argument("--quality", choices=("natural", "fast"), default=None,
                   help="faceswap: natural (default: pixel boost 512, the real inside of the mouth kept) or fast")

    fp = sub.add_parser("facepack", help="check a face pack (.c101face) and show the frames generate would pick")
    fp.add_argument("--pack", required=True, metavar="DIR", help="the .c101face folder")
    fp.add_argument("--max-refs", type=int, default=None, help="frames to pick (default 5)")
    fp.add_argument("--json", action="store_true")

    mo = sub.add_parser("motion", help="summary of a job's motion capture (body, hands, face, limbs, BVH)")
    mo.add_argument("--job", required=True)
    mo.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="print job.json")
    s.add_argument("--job", required=True)
    s.add_argument("--json", action="store_true")

    li = sub.add_parser("list", help="jobs under Jobs/, newest first")
    li.add_argument("--json", action="store_true")

    vo = sub.add_parser("voice", help="change who speaks, same words, timing and emotion (needs --consent): "
                                      "audio-voice.m4a + final-voice.mp4")
    vo.add_argument("--job", required=True)
    vo.add_argument("--voice", action="append", default=None, metavar="FILE",
                    help="a recording of the new voice (audio or video; repeat for more; 10-30 s of clear speech)")
    vo.add_argument("--consent", action="store_true", help="the voice is mine or of someone who agreed")
    vo.add_argument("--keep-music", action=argparse.BooleanOptionalAction, default=True,
                    help="put the original music and ambience back under the new voice (default; "
                         "--no-keep-music = the new voice alone)")
    vo.add_argument("--grade", choices=("match", "none"), default="match",
                    help="match = EQ, de-ess and EBU R128 loudness matched to the original voice (default)")
    vo.add_argument("--deess", choices=("auto", "on", "off"), default="auto",
                    help="auto = only when the new voice is hissier than the original (default)")
    vo.add_argument("--check", choices=("auto", "on", "off"), default="auto",
                    help="whisper-cli transcripts before/after (auto = when whisper-cli and its model exist)")
    vo.add_argument("--engine", choices=("chatterbox",), default="chatterbox",
                    help="voice conversion engine (Chatterbox VC, Resemble AI, MIT)")
    vo.add_argument("--device", choices=("auto", "mps", "cpu"), default="auto", help="where the models run")
    vo.add_argument("--steps", type=int, default=None, help="Chatterbox flow-matching steps (default 10)")
    vo.add_argument("--seed", type=int, default=None,
                    help="seed of the conversion (default 0); another seed = another take of the same voice")
    return p


COMMANDS = {"doctor": cmd_doctor, "prepare": cmd_prepare, "approve": cmd_approve, "generate": cmd_generate,
            "facepack": cmd_facepack, "motion": cmd_motion, "status": cmd_status, "list": cmd_list,
            "voice": cmd_voice}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    progress = Progress()
    started = time.monotonic()
    try:
        code = COMMANDS[args.command](args, progress)
    except CLIError as exc:
        progress.error(str(exc), exc.stage)
        return 1
    except KeyboardInterrupt:
        progress.error("Cancelled.", progress.stage)
        return 130
    except Exception as exc:  # last resort: still one plain JSON error line
        progress.logger.exception("%s failed", args.command)
        if not progress.finished:
            progress.error(_plain(exc), progress.stage)
        return 1
    progress.log(f"{args.command} finished in {time.monotonic() - started:.1f} s")
    return code


if __name__ == "__main__":
    sys.exit(main())
