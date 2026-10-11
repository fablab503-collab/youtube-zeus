"""voice: change WHO speaks in a job's audio, keep the exact words, timing and emotion (speech-to-speech).

    zeus_effects.py voice --job DIR --voice REF.wav [--voice REF2.wav] --consent [--keep-music | --no-keep-music]
                          [--grade match|none] [--deess auto|on|off] [--check auto|on|off]
                          [--device auto|mps|cpu] [--steps N] [--seed N]

Because nothing is re-timed, the lips in final.mp4 stay in sync without any lip-sync model. Free and local:
Demucs htdemucs (MIT) splits the voice from the rest, Chatterbox VC (Resemble AI, MIT) re-speaks the same speech
tokens in the reference voice, the grade below is plain numpy. Heavy models run in their own venv
($ZEUS_EFFECTS_HOME/voice/.venv, effects/setup-voice.sh) as subprocesses (zfx/engines/vc_demucs.py,
zfx/engines/vc_chatterbox.py); this module only needs numpy + ffmpeg, like the rest of the main venv.

Order (run_voice):
 0. refuse without consent ("the voice is mine or of someone who agreed") and unless the job's review approval
    still matches the prepared files (same gate as generate);
 a. the job's original audio (audio.m4a, else source.mp4's) -> stems/original.wav; Demucs -> stems/vocals.wav +
    stems/accompaniment.wav (drums + bass + other), same rate, channels and sample count;
    each --voice file (any audio or video; its first audio stream) gets the same split, then the voice is cleaned:
    high-pass 70 Hz (wind, handling noise), silences trimmed, -20 LUFS -> voice-refs/ref-N-clean.wav; all of them
    joined (Chatterbox uses at most 10 s) -> voice-refs/reference.wav;
 b. Chatterbox VC: stems/vocals.wav -> stems/vocals-converted-raw.wav (mono, same rate and sample count);
 c. grade (--grade match, default): panned like the original voice, EQ matched to the original voice's long-term
    spectrum in octave bands (Voice Grader's "Match the reference" method without its pitch shift), de-essed when the
    new voice is hissier than the original (--deess auto), then EBU R128 integrated loudness = the original voice's;
 d. remix: graded voice + the original accompaniment (--keep-music, default; --no-keep-music = voice only),
    sample-accurate, same length; nothing is time-stretched; a peak above 0 dBFS lowers the whole remix;
 e. audio-voice.m4a (AAC, the original's rate and channels) and final-voice.mp4 = final.mp4's video (source.mp4's
    when there is no final.mp4) stream-copied + the new audio; metadata comment
    "AI-generated voice with YouTube Zeus Effects (<engine>)" (after the video's own AI comment);
 f. checks (--check auto: when whisper-cli and its model exist): transcripts before/after + word error rate,
    median F0 of the original voice, the converted voice and the reference, loudness, measurable artefacts;
 g. everything recorded in job.json `voices[]`; stems stay in stems/, references in voice-refs/.

Voice Grader's AI helper (127.0.0.1:8765) has no matching endpoint (/transcribe /diarize /split /speak /tts only;
its "Match the reference" lives in the Swift app and also shifts pitch, which would undo the conversion), so the
grade is implemented here. Raises VoiceError (plain English) on refusal or failure.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np

CONSENT_TEXT = "the voice is mine or of someone who agreed"
AI_VOICE_COMMENT = "AI-generated voice with YouTube Zeus Effects ({engine})"
VOICE_ENGINES = {"chatterbox": "vc_chatterbox"}
GRADE_MODES = ("match", "none")
DEESS_MODES = ("auto", "on", "off")
CHECK_MODES = ("auto", "on", "off")
MAX_VOICE_REFS = 4
MAX_SECONDS = 30.0            # one Chatterbox pass (jobs are <= 10 s)
REF_TARGET_LUFS = -20.0
REF_MAX_SECONDS = 10.0        # what Chatterbox VC uses of the reference
REF_MIN_SECONDS = 1.5         # refuse below this much speech
REF_GOOD_SECONDS = 10.0       # warn below this much speech
REF_HIGHPASS_HZ = 70.0
OCTAVE_CENTERS = (31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0)
WHISPER_MODEL = (Path.home() / "Library" / "Application Support" / "YouTube Zeus" / "Models"
                 / "ggml-large-v3-turbo-q5_0.bin")

Progress = Callable[[str, float, str], None]


class VoiceError(RuntimeError):
    """Plain-English refusal or failure of the voice step."""


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
    raise VoiceError(f"{name} is not installed (brew install {'ffmpeg' if name.startswith('ff') else name}).")


# ============================================================================= audio files (ffmpeg + numpy)
def probe_audio(path, stream: int = 0) -> dict:
    """Sample rate, channels, codec, bit rate, start and duration of audio stream `stream` (0 = the first)."""
    proc = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", f"a:{stream}", "-show_streams",
                           "-show_format", "-print_format", "json", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise VoiceError(f"Could not read {Path(path).name}: {proc.stderr.strip()[-200:]}")
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise VoiceError(f"{Path(path).name} has no audio.")
    s, fmt = streams[0], info.get("format") or {}

    def num(v, cast=float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None
    return {"sr": num(s.get("sample_rate"), int), "channels": num(s.get("channels"), int) or 1,
            "codec": s.get("codec_name"), "bit_rate": num(s.get("bit_rate"), int) or num(fmt.get("bit_rate"), int),
            "duration": num(s.get("duration")) or num(fmt.get("duration")), "start": num(s.get("start_time")) or 0.0,
            "comment": (fmt.get("tags") or {}).get("comment")}


def read_audio(path, sr: Optional[int] = None, channels: Optional[int] = None, stream: int = 0):
    """Decode audio stream `stream` with ffmpeg -> (float32 array (channels, n), sr)."""
    info = probe_audio(path, stream)
    sr = int(sr or info["sr"] or 48000)
    ch = int(channels or info["channels"] or 1)
    proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-map", f"0:a:{stream}",
                           "-vn", "-f", "f32le", "-acodec", "pcm_f32le", "-ac", str(ch), "-ar", str(sr), "-"],
                          capture_output=True)
    if proc.returncode != 0:
        raise VoiceError(f"Could not decode {Path(path).name}: {proc.stderr.decode(errors='replace')[-200:]}")
    x = np.frombuffer(proc.stdout, dtype="<f4")
    x = x[: (len(x) // ch) * ch].reshape(-1, ch).T.copy()
    return x.astype(np.float32), sr


def write_wav(path, x, sr: int) -> Path:
    """32-bit float WAV (format 3), x = (channels, n) or (n,)."""
    x = np.asarray(x, dtype=np.float32)
    x2 = x[None] if x.ndim == 1 else x
    ch, n = x2.shape
    data = np.ascontiguousarray(x2.T).astype("<f4").tobytes()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (b"RIFF" + struct.pack("<I", 4 + 26 + 12 + 8 + len(data)) + b"WAVE"
              + b"fmt " + struct.pack("<IHHIIHHH", 18, 3, ch, sr, sr * ch * 4, ch * 4, 32, 0)
              + b"fact" + struct.pack("<II", 4, n)
              + b"data" + struct.pack("<I", len(data)))
    tmp = path.with_name(path.name + ".partial")
    with open(tmp, "wb") as fh:
        fh.write(header)
        fh.write(data)
    os.replace(tmp, path)
    return path


# ============================================================================= small DSP helpers
def as2d(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x[None] if x.ndim == 1 else x


def mono(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x if x.ndim == 1 else x.mean(axis=0)


def fit_length(x, n: int) -> np.ndarray:
    """Pad with silence or cut at the end (last axis) to exactly n samples. Never resamples."""
    x = np.asarray(x, dtype=np.float32)
    m = x.shape[-1]
    if m == n:
        return x
    if m > n:
        return x[..., :n]
    pad = [(0, 0)] * (x.ndim - 1) + [(0, n - m)]
    return np.pad(x, pad)


def resample(x, sr_in: int, sr_out: int) -> np.ndarray:
    """Band-limited FFT resampling along the last axis (analysis only; the workers use soxr)."""
    if sr_in == sr_out:
        return np.asarray(x, dtype=np.float32)
    x = np.asarray(x, dtype=np.float64)
    n = x.shape[-1]
    m = int(round(n * sr_out / sr_in))
    spec = np.fft.rfft(x, axis=-1)
    keep = min(spec.shape[-1], m // 2 + 1)
    out = np.zeros(x.shape[:-1] + (m // 2 + 1,), dtype=complex)
    out[..., :keep] = spec[..., :keep]
    return (np.fft.irfft(out, m, axis=-1) * (m / n)).astype(np.float32)


def apply_response(x, sr: int, response: Callable[[np.ndarray], np.ndarray], pad_s: float = 0.25) -> np.ndarray:
    """Filter along the last axis by multiplying the spectrum with response(freqs) (real = zero phase, complex =
    a causal IIR's DTFT). Padded with silence so nothing wraps around; same length, no delay."""
    x = np.asarray(x, dtype=np.float32)
    x2 = as2d(x).astype(np.float64)
    n = x2.shape[-1]
    pad = int(pad_s * sr)
    nfft = 1 << int(math.ceil(math.log2(max(2, n + 2 * pad))))
    xp = np.zeros((x2.shape[0], nfft))
    xp[:, pad:pad + n] = x2
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    y = np.fft.irfft(np.fft.rfft(xp, axis=-1) * response(freqs)[None], nfft, axis=-1)[:, pad:pad + n]
    y = y.astype(np.float32)
    return y[0] if x.ndim == 1 else y


def highpass(x, sr: int, fc: float = REF_HIGHPASS_HZ, order: int = 2) -> np.ndarray:
    """Zero-phase Butterworth-shaped high-pass (magnitude 1/sqrt(1 + (fc/f)^2n))."""
    def resp(f):
        with np.errstate(divide="ignore"):
            r = 1.0 / np.sqrt(1.0 + (fc / np.maximum(f, 1e-9)) ** (2 * order))
        r[f <= 0] = 0.0
        return r
    return apply_response(x, sr, resp)


def db(v: float, floor: float = -150.0) -> float:
    return float(max(floor, 10.0 * math.log10(v))) if v > 0 else floor


def frame_rms(x_mono, sr: int, frame_s: float = 0.02, hop_s: float = 0.01) -> np.ndarray:
    x = np.asarray(x_mono, dtype=np.float64)
    frame, hop = max(1, int(frame_s * sr)), max(1, int(hop_s * sr))
    if len(x) < frame:
        return np.array([math.sqrt(np.mean(x ** 2))]) if len(x) else np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(x ** 2)])
    starts = np.arange(0, len(x) - frame + 1, hop)
    return np.sqrt((c[starts + frame] - c[starts]) / frame)


# ============================================================================= EBU R128 / ITU-R BS.1770-4 loudness
def _biquad_response(b, a, f, sr):
    z = np.exp(-2j * np.pi * f / sr)
    return (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)


def k_weighting(f, sr: int) -> np.ndarray:
    """BS.1770 K-weighting (pre-filter shelf + RLB high-pass) at any rate; libebur128's analogue prototypes."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / sr)
    vh = 10.0 ** (gain / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    shelf_b = ((vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0)
    shelf_a = (1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0)
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / sr)
    a0 = 1.0 + k / q + k * k
    hp_b = (1.0, -2.0, 1.0)
    hp_a = (1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0)
    return _biquad_response(shelf_b, shelf_a, f, sr) * _biquad_response(hp_b, hp_a, f, sr)


def integrated_loudness(x, sr: int) -> float:
    """Integrated loudness in LUFS (400 ms blocks, 75 % overlap, -70 LUFS absolute and -10 LU relative gates).
    Channels weigh 1.0 (mono/stereo). -inf for silence or clips shorter than one block."""
    xk = as2d(apply_response(as2d(x), sr, lambda f: k_weighting(f, sr), pad_s=0.5)).astype(np.float64)
    n = xk.shape[-1]
    block, hop = int(round(0.4 * sr)), int(round(0.1 * sr))
    if n < block:
        return float("-inf")
    starts = np.arange(0, n - block + 1, hop)
    c = np.concatenate([np.zeros((xk.shape[0], 1)), np.cumsum(xk ** 2, axis=-1)], axis=-1)
    z = (c[:, starts + block] - c[:, starts]) / block          # (channels, blocks) mean squares
    power = z.sum(axis=0)
    with np.errstate(divide="ignore"):
        lj = -0.691 + 10.0 * np.log10(power)
    gated = lj > -70.0
    if not gated.any():
        return float("-inf")
    rel = -0.691 + 10.0 * math.log10(power[gated].mean()) - 10.0
    gated &= lj > rel
    if not gated.any():
        return float("-inf")
    return float(-0.691 + 10.0 * math.log10(power[gated].mean()))


def match_loudness(x, sr: int, target_lufs: float, max_gain_db: float = 30.0):
    """(x scaled to target_lufs, gain dB). Silence stays silence."""
    cur = integrated_loudness(x, sr)
    if not math.isfinite(cur) or not math.isfinite(target_lufs):
        return np.asarray(x, dtype=np.float32), 0.0
    g = float(np.clip(target_lufs - cur, -max_gain_db, max_gain_db))
    return (np.asarray(x, dtype=np.float32) * (10.0 ** (g / 20.0))).astype(np.float32), g


# ============================================================================= long-term spectrum + EQ match
def active_frames(x_mono, sr: int, frame: int, hop: int, rel_db: float = -30.0, abs_db: float = -60.0):
    """Frame start indices whose RMS is within rel_db of the 95th percentile frame (and above abs_db)."""
    x = np.asarray(x_mono, dtype=np.float64)
    if len(x) < frame:
        return np.zeros(0, dtype=int)
    starts = np.arange(0, len(x) - frame + 1, hop)
    c = np.concatenate([[0.0], np.cumsum(x ** 2)])
    rms = np.sqrt((c[starts + frame] - c[starts]) / frame)
    if not np.any(rms > 0):
        return np.zeros(0, dtype=int)
    p95 = np.percentile(rms, 95)
    keep = (rms > p95 * 10 ** (rel_db / 20.0)) & (rms > 10 ** (abs_db / 20.0))
    return starts[keep]


def ltas(x, sr: int, nfft: int = 4096):
    """Long-term average power spectrum of the active (speaking) frames: (freqs, power)."""
    xm = mono(x).astype(np.float64)
    hop = nfft // 2
    if len(xm) < nfft:
        xm = np.pad(xm, (0, nfft - len(xm)))
    starts = active_frames(xm, sr, nfft, hop)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    if len(starts) == 0:
        return freqs, np.zeros(len(freqs))
    win = np.hanning(nfft)
    frames = np.stack([xm[s:s + nfft] * win for s in starts])
    return freqs, (np.abs(np.fft.rfft(frames, axis=-1)) ** 2).mean(axis=0)


def band_levels(freqs, power, centers=OCTAVE_CENTERS) -> np.ndarray:
    """dB per octave band (c/sqrt2 .. c*sqrt2); -150 for an empty band or one above Nyquist."""
    out = []
    for c in centers:
        sel = (freqs >= c / math.sqrt(2)) & (freqs < c * math.sqrt(2))
        out.append(db(float(power[sel].sum())) if sel.any() else -150.0)
    return np.array(out)


def eq_match(src, target, sr: int, strength: float = 0.8, max_db: float = 6.0, range_db: float = 30.0,
             centers=OCTAVE_CENTERS) -> dict:
    """Octave-band EQ that moves src's long-term spectrum toward target's (Voice Grader's VoiceMatcher method:
    difference of band levels, overall level removed, [1 2 1]/4 smoothing, 80 % strength, clamped). Bands where
    either signal is more than range_db under its own loudest band get 0 dB: nothing is boosted where the voice has
    (almost) nothing, e.g. above the converted voice's 12 kHz bandwidth or below a higher voice's fundamental."""
    f, ps = ltas(src, sr)
    _, pt = ltas(target, sr)
    ls, lt = band_levels(f, ps, centers), band_levels(f, pt, centers)
    valid = (np.array(centers) < 0.45 * sr) & (ls > ls.max() - range_db) & (lt > lt.max() - range_db)
    gains = np.zeros(len(centers))
    if valid.sum() >= 2:
        diff = np.where(valid, lt - ls, 0.0)
        diff[valid] -= diff[valid].mean()
        smooth = diff.copy()
        for i in range(1, len(centers) - 1):
            if valid[i] and valid[i - 1] and valid[i + 1]:
                smooth[i] = (diff[i - 1] + 2 * diff[i] + diff[i + 1]) / 4.0
        gains = np.where(valid, np.clip(smooth * strength, -max_db, max_db), 0.0)
    return {"centers_hz": list(centers), "source_db": [round(float(v), 2) for v in ls],
            "target_db": [round(float(v), 2) for v in lt], "valid": [bool(v) for v in valid],
            "gains_db": [round(float(g), 2) for g in gains], "strength": strength, "max_db": max_db}


def eq_curve(freqs, centers, gains_db) -> np.ndarray:
    """Linear gain per frequency: band gains interpolated on a log-frequency axis, held beyond the end bands."""
    lf = np.log2(np.maximum(freqs, 1.0))
    g = np.interp(lf, np.log2(np.asarray(centers, dtype=float)), np.asarray(gains_db, dtype=float))
    return 10.0 ** (g / 20.0)


def apply_eq(x, sr: int, centers, gains_db) -> np.ndarray:
    if not np.any(np.abs(np.asarray(gains_db)) > 1e-6):
        return np.asarray(x, dtype=np.float32)
    return apply_response(x, sr, lambda f: eq_curve(f, centers, gains_db))


def spectral_distance(a, b, sr: int, centers=OCTAVE_CENTERS) -> float:
    """RMS dB distance between two signals' octave-band spectra (level removed), over bands both use."""
    f, pa = ltas(a, sr)
    _, pb = ltas(b, sr)
    la, lb = band_levels(f, pa, centers), band_levels(f, pb, centers)
    valid = (np.array(centers) < 0.45 * sr) & (la > la.max() - 45) & (lb > lb.max() - 45)
    if valid.sum() < 2:
        return 0.0
    d = (la - lb)[valid]
    d -= d.mean()
    return float(np.sqrt(np.mean(d ** 2)))


# ============================================================================= STFT + de-ess
def _stft(x, n_fft: int, hop: int):
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    pad = n_fft
    total = n + 2 * pad
    total += (-(total - n_fft)) % hop
    xp = np.zeros(total)
    xp[pad:pad + n] = x
    win = np.sqrt(np.hanning(n_fft + 1)[:-1])       # periodic sqrt-Hann: analysis x synthesis = Hann
    starts = np.arange(0, total - n_fft + 1, hop)
    frames = np.stack([xp[s:s + n_fft] * win for s in starts])
    return np.fft.rfft(frames, axis=-1), starts, win, total, pad


def _istft(spec, starts, win, total: int, pad: int, n: int, n_fft: int) -> np.ndarray:
    frames = np.fft.irfft(spec, n_fft, axis=-1) * win
    out = np.zeros(total)
    norm = np.zeros(total)
    for f, s in zip(frames, starts):
        out[s:s + n_fft] += f
        norm[s:s + n_fft] += win * win
    out /= np.maximum(norm, 1e-8)
    return out[pad:pad + n]


def sibilance_db(x, sr: int, band=(4500.0, 10000.0), n_fft: int = 1024, hop: int = 256):
    """Per active STFT frame: energy in the sibilant band relative to the whole frame (dB)."""
    xm = mono(x)
    spec, starts, *_ = _stft(xm, n_fft, hop)
    p = np.abs(spec) ** 2
    f = np.fft.rfftfreq(n_fft, 1.0 / sr)
    sib = p[:, (f >= band[0]) & (f < min(band[1], 0.48 * sr))].sum(axis=1)
    tot = p[:, f >= 80].sum(axis=1)
    level = 10 * np.log10(tot + 1e-20)
    active = level > np.percentile(level, 95) - 35
    return 10 * np.log10((sib + 1e-20) / (tot + 1e-20)), active


def deess(x, sr: int, mode: str = "auto", reference=None, max_cut_db: float = 6.0, band=(4500.0, 10000.0),
          margin_db: float = 1.5):
    """Gentle dynamic de-esser. Frames whose sibilant-band share is above the threshold (the reference's 95th
    percentile when given, else -12 dB) get that band lowered by the excess, at most max_cut_db, smoothed over
    ~20 ms. mode 'auto' only acts when the voice is hissier than the reference by more than margin_db.
    Returns (y, info). Same length; unity gain is an exact pass-through."""
    info = {"mode": mode, "applied": False}
    if mode == "off" or 0.48 * sr <= band[0]:
        return np.asarray(x, dtype=np.float32), info
    n_fft, hop = 1024, 256
    ratio, active = sibilance_db(x, sr, band, n_fft, hop)
    if not active.any():
        return np.asarray(x, dtype=np.float32), info
    p95 = float(np.percentile(ratio[active], 95))
    info["voice_p95_db"] = round(p95, 2)
    if reference is not None:
        r_ratio, r_active = sibilance_db(reference, sr, band, n_fft, hop)
        thr = float(np.percentile(r_ratio[r_active], 95)) if r_active.any() else -12.0
        info["reference_p95_db"] = round(thr, 2)
    else:
        thr = -12.0
    info["threshold_db"] = round(thr, 2)
    if mode == "auto" and p95 <= thr + margin_db:
        info["reason"] = "not hissier than the original voice"
        return np.asarray(x, dtype=np.float32), info
    cut = np.where(active, np.clip(ratio - thr, 0.0, max_cut_db), 0.0)
    k = 3                                                   # ~20 ms at 44.1 kHz / hop 256: smooth attack/release
    cut = np.convolve(np.pad(cut, k, mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), mode="valid")
    f = np.fft.rfftfreq(n_fft, 1.0 / sr)
    shape = np.clip((f - (band[0] - 500.0)) / 500.0, 0.0, 1.0) * (f < min(band[1] + 1500.0, sr / 2))
    gains = 10 ** (-(cut[:, None] * shape[None, :]) / 20.0)
    x2 = as2d(x)
    out = []
    for ch in x2:
        spec, starts, win, total, pad = _stft(ch, n_fft, hop)
        out.append(_istft(spec * gains, starts, win, total, pad, len(ch), n_fft))
    y = np.stack(out).astype(np.float32)
    info.update(applied=True, frames_cut=int((cut > 0.5).sum()), max_cut_db=round(float(cut.max()), 2),
                mean_cut_db_active=round(float(cut[active].mean()), 3))
    return (y[0] if np.asarray(x).ndim == 1 else y), info


# ============================================================================= pitch (YIN), speech segments, timing
def f0_track(x, sr: int, fmin: float = 60.0, fmax: float = 400.0, hop_s: float = 0.01, threshold: float = 0.15):
    """YIN pitch per 10 ms frame (NaN = unvoiced), computed at 16 kHz."""
    y = resample(mono(x), sr, 16000).astype(np.float64)
    sr2 = 16000
    w = int(0.03 * sr2)
    tmin, tmax = int(sr2 / fmax), int(sr2 / fmin)
    hop = int(hop_s * sr2)
    seg = w + tmax
    if len(y) < seg + 1:
        return np.full(0, np.nan)
    starts = np.arange(0, len(y) - seg, hop)
    frames = np.stack([y[s:s + seg] for s in starts])
    rms = np.sqrt((frames[:, :w] ** 2).mean(axis=1))
    gate = max(1e-4, np.percentile(rms, 95) * 10 ** (-30 / 20)) if len(rms) else 1e-4
    nfft = 1 << int(math.ceil(math.log2(2 * seg)))
    fa = np.fft.rfft(frames[:, :w], nfft, axis=1)
    fs = np.fft.rfft(frames, nfft, axis=1)
    r = np.fft.irfft(np.conj(fa) * fs, nfft, axis=1)[:, :tmax + 1]       # sum_j a_j x_{j+tau}
    sq = np.concatenate([np.zeros((len(frames), 1)), np.cumsum(frames ** 2, axis=1)], axis=1)
    e0 = sq[:, w][:, None]
    taus = np.arange(tmax + 1)
    et = sq[:, taus + w] - sq[:, taus]
    d = np.maximum(e0 + et - 2 * r, 0.0)
    cm = np.cumsum(d[:, 1:], axis=1)
    dn = np.ones_like(d)
    dn[:, 1:] = d[:, 1:] * np.arange(1, tmax + 1)[None] / np.maximum(cm, 1e-12)
    f0 = np.full(len(frames), np.nan)
    for i in range(len(frames)):
        if rms[i] < gate:
            continue
        row = dn[i]
        below = np.nonzero(row[tmin:tmax] < threshold)[0]
        if len(below) == 0:
            continue
        t = below[0] + tmin
        while t + 1 < tmax and row[t + 1] < row[t]:
            t += 1
        a, b, c = row[t - 1], row[t], row[t + 1] if t + 1 <= tmax else row[t]
        den = a - 2 * b + c
        shift = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
        f0[i] = sr2 / (t + float(np.clip(shift, -1, 1)))
    return f0


def f0_stats(x, sr: int) -> dict:
    f0 = f0_track(x, sr)
    v = f0[np.isfinite(f0)]
    if len(v) == 0:
        return {"median_hz": None, "p10_hz": None, "p90_hz": None, "voiced_frames": 0, "frames": int(len(f0))}
    return {"median_hz": round(float(np.median(v)), 1), "p10_hz": round(float(np.percentile(v, 10)), 1),
            "p90_hz": round(float(np.percentile(v, 90)), 1), "voiced_frames": int(len(v)), "frames": int(len(f0))}


def speech_segments(x_mono, sr: int, rel_db: float = -30.0, abs_db: float = -55.0, pad_s: float = 0.1,
                    min_gap_s: float = 0.25, min_len_s: float = 0.08) -> list:
    """[(start, end)] sample ranges with speech: 20 ms frames within rel_db of the loud ones, padded, merged."""
    x = np.asarray(x_mono, dtype=np.float64)
    hop = int(0.01 * sr)
    rms = frame_rms(x, sr, 0.02, 0.01)
    if len(rms) == 0 or not np.any(rms > 0):
        return []
    thr = max(np.percentile(rms, 95) * 10 ** (rel_db / 20.0), 10 ** (abs_db / 20.0))
    on = rms > thr
    segs, i = [], 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            segs.append([i * hop, min(len(x), j * hop + int(0.02 * sr))])
            i = j
        else:
            i += 1
    pad = int(pad_s * sr)
    segs = [[max(0, s - pad), min(len(x), e + pad)] for s, e in segs]
    merged = []
    for s, e in segs:
        if merged and s - merged[-1][1] < int(min_gap_s * sr):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= int(min_len_s * sr)]


def trim_silence(x_mono, sr: int, gap_s: float = 0.12, fade_s: float = 0.01, **kw):
    """Speech segments joined with gap_s of silence (10 ms fades, no clicks): (y, segments, speech seconds)."""
    x = np.asarray(x_mono, dtype=np.float32)
    segs = speech_segments(x, sr, **kw)
    if not segs:
        return np.zeros(0, np.float32), [], 0.0
    fade = max(1, int(fade_s * sr))
    gap = np.zeros(int(gap_s * sr), np.float32)
    parts = []
    for k, (s, e) in enumerate(segs):
        p = x[s:e].copy()
        f = min(fade, len(p) // 2)
        if f > 0:
            ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)
            p[:f] *= ramp
            p[-f:] *= ramp[::-1]
        if k:
            parts.append(gap)
        parts.append(p)
    return np.concatenate(parts), [(round(s / sr, 3), round(e / sr, 3)) for s, e in segs], \
        round(sum(e - s for s, e in segs) / sr, 2)


def estimate_lag(reference, test, sr: int, max_ms: float = 200.0, hop_s: float = 0.005) -> dict:
    """Timing of `test` against `reference` from their log-energy envelopes (works across different voices):
    lag_ms > 0 = test is late. corr = normalised correlation at the best lag (1 = identical rhythm)."""
    ea = 20 * np.log10(frame_rms(mono(reference), sr, 0.02, hop_s) + 1e-5)
    eb = 20 * np.log10(frame_rms(mono(test), sr, 0.02, hop_s) + 1e-5)
    n = min(len(ea), len(eb))
    if n < 10:
        return {"lag_ms": 0.0, "corr": None}
    ea, eb = np.maximum(ea[:n], ea[:n].max() - 50), np.maximum(eb[:n], eb[:n].max() - 50)
    ea, eb = ea - ea.mean(), eb - eb.mean()
    k = min(int(max_ms / 1000 / hop_s), n // 2)
    best, best_l, cs = -2.0, 0, {}
    for lag in range(-k, k + 1):           # test[i + lag] against reference[i]
        a, b = (ea[:n - lag], eb[lag:]) if lag >= 0 else (ea[-lag:], eb[:n + lag])
        c = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
        cs[lag] = c
        if c > best:
            best, best_l = c, lag
    frac = 0.0
    if -k < best_l < k:
        y0, y1, y2 = cs[best_l - 1], cs[best_l], cs[best_l + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
    return {"lag_ms": round((best_l + float(np.clip(frac, -1, 1))) * hop_s * 1000, 1), "corr": round(best, 3)}


def sample_offset(a, b, max_samples: int = 4096) -> int:
    """Exact sample offset of b against a (b late > 0) by cross-correlation of the waveforms (same signal)."""
    a, b = mono(a).astype(np.float64), mono(b).astype(np.float64)
    n = min(len(a), len(b))
    if n == 0:
        return 0
    a, b = a[:n], b[:n]
    nfft = 1 << int(math.ceil(math.log2(2 * n)))
    c = np.fft.irfft(np.conj(np.fft.rfft(a, nfft)) * np.fft.rfft(b, nfft), nfft)
    lags = np.concatenate([np.arange(0, max_samples + 1), np.arange(-max_samples, 0)])
    vals = np.concatenate([c[:max_samples + 1], c[-max_samples:]])
    return int(lags[int(np.argmax(vals))])


# ============================================================================= measurable artefacts
def peak_dbfs(x) -> float:
    p = float(np.max(np.abs(x))) if np.size(x) else 0.0
    return round(20 * math.log10(p), 2) if p > 0 else -150.0


def true_peak_dbfs(x, sr: int) -> float:
    """4x oversampled peak (BS.1770 style, FFT interpolation)."""
    return peak_dbfs(resample(as2d(x), sr, sr * 4))


def clipped_samples(x, level: float = 0.999) -> int:
    return int(np.sum(np.abs(np.asarray(x)) >= level))


def click_count(x, sr: int, factor: float = 12.0) -> int:
    """Sudden jumps: second difference above `factor` x its local robust scale (5 ms apart counts once)."""
    y = mono(x).astype(np.float64)
    if len(y) < 3:
        return 0
    e = np.abs(y[2:] - 2 * y[1:-1] + y[:-2])
    w = max(8, int(0.02 * sr))
    k = len(e) // w
    if k == 0:
        return 0
    scale = np.median(e[:k * w].reshape(k, w), axis=1) / 0.6745
    scale = np.repeat(scale, w)
    scale = np.concatenate([scale, np.full(len(e) - len(scale), scale[-1])])
    hits = np.nonzero(e > factor * (scale + 1e-4))[0]
    if len(hits) == 0:
        return 0
    return int(1 + np.sum(np.diff(hits) > int(0.005 * sr)))


def pause_level_db(voice, original_voice, sr: int) -> Optional[float]:
    """Level of `voice` where the original voice is silent, relative to `voice` while it speaks (dB): high values
    mean the model put sound (breath, babble, noise) where nobody spoke."""
    seg = speech_segments(mono(original_voice), sr, pad_s=0.05)
    v = mono(voice).astype(np.float64)
    mask = np.zeros(len(v), bool)
    for s, e in seg:
        mask[s:e] = True
    if mask.all() or not mask.any():
        return None
    speak = math.sqrt(np.mean(v[mask] ** 2)) + 1e-12
    quiet = math.sqrt(np.mean(v[~mask] ** 2)) + 1e-12
    return round(20 * math.log10(quiet / speak), 1)


def bandwidth_hz(x, sr: int, fraction: float = 0.99) -> float:
    f, p = ltas(x, sr)
    if p.sum() <= 0:
        return 0.0
    c = np.cumsum(p) / p.sum()
    return round(float(f[min(len(f) - 1, int(np.searchsorted(c, fraction)))]), 0)


# ============================================================================= transcripts
def words(text: str) -> list:
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("’", "'"))


def word_error_rate(reference: str, hypothesis: str) -> Optional[float]:
    r, h = words(reference), words(hypothesis)
    if not r:
        return None if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return round(d[len(h)] / len(r), 3)


def whisper_available() -> Optional[tuple]:
    cli = shutil.which("whisper-cli") or ("/opt/homebrew/bin/whisper-cli"
                                          if Path("/opt/homebrew/bin/whisper-cli").is_file() else None)
    model = Path(os.environ.get("ZEUS_VOICE_WHISPER_MODEL", "") or WHISPER_MODEL).expanduser()
    return (cli, model) if cli and model.is_file() else None


def transcribe(path, work_dir: Path, language: str = "auto") -> str:
    found = whisper_available()
    if not found:
        raise VoiceError("whisper-cli or its model is missing.")
    cli, model = found
    wav = Path(work_dir) / f".whisper-{Path(path).stem}.wav"
    subprocess.run([_tool("ffmpeg"), "-y", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar",
                    "16000", "-c:a", "pcm_s16le", str(wav)], check=True, capture_output=True)
    try:
        proc = subprocess.run([cli, "-m", str(model), "-f", str(wav), "-l", language, "-nt", "-np"],
                              capture_output=True, text=True, timeout=600)
    finally:
        wav.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise VoiceError(f"whisper-cli failed: {proc.stderr.strip()[-200:]}")
    return " ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())


# ============================================================================= worker processes (voice venv)
def engine_module(name: str):
    import importlib
    key = (name or "").strip().lower()
    if key not in VOICE_ENGINES:
        raise VoiceError(f"Unknown voice engine '{name}'. Choose one of: {', '.join(VOICE_ENGINES)}.")
    return importlib.import_module(f"zfx.engines.{VOICE_ENGINES[key]}")


def voice_status(engine: str = "chatterbox") -> dict:
    """Readiness of the voice step (venv, Chatterbox weights, Demucs weights). Never downloads."""
    from .engines import vc_demucs
    conv = engine_module(engine).status()
    sep = vc_demucs.status()
    ok = bool(conv.get("ready") and sep.get("ready"))
    msg = "ready" if ok else (conv["message"] if not conv.get("ready") else sep["message"])
    return {"ready": ok, "message": msg, "convert": conv, "separate": sep}


def run_worker(module: str, task: dict, task_dir: Path, log_path: Path, report: Progress, stage: str,
               lo: float = 0.0, hi: float = 1.0, timeout: float = 1800.0) -> dict:
    """Run `python -m zfx.engines.<module> task.json` in the voice venv; relay its progress into [lo, hi]."""
    from .engines.vc_chatterbox import voice_python
    effects = Path(__file__).resolve().parents[1]
    py = voice_python()
    if not py.exists():
        raise VoiceError(f"The voice runtime is not installed ({py}): run bash effects/setup-voice.sh")
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / f".task-{module}-{os.getpid()}.json"
    task_file.write_text(json.dumps(task), encoding="utf-8")
    env = dict(os.environ)
    env.update(PYTHONPATH=str(effects), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUNBUFFERED="1",
               TOKENIZERS_PARALLELISM="false", PYTHONWARNINGS="ignore")
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    result, error = None, None
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n== {_now_iso()} {module}\n")
        log.flush()
        proc = subprocess.Popen([str(py), "-m", f"zfx.engines.{module}", str(task_file)], cwd=str(effects),
                                env=env, stdout=subprocess.PIPE, stderr=log, text=True)
        t0 = time.monotonic()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if "result" in obj:
                    result = obj["result"]
                elif "error" in obj:
                    error = obj["error"]
                elif "progress" in obj:
                    report(stage, lo + (hi - lo) * float(obj.get("progress") or 0), obj.get("message", ""))
                if time.monotonic() - t0 > timeout:
                    proc.kill()
                    error = f"{module} took longer than {timeout:.0f} s"
                    break
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            task_file.unlink(missing_ok=True)
    if error or result is None:
        raise VoiceError((error or f"{module} stopped without a result (exit {proc.returncode})")
                         + f". Details: {log_path}")
    return result


# ============================================================================= encode + mux
def encode_aac(wav: Path, out: Path, sr: int, channels: int, bitrate: int, comment: str) -> dict:
    """AAC in an .m4a with FFmpeg's own encoder: its edit list removes the encoder delay and the end padding, so the
    file decodes to exactly the WAV's samples (measured: offset 0, same count). Apple's aac_at starts at the right
    sample too but keeps ~1000 samples of end padding, so the audio would outlast the video by ~23 ms."""
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    cmd = [_tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", str(wav), "-c:a", "aac", "-b:a", str(bitrate),
           "-ar", str(sr), "-ac", str(channels), "-map_metadata", "-1", "-metadata", f"comment={comment}",
           "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise VoiceError("Encoding the new audio failed: " + (proc.stderr.strip().splitlines() or ["?"])[-1][:300])
    os.replace(tmp, out)
    return {"encoder": "aac (FFmpeg)", "bitrate": bitrate, "sr": sr, "channels": channels}


def mux_voice(video_src: Path, audio: Path, out: Path, comment: str) -> dict:
    """final-voice.mp4 = video_src's video stream-copied (same timestamps) + `audio` stream-copied."""
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    cmd = [_tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", str(video_src), "-i", str(audio),
           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-map_metadata", "-1", "-metadata", f"comment={comment}",
           "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise VoiceError("Muxing final-voice.mp4 failed: " + (proc.stderr.strip().splitlines() or ["?"])[-1][:300])
    os.replace(tmp, out)
    return {"video_from": video_src.name, "video_stream_copy": True, "audio_stream_copy": True, "comment": comment}


def _streams(path: Path) -> dict:
    proc = subprocess.run([_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-print_format",
                           "json", str(path)], capture_output=True, text=True)
    info = json.loads(proc.stdout or "{}")
    out = {"format_duration": float((info.get("format") or {}).get("duration") or 0),
           "comment": ((info.get("format") or {}).get("tags") or {}).get("comment")}
    for s in info.get("streams") or []:
        kind = s.get("codec_type")
        if kind in ("video", "audio") and kind not in out:
            out[kind] = {"codec": s.get("codec_name"), "start": float(s.get("start_time") or 0),
                         "duration": float(s.get("duration") or 0), "frames": s.get("nb_frames")}
    return out


# ============================================================================= the pipeline
def _progress(progress) -> Progress:
    if progress is None:
        return lambda stage, p, message="": None
    if hasattr(progress, "update"):
        return progress.update
    return progress


def _job(job):
    from .jobs import Job, JobError
    if hasattr(job, "dir") and hasattr(job, "data"):
        return job
    try:
        return Job.load(job)
    except JobError as exc:
        raise VoiceError(str(exc)) from exc


def check_consent(consent: bool) -> None:
    if not consent:
        raise VoiceError(f"Voice change refused: consent is missing. Confirm that {CONSENT_TEXT} (--consent).")


def check_voices(voices) -> list:
    paths = []
    for v in voices or []:
        p = Path(v).expanduser()
        if not p.is_file():
            raise VoiceError(f"Voice sample not found: {p}")
        paths.append(p.resolve())
    if not paths:
        raise VoiceError("Add the voice to use with --voice (a recording of that person speaking, 10-30 s).")
    if len(paths) > MAX_VOICE_REFS:
        raise VoiceError(f"Use at most {MAX_VOICE_REFS} voice samples.")
    return paths


def pan_like(voice_mono, original_voice) -> np.ndarray:
    """Mono voice -> the original voice's channel layout with its per-channel level (keeps the stereo image)."""
    o = as2d(original_voice).astype(np.float64)
    v = np.asarray(voice_mono, dtype=np.float32)
    if o.shape[0] == 1:
        return v[None]
    rms = np.sqrt((o ** 2).mean(axis=1)) + 1e-12
    ref = math.sqrt(float(np.mean(o.mean(axis=0) ** 2))) + 1e-12
    g = np.clip(rms / ref, 0.0, 2.0) if ref > 1e-9 else np.ones(o.shape[0])
    return (g[:, None] * v[None]).astype(np.float32)


def voice_over_noise_db(vocals, rest, sr: int) -> float:
    """How far the background (Demucs' non-voice part: wind, room, music) sits under the voice while it speaks."""
    v, r = mono(vocals).astype(np.float64), mono(rest).astype(np.float64)
    n = min(len(v), len(r))
    v, r = v[:n], r[:n]
    mask = np.zeros(n, bool)
    for a, b in speech_segments(v, sr, pad_s=0.0):
        mask[a:b] = True
    if not mask.any():
        mask[:] = True
    return round(db(float(np.mean(v[mask] ** 2)) + 1e-20) - db(float(np.mean(r[mask] ** 2)) + 1e-20), 1)


def clean_reference(raw_vocals, sr: int) -> tuple:
    """Separated voice of a sample -> (cleaned mono, info): high-pass, silences trimmed, -20 LUFS, peak <= -1 dBFS."""
    x = highpass(mono(raw_vocals), sr, REF_HIGHPASS_HZ)
    y, segs, speech = trim_silence(x, sr)
    info = {"input_seconds": round(len(x) / sr, 2), "speech_seconds": speech, "segments": segs}
    if len(y) == 0:
        return y, info
    y, g = match_loudness(y, sr, REF_TARGET_LUFS)
    pk = float(np.max(np.abs(y))) if len(y) else 0.0
    if pk > 10 ** (-1 / 20):
        y = y * (10 ** (-1 / 20) / pk)
    info.update(gain_db=round(g, 2), loudness_lufs=round(integrated_loudness(y, sr), 2), peak_dbfs=peak_dbfs(y),
                f0=f0_stats(y, sr))
    return y.astype(np.float32), info


def run_voice(args, job, progress=None) -> dict:
    """The `voice` command. args: voice (list of paths), consent, engine, keep_music, grade, deess, check,
    device, steps, seed. Returns the result dict (also appended to job.json `voices`)."""
    report = _progress(progress)
    check_consent(bool(getattr(args, "consent", False)))           # 0. before anything is read or run
    job = _job(job)
    from .review import ReviewError, verify
    try:
        verify(job)
    except ReviewError as exc:
        raise VoiceError(f"Voice change refused: {exc}") from exc
    voices = check_voices(getattr(args, "voice", None))
    engine = (getattr(args, "engine", None) or "chatterbox").lower()
    grade = (getattr(args, "grade", None) or "match").lower()
    deess_mode = (getattr(args, "deess", None) or "auto").lower()
    check_mode = (getattr(args, "check", None) or "auto").lower()
    keep_music = getattr(args, "keep_music", True)
    keep_music = True if keep_music is None else bool(keep_music)
    device = (getattr(args, "device", None) or "auto").lower()
    steps = getattr(args, "steps", None)
    seed = int(getattr(args, "seed", None) or 0)
    if grade not in GRADE_MODES:
        raise VoiceError(f"--grade must be one of {', '.join(GRADE_MODES)}.")
    if deess_mode not in DEESS_MODES:
        raise VoiceError(f"--deess must be one of {', '.join(DEESS_MODES)}.")
    st = voice_status(engine)
    if not st["ready"]:
        raise VoiceError(st["message"])

    jd = job.dir
    stems, refs_dir = jd / "stems", jd / "voice-refs"
    stems.mkdir(exist_ok=True)
    refs_dir.mkdir(exist_ok=True)
    log_path = jd / "voice.log"
    audio_src = job.artifact("audio") or (jd / "audio.m4a" if (jd / "audio.m4a").is_file() else None)
    if audio_src is None:
        audio_src = jd / "source.mp4"
    if not Path(audio_src).is_file():
        raise VoiceError("The job has no audio (audio.m4a / source.mp4). Run prepare again.")
    timings: dict = {}
    warnings: list = []
    t_all = time.monotonic()

    # a. original audio + separation (job and voice samples in one Demucs run) -------------------------------
    report("voice", 0.01, f"reading {Path(audio_src).name}")
    a_info = probe_audio(audio_src)
    orig, sr = read_audio(audio_src)
    ch, n = orig.shape
    if n / sr > MAX_SECONDS:
        raise VoiceError(f"The audio is {n / sr:.1f} s long; the voice step takes at most {MAX_SECONDS:g} s at once.")
    write_wav(stems / "original.wav", orig, sr)
    items = [{"input": str(stems / "original.wav"), "out_dir": str(stems), "sr": sr}]
    ref_raw = []
    for i, v in enumerate(voices, 1):
        r, rsr = read_audio(v, sr=44100, channels=min(2, max(1, probe_audio(v)["channels"])))
        if r.shape[1] < int(0.5 * rsr):
            raise VoiceError(f"{v.name} has less than half a second of audio.")
        p = write_wav(refs_dir / f"ref-{i}-raw.wav", r, rsr)
        ref_raw.append((v, p, rsr))
        items.append({"input": str(p), "out_dir": str(refs_dir / f"ref-{i}-split"), "sr": rsr})
    t0 = time.monotonic()
    sep = run_worker("vc_demucs", {"device": device, "items": items}, stems, log_path, report, "separate", 0.02, 0.3)
    timings["separate"] = round(time.monotonic() - t0, 2)
    vocals, _ = read_audio(stems / "vocals.wav")
    accomp, _ = read_audio(stems / "accompaniment.wav")
    vocals, accomp = fit_length(vocals, n), fit_length(accomp, n)

    # a'. clean the voice samples -> reference.wav ------------------------------------------------------------
    report("reference", 0.0, "cleaning the voice sample")
    ref_records, cleaned = [], []
    for i, (v, raw_path, rsr) in enumerate(ref_raw, 1):
        rv, _ = read_audio(refs_dir / f"ref-{i}-split" / "vocals.wav")
        rn, _ = read_audio(refs_dir / f"ref-{i}-split" / "accompaniment.wav")
        y, info = clean_reference(rv, rsr)
        snr = voice_over_noise_db(rv, rn, rsr)
        info["voice_over_noise_db"] = snr      # while speaking: Demucs' voice vs everything else (wind, room)
        if snr < 15:
            warnings.append(f"{v.name}: the background (wind, room, music) is only {snr:.0f} dB under the voice; "
                            "the cleaned sample keeps some of it.")
        clean_path = refs_dir / f"ref-{i}-clean.wav"
        if len(y):
            write_wav(clean_path, y, rsr)
            cleaned.append(y)
        info.update(file=str(v), sha256=sha256_file(v), raw=str(raw_path.relative_to(jd)),
                    clean=str(clean_path.relative_to(jd)) if len(y) else None)
        ref_records.append(info)
    speech_total = sum(r["speech_seconds"] for r in ref_records)
    if speech_total < REF_MIN_SECONDS:
        raise VoiceError(f"The voice sample has only {speech_total:.1f} s of clear speech; record at least "
                         f"{REF_GOOD_SECONDS:g} s (better 20-30 s) of the person talking in a quiet room.")
    if speech_total < REF_GOOD_SECONDS:
        warnings.append(f"Short voice sample: {speech_total:.1f} s of speech (Chatterbox uses up to "
                        f"{REF_MAX_SECONDS:g} s). The new voice will be only roughly like the person's; "
                        "10-30 s of clean speech works better.")
    gap = np.zeros(int(0.15 * 44100), np.float32)
    joined = np.concatenate([p for y in cleaned for p in (y, gap)][:-1]) if cleaned else np.zeros(0, np.float32)
    if len(joined) > REF_MAX_SECONDS * 44100:
        warnings.append(f"The voice samples hold {len(joined) / 44100:.1f} s; Chatterbox uses the first "
                        f"{REF_MAX_SECONDS:g} s.")
        joined = joined[: int(REF_MAX_SECONDS * 44100)]
    ref_path = write_wav(refs_dir / "reference.wav", joined, 44100)
    report("reference", 1.0, f"{speech_total:.1f} s of speech in the voice sample")

    # b. convert ------------------------------------------------------------------------------------------
    t0 = time.monotonic()
    task = {"input": str(stems / "vocals.wav"), "ref": str(ref_path), "out": str(stems / "vocals-converted-raw.wav"),
            "sr": sr, "device": device, "lead_ms": 80, "seed": seed}
    if steps:
        task["steps"] = int(steps)
    conv = run_worker(VOICE_ENGINES[engine], task, stems, log_path, report, "convert", 0.0, 1.0)
    timings["convert"] = round(time.monotonic() - t0, 2)
    raw_conv, _ = read_audio(stems / "vocals-converted-raw.wav", sr=sr, channels=1)
    raw_conv = fit_length(raw_conv[0], n)
    timing = estimate_lag(vocals, raw_conv, sr)
    if timing["corr"] is not None and timing["corr"] > 0.6 and abs(timing["lag_ms"]) > 25:
        warnings.append(f"The new voice seems {timing['lag_ms']:+.0f} ms off the original (envelope correlation "
                        f"{timing['corr']}); check the lips.")

    # c. grade ----------------------------------------------------------------------------------------------
    t0 = time.monotonic()
    report("grade", 0.0, "matching the original voice" if grade == "match" else "no grade")
    voice = pan_like(raw_conv, vocals)
    grade_info: dict = {"mode": grade}
    vocal_lufs = integrated_loudness(vocals, sr)
    if grade == "match":
        eq = eq_match(voice, vocals, sr)
        before = spectral_distance(voice, vocals, sr)
        voice = apply_eq(voice, sr, eq["centers_hz"], eq["gains_db"])
        eq["distance_db_before"] = round(before, 2)
        eq["distance_db_after"] = round(spectral_distance(voice, vocals, sr), 2)
        report("grade", 0.4, "EQ matched")
        voice, de = deess(voice, sr, deess_mode, reference=vocals)
        report("grade", 0.7, "de-ess " + ("applied" if de.get("applied") else "not needed"))
        before_lufs = integrated_loudness(voice, sr)
        voice, gain = match_loudness(voice, sr, vocal_lufs)
        grade_info.update(eq=eq, deess=de, loudness={"original_voice_lufs": round(vocal_lufs, 2),
                                                     "converted_before_lufs": round(before_lufs, 2),
                                                     "gain_db": round(gain, 2),
                                                     "converted_after_lufs": round(integrated_loudness(voice, sr), 2)},
                          method="numpy: EBU R128 (BS.1770-4) + octave-band EQ (Voice Grader's VoiceMatcher method, "
                                 "no pitch shift) + dynamic de-ess; Voice Grader's helper has no matching endpoint")
    timings["grade"] = round(time.monotonic() - t0, 2)
    write_wav(stems / "vocals-converted.wav", voice, sr)

    # d. remix ----------------------------------------------------------------------------------------------
    report("mix", 0.0, "converted voice + original accompaniment" if keep_music else "converted voice only")
    mix = (voice + accomp) if keep_music else voice.copy()
    mix = fit_length(mix, n)
    pk = float(np.max(np.abs(mix))) if mix.size else 0.0
    safety = 0.0
    if pk > 0.999:
        safety = 20 * math.log10(0.999 / pk)
        mix = mix * (0.999 / pk)
        warnings.append(f"The remix peaked at {20 * math.log10(pk):+.1f} dBFS: lowered by {-safety:.1f} dB.")
    write_wav(stems / "mix-voice.wav", mix, sr)

    # e. encode + mux ------------------------------------------------------------------------------------------
    t0 = time.monotonic()
    comment = AI_VOICE_COMMENT.format(engine=engine)
    audio_out, final_out = jd / "audio-voice.m4a", jd / "final-voice.mp4"
    bitrate = max(192000, int(a_info.get("bit_rate") or 0))
    enc = encode_aac(stems / "mix-voice.wav", audio_out, sr, ch, bitrate, comment)
    dec, _ = read_audio(audio_out, sr=sr, channels=ch)
    enc.update(decoded_samples=int(dec.shape[1]), expected_samples=int(n),
               offset_samples=sample_offset(mix, dec), original_codec=a_info.get("codec"),
               original_bit_rate=a_info.get("bit_rate"))
    final = job.artifact("final") or (jd / "final.mp4" if (jd / "final.mp4").is_file() else None)
    video_src = final if final is not None else jd / "source.mp4"
    if not video_src.is_file():
        raise VoiceError("Neither final.mp4 nor source.mp4 is in the job folder.")
    prior = _streams(video_src).get("comment")
    full_comment = f"{prior}; {comment}" if prior and prior != comment and "YouTube Zeus" in prior else comment
    mux = mux_voice(video_src, audio_out, final_out, full_comment)
    mux.update(video_sha256=sha256_file(video_src), out=_streams(final_out), video_src_streams=_streams(video_src))
    timings["encode"] = round(time.monotonic() - t0, 2)
    report("encode", 1.0, "final-voice.mp4 ready")

    # f. checks ----------------------------------------------------------------------------------------------
    checks: dict = {}
    t0 = time.monotonic()
    report("check", 0.0, "measuring")
    ref_mono, _ = read_audio(ref_path, sr=44100, channels=1)
    checks["f0"] = {"original_voice": f0_stats(vocals, sr), "converted_voice": f0_stats(voice, sr),
                    "reference": f0_stats(ref_mono[0], 44100)}
    checks["loudness_lufs"] = {"original_mix": round(integrated_loudness(orig, sr), 2),
                               "new_mix": round(integrated_loudness(mix, sr), 2),
                               "original_voice": round(vocal_lufs, 2),
                               "converted_voice": round(integrated_loudness(voice, sr), 2)}
    checks["artefacts"] = {
        "peak_dbfs": peak_dbfs(mix), "true_peak_dbfs": true_peak_dbfs(mix, sr),
        "clipped_samples": clipped_samples(mix),
        "clicks_converted_voice": click_count(voice, sr), "clicks_original_voice": click_count(vocals, sr),
        "pause_level_db_converted": pause_level_db(voice, vocals, sr),
        "pause_level_db_original": pause_level_db(vocals, vocals, sr),
        "bandwidth_99_hz_converted": bandwidth_hz(voice, sr), "bandwidth_99_hz_original": bandwidth_hz(vocals, sr),
        "separation_residual_db": (sep.get("items") or [{}])[0].get("residual_db")}
    checks["timing"] = timing
    if check_mode == "on" or (check_mode == "auto" and whisper_available()):
        try:
            report("check", 0.3, "whisper: transcript before")
            before = transcribe(audio_src, stems)
            report("check", 0.65, "whisper: transcript after")
            after = transcribe(audio_out, stems)
            checks["transcript"] = {"before": before, "after": after, "wer": word_error_rate(before, after),
                                    "model": whisper_available()[1].name}
            if checks["transcript"]["wer"] and checks["transcript"]["wer"] > 0.15:
                warnings.append(f"The words changed: word error rate {checks['transcript']['wer']:.0%} "
                                f"('{before}' -> '{after}').")
        except (VoiceError, subprocess.SubprocessError, OSError) as exc:
            if check_mode == "on":
                raise VoiceError(f"Transcript check failed: {exc}") from exc
            checks["transcript"] = {"error": str(exc)}
    timings["check"] = round(time.monotonic() - t0, 2)
    timings["total"] = round(time.monotonic() - t_all, 2)
    report("check", 1.0, "done")

    # g. record ---------------------------------------------------------------------------------------------
    rel = lambda p: str(Path(p).relative_to(jd))  # noqa: E731
    record = {
        "engine": engine, "engine_info": conv, "created": _now_iso(), "consent": True, "consent_text": CONSENT_TEXT,
        "options": {"keep_music": keep_music, "grade": grade, "deess": deess_mode, "check": check_mode,
                    "device": device, "seed": seed, **({"steps": int(steps)} if steps else {})},
        "voices": ref_records, "reference": rel(ref_path), "reference_speech_seconds": round(speech_total, 2),
        "source_audio": {"file": Path(audio_src).name, "sha256": sha256_file(audio_src), "sr": sr, "channels": ch,
                         "samples": int(n), "codec": a_info.get("codec"), "bit_rate": a_info.get("bit_rate")},
        "separation": sep,
        "stems": {k: rel(stems / f) for k, f in (("original", "original.wav"), ("vocals", "vocals.wav"),
                                                 ("accompaniment", "accompaniment.wav"),
                                                 ("converted_raw", "vocals-converted-raw.wav"),
                                                 ("converted", "vocals-converted.wav"), ("mix", "mix-voice.wav"))},
        "grade": grade_info, "mix": {"keep_music": keep_music, "samples": int(mix.shape[-1]),
                                     "same_length": int(mix.shape[-1]) == int(n), "time_stretch": False,
                                     "peak_safety_gain_db": round(safety, 2)},
        "audio_voice": audio_out.name, "audio_voice_sha256": sha256_file(audio_out), "encode": enc,
        "final_voice": final_out.name, "final_voice_sha256": sha256_file(final_out), "mux": mux,
        "checks": checks, "timings": timings, "warnings": warnings, "log": log_path.name,
    }

    def save(d: dict) -> None:
        d.setdefault("voices", []).append(record)
        d.setdefault("artifacts", {}).update({"audio_voice": audio_out.name, "final_voice": final_out.name})
    job.reload()
    save(job.data)
    job.save()
    return {"engine": engine, "final_voice": str(final_out), "audio_voice": str(audio_out),
            "stems": str(stems), "voice_record": len(job.get("voices") or []), "seconds": timings["total"],
            "reference_speech_seconds": round(speech_total, 2), "f0": checks["f0"],
            "loudness_lufs": checks["loudness_lufs"], "timing": timing,
            "transcript": checks.get("transcript"), "warnings": warnings}
