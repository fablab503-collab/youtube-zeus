"""Mac assist by shared folder (MAKE-IT-ME.md): The Swap on the iPhone writes a job folder, this Mac does it.

    zeus_effects.py watch [--folder DIR …] [--once] [--poll S] [--clean-days N]

No network service: nothing listens on a port. The watcher polls the folders it is given (default: iCloud Drive ›
The Swap › Jobs and /Volumes/Volume1/The Swap Jobs, the ones that exist) for `<yyyyMMdd-HHmmss>-<name>.swapjob/`:

    clip.mp4 | clip.txt           the clip, or a YouTube link (+ start/duration: in job.json, or `start: S` lines)
    me.c101face/ | photos/        who (a face pack, or photos); a `self-video.*` file is accepted too
    voice.m4a                     optional voice sample (voice.wav / voice.mp3 also accepted)
    job.json                      {quality, hat, voice, consent: true, device, app, version, start, duration, hair, person}
    request                       empty marker, written LAST by the phone: a folder without it is never read

The Mac, on `request`: status.json every few seconds ({step, progress, message, started, updated, error, …}), then
result.mp4 (final-voice.mp4 or final.mp4), compare.mp4, preview.jpg, and `done` (marker) last. A `cancel` marker
from the phone stops the job (`cancelled` marker back). Errors go into status.json (`error`, `finished`); such a job
is not retried unless the phone removes status.json. One job at a time, oldest first; while another effect runs on
this Mac (generate, makeme, prepare, voice) the job waits with step "waiting". Jobs older than --clean-days are
removed (off unless given). The Zeus job itself lives in Jobs/<swapjob name> so the app lists it like any other.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

EXT = ".swapjob"
REQUEST, CANCEL, DONE, CANCELLED = "request", "cancel", "done", "cancelled"
STATUS = "status.json"
RESULT, COMPARE, PREVIEW = "result.mp4", "compare.mp4", "preview.jpg"
CLIP_FILES = ("clip.mp4", "clip.mov", "clip.m4v")
CLIP_LINK = "clip.txt"
PACK = "me.c101face"
PHOTOS = "photos"
VOICE_FILES = ("voice.m4a", "voice.wav", "voice.mp3", "voice.aac", "voice.caf")
SELF_VIDEO_STEMS = ("self-video", "self", "me")
VIDEO_EXT = (".mp4", ".mov", ".m4v")
PHOTO_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")
POLL_S = 3.0
STATUS_EVERY_S = 2.0
BUSY_POLL_S = 10.0
MAX_WAIT_S = 6 * 3600
LOCK_NAME = "watch.lock"
# ZEUS_SWAP_BUSY_CHECK=0 skips the "another engine process is running" wait (tests)
BUSY_CHECK = os.environ.get("ZEUS_SWAP_BUSY_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")

Progress = Callable[[str, float, str], None]


class SwapJobError(RuntimeError):
    """Plain-English problem with a .swapjob folder."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- folders
def default_folders() -> list[Path]:
    home = Path.home()
    candidates = [home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "The Swap" / "Jobs",
                  Path("/Volumes/Volume1/The Swap Jobs")]
    env = os.environ.get("ZEUS_SWAP_FOLDERS", "").strip()
    if env:
        candidates = [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    return [p for p in candidates if p.is_dir()]


def pending_jobs(folders: Sequence[Path]) -> list[Path]:
    """`.swapjob` folders with a `request` marker that are not done, cancelled or failed; oldest first by name."""
    found: list[Path] = []
    for folder in folders:
        try:
            children = list(Path(folder).iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not child.name.lower().endswith(EXT) or child.name.startswith("."):
                continue
            if not (child / REQUEST).exists():
                continue
            if (child / DONE).exists() or (child / CANCELLED).exists():
                continue
            if (child / CANCEL).exists() and not (child / STATUS).exists():
                continue  # cancelled before it started: nothing to do (the phone may clean it)
            status = read_status(child)
            if status.get("error") and status.get("finished"):
                continue
            if not files_complete(child):
                continue  # iCloud may sync `request` before the big files: wait until every listed file is whole
            found.append(child)
    return sorted(found, key=lambda p: p.name)


def files_complete(swapjob: Path) -> bool:
    """True when every file listed in job.json's `files` {relative path: bytes} exists with that size.
    Jobs without a `files` map (hand-made, or an older phone app) are always considered complete."""
    try:
        data = json.loads((Path(swapjob) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return True
    for rel, size in files.items():
        if not isinstance(rel, str) or rel in ("...", "") or not isinstance(size, (int, float)) or isinstance(size, bool):
            continue
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            continue  # never look outside the job folder
        path = Path(swapjob) / rel
        try:
            if path.stat().st_size != int(size):
                return False
        except OSError:
            return False
    return True


def read_status(swapjob: Path) -> dict:
    try:
        data = json.loads((Path(swapjob) / STATUS).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(swapjob: Path, status: dict) -> None:
    """status.json, written atomically (the phone never sees a half file)."""
    swapjob = Path(swapjob)
    status = dict(status)
    status["updated"] = _now_iso()
    tmp = swapjob / (".status.json." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(status, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, swapjob / STATUS)


def touch(path: Path) -> None:
    Path(path).write_bytes(b"")


def copy_in(src: Path, dst: Path) -> None:
    """Copy through a temporary name, then rename: the final name appears complete."""
    tmp = dst.with_name("." + dst.name + ".partial")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


# ----------------------------------------------------------------------------- reading a job folder
def _bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true", "on", "1", "remove"):
            return True
        if v in ("no", "false", "off", "0", "keep", ""):
            return False
    return default


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_clip_link(text: str) -> dict:
    """clip.txt: the link on the first line; optional `start: S` / `duration: D` (also `start=S`, or `link S D`)."""
    out: dict = {}
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return out
    first = lines[0].split()
    out["input"] = first[0]
    if len(first) >= 2 and _number(first[1]) is not None:
        out["start"] = _number(first[1])
    if len(first) >= 3 and _number(first[2]) is not None:
        out["duration"] = _number(first[2])
    for ln in lines[1:]:
        for sep in (":", "="):
            if sep in ln:
                k, v = ln.split(sep, 1)
                k = k.strip().lower()
                if k in ("start", "duration") and _number(v.strip()) is not None:
                    out[k] = _number(v.strip())
                break
    return out


def read_job(swapjob: Path) -> dict:
    """What the phone asked for: {input, start, duration, me: {kind, path|paths}, voice, quality, hat, voice_on,
    consent, device, app, version, hair, person, name}. Raises SwapJobError when something is missing."""
    swapjob = Path(swapjob)
    job: dict = {"name": swapjob.name[: -len(EXT)] if swapjob.name.lower().endswith(EXT) else swapjob.name}
    options: dict = {}
    jpath = swapjob / "job.json"
    if jpath.is_file():
        try:
            options = json.loads(jpath.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SwapJobError(f"job.json could not be read: {exc}")
        if not isinstance(options, dict):
            raise SwapJobError("job.json is not a JSON object.")
        # The Swap (iPhone) nests quality/hat/voice under "options" (effects/MAKE-IT-ME.md appendix); flat keys
        # are accepted too, nested ones win.
        nested = options.get("options")
        if isinstance(nested, dict):
            options = {**options, **nested}
    clip = next((swapjob / n for n in CLIP_FILES if (swapjob / n).is_file()), None)
    if clip is not None:
        job["input"] = str(clip)
    elif (swapjob / CLIP_LINK).is_file():
        link = parse_clip_link((swapjob / CLIP_LINK).read_text(encoding="utf-8", errors="replace"))
        if not link.get("input"):
            raise SwapJobError("clip.txt has no link.")
        job.update(link)
    else:
        raise SwapJobError("No clip: add clip.mp4 or clip.txt (a YouTube link).")
    for key in ("start", "duration"):
        v = _number(options.get(key))
        if v is not None:
            job[key] = v
    job.setdefault("start", 0.0)
    job.setdefault("duration", None)

    pack = swapjob / PACK
    photos_dir = swapjob / PHOTOS
    self_video = next((p for p in sorted(swapjob.iterdir()) if p.is_file() and p.suffix.lower() in VIDEO_EXT
                       and p.stem.lower() in SELF_VIDEO_STEMS), None)
    if pack.is_dir():
        job["me"] = {"kind": "face-pack", "path": str(pack)}
    elif self_video is not None:
        job["me"] = {"kind": "self-video", "path": str(self_video)}
    elif photos_dir.is_dir():
        photos = sorted(p for p in photos_dir.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_EXT
                        and not p.name.startswith("."))
        if not photos:
            raise SwapJobError("photos/ is empty.")
        job["me"] = {"kind": "photos", "paths": [str(p) for p in photos[:4]]}
    else:
        raise SwapJobError("No reference: add me.c101face/ (a face pack), photos/, or a self-video.")
    voice = next((swapjob / n for n in VOICE_FILES if (swapjob / n).is_file()), None)
    job["voice"] = str(voice) if voice else None
    job["quality"] = str(options.get("quality") or "natural").lower()
    job["hat"] = "remove" if _bool(options.get("hat"), False) else "keep"
    job["voice_on"] = _bool(options.get("voice"), voice is not None)
    job["consent"] = options.get("consent") is True
    for key in ("device", "app", "version", "hair", "person"):
        v = options.get(key)
        if isinstance(v, bool) or v is None:
            job[key] = None
        elif isinstance(v, (int, float)):
            job[key] = str(v)                      # "version": 1 is fine too
        else:
            job[key] = v.strip() if isinstance(v, str) and v.strip() else None
    return job


# ----------------------------------------------------------------------------- the makeme command
def makeme_command(job: dict, zeus_job_dir: Path, swapjob: Path) -> list[str]:
    """The makeme command line for a job (ZEUS_MAKEME_COMMAND overrides the interpreter + script, for tests)."""
    override = os.environ.get("ZEUS_MAKEME_COMMAND", "").strip()
    if override:
        try:
            head = json.loads(override) if override.startswith("[") else None
        except ValueError:
            head = None
        head = head or override.split()
    else:
        head = [sys.executable, str(Path(__file__).resolve().parents[1] / "zeus_effects.py")]
    cmd = list(head) + ["makeme", "--input", job["input"], "--start", f"{float(job.get('start') or 0):g}"]
    if job.get("duration") is not None:
        cmd += ["--duration", f"{float(job['duration']):g}"]
    me = job["me"]
    if me["kind"] == "face-pack":
        cmd += ["--ref-pack", me["path"]]
    elif me["kind"] == "self-video":
        cmd += ["--self-video", me["path"]]
    else:
        for p in me["paths"]:
            cmd += ["--ref", p]
    if job.get("voice"):
        cmd += ["--voice", job["voice"]]
    if not job.get("voice_on"):
        cmd.append("--no-voice")
    cmd += ["--quality", job.get("quality") or "natural", "--hat", job.get("hat") or "keep"]
    if job.get("hair"):
        cmd += ["--hair", job["hair"]]
    if job.get("person"):
        cmd += ["--person", job["person"]]
    cmd += ["--job", str(zeus_job_dir), "--swapjob", str(swapjob)]
    if job.get("consent"):
        cmd.append("--consent")
    return cmd


# ----------------------------------------------------------------------------- is the Mac busy?
BUSY_PATTERNS = ("zeus_effects.py generate", "zeus_effects.py makeme", "zeus_effects.py prepare",
                 "zeus_effects.py voice", "zfx.engines.vace", "facefusion.py headless-run", "zfx.engines.vc_")


def busy_processes(exclude: Sequence[int] = ()) -> list[str]:
    """Other heavy engine processes on this Mac (one heavy job at a time, 16 GB)."""
    try:
        out = subprocess.run(["/bin/ps", "-Ao", "pid=,args="], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    skip = set(int(p) for p in exclude) | {os.getpid()}
    found = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        args = parts[1]
        if pid in skip or "python" not in args.lower():
            continue
        if any(pat in args for pat in BUSY_PATTERNS):
            found.append(f"{pid} {args[:120]}")
    return found


# ----------------------------------------------------------------------------- running one job
def _reader(proc: subprocess.Popen, lines: list, lock: threading.Lock) -> None:
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            with lock:
                lines.append(raw)
    except Exception:  # noqa: BLE001
        pass


def _kill_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.2)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=10)


def preview_frame(video: Path, out: Path) -> bool:
    """One frame from the middle of the result as preview.jpg."""
    from .io import probe, tool
    try:
        dur = float(probe(video).get("duration") or 0.0)
        at = max(0.0, dur / 2.0)
        tmp = out.with_name("." + out.name + ".partial.jpg")
        subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", str(video), "-frames:v", "1",
                        "-q:v", "3", str(tmp)], check=True, capture_output=True, timeout=120)
        os.replace(tmp, out)
        return True
    except Exception:  # noqa: BLE001
        return False


def deliver(swapjob: Path, zeus_job_dir: Path, result_info: dict) -> dict:
    """result.mp4, compare.mp4, preview.jpg into the .swapjob, then `done`."""
    zeus_job_dir = Path(zeus_job_dir)
    result = Path(result_info.get("result") or "") if result_info.get("result") else zeus_job_dir / "final.mp4"
    if not result.is_file():
        for name in ("final-voice.mp4", "final.mp4"):
            if (zeus_job_dir / name).is_file():
                result = zeus_job_dir / name
                break
    if not result.is_file():
        raise SwapJobError("The job finished without a result video.")
    copy_in(result, swapjob / RESULT)
    delivered = {"result": RESULT, "from": result.name}
    compare = Path(result_info.get("compare") or "") if result_info.get("compare") else zeus_job_dir / "compare.mp4"
    if compare.is_file():
        copy_in(compare, swapjob / COMPARE)
        delivered["compare"] = COMPARE
    if preview_frame(swapjob / RESULT, swapjob / PREVIEW):
        delivered["preview"] = PREVIEW
    return delivered


def run_job(swapjob: Path, jobs_root: Path, report: Optional[Callable[[dict], None]] = None,
            poll: float = 1.0, status_every: float = STATUS_EVERY_S) -> dict:
    """One .swapjob from `request` to `done` / `cancelled` / error. Returns the final status dict."""
    swapjob = Path(swapjob)
    say = report or (lambda obj: None)
    started = _now_iso()
    status = {"step": "starting", "progress": 0.0, "message": "The Mac picked up the job", "started": started,
              "error": None, "mac": os.uname().nodename, "pid": os.getpid()}
    try:
        job = read_job(swapjob)
    except SwapJobError as exc:
        status.update(step="error", error=str(exc), finished=_now_iso(), message=str(exc))
        write_status(swapjob, status)
        say({"stage": "job", "job": str(swapjob), "status": "error", "message": str(exc)})
        return status
    if not job["consent"]:
        msg = "Refused: job.json must say \"consent\": true (the clip, face and voice are yours or of people who agreed)."
        status.update(step="error", error=msg, finished=_now_iso(), message=msg)
        write_status(swapjob, status)
        say({"stage": "job", "job": str(swapjob), "status": "error", "message": msg})
        return status
    zeus_job_dir = Path(jobs_root) / job["name"]
    status.update(zeus_job=str(zeus_job_dir), device=job.get("device"), app=job.get("app"), version=job.get("version"),
                  options={k: job.get(k) for k in ("quality", "hat", "voice_on", "start", "duration")},
                  steps=[], step="waiting", message="Waiting for the Mac to be free")
    write_status(swapjob, status)
    say({"stage": "job", "job": str(swapjob), "zeus_job": str(zeus_job_dir), "status": "picked",
         "step": "waiting", "progress": 0.0, "message": f"{job['name']}: picked up"})

    # wait while another heavy engine process runs (one heavy job at a time)
    waited = 0.0
    while True:
        if (swapjob / CANCEL).exists():
            status.update(step="cancelled", message="Cancelled before it started", finished=_now_iso())
            write_status(swapjob, status)
            touch(swapjob / CANCELLED)
            say({"stage": "job", "job": str(swapjob), "status": "cancelled", "message": "cancelled"})
            return status
        busy = busy_processes() if BUSY_CHECK else []
        if not busy or waited >= MAX_WAIT_S:
            break
        status.update(step="waiting", message="Another effect is running on the Mac; this job starts after it",
                      busy=busy[:3])
        write_status(swapjob, status)
        say({"stage": "job", "job": str(swapjob), "status": "waiting", "step": "waiting", "progress": 0.0,
             "message": status["message"]})
        time.sleep(BUSY_POLL_S)
        waited += BUSY_POLL_S
    status.pop("busy", None)

    cmd = makeme_command(job, zeus_job_dir, swapjob)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    log_path = swapjob / "mac.log"
    log = open(log_path, "a", encoding="utf-8")
    log.write(f"\n== {started} {' '.join(cmd)}\n")
    log.flush()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, env=env, start_new_session=True,
                                cwd=str(Path(cmd[1]).parent) if len(cmd) > 1 and Path(cmd[1]).is_file() else None)
    except OSError as exc:
        log.close()
        msg = f"Could not start the engine: {exc}"
        status.update(step="error", error=msg, finished=_now_iso(), message=msg)
        write_status(swapjob, status)
        return status
    status.update(step="starting", message="Starting", progress=0.0, pid=proc.pid)
    write_status(swapjob, status)
    lines: list = []
    lock = threading.Lock()
    reader = threading.Thread(target=_reader, args=(proc, lines, lock), daemon=True)
    reader.start()
    last_write = time.monotonic()
    dirty = False
    done_result: Optional[dict] = None
    error: Optional[str] = None
    cancelled = False
    steps_seen: list = []
    try:
        while True:
            with lock:
                batch, lines[:] = list(lines), []
            for raw in batch:
                line = raw.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("done") is True:
                    done_result = obj.get("result") or {}
                    status.update(step="final", progress=1.0, message="Finished; copying the result")
                    dirty = True
                elif obj.get("error"):
                    error = str(obj["error"])
                    status.update(error=error, message=error, step=obj.get("stage") or status.get("step"))
                    dirty = True
                elif obj.get("stage") == "plan":
                    status["steps"] = list(obj.get("steps") or [])
                    status.update(step="plan", message=obj.get("message", ""), progress=0.0)
                    dirty = True
                elif obj.get("stage"):
                    step = obj.get("step") or obj.get("stage")
                    if step not in steps_seen:
                        steps_seen.append(step)
                    status.update(step=step, progress=obj.get("overall", obj.get("progress", 0.0)),
                                  step_progress=obj.get("progress"), message=obj.get("message", ""),
                                  steps_done=steps_seen[:-1])
                    dirty = True
                    say({"stage": "job", "job": str(swapjob), "zeus_job": str(zeus_job_dir), "status": "running",
                         "step": step, "progress": status["progress"], "message": status["message"]})
            if (swapjob / CANCEL).exists() and proc.poll() is None:
                cancelled = True
                status.update(step="cancelling", message="Stopping (cancel from the phone)")
                write_status(swapjob, status)
                _kill_group(proc)
                break
            if proc.poll() is not None and not batch:
                break
            now = time.monotonic()
            if dirty and now - last_write >= status_every or now - last_write >= 15.0:
                write_status(swapjob, status)
                last_write, dirty = now, False
            time.sleep(poll)
        reader.join(timeout=5)
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass
        with lock:
            tail = list(lines)
        for raw in tail:
            line = raw.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("done") is True:
                    done_result = obj.get("result") or {}
                elif obj.get("error"):
                    error = str(obj["error"])
    finally:
        log.close()
    code = proc.returncode
    if cancelled:
        status.update(step="cancelled", message="Cancelled", finished=_now_iso(), exit=code)
        write_status(swapjob, status)
        touch(swapjob / CANCELLED)
        say({"stage": "job", "job": str(swapjob), "zeus_job": str(zeus_job_dir), "status": "cancelled",
             "message": "cancelled"})
        return status
    if code != 0 or done_result is None:
        msg = error or f"The engine stopped (exit {code}). Details: {log_path.name}"
        status.update(step="error", error=msg, message=msg, finished=_now_iso(), exit=code)
        write_status(swapjob, status)
        say({"stage": "job", "job": str(swapjob), "zeus_job": str(zeus_job_dir), "status": "error", "message": msg})
        return status
    try:
        delivered = deliver(swapjob, zeus_job_dir, done_result)
    except (SwapJobError, OSError) as exc:
        msg = f"Could not copy the result: {exc}"
        status.update(step="error", error=msg, message=msg, finished=_now_iso())
        write_status(swapjob, status)
        say({"stage": "job", "job": str(swapjob), "zeus_job": str(zeus_job_dir), "status": "error", "message": msg})
        return status
    status.update(step="done", progress=1.0, message="Done", finished=_now_iso(), error=None, exit=0,
                  result=delivered, comment=done_result.get("comment"), seconds=done_result.get("seconds"),
                  timings=done_result.get("timings"), warnings=done_result.get("warnings"),
                  hat=done_result.get("hat"), steps_done=steps_seen)
    write_status(swapjob, status)
    touch(swapjob / DONE)
    say({"stage": "job", "job": str(swapjob), "zeus_job": str(zeus_job_dir), "status": "done", "progress": 1.0,
         "message": "done", "result": str(swapjob / RESULT)})
    return status


# ----------------------------------------------------------------------------- housekeeping
def clean_old(folders: Sequence[Path], days: float, say: Optional[Callable[[dict], None]] = None) -> list[str]:
    """Remove finished (.swapjob with done/cancelled/error) jobs older than `days` (by the folder's time stamp)."""
    if days <= 0:
        return []
    removed: list[str] = []
    cutoff = time.time() - days * 86400.0
    for folder in folders:
        try:
            children = list(Path(folder).iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not child.name.lower().endswith(EXT):
                continue
            finished = (child / DONE).exists() or (child / CANCELLED).exists() or bool(read_status(child).get("error"))
            if not finished:
                continue
            try:
                stamp = datetime.strptime(child.name[:15], "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                stamp = child.stat().st_mtime
            if stamp < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(str(child))
                if say:
                    say({"stage": "clean", "job": str(child), "message": f"removed, older than {days:g} days"})
    return removed


class WatchLock:
    """One watcher per Effects home (flock on <home>/watch.lock)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self) -> "WatchLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fh.close()
            self.fh = None
            raise SwapJobError("Another `watch` is already running for this Effects folder "
                               f"({self.path}).") from exc
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc) -> None:
        if self.fh:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()


# ----------------------------------------------------------------------------- the loop
def watch(folders: Sequence[Path], once: bool = False, poll: float = POLL_S, clean_days: float = 0.0,
          report: Optional[Callable[[dict], None]] = None, jobs_root: Optional[Path] = None,
          lock_path: Optional[Path] = None, stop: Optional[Callable[[], bool]] = None) -> dict:
    """Poll the folders; run every pending job in turn. --once: one pass over what is pending, then return."""
    from . import effects_home, jobs_dir
    say = report or (lambda obj: None)
    folders = [Path(f) for f in folders]
    jobs_root = Path(jobs_root) if jobs_root else jobs_dir()
    jobs_root.mkdir(parents=True, exist_ok=True)
    summary: dict = {"folders": [str(f) for f in folders], "jobs": [], "cleaned": []}
    if not folders:
        raise SwapJobError("No folder to watch: create iCloud Drive › The Swap › Jobs (or give --folder).")
    with WatchLock(lock_path or effects_home() / LOCK_NAME):
        say({"stage": "watch", "message": f"watching {len(folders)} folder(s)", "folders": summary["folders"],
             "once": once})
        last_clean = 0.0
        while True:
            if clean_days > 0 and time.monotonic() - last_clean > 3600:
                summary["cleaned"] += clean_old(folders, clean_days, say)
                last_clean = time.monotonic()
            pending = pending_jobs(folders)
            for swapjob in pending:
                status = run_job(swapjob, jobs_root, say)
                summary["jobs"].append({"job": str(swapjob), "status": status.get("step"), "error": status.get("error"),
                                        "zeus_job": status.get("zeus_job")})
                if stop and stop():
                    return summary
            if once:
                if not pending:
                    say({"stage": "watch", "message": "nothing pending"})
                return summary
            if stop and stop():
                return summary
            time.sleep(poll)
