"""Full character replacement with Wan2.1-VACE-1.3B (Apache-2.0) via diffusers on Apple MPS.

Model: Wan-AI/Wan2.1-VACE-1.3B-diffusers, folder $ZEUS_EFFECTS_HOME/models/Wan2.1-VACE-1.3B-diffusers
(override ZEUS_VACE_DIR), installed by `effects/setup-engines.sh --vace`.

Inputs from the job folder (all 16 fps, same frames):
    control.mp4  -> `video`             (depth people over the original background / mesh overlay)
    mask.mp4     -> `mask`              (white = person = regenerate, black = keep)
    --ref photos -> `reference_images`  (PIL, the new person)

Memory plan for 16 GB unified memory:
    1. tokenizer + UMT5-XXL text encoder alone (bf16, ~5.7 GB), encode prompt + negative prompt,
       delete it, gc.collect(), torch.mps.empty_cache();
    2. load transformer (bf16, ~3.6 GB) + VAE (fp32, 0.5 GB) without a text encoder and call the
       pipeline with prompt_embeds / negative_prompt_embeds; VAE tiling + slicing on;
    3. attention is computed in query chunks when one score matrix would exceed
       ZEUS_VACE_ATTN_MB (default 1024 MB), so 480p x 33 frames does not need ~6 GB per call.

Argument names come from the installed diffusers 0.41.0 source
(diffusers/pipelines/wan/pipeline_wan_vace.py, WanVACEPipeline.__call__):
    video, mask, reference_images, prompt_embeds, negative_prompt_embeds, height, width,
    num_frames, num_inference_steps, guidance_scale, generator, output_type,
    callback_on_step_end(pipe, step, timestep, callback_kwargs) -> dict,
    callback_on_step_end_tensor_inputs, max_sequence_length (default 512)
and encode_prompt(prompt, negative_prompt, do_classifier_free_guidance, num_videos_per_prompt,
    max_sequence_length, device, dtype).
Scheduler: UniPCMultistepScheduler with flow_shift 3.0 ("5.0 for 720P, 3.0 for 480P", model card).

Long clips: windows of N = 4k+1 frames overlapping by 8; each later window is conditioned on the
previous window's last generated frames (mask black there) and the overlap is cross-faded.
Output: result-raw.mp4 at 16 fps, model size (generate.py rescales/retimes and adds the audio).
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import json
import os
import random
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import EngineError

NAME = "vace"
LABEL = "Full replace · slow (Wan2.1-VACE-1.3B, local)"
MODEL_REPO = "Wan-AI/Wan2.1-VACE-1.3B-diffusers"
MODEL_LICENSE = "Apache-2.0"

SIZES = {"480p": (480, 832), "360p": (368, 640)}   # (short side, long side); multiples of 16
DEFAULT_SIZE = "480p"
DEFAULT_FRAMES = 33
DEFAULT_STEPS = 20
GUIDANCE = 5.0
FLOW_SHIFT = 3.0
OVERLAP = 8
FPS = 16
MAX_SEQUENCE_LENGTH = 512        # WanVACEPipeline.__call__ default
MIN_FRAMES = 5

REPLACE_PROMPT = ("replace the person with the person in the reference image, keep the exact motion, "
                  "lips and timing; depth colours are guidance only and must not appear")
# Wan2.1 standard negative prompt (model card) + what our control video must not leak.
NEGATIVE_PROMPT = ("Bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, "
                   "images, static, overall gray, worst quality, low quality, JPEG compression residue, ugly, "
                   "incomplete, extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, "
                   "misshapen limbs, fused fingers, still picture, messy background, three legs, many people "
                   "in the background, walking backwards, depth map colours, heat map, inferno colours, "
                   "glowing silhouette, face mesh lines, wireframe")

Progress = Callable[[str, float, str], None]


# ----------------------------------------------------------------------------- paths / status
def runtime_home() -> Path:
    env = os.environ.get("ZEUS_EFFECTS_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library" / "Application Support" / "YouTube Zeus" / "Effects"


def model_dir() -> Path:
    env = os.environ.get("ZEUS_VACE_DIR", "").strip()
    return Path(env).expanduser() if env else runtime_home() / "models" / "Wan2.1-VACE-1.3B-diffusers"


def _shard_complete(path: Path) -> bool:
    """safetensors file is as long as its header says (catches interrupted downloads)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            header = json.loads(fh.read(n))
        end = max(v["data_offsets"][1] for k, v in header.items() if k != "__metadata__")
        return size == 8 + n + end
    except Exception:
        return False


def missing_files(path: Optional[Path] = None, deep: bool = True) -> list[str]:
    path = path or model_dir()
    if not (path / "model_index.json").is_file():
        return ["model_index.json"]
    missing = []
    for rel in ("tokenizer/tokenizer.json", "scheduler/scheduler_config.json", "transformer/config.json",
                "text_encoder/config.json", "vae/config.json", "vae/diffusion_pytorch_model.safetensors"):
        if not (path / rel).is_file():
            missing.append(rel)
    for sub in ("transformer", "text_encoder"):
        idx = sorted((path / sub).glob("*.safetensors.index.json")) if (path / sub).is_dir() else []
        if not idx:
            missing.append(f"{sub}/*.safetensors.index.json")
            continue
        try:
            shards = sorted(set(json.loads(idx[0].read_text())["weight_map"].values()))
        except (OSError, ValueError, KeyError):
            missing.append(str(idx[0].relative_to(path)))
            continue
        for shard in shards:
            p = path / sub / shard
            if not p.is_file() or (deep and not _shard_complete(p)):
                missing.append(f"{sub}/{shard}")
    vae = path / "vae" / "diffusion_pytorch_model.safetensors"
    if deep and vae.is_file() and not _shard_complete(vae):
        missing.append("vae/diffusion_pytorch_model.safetensors")
    return missing


def check_model(path: Optional[Path] = None) -> Path:
    path = path or model_dir()
    missing = missing_files(path)
    if missing:
        raise EngineError(f"Wan2.1-VACE weights are missing or incomplete in {path} ({', '.join(missing[:4])}). "
                          "Run: effects/setup-engines.sh --vace")
    return path


def status() -> dict:
    path = model_dir()
    missing = missing_files(path, deep=False)
    if missing:
        return {"ready": False, "path": str(path), "license": MODEL_LICENSE,
                "message": "Wan2.1-VACE-1.3B not installed. Run: effects/setup-engines.sh --vace"}
    return {"ready": True, "path": str(path), "license": MODEL_LICENSE, "message": "ready"}


# ----------------------------------------------------------------------------- planning (pure)
def normalize_frames(n: int) -> int:
    """Largest 4k+1 <= n (at least MIN_FRAMES). Wan needs num_frames - 1 divisible by 4."""
    n = max(MIN_FRAMES, int(n))
    return ((n - 1) // 4) * 4 + 1


def frames_up(n: int) -> int:
    """Smallest 4k+1 >= n (at least MIN_FRAMES)."""
    n = max(MIN_FRAMES, int(n))
    return ((n - 1 + 3) // 4) * 4 + 1


def plan_windows(total: int, window: int = DEFAULT_FRAMES, overlap: int = OVERLAP) -> list[tuple[int, int]]:
    """[start, end) frame windows covering `total` frames.

    Every window has the same 4k+1 length; consecutive windows overlap by at least `overlap`
    (capped at half a window); the last one ends exactly at `total`. A clip shorter than one window
    gives a single window of the smallest 4k+1 >= total (end may exceed total: pad with the last frame).
    """
    if total <= 0:
        return []
    window = normalize_frames(window)
    if total <= window:
        return [(0, frames_up(total))]
    overlap = max(0, min(int(overlap), (window - 1) // 2))
    step = window - overlap
    starts = list(range(0, total - window + 1, step))
    if starts[-1] + window < total:
        starts.append(total - window)
    return [(s, s + window) for s in starts]


def target_size(width: int, height: int, size: str = DEFAULT_SIZE) -> tuple[int, int]:
    """(width, height) for the model, matching the clip's orientation."""
    key = (size or DEFAULT_SIZE).lower()
    if key not in SIZES:
        raise EngineError(f"Unknown size '{size}'. Use one of: {', '.join(SIZES)}.")
    short, long = SIZES[key]
    return (long, short) if width >= height else (short, long)


def blend_weights(overlap: int) -> np.ndarray:
    """Weights of the NEW window over an overlap of n frames (linear ramp, excludes 0 and 1)."""
    return np.arange(1, overlap + 1, dtype=np.float32) / (overlap + 1)


# ----------------------------------------------------------------------------- ffmpeg I/O
def _tool(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", shutil.which(name)):
        if cand and Path(cand).is_file():
            return cand
    raise EngineError(f"{name} is not installed (brew install ffmpeg).")


def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        w, h = (int(x) for x in out.split("\n")[0].split("x")[:2])
        return w, h
    except ValueError as exc:
        raise EngineError(f"Could not read the size of {path.name}.") from exc


def read_frames(path: Path, width: int, height: int, gray: bool = False) -> np.ndarray:
    """All frames resized to width x height: uint8 (N,H,W,3) or (N,H,W)."""
    pix, ch = ("gray", 1) if gray else ("rgb24", 3)
    proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-i", str(path), "-vf",
                           f"scale={width}:{height}:flags=bicubic", "-f", "rawvideo", "-pix_fmt", pix, "-"],
                          capture_output=True)
    if proc.returncode != 0:
        raise EngineError(f"Could not read {path.name}: {proc.stderr.decode(errors='replace')[-300:]}")
    frame_bytes = width * height * ch
    n = len(proc.stdout) // frame_bytes
    arr = np.frombuffer(proc.stdout[: n * frame_bytes], dtype=np.uint8)
    return arr.reshape((n, height, width) if gray else (n, height, width, 3))


def write_frames(frames: np.ndarray, path: Path, fps: int = FPS) -> Path:
    n, h, w, _ = frames.shape
    tmp = path.with_name(path.stem + ".partial" + path.suffix)
    cmd = [_tool("ffmpeg"), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, input=np.ascontiguousarray(frames).tobytes(), capture_output=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise EngineError("Could not write the result video: " + proc.stderr.decode(errors="replace")[-300:])
    os.replace(tmp, path)
    return path


# ----------------------------------------------------------------------------- torch helpers
def _empty_cache() -> None:
    gc.collect()
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


def pick_device() -> str:
    import torch
    want = os.environ.get("ZEUS_VACE_DEVICE", "").strip().lower()
    if want:
        return want
    if torch.backends.mps.is_available():
        return "mps"
    raise EngineError("The Apple GPU (Metal/MPS) is not available to PyTorch; VACE would take hours on the CPU.")


def pick_dtype(device: str):
    """bfloat16 when the device supports it (Wan was trained in bf16; fp16 can overflow), else float16."""
    import torch
    want = os.environ.get("ZEUS_VACE_DTYPE", "").strip().lower()
    if want in ("fp16", "float16", "half"):
        return torch.float16
    if want in ("fp32", "float32"):
        return torch.float32
    try:
        x = torch.ones(8, 8, device=device, dtype=torch.bfloat16)
        _ = (x @ x).sum().item()
        return torch.bfloat16
    except Exception:
        return torch.float16


@contextlib.contextmanager
def chunked_attention(budget_mb: Optional[int] = None):
    """Compute SDPA in query chunks on MPS when one score matrix would exceed the budget.

    diffusers' native attention backend calls torch.nn.functional.scaled_dot_product_attention at
    call time with (B, heads, L, D) tensors, so swapping the function here bounds peak memory without
    touching diffusers. Results are identical (softmax is per query row)."""
    import torch
    import torch.nn.functional as F

    budget = int((budget_mb or int(os.environ.get("ZEUS_VACE_ATTN_MB", "1024"))) * 1024 * 1024)
    original = F.scaled_dot_product_attention

    def sdpa(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None, **kwargs):
        if query.device.type != "mps" or is_causal or query.dim() != 4:
            return original(query, key, value, attn_mask=attn_mask, dropout_p=dropout_p,
                            is_causal=is_causal, scale=scale, **kwargs)
        b, h, lq, _ = query.shape
        lk = key.shape[-2]
        per_row = b * h * lk * query.element_size()
        if per_row * lq <= budget:
            return original(query, key, value, attn_mask=attn_mask, dropout_p=dropout_p,
                            is_causal=is_causal, scale=scale, **kwargs)
        chunk = max(128, budget // per_row)
        outs = []
        for i in range(0, lq, chunk):
            mask = attn_mask
            if mask is not None and mask.dim() >= 2 and mask.shape[-2] == lq:
                mask = mask[..., i:i + chunk, :]
            outs.append(original(query[:, :, i:i + chunk], key, value, attn_mask=mask, dropout_p=dropout_p,
                                 is_causal=False, scale=scale, **kwargs))
        return torch.cat(outs, dim=2)

    F.scaled_dot_product_attention = sdpa
    try:
        yield
    finally:
        F.scaled_dot_product_attention = original


def _triple(v) -> tuple:
    return tuple(v) if isinstance(v, (tuple, list)) else (v, v, v)


@contextlib.contextmanager
def conv3d_as_conv2d():
    """Run 3-D convolutions on MPS as a sum of 2-D convolutions over the temporal kernel taps.

    PyTorch's MPS conv3d keeps growing driver memory ("other allocations") on the Wan VAE: 17 frames at
    640x368 hit the 20 GiB MPS limit even with 128 px tiles. conv2d on MPS is mature, and
    conv3d(x, w)[t] = sum_i conv2d(x[t*stride + i*dilation], w[:, :, i]) is exact, so swapping
    torch.nn.functional.conv3d (looked up at call time by nn.Conv3d) keeps the VAE on the GPU."""
    import torch
    import torch.nn.functional as F

    original = F.conv3d

    def conv3d(input, weight, bias=None, stride=1, padding=0, dilation=1, groups=1):
        if input.device.type != "mps" or input.dim() != 5 or isinstance(padding, str):
            return original(input, weight, bias, stride, padding, dilation, groups)
        st, sh, sw = _triple(stride)
        pt, ph, pw = _triple(padding)
        dt, dh, dw = _triple(dilation)
        if pt:
            input = F.pad(input, (0, 0, 0, 0, pt, pt))
        b, c, t, h, w = input.shape
        kt = weight.shape[2]
        t_out = (t - dt * (kt - 1) - 1) // st + 1
        out = None
        for i in range(kt):
            start = i * dt
            frames = input[:, :, start:start + st * (t_out - 1) + 1:st]
            x2 = frames.transpose(1, 2).reshape(b * t_out, c, h, w)
            # .contiguous(): MPS conv2d returns wrong values for a strided weight view (torch 2.11)
            y = F.conv2d(x2, weight[:, :, i].contiguous(), None, (sh, sw), (ph, pw), (dh, dw), groups)
            del x2
            out = y if out is None else out.add_(y)
        if bias is not None:
            out = out + bias.view(1, -1, 1, 1)
        _, c_out, h_out, w_out = out.shape
        return out.reshape(b, t_out, c_out, h_out, w_out).transpose(1, 2).contiguous()

    F.conv3d = conv3d
    try:
        yield
    finally:
        F.conv3d = original


def _transformers_dtype_kw() -> str:
    import transformers
    try:
        major, minor = (int(x) for x in transformers.__version__.split(".")[:2])
    except ValueError:
        return "dtype"
    return "dtype" if (major, minor) >= (4, 56) else "torch_dtype"


def embed_cache_dir() -> Path:
    return runtime_home() / "cache" / "vace-prompt-embeds"


def _cache_path(text: str, dtype) -> Path:
    key = hashlib.sha256(f"{MODEL_REPO}|{MAX_SEQUENCE_LENGTH}|{dtype}|{text}".encode("utf-8")).hexdigest()[:40]
    return embed_cache_dir() / f"{key}.pt"


def _cache_load(text: str, dtype):
    import torch
    p = _cache_path(text, dtype)
    if not p.is_file():
        return None
    try:
        t = torch.load(p, map_location="cpu", weights_only=True)
        return t if tuple(t.shape[-2:]) == (MAX_SEQUENCE_LENGTH, 4096) else None
    except Exception:
        return None


def _cache_save(text: str, dtype, tensor) -> None:
    import torch
    p = _cache_path(text, dtype)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        torch.save(tensor.contiguous().clone(), tmp)
        os.replace(tmp, p)
    except OSError:
        pass


def encode_prompts(path: Path, prompt: str, negative: str, device: str, dtype, report: Optional[Callable] = None):
    """Step 1 of the memory plan: UMT5 alone -> (prompt_embeds, negative_prompt_embeds) on CPU, [1, 512, 4096].

    The stored UMT5-XXL encoder is bf16 (5.7 B params = 11.4 GB), the biggest single memory cost, so:
      * embeddings are cached on disk by text (the fixed negative prompt is encoded once, ever);
      * texts are encoded at their real token length and zero-padded to 512 afterwards — identical to
        the pipeline's own padding (pad tokens are masked, the pipeline zeroes those rows), ~4x less work.
    Encoding goes through the pipeline's public encode_prompt (prompt_clean, tokenizer, trimming)."""
    import torch

    texts = list(dict.fromkeys([prompt, negative]))
    found = {t: _cache_load(t, dtype) for t in texts}
    todo = [t for t in texts if found[t] is None]
    if todo:
        from diffusers import WanVACEPipeline
        from diffusers.pipelines.wan.pipeline_wan_vace import prompt_clean
        from transformers import AutoTokenizer, UMT5EncoderModel

        tokenizer = AutoTokenizer.from_pretrained(str(path / "tokenizer"))
        text_encoder = UMT5EncoderModel.from_pretrained(str(path / "text_encoder"),
                                                        **{_transformers_dtype_kw(): dtype})
        text_encoder.to(device).eval()
        helper = WanVACEPipeline(tokenizer=tokenizer, text_encoder=text_encoder, vae=None, scheduler=None,
                                 transformer=None)
        try:
            for t in todo:
                n_tokens = len(tokenizer(prompt_clean(t), add_special_tokens=True, truncation=True,
                                         max_length=MAX_SEQUENCE_LENGTH).input_ids)
                with torch.no_grad():
                    emb, _ = helper.encode_prompt(
                        prompt=t,
                        do_classifier_free_guidance=False,
                        num_videos_per_prompt=1,
                        max_sequence_length=n_tokens,
                        device=torch.device(device),
                        dtype=dtype,
                    )
                emb = emb.to("cpu")
                emb = torch.nn.functional.pad(emb, (0, 0, 0, MAX_SEQUENCE_LENGTH - emb.shape[1]))
                found[t] = emb
                _cache_save(t, dtype, emb)
        finally:
            del helper, text_encoder, tokenizer
            _empty_cache()
    elif report:
        report("generate", 0.03, "prompt embeddings from cache (text encoder not loaded)")
    return found[prompt], found[negative]


def load_pipeline(path: Path, device: str, dtype):
    """Step 2: transformer + VAE + scheduler, no text encoder."""
    import torch
    from diffusers import AutoencoderKLWan, UniPCMultistepScheduler, WanVACEPipeline, WanVACETransformer3DModel

    transformer = WanVACETransformer3DModel.from_pretrained(str(path), subfolder="transformer", torch_dtype=dtype)
    vae = AutoencoderKLWan.from_pretrained(str(path), subfolder="vae", torch_dtype=torch.float32)
    scheduler = UniPCMultistepScheduler.from_pretrained(str(path), subfolder="scheduler")
    scheduler = UniPCMultistepScheduler.from_config(scheduler.config, flow_shift=FLOW_SHIFT)
    pipe = WanVACEPipeline(tokenizer=None, text_encoder=None, vae=vae, scheduler=scheduler, transformer=transformer)
    pipe.to(device)
    for fn in ("enable_tiling", "enable_slicing"):
        if hasattr(pipe.vae, fn):
            getattr(pipe.vae, fn)()
    pipe.set_progress_bar_config(disable=True)
    return pipe


def _load_refs(refs: Sequence) -> list:
    from PIL import Image, ImageOps

    images = []
    for r in refs:
        p = Path(r).expanduser()
        try:
            img = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        except Exception as exc:
            raise EngineError(f"Could not open the reference photo {p.name}: {exc}") from exc
        img.thumbnail((1024, 1024))
        images.append(img)
    if not images:
        raise EngineError("Full replace needs at least one reference photo (--ref).")
    return images


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "mps backend out of memory" in text or "failed to allocate" in text


# ----------------------------------------------------------------------------- run
def run(job_dir, refs: Sequence, out_path, progress: Optional[Progress] = None, *,
        prompt: str = "", frames: Optional[int] = None, steps: Optional[int] = None,
        size: Optional[str] = None, seed: Optional[int] = None,
        control: Optional[Path] = None, mask: Optional[Path] = None, **_ignored) -> dict:
    job_dir = Path(job_dir)
    out_path = Path(out_path)
    report: Progress = progress or (lambda *a, **k: None)
    path = check_model()
    control = Path(control) if control else job_dir / "control.mp4"
    mask = Path(mask) if mask else job_dir / "mask.mp4"
    for p in (control, mask):
        if not p.is_file():
            raise EngineError(f"{p.name} is missing in the job folder. Run prepare again.")

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    try:
        import torch
        from PIL import Image
    except ImportError as exc:
        raise EngineError(f"PyTorch/Pillow are not installed in this Python ({exc}). Run effects/setup.sh.") from exc
    try:
        import diffusers  # noqa: F401
    except ImportError as exc:
        raise EngineError("diffusers is not installed in this Python. Run effects/setup.sh.") from exc

    size = (size or DEFAULT_SIZE).lower()
    window = normalize_frames(frames or DEFAULT_FRAMES)
    steps = max(1, int(steps or DEFAULT_STEPS))
    seed = int(seed) if seed is not None else random.randint(0, 2**31 - 1)
    device = pick_device()
    dtype = pick_dtype(device)
    te_device = os.environ.get("ZEUS_VACE_TEXT_DEVICE", "").strip().lower() or device

    src_w, src_h = video_size(control)
    width, height = target_size(src_w, src_h, size)
    ctrl = read_frames(control, width, height)
    msk = read_frames(mask, width, height, gray=True)
    total = min(len(ctrl), len(msk))
    if total == 0:
        raise EngineError("control.mp4 / mask.mp4 have no frames. Run prepare again.")
    windows = plan_windows(total, window, OVERLAP)
    ref_images = _load_refs(refs)
    full_prompt = (prompt.strip().rstrip(".") + ". " if prompt and prompt.strip() else "") + REPLACE_PROMPT

    timings: dict = {}
    t_all = time.monotonic()
    report("generate", 0.0, f"VACE {width}x{height}, {total} frames in {len(windows)} window(s) of "
           f"{windows[0][1] - windows[0][0]}, {steps} steps, seed {seed}")

    # 1) text encoder alone
    t0 = time.monotonic()
    report("generate", 0.01, f"encoding the prompt (UMT5 text encoder on {te_device})")
    try:
        prompt_embeds, negative_prompt_embeds = encode_prompts(path, full_prompt, NEGATIVE_PROMPT, te_device, dtype,
                                                                 report)
    except RuntimeError as exc:
        if te_device != "cpu" and _is_oom(exc):
            _empty_cache()
            report("generate", 0.01, "text encoder out of GPU memory, retrying on the CPU")
            te_device = "cpu"
            prompt_embeds, negative_prompt_embeds = encode_prompts(path, full_prompt, NEGATIVE_PROMPT, "cpu", dtype)
        else:
            raise
    timings["encode_prompt"] = round(time.monotonic() - t0, 1)

    # 2) transformer + VAE
    t0 = time.monotonic()
    report("generate", 0.04, "loading the Wan2.1-VACE transformer and VAE")
    pipe = load_pipeline(path, device, dtype)
    prompt_embeds = prompt_embeds.to(device=device, dtype=dtype)
    negative_prompt_embeds = negative_prompt_embeds.to(device=device, dtype=dtype)
    timings["load"] = round(time.monotonic() - t0, 1)

    out = np.zeros((total, height, width, 3), dtype=np.uint8)
    generator = torch.Generator(device="cpu").manual_seed(seed)
    prev_end = 0
    t0 = time.monotonic()
    lo, hi = 0.06, 0.97
    try:
        for wi, (start, end) in enumerate(windows):
            n = end - start
            idx = [min(i, total - 1) for i in range(start, end)]
            vid = ctrl[idx].copy()
            mk = msk[idx].copy()
            carry = max(0, min(prev_end, end) - start) if wi > 0 else 0
            if carry:
                # continue from what we already generated: keep those frames (mask black)
                vid[:carry] = out[start:start + carry]
                mk[:carry] = 0
            video_imgs = [Image.fromarray(f) for f in vid]
            mask_imgs = [Image.fromarray(m) for m in mk]  # 2-D uint8 -> mode "L"

            def on_step(pipeline, step, timestep, callback_kwargs, _wi=wi):
                frac = (_wi + (step + 1) / steps) / len(windows)
                report("generate", lo + (hi - lo) * frac,
                       f"window {_wi + 1}/{len(windows)} · step {step + 1}/{steps}")
                return callback_kwargs

            with chunked_attention():
                result = pipe(
                    video=video_imgs,
                    mask=mask_imgs,
                    reference_images=ref_images,
                    prompt_embeds=prompt_embeds,
                    negative_prompt_embeds=negative_prompt_embeds,
                    height=height,
                    width=width,
                    num_frames=n,
                    num_inference_steps=steps,
                    guidance_scale=GUIDANCE,
                    generator=generator,
                    output_type="np",
                    callback_on_step_end=on_step,
                    callback_on_step_end_tensor_inputs=["latents"],
                    max_sequence_length=MAX_SEQUENCE_LENGTH,
                ).frames[0]
            res = (np.clip(np.asarray(result, dtype=np.float32), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
            del result
            if res.shape[1:3] != (height, width):
                raise EngineError(f"VACE returned {res.shape[2]}x{res.shape[1]} instead of {width}x{height}.")
            valid = min(end, total) - start
            overlap_n = max(0, prev_end - start) if wi > 0 else 0
            weights = blend_weights(overlap_n)
            for j in range(valid):
                i = start + j
                if j < overlap_n:
                    a = weights[j]
                    out[i] = (out[i].astype(np.float32) * (1 - a) + res[j].astype(np.float32) * a + 0.5).astype(np.uint8)
                else:
                    out[i] = res[j]
            prev_end = start + valid
            del res, video_imgs, mask_imgs
            _empty_cache()
    except RuntimeError as exc:
        if _is_oom(exc):
            raise EngineError("Ran out of memory during VACE generation. Close other apps, then try "
                              "--size 360p and/or --frames 17.") from exc
        raise
    finally:
        del pipe
        _empty_cache()
    timings["denoise"] = round(time.monotonic() - t0, 1)

    report("generate", 0.98, "writing result-raw.mp4")
    write_frames(out, out_path, FPS)
    timings["total"] = round(time.monotonic() - t_all, 1)
    report("generate", 1.0, f"VACE done in {timings['total']:.0f} s")
    return {
        "engine": NAME,
        "model": MODEL_REPO,
        "license": MODEL_LICENSE,
        "seconds": timings["total"],
        "timings": timings,
        "width": width,
        "height": height,
        "frames": total,
        "fps": FPS,
        "windows": [list(w) for w in windows],
        "window_frames": windows[0][1] - windows[0][0],
        "overlap": OVERLAP,
        "steps": steps,
        "guidance_scale": GUIDANCE,
        "flow_shift": FLOW_SHIFT,
        "seed": seed,
        "device": device,
        "text_encoder_device": te_device,
        "dtype": str(dtype).replace("torch.", ""),
        "prompt": full_prompt,
    }
