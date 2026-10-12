"""References for Make it me: who the person looks like, and how they sound.

    from a face pack   refs_from_pack(pack, job_dir)            -> zfx.facepack picks (frontal, ±20°, ±40°, smile)
    from a self-video  refs_from_self_video(video, job_dir, …)  -> a `.c101face` pack written into the job
                                                                   (<job>/me.c101face, manifest of effects/FACE-PACK.md),
                                                                   then the same picks
    from photos        refs_from_photos(photos, job_dir)        -> zfx.generate.prepare_refs (copies into refs/)
    the voice sample   voice_from_video(video, out, …)           -> <job>/me-voice.m4a: Demucs vocals, silences trimmed,
                                                                   normalized, 10–60 s (fewer when the video is short)

Self-video ("Film yourself", MAKE-IT-ME.md): frames are taken at 2 per second; each frame with a face gets, from
MediaPipe's Face Landmarker (478 points + the facial transformation matrix):
  - yaw / pitch / roll in degrees (camera's point of view: yaw + = the person turns to their left, pitch + = chin up;
    the matrix's signs are checked against the landmarks: nose offset for yaw, nose height for pitch),
  - the eye aspect ratio (EAR, both eyes): eyes count as open at >= max(0.11, 0.6 x the video's median EAR),
  - sharpness: the Laplacian variance of the face crop (grey, resized to 256 px wide), like Cinema 101's scan,
  - the mouth-corner distance over the outer eye-corner distance: a smile is >= 1.1 x the median (and +0.04).
Frames turned further than the bins reach (|yaw| > 52.5°, |pitch| > 30°) are dropped (the measurements are poor
there). Neutral frames go to bins yaw -45…+45 in 15° steps x pitch -20/0/+20 (`y+15_p+00`); the sharpest frame of each bin
with its eyes open and at least 35 % of the best sharpness is kept; the sharpest smile within ±20° yaw becomes the
`smile` frame. zfx.facepack then picks the references exactly as for a Cinema 101 pack.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import __version__, face_landmarker_model

PACK_NAME = "me.c101face"
VOICE_NAME = "me-voice.m4a"
SAMPLE_FPS = 2.0
YAW_STEP, YAW_MAX = 15, 45
PITCH_STEP, PITCH_MAX = 20, 20
EAR_MIN = 0.11                  # closed/blinking eyes measure under ~0.1; people squinting in the sun ~0.12-0.19
EAR_RELATIVE = 0.6
SHARP_FLOOR = 0.35
SMILE_RATIO = 1.10
SMILE_MIN_DELTA = 0.04
SMILE_MAX_YAW = 20.0
MIN_FACE_FRAMES = 1
VOICE_MIN_SECONDS = 10.0        # under this: a warning (the contract wants 10-60 s)
VOICE_MAX_SECONDS = 60.0
VOICE_MIN_SPEECH = 1.5          # under this: no sample (same floor as the voice step)
VOICE_SR = 44100

# MediaPipe Face Landmarker indices
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH_CORNERS = (61, 291)
EYE_OUTER = (33, 263)
FACE_SIDES = (234, 454)
NOSE_TIP = 1
CHIN = 152

Progress = Callable[[str, float, str], None]


class RefsError(RuntimeError):
    """Plain-English problem with the references or the voice sample."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------- measurements (pure functions)
@dataclass
class Measure:
    """What one sampled frame of the self-video measured (the selection works on these alone)."""
    index: int
    time: float
    yaw: float
    pitch: float
    roll: float = 0.0
    ear: float = 0.3
    sharpness: float = 0.0
    mouth_ratio: float = 0.6
    face_box: list = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])   # normalized x, y, w, h
    width: int = 0
    height: int = 0


def eye_aspect_ratio(pts: np.ndarray, idx: Sequence[int]) -> float:
    p1, p2, p3, p4, p5, p6 = (pts[i] for i in idx)
    horizontal = float(np.linalg.norm(p1 - p4))
    if horizontal <= 1e-9:
        return 0.0
    return (float(np.linalg.norm(p2 - p6)) + float(np.linalg.norm(p3 - p5))) / (2.0 * horizontal)


def mouth_ratio(pts: np.ndarray) -> float:
    eyes = float(np.linalg.norm(pts[EYE_OUTER[0]] - pts[EYE_OUTER[1]]))
    if eyes <= 1e-9:
        return 0.0
    return float(np.linalg.norm(pts[MOUTH_CORNERS[0]] - pts[MOUTH_CORNERS[1]])) / eyes


def bin_name(yaw: float, pitch: float) -> str:
    by = int(max(-YAW_MAX, min(YAW_MAX, round(yaw / YAW_STEP) * YAW_STEP)))
    bp = int(max(-PITCH_MAX, min(PITCH_MAX, round(pitch / PITCH_STEP) * PITCH_STEP)))
    return f"y{by:+03d}_p{bp:+03d}"


def select_frames(measures: Sequence[Measure], sharp_floor: float = SHARP_FLOOR, ear_min: float = EAR_MIN,
                  ear_relative: float = EAR_RELATIVE) -> tuple[dict, dict]:
    """The frame to keep per bin ({bin: Measure}) and how the choice went (counts, thresholds)."""
    stats = {"frames": len(measures), "eyes_open": 0, "sharp_enough": 0, "smiles": 0, "bins": 0}
    if not measures:
        return {}, stats
    in_range = [m for m in measures if abs(m.yaw) <= YAW_MAX + YAW_STEP / 2 and abs(m.pitch) <= PITCH_MAX + PITCH_STEP / 2]
    stats["in_range"] = len(in_range)
    if not in_range:
        return {}, stats
    ears = np.array([m.ear for m in in_range], dtype=float)
    ear_threshold = max(ear_min, ear_relative * float(np.median(ears)))
    open_eyes = [m for m in in_range if m.ear >= ear_threshold]
    stats["eyes_open"] = len(open_eyes)
    stats["ear_threshold"] = round(ear_threshold, 3)
    if not open_eyes:
        return {}, stats
    best = max(m.sharpness for m in open_eyes)
    floor = best * sharp_floor
    pool = [m for m in open_eyes if m.sharpness >= floor]
    stats["sharp_enough"] = len(pool)
    stats["sharpness_floor"] = round(floor, 2)
    stats["best_sharpness"] = round(best, 2)
    ratios = np.array([m.mouth_ratio for m in in_range], dtype=float)
    median_ratio = float(np.median(ratios))
    smile_threshold = max(median_ratio * SMILE_RATIO, median_ratio + SMILE_MIN_DELTA)
    stats["smile_threshold"] = round(smile_threshold, 3)
    chosen: dict = {}
    for m in pool:
        if m.mouth_ratio >= smile_threshold:
            stats["smiles"] += 1
            if abs(m.yaw) <= SMILE_MAX_YAW and (("smile" not in chosen) or m.sharpness > chosen["smile"].sharpness):
                chosen["smile"] = m
            continue
        key = bin_name(m.yaw, m.pitch)
        if key not in chosen or m.sharpness > chosen[key].sharpness:
            chosen[key] = m
    stats["bins"] = len([k for k in chosen if k != "smile"])
    return chosen, stats


def expression_of(m: Measure, chosen: dict) -> str:
    return "smile" if chosen.get("smile") is m else "neutral"


# ----------------------------------------------------------------------------- measuring a video (MediaPipe)
def _tool(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", shutil.which(name)):
        if cand and Path(cand).is_file():
            return cand
    raise RefsError(f"{name} is not installed (brew install ffmpeg).")


def sampled_frames(video: Path, fps: float = SAMPLE_FPS):
    """Yield (index, time_s, rgb frame) at `fps` frames per second (ffmpeg, rotation applied)."""
    from .io import probe
    info = probe(video)
    w, h = info["width"], info["height"]
    cmd = [_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(video), "-map", "0:v:0",
           "-vf", f"fps={fps:g}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h * 3
    i = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield i, i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _angles(matrix) -> tuple[float, float, float]:
    """(pitch, yaw, roll) in degrees from MediaPipe's facial transformation matrix (rotation about x, y, z)."""
    import cv2
    rot = np.asarray(matrix, dtype=np.float64)[:3, :3]
    angles, *_ = cv2.RQDecomp3x3(rot)
    return float(angles[0]), float(angles[1]), float(angles[2])


def grab_frames(video: Path, indices: Sequence[int], fps: float = SAMPLE_FPS) -> dict:
    """{index: rgb frame} for the sampled frames wanted (a second, cheap pass: nothing is kept in memory before)."""
    wanted = set(int(i) for i in indices)
    out: dict = {}
    if not wanted:
        return out
    last = max(wanted)
    for i, _t, rgb in sampled_frames(video, fps):
        if i in wanted:
            out[i] = rgb.copy()
        if i >= last:
            break
    return out


def measure_video(video: Path, report: Optional[Progress] = None, fps: float = SAMPLE_FPS) -> list[Measure]:
    """Face measurements of the frames sampled from `video` (frames themselves are not kept: see grab_frames)."""
    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    model = face_landmarker_model()
    if not model.is_file():
        raise RefsError(f"MediaPipe face_landmarker.task is missing ({model}). Run: bash effects/setup.sh")
    from .io import probe
    info = probe(video)
    total = max(1, int(math.ceil((info.get("duration") or 0) * fps)))
    options = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(model)),
                                           running_mode=vision.RunningMode.IMAGE, num_faces=1,
                                           output_facial_transformation_matrixes=True)
    rows: list = []
    raw: list = []
    with vision.FaceLandmarker.create_from_options(options) as landmarker:
        for i, t, rgb in sampled_frames(video, fps):
            h, w = rgb.shape[:2]
            result = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
            faces = getattr(result, "face_landmarks", None) or []
            if report:
                report("refs", min(1.0, (i + 1) / total), f"self-video: frame {i + 1}/{total}, faces {len(rows)}")
            if not faces:
                continue
            pts = np.array([[p.x * w, p.y * h] for p in faces[0]], dtype=np.float64)
            norm = pts / np.array([w, h], dtype=np.float64)
            x0, y0 = norm.min(axis=0)
            x1, y1 = norm.max(axis=0)
            px0, py0 = max(0, int((x0 - 0.05) * w)), max(0, int((y0 - 0.05) * h))
            px1, py1 = min(w, int((x1 + 0.05) * w)), min(h, int((y1 + 0.05) * h))
            crop = cv2.cvtColor(rgb[py0:py1, px0:px1], cv2.COLOR_RGB2GRAY) if px1 > px0 and py1 > py0 else None
            sharp = 0.0
            if crop is not None and crop.size:
                crop = cv2.resize(crop, (256, max(1, round(256 * crop.shape[0] / crop.shape[1]))),
                                  interpolation=cv2.INTER_AREA)
                sharp = float(cv2.Laplacian(crop, cv2.CV_64F).var())
            matrices = getattr(result, "facial_transformation_matrixes", None) or []
            pitch_m, yaw_m, roll_m = _angles(matrices[0]) if matrices else (0.0, 0.0, 0.0)
            left, right, nose = pts[FACE_SIDES[0]], pts[FACE_SIDES[1]], pts[NOSE_TIP]
            half = max(1e-6, abs(right[0] - left[0]) / 2.0)
            nose_off = (nose[0] - (left[0] + right[0]) / 2.0) / half
            eye_y = (pts[EYE_OUTER[0]][1] + pts[EYE_OUTER[1]][1]) / 2.0
            nose_ratio = (nose[1] - eye_y) / max(1e-6, pts[CHIN][1] - eye_y)
            raw.append((yaw_m, pitch_m, nose_off, nose_ratio))
            ear = (eye_aspect_ratio(pts, LEFT_EYE) + eye_aspect_ratio(pts, RIGHT_EYE)) / 2.0
            rows.append(Measure(index=i, time=round(t, 3), yaw=yaw_m, pitch=pitch_m, roll=round(roll_m, 1),
                                ear=round(ear, 4), sharpness=round(sharp, 2), mouth_ratio=round(mouth_ratio(pts), 4),
                                face_box=[round(float(x0), 4), round(float(y0), 4), round(float(x1 - x0), 4),
                                          round(float(y1 - y0), 4)], width=w, height=h))
    if not rows:
        return rows
    # the matrix's signs, checked against the landmarks (as the test pack of 2026-10-11 did)
    yaw_sign, pitch_sign = 1.0, 1.0
    if len(raw) >= 6:
        ys, ps, no, nr = (np.array([r[k] for r in raw], dtype=float) for k in range(4))
        if ys.std() > 3.0 and no.std() > 1e-3:
            yaw_sign = 1.0 if np.corrcoef(ys, no)[0, 1] >= 0 else -1.0
        if ps.std() > 3.0 and nr.std() > 1e-3:
            pitch_sign = 1.0 if np.corrcoef(ps, nr)[0, 1] <= 0 else -1.0
    for m in rows:
        m.yaw = round(yaw_sign * m.yaw, 1)
        m.pitch = round(pitch_sign * m.pitch, 1)
    return rows


# ----------------------------------------------------------------------------- the pack written into the job
def write_pack(out_dir: Path, chosen: dict, frames: dict, *, person: Optional[str], consent: bool, source: Path,
               stats: dict, extra: Optional[dict] = None) -> Path:
    """<out_dir>.c101face with frames/<bin>.jpg and the manifest of effects/FACE-PACK.md (+ a few extra fields)."""
    import cv2
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "frames").mkdir(parents=True)
    items = []
    for key in sorted(chosen):
        m = chosen[key]
        rgb = frames.get(m.index)
        if rgb is None:
            continue
        rel = f"frames/{key}.jpg"
        cv2.imwrite(str(out_dir / rel), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
        items.append({"file": rel, "bin": key, "yaw": m.yaw, "pitch": m.pitch, "roll": m.roll,
                      "expression": expression_of(m, chosen), "sharpness": m.sharpness, "width": m.width,
                      "height": m.height, "time": m.time, "face_box": m.face_box,
                      "ear": m.ear, "mouth_ratio": m.mouth_ratio})
    manifest = {"format": "c101face", "version": 1, "created": _now_iso(), "person": person or "",
                "consent": bool(consent), "device": "Mac", "app": f"YouTube Zeus Effects {__version__} (self-video)",
                "source_video": source.name, "angles_from": "MediaPipe Face Landmarker transformation matrix",
                "selection": stats, "frames": items}
    manifest.update(extra or {})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                           encoding="utf-8")
    return out_dir


def refs_from_self_video(video, job_dir, *, consent: bool, person: Optional[str] = None,
                         report: Optional[Progress] = None, engine: str = "faceswap") -> tuple[list[Path], dict]:
    """Self-video -> <job>/me.c101face -> the picked references copied into <job>/refs/. (copies, record)."""
    from . import facepack, generate
    video = Path(video).expanduser()
    if not video.is_file():
        raise RefsError(f"Self-video not found: {video}")
    job_dir = Path(job_dir)
    t0 = time.monotonic()
    measures = measure_video(video, report)
    if len(measures) < MIN_FACE_FRAMES:
        raise RefsError(f"No face was found in {video.name}: film yourself chest up, facing the camera, in soft light.")
    chosen, stats = select_frames(measures)
    if not chosen:
        raise RefsError(f"No usable frame in {video.name}: every frame has closed eyes or is blurred "
                        f"({stats['frames']} frames with a face). Film again in better light.")
    frames = grab_frames(video, [m.index for m in chosen.values()])
    pack_dir = write_pack(job_dir / PACK_NAME, chosen, frames, person=person, consent=consent, source=video,
                          stats=stats)
    del frames
    if report:
        report("refs", 1.0, f"{len(chosen)} frames kept out of {stats['frames']} with a face")
    try:
        copies, record = generate.prepare_pack_refs(pack_dir, job_dir, engine)
    except generate.GenerateError as exc:
        raise RefsError(str(exc)) from exc
    record.update(source="self-video", video=str(video), video_sha256=generate.sha256_file(video),
                  selection=stats, seconds=round(time.monotonic() - t0, 1),
                  bins={k: {"time": m.time, "yaw": m.yaw, "pitch": m.pitch, "sharpness": m.sharpness, "ear": m.ear}
                        for k, m in sorted(chosen.items())})
    return copies, record


def refs_from_pack(pack, job_dir, engine: str = "faceswap") -> tuple[list[Path], dict]:
    from . import generate
    try:
        copies, record = generate.prepare_pack_refs(pack, Path(job_dir), engine)
    except generate.GenerateError as exc:
        raise RefsError(str(exc)) from exc
    record["source"] = "face-pack"
    return copies, record


def refs_from_photos(photos: Sequence, job_dir) -> tuple[list[Path], dict]:
    from . import generate
    try:
        copies = generate.prepare_refs([str(p) for p in photos], Path(job_dir), required=True)
    except generate.GenerateError as exc:
        raise RefsError(str(exc)) from exc
    return copies, {"source": "photos", "photos": [str(p) for p in photos],
                    "sha256": [generate.sha256_file(p) for p in photos]}


# ----------------------------------------------------------------------------- the voice sample
def voice_from_video(video, out_path, work_dir, *, report: Optional[Progress] = None,
                     device: str = "auto") -> Optional[dict]:
    """The speech in `video` -> `out_path` (AAC): Demucs vocals, high-pass, silences trimmed, -20 LUFS, at most
    60 s. None (with the reason in the returned record's place) when there is under 1.5 s of speech."""
    from . import voice as V
    video, out_path, work_dir = Path(video).expanduser(), Path(out_path), Path(work_dir)
    if not video.is_file():
        raise RefsError(f"Self-video not found: {video}")
    st = V.voice_status()
    if not st["ready"]:
        raise RefsError("The voice engine is not installed (" + str(st["message"]) + "): run effects/setup-voice.sh, "
                        "or use --no-voice.")
    try:
        info = V.probe_audio(video)
    except V.VoiceError as exc:
        return {"ok": False, "reason": f"{video.name} has no audio ({exc})."}
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "voice-sample.log"
    t0 = time.monotonic()
    if report:
        report("refs", 0.0, "reading the self-video's sound")
    try:
        x, sr = V.read_audio(video, sr=VOICE_SR, channels=min(2, max(1, info["channels"])))
        raw = V.write_wav(work_dir / "self-raw.wav", x, sr)
        if report:
            report("refs", 0.1, "Demucs: keeping the voice, dropping the room and the music")
        sep = V.run_worker("vc_demucs", {"device": device, "items": [{"input": str(raw), "out_dir": str(work_dir / "self-split"),
                                                                       "sr": sr}]},
                           work_dir, log_path, report or (lambda *a: None), "refs", 0.1, 0.8)
        vocals, _ = V.read_audio(work_dir / "self-split" / "vocals.wav")
        rest, _ = V.read_audio(work_dir / "self-split" / "accompaniment.wav")
        clean, cinfo = V.clean_reference(vocals, sr)
    except V.VoiceError as exc:
        raise RefsError(f"Could not take the voice from {video.name}: {exc}") from exc
    speech = float(cinfo.get("speech_seconds") or 0.0)
    warnings: list = []
    if len(clean) == 0 or speech < VOICE_MIN_SPEECH:
        return {"ok": False, "reason": f"Only {speech:.1f} s of speech in {video.name}: no voice sample "
                                       f"(talk for 20 s in the self-video).", "speech_seconds": speech}
    if speech < VOICE_MIN_SECONDS:
        warnings.append(f"Short voice sample: {speech:.1f} s of speech in the self-video (10-60 s is better).")
    snr = V.voice_over_noise_db(vocals, rest, sr)
    if snr < 15:
        warnings.append(f"The self-video's background is only {snr:.0f} dB under the voice; the sample keeps some of it.")
    if len(clean) > VOICE_MAX_SECONDS * sr:
        clean = clean[: int(VOICE_MAX_SECONDS * sr)]
        fade = int(0.02 * sr)
        clean[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
    wav = V.write_wav(work_dir / "self-voice.wav", clean, sr)
    try:
        enc = V.encode_aac(wav, out_path, sr, 1, 192000, "voice sample for YouTube Zeus Effects (Make it me)")
    except V.VoiceError as exc:
        raise RefsError(str(exc)) from exc
    if report:
        report("refs", 1.0, f"voice sample: {min(speech, VOICE_MAX_SECONDS):.1f} s of speech")
    return {"ok": True, "file": str(out_path), "speech_seconds": round(speech, 2), "seconds_kept": round(len(clean) / sr, 2),
            "voice_over_noise_db": snr, "loudness_lufs": cinfo.get("loudness_lufs"), "f0": cinfo.get("f0"),
            "segments": cinfo.get("segments"), "encode": enc, "separation": sep, "warnings": warnings,
            "elapsed": round(time.monotonic() - t0, 1)}


def measures_to_json(measures: Sequence[Measure]) -> list:
    return [asdict(m) for m in measures]
