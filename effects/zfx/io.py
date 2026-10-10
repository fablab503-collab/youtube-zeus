"""Download, trim, normalize, audio, and raw frame I/O through ffmpeg.

source.mp4  trimmed original: original fps, original audio (copied when possible)
audio.m4a   original audio, stream copy (fallback AAC)
work.mp4    16 fps, long side <= 832, even dims, no audio (what the models see)
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Union

import numpy as np

from . import WORK_FPS, WORK_MAX_SIDE
from .progress import get_logger

PathLike = Union[str, Path]
log = get_logger()

BREW_BIN = "/opt/homebrew/bin"
# Best video up to 1080p + best audio; AAC (m4a) audio preferred so it can be stream-copied
# untouched into source.mp4 / audio.m4a / final.mp4.
YTDLP_FORMAT = "bv*[height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba/b"
# With --force-keyframes-at-cuts yt-dlp lets ffmpeg re-encode for a frame-exact cut: keep the
# audio packets as they are and encode the video near-losslessly (it is trimmed again locally).
YTDLP_FFMPEG_OUT = "ffmpeg_o:-c:a copy -c:v libx264 -crf 14 -preset veryfast -pix_fmt yuv420p"


class MediaError(RuntimeError):
    """Plain-English media problem (download failed, no video stream...)."""


# -- tools ---------------------------------------------------------------
def which(name: str) -> Optional[str]:
    """/opt/homebrew/bin/<name> first, then PATH."""
    brew = Path(BREW_BIN) / name
    if brew.is_file() and os.access(brew, os.X_OK):
        return str(brew)
    return shutil.which(name)


def tool(name: str) -> str:
    path = which(name)
    if not path:
        hint = "brew install ffmpeg" if name.startswith("ff") else f"brew install {name}"
        raise MediaError(f"{name} is not installed ({hint}).")
    return path


def tool_env() -> dict:
    """Environment for child tools: Homebrew first so yt-dlp finds ffmpeg and deno."""
    env = dict(os.environ)
    parts = [BREW_BIN, str(Path.home() / ".local" / "bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    current = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(parts + [p for p in current.split(os.pathsep) if p and p not in parts])
    return env


def run(cmd: Sequence[str], what: str = "", timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(_q(c) for c in cmd))
    proc = subprocess.run(list(cmd), capture_output=True, text=True, env=tool_env(), timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise MediaError(f"{what or Path(cmd[0]).name} failed: " + (" | ".join(tail) or f"exit {proc.returncode}"))
    return proc


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


@lru_cache(maxsize=1)
def h264_args() -> tuple:
    """libx264 when available (crf), else Apple VideoToolbox."""
    try:
        out = subprocess.run([tool("ffmpeg"), "-hide_banner", "-encoders"], capture_output=True, text=True,
                             env=tool_env()).stdout
    except Exception:
        out = ""
    if "libx264" in out:
        return ("-c:v", "libx264", "-preset", "medium")
    return ("-c:v", "h264_videotoolbox", "-q:v", "70")


def video_codec_args(crf: int = 16) -> list:
    args = list(h264_args())
    if "libx264" in args:
        args += ["-crf", str(crf)]
    return args + ["-pix_fmt", "yuv420p"]


# -- probing -------------------------------------------------------------
def _parse_rate(rate: Optional[str]) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(rate)
    except ValueError:
        return 0.0


def probe(path: PathLike) -> dict:
    """{width, height (display, rotation applied), fps, fps_rational, duration, frames, has_audio, audio_codec, video_codec}"""
    out = run([tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
              "ffprobe").stdout
    info = json.loads(out or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError(f"No video stream in {Path(path).name}.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rotation = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    if "rotate" in (video.get("tags") or {}):
        try:
            rotation = int(video["tags"]["rotate"])
        except ValueError:
            pass
    if abs(rotation) % 180 == 90:
        width, height = height, width
    fps_rational = video.get("avg_frame_rate") if _parse_rate(video.get("avg_frame_rate")) else video.get("r_frame_rate")
    duration = float(video.get("duration") or info.get("format", {}).get("duration") or 0.0)
    frames = int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None
    return {
        "width": width,
        "height": height,
        "fps": round(_parse_rate(fps_rational), 4),
        "fps_rational": fps_rational or "0/0",
        "duration": round(duration, 3),
        "frames": frames,
        "has_audio": audio is not None,
        "audio_codec": (audio or {}).get("codec_name"),
        "video_codec": video.get("codec_name"),
    }


def count_frames(path: PathLike) -> int:
    out = run([tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-count_packets",
               "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)], "ffprobe").stdout
    try:
        return int(out.strip().split(",")[0])
    except ValueError:
        return 0


# -- input ---------------------------------------------------------------
def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", (text or "").strip(), re.I))


def _fmt_ts(seconds: float) -> str:
    return f"{seconds:.3f}".rstrip("0").rstrip(".") if seconds % 1 else str(int(seconds))


def download_section(url: str, start: float, duration: float, dest_dir: PathLike,
                     cookies: Optional[PathLike] = None,
                     report: Optional[Callable[[float, str], None]] = None) -> Path:
    """yt-dlp only the [start, start+duration] section (precise cut). Returns the downloaded file.

    The anonymous download is tried first: signed-in sessions can be limited by YouTube's
    SABR experiment to a single 360p format. The cookies file is used when YouTube refuses
    the anonymous request (sign-in / bot check / age gate), or when only it works.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    cookie_file: Optional[Path] = None
    if cookies:
        cookie_file = Path(cookies).expanduser()
        if not cookie_file.is_file():
            log.warning("cookies file not found, continuing without: %s", cookie_file)
            cookie_file = None
    attempts: list[Optional[Path]] = [None] + ([cookie_file] if cookie_file else [])
    last_error: Optional[MediaError] = None
    for i, cookie in enumerate(attempts):
        try:
            return _yt_dlp_section(url, start, duration, dest_dir, cookie, report)
        except MediaError as exc:
            last_error = exc
            if i + 1 < len(attempts):
                log.info("anonymous download failed (%s); retrying with the cookies file", str(exc)[:160])
                if report:
                    report(0.0, "retrying with the YouTube cookies")
    assert last_error is not None
    raise last_error


def _yt_dlp_section(url: str, start: float, duration: float, dest_dir: Path, cookies: Optional[Path],
                    report: Optional[Callable[[float, str], None]]) -> Path:
    for old in dest_dir.glob("download.*"):
        old.unlink()
    end = start + duration
    cmd = [tool("yt-dlp"), "--no-playlist", "--newline", "--no-part", "--no-mtime",
           "-f", YTDLP_FORMAT,
           "--download-sections", f"*{_fmt_ts(start)}-{_fmt_ts(end)}",
           "--force-keyframes-at-cuts",
           "--downloader-args", YTDLP_FFMPEG_OUT,
           "--merge-output-format", "mkv",
           "--ffmpeg-location", str(Path(tool("ffmpeg")).parent),
           "-o", str(dest_dir / "download.%(ext)s")]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd += ["--", url]
    log.info("yt-dlp section %s-%s of %s (%s)", _fmt_ts(start), _fmt_ts(end), url,
             "with cookies" if cookies else "anonymous")
    log.debug("run: %s", " ".join(_q(c) for c in cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=tool_env(),
                            bufsize=1)
    tail: list[str] = []
    pct = re.compile(r"\[download\]\s+([\d.]+)%")
    timeline = re.compile(r"time=(\d+):(\d+):([\d.]+)")
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail = (tail + [line])[-12:]
        log.debug("yt-dlp: %s", line)
        m = pct.search(line)
        if m and report:
            report(float(m.group(1)) / 100.0, line.strip()[:120])
            continue
        m = timeline.search(line)
        if m and report:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            report(min(0.99, secs / max(duration, 0.1)), f"downloaded {secs:.1f}/{duration:g} s")
    code = proc.wait()
    proc.stdout.close()
    files = sorted(p for p in dest_dir.glob("download.*") if p.suffix not in (".part", ".ytdl", ".json"))
    if code != 0 or not files:
        joined = " | ".join(l for l in tail if "ERROR" in l) or " | ".join(tail[-3:])
        hint = ""
        if not cookies and ("Sign in" in joined or "cookies" in joined.lower() or "bot" in joined.lower()):
            hint = " YouTube wants a sign-in: pass --cookies with your exported youtube-cookies.txt."
        raise MediaError(f"Download failed (yt-dlp exit {code}): {joined[:400]}{hint}")
    return files[-1]


def trim_source(src: PathLike, out: PathLike, start: float, duration: float) -> dict:
    """source.mp4: exact [start, start+duration] of src, original fps, original audio (copy, fallback AAC)."""
    src, out = Path(src), Path(out)
    info = probe(src)
    base = [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        base += ["-ss", f"{start:.3f}"]
    base += ["-i", str(src), "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
             "-map_metadata", "-1", "-fps_mode", "passthrough"] + video_codec_args(crf=16)
    tail = ["-movflags", "+faststart", str(out)]
    copy_ok = info["has_audio"] and info.get("audio_codec") in ("aac", "mp3", "alac", "opus", "flac", "ac3", "eac3")
    try:
        if copy_ok:
            run(base + ["-c:a", "copy"] + tail, "ffmpeg trim")
        else:
            run(base + ["-c:a", "aac", "-b:a", "192k"] + tail, "ffmpeg trim")
    except MediaError:
        if not copy_ok:
            raise
        log.info("audio copy into source.mp4 failed, re-encoding audio to AAC")
        run(base + ["-c:a", "aac", "-b:a", "192k"] + tail, "ffmpeg trim")
    result = probe(out)
    if result["duration"] <= 0.05:
        raise MediaError("The trimmed clip is empty. Check --start and --duration against the video length.")
    return result


def extract_audio(src: PathLike, out: PathLike) -> Optional[Path]:
    """audio.m4a from source.mp4: stream copy, fallback AAC 192k. None when the clip has no audio."""
    src, out = Path(src), Path(out)
    if not probe(src)["has_audio"]:
        return None
    base = [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-map", "0:a:0", "-vn"]
    try:
        run(base + ["-c:a", "copy", str(out)], "ffmpeg audio copy")
    except MediaError:
        log.info("audio stream copy to m4a failed, encoding AAC")
        run(base + ["-c:a", "aac", "-b:a", "192k", str(out)], "ffmpeg audio")
    return out


def work_size(width: int, height: int, max_side: int = WORK_MAX_SIDE) -> tuple[int, int]:
    """Scale so the long side <= max_side, keep aspect, even dims."""
    scale = min(1.0, max_side / float(max(width, height)))
    w = max(2, int(math.floor(width * scale / 2.0 + 0.5)) * 2)
    h = max(2, int(math.floor(height * scale / 2.0 + 0.5)) * 2)
    while max(w, h) > max_side:
        if w >= h:
            w -= 2
        else:
            h -= 2
    return w, h


def make_work(src: PathLike, out: PathLike, fps: int = WORK_FPS, max_side: int = WORK_MAX_SIDE) -> dict:
    """work.mp4: fps 16, long side <= 832, even dims, no audio."""
    info = probe(src)
    w, h = work_size(info["width"], info["height"], max_side)
    run([tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-an", "-sn", "-dn",
         # start_time=0: frame i of work.mp4 is source time i/16 even when the first source
         # frame starts a few ms late (YouTube cuts), so 4 s always gives 64 frames.
         "-vf", f"fps={fps}:start_time=0,scale={w}:{h}:flags=lanczos,setsar=1", "-map_metadata", "-1"]
        + video_codec_args(crf=14) + ["-movflags", "+faststart", str(out)], "ffmpeg normalize")
    result = probe(out)
    result["frames"] = count_frames(out)
    return result


def prepare_input(input_ref: str, start: float, duration: float, job_dir: PathLike,
                  cookies: Optional[PathLike] = None,
                  report: Optional[Callable[[str, float, str], None]] = None) -> dict:
    """Download or open the input, write source.mp4, audio.m4a, work.mp4. Returns infos + timings."""
    job_dir = Path(job_dir)
    say = report or (lambda stage, p, msg: None)
    timings: dict = {}
    import time

    t0 = time.monotonic()
    if is_url(input_ref):
        say("download", 0.0, "yt-dlp: fetching the section")
        downloaded = download_section(input_ref, start, duration, job_dir, cookies,
                                      lambda p, m: say("download", p, m))
        offset = 0.0
        say("download", 1.0, f"downloaded {downloaded.name}")
    else:
        downloaded = Path(input_ref).expanduser()
        if not downloaded.is_file():
            raise MediaError(f"Input file not found: {downloaded}")
        offset = start
        length = probe(downloaded)["duration"]
        if length and start >= length:
            raise MediaError(f"--start {start:g} s is past the end of the video ({length:.1f} s).")
    timings["download" if is_url(input_ref) else "open"] = round(time.monotonic() - t0, 2)

    t0 = time.monotonic()
    say("trim", 0.1, "trimming source.mp4")
    source = trim_source(downloaded, job_dir / "source.mp4", offset, duration)
    if is_url(input_ref):
        try:
            downloaded.unlink()
        except OSError:
            pass
    say("trim", 0.6, "extracting audio.m4a")
    audio = extract_audio(job_dir / "source.mp4", job_dir / "audio.m4a")
    say("trim", 1.0, f"source {source['width']}x{source['height']} @ {source['fps']:g} fps, "
                     f"{source['duration']:.2f} s, audio {'yes' if audio else 'none'}")
    timings["trim"] = round(time.monotonic() - t0, 2)

    t0 = time.monotonic()
    say("normalize", 0.1, "making work.mp4 (16 fps)")
    work = make_work(job_dir / "source.mp4", job_dir / "work.mp4")
    say("normalize", 1.0, f"work {work['width']}x{work['height']}, {work['frames']} frames")
    timings["normalize"] = round(time.monotonic() - t0, 2)
    return {"source": source, "audio": str(audio) if audio else None, "work": work, "timings": timings}


# -- raw frames ----------------------------------------------------------
def iter_frames(path: PathLike, size: Optional[tuple[int, int]] = None, gray: bool = False) -> Iterator[np.ndarray]:
    """Decode frames as uint8 RGB (H, W, 3) or gray (H, W) numpy arrays."""
    info = probe(path)
    w, h = size or (info["width"], info["height"])
    pix, ch = ("gray", 1) if gray else ("rgb24", 3)
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0"]
    if size:
        cmd += ["-vf", f"scale={w}:{h}:flags=bilinear"]
    cmd += ["-f", "rawvideo", "-pix_fmt", pix, "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=tool_env())
    frame_bytes = w * h * ch
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            arr = np.frombuffer(buf, dtype=np.uint8)
            yield arr.reshape(h, w) if gray else arr.reshape(h, w, 3)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def read_frames(path: PathLike, gray: bool = False, size: Optional[tuple[int, int]] = None) -> np.ndarray:
    frames = list(iter_frames(path, size=size, gray=gray))
    if not frames:
        raise MediaError(f"Could not decode any frame from {Path(path).name}.")
    return np.stack(frames, axis=0)


class VideoWriter:
    """Pipe uint8 RGB (or gray) frames into an H.264 mp4 (yuv420p, constant fps)."""

    def __init__(self, path: PathLike, width: int, height: int, fps: float = WORK_FPS, crf: int = 16,
                 metadata: Optional[dict] = None):
        self.path = Path(path)
        self.width, self.height = int(width), int(height)
        self.count = 0
        tmp = self.path.with_name(self.path.stem + ".partial" + self.path.suffix)
        self.tmp = tmp
        cmd = [tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.width}x{self.height}", "-r", f"{fps:g}",
               "-i", "-", "-an"] + video_codec_args(crf=crf)
        for key, value in (metadata or {}).items():
            cmd += ["-metadata", f"{key}={value}"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=tool_env())

    def write(self, frame: np.ndarray) -> None:
        if frame.ndim == 2:
            frame = np.repeat(frame[:, :, None], 3, axis=2)
        if frame.dtype != np.uint8:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
        if frame.shape[0] != self.height or frame.shape[1] != self.width:
            raise MediaError(f"frame size {frame.shape[1]}x{frame.shape[0]} != {self.width}x{self.height}")
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())
        except BrokenPipeError:
            err = (self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else "").strip()
            raise MediaError(f"ffmpeg encoder stopped: {err[-300:]}")
        self.count += 1

    def close(self) -> Path:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        err = (self.proc.stderr.read().decode(errors="replace") if self.proc.stderr else "").strip()
        code = self.proc.wait()
        if self.proc.stderr:
            self.proc.stderr.close()
        if code != 0 or self.count == 0:
            raise MediaError(f"ffmpeg could not write {self.path.name}: {err[-300:] or 'no frames'}")
        os.replace(self.tmp, self.path)
        return self.path

    def abort(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        if self.proc.stderr:
            self.proc.stderr.close()
        try:
            self.tmp.unlink()
        except OSError:
            pass

    def __enter__(self) -> "VideoWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


def write_video(frames, path: PathLike, fps: float = WORK_FPS, crf: int = 16) -> Path:
    frames = list(frames)
    if not frames:
        raise MediaError(f"No frames to write to {Path(path).name}.")
    h, w = frames[0].shape[:2]
    with VideoWriter(path, w, h, fps, crf) as writer:
        for frame in frames:
            writer.write(frame)
    return Path(path)


def sanitize_seconds(value: float, name: str, minimum: float = 0.0, maximum: float = math.inf) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise MediaError(f"{name} must be a number of seconds.")
    if math.isnan(value) or value < minimum or value > maximum:
        raise MediaError(f"{name} must be between {minimum:g} and {maximum:g} seconds.")
    return value
