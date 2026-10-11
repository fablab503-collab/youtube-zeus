"""Voice step, part 2: speech-to-speech voice conversion with Resemble AI's Chatterbox VC (MIT).

Changes WHO speaks and keeps the words, timing and delivery: the input is cut into S3 speech tokens (25 per
second, frame-synchronous), and S3Gen (flow matching + HiFT vocoder) speaks those same tokens in the reference
voice. One token = 40 ms in, 40 ms out, so the output has the input's timing by construction: nothing is
time-stretched and the lips in the video stay in sync. Same model family as Voice Grader's helper
(mlx-community/chatterbox-multilingual-v3), here the PyTorch package `chatterbox-tts` on MPS or CPU.

Runs inside the voice runtime ($ZEUS_EFFECTS_HOME/voice/.venv, effects/setup-voice.sh) as a subprocess:

    voice/.venv/bin/python -m zfx.engines.vc_chatterbox <task.json>

task.json: {"input": wav, "ref": wav, "out": wav, "sr": out rate, "device": "auto"|"mps"|"cpu",
            "steps": 10, "seed": 0, "lead_ms": 80, "watermark": true}
Writes `out`: mono float32 WAV at `sr` with exactly the input's duration (round(n_in * sr / sr_in) samples).
stdout: JSON lines {"stage","progress","message"}, then {"result": {...}} or {"error": "..."} (exit 1).

Details that keep the timing exact:
- the 16 kHz input gets `lead_ms` (a multiple of 40 ms) of silence in front, because S3Gen silences the first
  20 ms of its output and fades in the next 20 ms; the same span is cut from the output, so the first syllable
  survives and the token grid stays where it was;
- the tail is padded to whole tokens, the output trimmed to the input's length;
- the reference is padded to whole tokens too: with a reference that is not a multiple of 40 ms, S3Gen's prompt
  has one mel frame more than twice its tokens, the generated part loses a frame and the whole voice comes out
  20 ms early (measured 2026-10-11: a steady -18 to -21 ms in every 1.5 s window; 0 +/- 3 ms once padded, on MPS
  and on the CPU, converting to another voice or to the same one).
Output is watermarked with Resemble AI's Perth implicit watermark (as ChatterboxVC.generate does) unless the
task says "watermark": false.

Weights: ResembleAI/chatterbox `s3gen.safetensors` (1.06 GB, MIT; it also holds the S3 tokenizer and the CAMPPlus
speaker encoder), downloaded by setup-voice.sh to $ZEUS_EFFECTS_HOME/models/chatterbox-vc. The reference is used
up to 10 s (Chatterbox's DEC_COND_LEN). Nothing is downloaded at run time.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

NAME = "chatterbox"
LABEL = "Chatterbox VC (Resemble AI, MIT) - speech-to-speech voice conversion"
HF_REPO = "ResembleAI/chatterbox"
LICENSE = "MIT"
TOKEN_MS = 40          # S3 tokens: 25 per second
REF_MAX_SECONDS = 10.0  # ChatterboxVC.DEC_COND_LEN


def weights_dir() -> Path:
    from .. import models_dir
    env = os.environ.get("ZEUS_CHATTERBOX_DIR", "").strip()
    return Path(env).expanduser() if env else models_dir() / "chatterbox-vc"


def voice_python() -> Path:
    from .. import effects_home
    env = os.environ.get("ZEUS_VOICE_PYTHON", "").strip()
    return Path(env).expanduser() if env else effects_home() / "voice" / ".venv" / "bin" / "python"


def status() -> dict:
    """{"ready", "message", ...}: never imports torch, never downloads."""
    py = voice_python()
    w = weights_dir() / "s3gen.safetensors"
    w_ok = w.is_file() and w.stat().st_size > 900_000_000
    ok = py.exists() and w_ok
    if ok:
        msg = "ready"
    elif not py.exists():
        msg = f"The voice runtime is not installed ({py}): run bash effects/setup-voice.sh"
    else:
        msg = f"Chatterbox VC weights missing ({w}): run bash effects/setup-voice.sh"
    return {"ready": ok, "message": msg, "python": str(py), "weights": str(w), "license": LICENSE}


# ----------------------------------------------------------------------------- worker (voice venv only)
def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def pick_device(want: str = "auto") -> str:
    import torch
    want = (want or "auto").lower()
    if want == "cpu":
        return "cpu"
    if want in ("auto", "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _load_mono(path, sr):
    import librosa
    import numpy as np
    wav, _ = librosa.load(str(path), sr=sr, mono=True, res_type="soxr_hq")
    return np.ascontiguousarray(wav, dtype=np.float32)


def convert(task: dict) -> dict:
    import librosa
    import numpy as np
    import soundfile as sf
    import torch
    from chatterbox.models.s3gen import S3GEN_SR
    from chatterbox.models.s3tokenizer import S3_SR
    from chatterbox.vc import ChatterboxVC

    t0 = time.monotonic()
    device = pick_device(task.get("device", "auto"))
    out_sr = int(task.get("sr") or S3GEN_SR)
    steps = int(task.get("steps") or 10)
    seed = int(task.get("seed") or 0)   # S3Gen starts from noise; seeded (MPS runs are still not bit-identical)
    lead_ms = int(task.get("lead_ms", 80))
    lead_ms -= lead_ms % TOKEN_MS
    info_in = sf.info(task["input"])
    n_in = int(info_in.frames)
    n_out = int(round(n_in * out_sr / info_in.samplerate))
    seconds_in = n_in / float(info_in.samplerate)

    _emit({"stage": "convert", "progress": 0.05, "message": f"loading Chatterbox VC on {device}"})
    vc = ChatterboxVC.from_local(weights_dir(), device)
    load_s = time.monotonic() - t0

    _emit({"stage": "convert", "progress": 0.25, "message": "reading the reference voice"})
    ref = _load_mono(task["ref"], S3GEN_SR)
    ref = ref[: int(REF_MAX_SECONDS * S3GEN_SR)]
    # whole tokens, else the output is 20 ms early (see the module docstring)
    ref = np.concatenate([ref, np.zeros((-len(ref)) % (S3GEN_SR * TOKEN_MS // 1000), np.float32)])
    t1 = time.monotonic()
    vc.ref_dict = vc.s3gen.embed_ref(ref, S3GEN_SR, device=device)
    ref_s = time.monotonic() - t1

    x16 = _load_mono(task["input"], S3_SR)
    lead16 = S3_SR * lead_ms // 1000
    hop16 = S3_SR * TOKEN_MS // 1000
    total = lead16 + len(x16)
    tail = (-total) % hop16 + 2 * hop16           # whole tokens + 2 more so the end is not cut
    x16p = np.concatenate([np.zeros(lead16, np.float32), x16, np.zeros(tail, np.float32)])

    _emit({"stage": "convert", "progress": 0.4, "message": f"{seconds_in:.1f} s of voice, {steps} flow steps"})
    t2 = time.monotonic()
    torch.manual_seed(seed)
    with torch.inference_mode():
        audio16 = torch.from_numpy(x16p).float().to(device)[None, ]
        tokens, _ = vc.s3gen.tokenizer(audio16)
        wav, _ = vc.s3gen.inference(speech_tokens=tokens, ref_dict=vc.ref_dict, n_cfm_timesteps=steps)
        wav = wav.squeeze(0).detach().float().cpu().numpy()
    if device == "mps":
        torch.mps.synchronize()
    gen_s = time.monotonic() - t2
    n_tokens = int(tokens.shape[-1])

    lead24 = S3GEN_SR * lead_ms // 1000
    want24 = int(round(seconds_in * S3GEN_SR))
    y = wav[lead24: lead24 + want24]
    if len(y) < want24:
        y = np.concatenate([y, np.zeros(want24 - len(y), np.float32)])
    watermark = bool(task.get("watermark", True))
    if watermark:
        import perth
        y = np.asarray(perth.PerthImplicitWatermarker().apply_watermark(y, sample_rate=S3GEN_SR), dtype=np.float32)
    if out_sr != S3GEN_SR:
        y = librosa.resample(y, orig_sr=S3GEN_SR, target_sr=out_sr, res_type="soxr_hq").astype(np.float32)
    if len(y) >= n_out:
        y = y[:n_out]
    else:
        y = np.concatenate([y, np.zeros(n_out - len(y), np.float32)])
    Path(task["out"]).parent.mkdir(parents=True, exist_ok=True)
    sf.write(task["out"], y, out_sr, subtype="FLOAT")
    peak_gb = None
    try:
        import resource
        peak_gb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30, 2)   # bytes on macOS
    except Exception:
        pass
    return {"engine": NAME, "model": f"{HF_REPO} s3gen.safetensors", "device": device, "steps": steps, "seed": seed,
            "lead_ms": lead_ms, "tokens": n_tokens, "token_ms": TOKEN_MS,
            "input_seconds": round(seconds_in, 4),
            "ref_seconds_used": round(len(ref) / S3GEN_SR, 2), "out_sr": out_sr, "out_samples": int(len(y)),
            "watermark": "Perth implicit (Resemble AI)" if watermark else None,
            "seconds": {"load": round(load_s, 2), "reference": round(ref_s, 2), "convert": round(gen_s, 2),
                        "total": round(time.monotonic() - t0, 2)},
            "realtime_factor": round(gen_s / max(seconds_in, 1e-6), 3), "max_rss_gb": peak_gb}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        _emit({"error": "usage: python -m zfx.engines.vc_chatterbox <task.json>"})
        return 2
    try:
        task = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        try:
            result = convert(task)
        except Exception as exc:   # an op missing on MPS or MPS out of memory: the CPU always works
            if (task.get("device") or "auto") == "cpu":
                raise
            _emit({"stage": "convert", "progress": 0.1, "message": f"MPS failed ({type(exc).__name__}: {exc}); CPU"})
            result = convert(dict(task, device="cpu"))
            result["mps_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        _emit({"stage": "convert", "progress": 1.0, "message": f"done on {result['device']}"})
        _emit({"result": result})
        return 0
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        _emit({"error": f"Chatterbox VC failed: {type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
