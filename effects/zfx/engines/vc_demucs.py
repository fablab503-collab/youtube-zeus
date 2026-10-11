"""Voice step, part 1: split audio into the voice and everything else with Demucs (htdemucs, MIT, Meta).

Runs inside the voice runtime ($ZEUS_EFFECTS_HOME/voice/.venv, made by effects/setup-voice.sh), never in the main
venv: zfx/voice.py starts it as a subprocess

    voice/.venv/bin/python -m zfx.engines.vc_demucs <task.json>

task.json: {"device": "auto"|"mps"|"cpu", "items": [{"input": wav, "out_dir": dir, "sr": out rate or null}]}
For every item it writes <out_dir>/vocals.wav and <out_dir>/accompaniment.wav (= drums + bass + other, what
demucs --two-stems=vocals calls no_vocals), float32 WAV with the input's channels at `sr` (default: the input's
rate), exactly as many samples as the input. The model is loaded once for all items.

stdout: JSON lines {"stage","progress","message"}, then {"result": {...}} or {"error": "..."} (exit 1).

Weights: the official Hugging Face repo of Demucs' author (adefossez/HTDemucs: htdemucs.yaml + 955717e8.safetensors,
84 MB), downloaded by setup-voice.sh to $ZEUS_EFFECTS_HOME/models/htdemucs; nothing is downloaded at run time.
Importing this module from the main venv is cheap (status() only checks files).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

NAME = "demucs"
LABEL = "Demucs htdemucs (MIT, Meta) - voice / accompaniment split"
MODEL_NAME = "htdemucs"
MODEL_SIG = "955717e8"
HF_REPO = "adefossez/HTDemucs"


def weights_dir() -> Path:
    from .. import models_dir
    env = os.environ.get("ZEUS_DEMUCS_DIR", "").strip()
    return Path(env).expanduser() if env else models_dir() / "htdemucs"


def status() -> dict:
    """{"ready", "message", "weights"}: never imports torch, never downloads."""
    d = weights_dir()
    yaml_ok = (d / f"{MODEL_NAME}.yaml").is_file()
    st = d / f"{MODEL_SIG}.safetensors"
    w_ok = st.is_file() and st.stat().st_size > 50_000_000
    ok = yaml_ok and w_ok
    return {"ready": ok, "weights": str(d),
            "message": "ready" if ok else f"Demucs weights missing in {d}: run bash effects/setup-voice.sh"}


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


def load_model():
    """The htdemucs bag from the local copy of the official HF repo (same steps as demucs.hf.get_hf_model)."""
    import yaml
    from demucs.apply import BagOfModels
    from demucs.hf import load_safetensors_model
    d = weights_dir()
    bag = yaml.safe_load((d / f"{MODEL_NAME}.yaml").read_text())
    models = [load_safetensors_model(d / f"{sig}.safetensors") for sig in bag["models"]]
    model = BagOfModels(models, bag.get("weights"), bag.get("segment"))
    model.eval()
    return model


def separate(model, wav, sr: int, device: str):
    """wav: float32 tensor (channels, n) at sr -> (vocals, accompaniment) tensors (channels, n) at sr."""
    import torch
    from demucs.apply import apply_model
    from demucs.audio import convert_audio
    channels, n = wav.shape
    x = convert_audio(wav, sr, model.samplerate, model.audio_channels)
    ref = x.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    x = (x - mean) / std
    with torch.inference_mode():
        out = apply_model(model, x[None], device=device, split=True, overlap=0.25, progress=False, num_workers=0)[0]
    out = out * std + mean
    names = list(model.sources)
    vocals = out[names.index("vocals")]
    accomp = sum(out[i] for i, name in enumerate(names) if name != "vocals")
    vocals = convert_audio(vocals.cpu(), model.samplerate, sr, channels)
    accomp = convert_audio(accomp.cpu(), model.samplerate, sr, channels)

    def fit(t):
        if t.shape[1] >= n:
            return t[:, :n]
        return torch.nn.functional.pad(t, (0, n - t.shape[1]))
    return fit(vocals), fit(accomp)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        _emit({"error": "usage: python -m zfx.engines.vc_demucs <task.json>"})
        return 2
    try:
        task = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        import numpy as np
        import soundfile as sf
        import torch
        t0 = time.monotonic()
        device = pick_device(task.get("device", "auto"))
        _emit({"stage": "separate", "progress": 0.02, "message": f"loading htdemucs on {device}"})
        model = load_model()
        load_s = time.monotonic() - t0
        items, results = task.get("items") or [], []
        for k, item in enumerate(items):
            t1 = time.monotonic()
            data, sr = sf.read(item["input"], dtype="float32", always_2d=True)   # (n, channels)
            wav = torch.from_numpy(np.ascontiguousarray(data.T))
            used = device
            try:
                vocals, accomp = separate(model, wav, sr, device)
            except Exception as exc:   # an op missing on MPS: the CPU always works
                if device == "cpu":
                    raise
                _emit({"stage": "separate", "progress": 0.05, "message": f"MPS failed ({type(exc).__name__}), CPU"})
                used = "cpu"
                vocals, accomp = separate(model, wav, sr, "cpu")
            out_sr = int(item.get("sr") or sr)
            if out_sr != sr:
                from demucs.audio import convert_audio
                vocals = convert_audio(vocals, sr, out_sr, vocals.shape[0])
                accomp = convert_audio(accomp, sr, out_sr, accomp.shape[0])
            out_dir = Path(item["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            v, a = vocals.numpy().T.astype(np.float32), accomp.numpy().T.astype(np.float32)
            sf.write(out_dir / "vocals.wav", v, out_sr, subtype="FLOAT")
            sf.write(out_dir / "accompaniment.wav", a, out_sr, subtype="FLOAT")
            mix = data if out_sr == sr else None
            residual_db = None
            if mix is not None:   # what neither stem explains (separation error), relative to the mix
                err = mix - v - a
                residual_db = float(10 * np.log10((np.mean(err ** 2) + 1e-12) / (np.mean(mix ** 2) + 1e-12)))
            results.append({"input": item["input"], "out_dir": str(out_dir), "device": used, "sr": out_sr,
                            "channels": int(v.shape[1]), "samples": int(v.shape[0]),
                            "seconds": round(time.monotonic() - t1, 2),
                            "residual_db": None if residual_db is None else round(residual_db, 1)})
            _emit({"stage": "separate", "progress": (k + 1) / max(1, len(items)),
                   "message": f"{Path(item['input']).name}: {results[-1]['seconds']} s on {used}"})
        _emit({"result": {"model": f"{MODEL_NAME} ({HF_REPO}, {MODEL_SIG})", "device": device,
                          "load_seconds": round(load_s, 2), "items": results}})
        return 0
    except Exception as exc:  # one plain error line for zfx/voice.py
        import traceback
        traceback.print_exc(file=sys.stderr)
        _emit({"error": f"Demucs failed: {type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
