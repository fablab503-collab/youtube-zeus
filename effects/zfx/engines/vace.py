"""Full character replacement with Wan2.1-VACE-1.3B (Apache-2.0) via diffusers on Apple MPS.

Model: Wan-AI/Wan2.1-VACE-1.3B-diffusers, folder $ZEUS_EFFECTS_HOME/models/Wan2.1-VACE-1.3B-diffusers
(override ZEUS_VACE_DIR), installed by `effects/setup-engines.sh --vace`.

Inputs from the job folder (all 16 fps, same frames):
    control.mp4  -> `video`             (depth people over the original background / mesh overlay)
    mask.mp4     -> `mask`              (white = person = regenerate, black = keep)
    --ref photos -> `reference_images`  (PIL, the new person)

Memory plan for 16 GB unified memory (measurements in effects/ENGINES.md):
    1. prompt + negative prompt -> UMT5-XXL embeddings, cached on disk by text; a cache miss runs the
       11.4 GB bf16 text encoder on the CPU in a separate short-lived process (python -m zfx.engines.vace
       encode; mmapped weights, ~0.7 GB footprint), so this process never holds it;
    2. transformer from transformer-half/ (fp16 copy written once by setup-engines.sh --vace; mmap,
       nothing cast), its 45 blocks streamed to the GPU one at a time with one sequential read each;
       VAE fp16 on the GPU, 256 px tiles, its 3-D convs run as 2-D convs, GPU sync after each conv;
    3. attention as chunked matmul/softmax (PyTorch's MPS SDPA is slow and holds GBs of driver memory);
       the UniPC scheduler step runs on the CPU (on MPS it does not converge: burned, blurred output);
    4. PyTorch MPS allocator watermarks lowered so its buffer cache cannot push the Mac into swap.

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
Crops (zfx/cropbox.py): each window is generated on a box around the person (the person fills the 640x368 canvas),
its context taken from source.mp4 at full resolution; ZEUS_VACE_CROP=0 generates the whole frame as before.
Output: result-raw.mp4 (16 fps, work size, crops pasted back) and result-crops/ + result-crops.json, which
zfx/hires.py composites into the original frames at the source resolution (generate.py, with the audio).
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
DEFAULT_SIZE = "360p"              # 16 GB Mac: 480p takes ~2x as long per window (ENGINES.md)
DEFAULT_FRAMES = 33
DEFAULT_STEPS = 20
DEFAULT_HALF = "F16"             # transformer-half dtype (F16 or BF16), see ENGINES.md
GUIDANCE = 5.0
FLOW_SHIFT = 3.0
OVERLAP = 8
FPS = 16
MAX_SEQUENCE_LENGTH = 512        # WanVACEPipeline.__call__ default
MIN_FRAMES = 5
MARGIN_SHARE = 0.12              # crop box margin around the window's union of mask boxes (zfx.cropbox.MARGIN)

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


# ----------------------------------------------------------------------------- half-precision copy
# The downloaded transformer is 2.15 B params stored as 5.7 GB fp32 (base blocks) + 1.5 GB bf16 (VACE
# blocks). Loading those shards and casting them costs shard + model memory at once, so
# setup-engines.sh --vace writes one half-precision copy, transformer-half/ (4.3 GB fp16), once.
# (The UMT5-XXL text encoder is already bf16 on disk, 11.4 GB; it only ever runs in its own process.)
# The converter streams tensor by tensor (peak = one tensor) and needs numpy only.
HALF_DIRS = {"transformer": "transformer-half"}
HALF_FILES = {"transformer": "diffusion_pytorch_model.safetensors"}
HALF_MARKER = "zeus-half.json"
# WanVACETransformer3DModel._keep_in_fp32_modules (diffusers 0.41): kept fp32 in the copy so loading
# with torch_dtype=float16 does not round them through half precision first.
KEEP_FP32_MODULES = ("rope", "time_embedder", "scale_shift_table", "norm1", "norm2", "norm3")
_ST_SIZE = {"F32": 4, "F16": 2, "BF16": 2}
_FP16_MAX = 65504.0


def half_dir(component: str = "transformer", path: Optional[Path] = None) -> Path:
    return (path or model_dir()) / HALF_DIRS[component]


def _read_st_header(path: Path) -> tuple[int, dict]:
    with open(path, "rb") as fh:
        n = struct.unpack("<Q", fh.read(8))[0]
        return n, json.loads(fh.read(n))


def keep_fp32(name: str, modules: Sequence[str] = KEEP_FP32_MODULES) -> bool:
    return any(part in modules for part in name.split("."))


def plan_half_header(tensors: dict, half: str, keep: Callable[[str], bool]) -> dict:
    """safetensors header for the half copy: {name: {dtype, shape, data_offsets}} in name order.

    `tensors` maps name -> (dtype "F32"/"F16"/"BF16", shape). Every tensor becomes `half` ("F16"/"BF16")
    unless keep(name), which keeps its stored dtype."""
    header: dict = {}
    offset = 0
    for name in sorted(tensors):
        dtype, shape = tensors[name]
        if dtype not in _ST_SIZE:
            raise EngineError(f"Unsupported tensor dtype {dtype} for {name}.")
        out = dtype if keep(name) else half
        size = int(np.prod(shape, dtype=np.int64)) * _ST_SIZE[out] if shape else _ST_SIZE[out]
        header[name] = {"dtype": out, "shape": list(shape), "data_offsets": [offset, offset + size]}
        offset += size
    return header


def to_f32(raw: bytes, dtype: str) -> np.ndarray:
    if dtype == "F32":
        return np.frombuffer(raw, dtype="<f4")
    if dtype == "F16":
        return np.frombuffer(raw, dtype="<f2").astype(np.float32)
    if dtype == "BF16":
        return (np.frombuffer(raw, dtype="<u2").astype(np.uint32) << 16).view(np.float32)
    raise EngineError(f"Unsupported tensor dtype {dtype}.")


def f32_to_half_bytes(raw: bytes, half: str) -> bytes:
    """fp32 little-endian bytes -> F16 or BF16 bytes (round to nearest even)."""
    a = np.frombuffer(raw, dtype="<f4")
    if half == "F16":
        return a.astype("<f2").tobytes()
    u = a.view("<u4").astype(np.uint64)
    u = (u + 0x7FFF + ((u >> 16) & 1)) >> 16
    return u.astype("<u2").tobytes()


def convert_half(src: Path, dst: Path, out_name: str, half: str = "F16",
                 keep: Callable[[str], bool] = lambda n: False,
                 report: Optional[Callable[[float, str], None]] = None) -> Path:
    """Stream a sharded safetensors folder (src/*.safetensors.index.json) into one half-precision file.

    Writes dst/<out_name> + config.json + zeus-half.json; atomic (builds dst.partial, then renames).
    A tensor whose values exceed the float16 range keeps its stored dtype (no inf in the copy)."""
    half = {"fp16": "F16", "float16": "F16", "bf16": "BF16", "bfloat16": "BF16"}.get(half.lower(), half)
    if half not in ("F16", "BF16"):
        raise EngineError(f"Unknown half dtype {half}.")
    idx = sorted(src.glob("*.safetensors.index.json"))
    shard_names = sorted(set(json.loads(idx[0].read_text())["weight_map"].values())) if idx else \
        sorted(p.name for p in src.glob("*.safetensors"))
    if not shard_names:
        raise EngineError(f"No safetensors weights in {src}.")
    where: dict = {}
    tensors: dict = {}
    for shard in shard_names:
        n, header = _read_st_header(src / shard)
        for name, info in header.items():
            if name == "__metadata__":
                continue
            where[name] = (shard, 8 + n + info["data_offsets"][0], info["data_offsets"][1] - info["data_offsets"][0])
            tensors[name] = (info["dtype"], tuple(info["shape"]))

    def read(name: str) -> bytes:
        shard, start, size = where[name]
        with open(src / shard, "rb") as fh:
            fh.seek(start)
            return fh.read(size)

    # values beyond the fp16 range would become inf: keep those tensors as stored
    too_big = set()
    if half == "F16":
        for name, (dtype, _shape) in tensors.items():
            if dtype != "F16" and not keep(name):
                a = to_f32(read(name), dtype)
                if a.size and float(np.abs(a).max()) > _FP16_MAX:
                    too_big.add(name)
    header = plan_half_header(tensors, half, lambda n: keep(n) or n in too_big)
    header["__metadata__"] = {"format": "pt"}
    blob = json.dumps(header, separators=(",", ":")).encode("utf-8")
    blob += b" " * (-len(blob) % 8)

    tmp = dst.with_name(dst.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    names = sorted(n for n in header if n != "__metadata__")
    with open(tmp / out_name, "wb") as out:
        out.write(struct.pack("<Q", len(blob)))
        out.write(blob)
        for i, name in enumerate(names):
            raw = read(name)
            if header[name]["dtype"] != tensors[name][0]:
                raw = f32_to_half_bytes(to_f32(raw, tensors[name][0]).tobytes(), half)
            start, end = header[name]["data_offsets"]
            if len(raw) != end - start:
                raise EngineError(f"Size mismatch converting {name}.")
            out.write(raw)
            if report and (i % 50 == 0 or i == len(names) - 1):
                report((i + 1) / len(names), f"{src.name}: {i + 1}/{len(names)} tensors")
        out.flush()
        os.fsync(out.fileno())
    if (src / "config.json").is_file():
        shutil.copyfile(src / "config.json", tmp / "config.json")
    (tmp / HALF_MARKER).write_text(json.dumps({
        "dtype": half, "source": {s: (src / s).stat().st_size for s in shard_names},
        "kept": sorted(n for n in names if header[n]["dtype"] != half),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=1))
    shutil.rmtree(dst, ignore_errors=True)
    os.replace(tmp, dst)
    return dst / out_name


def half_ready(component: str = "transformer", path: Optional[Path] = None) -> Optional[str]:
    """'F16' / 'BF16' when a complete half copy of `component` exists, else None."""
    d = half_dir(component, path)
    f = d / HALF_FILES[component]
    try:
        marker = json.loads((d / HALF_MARKER).read_text())
    except (OSError, ValueError):
        return None
    if not (d / "config.json").is_file() or not f.is_file() or not _shard_complete(f):
        return None
    return marker.get("dtype")


def make_half_copies(path: Optional[Path] = None, half: Optional[str] = None,
                     report: Optional[Callable[[float, str], None]] = None) -> dict:
    """Create transformer-half/ unless a complete copy in that dtype exists. Returns {"transformer": dtype}."""
    path = path or model_dir()
    half = (half or os.environ.get("ZEUS_VACE_HALF", "") or DEFAULT_HALF).upper()
    half = {"FP16": "F16", "FLOAT16": "F16", "BFLOAT16": "BF16"}.get(half, half)
    if half_ready("transformer", path) != half:
        convert_half(path / "transformer", half_dir("transformer", path), HALF_FILES["transformer"], half,
                     keep_fp32, report)
    return {"transformer": half_ready("transformer", path)}


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
    half = half_ready("transformer", path)
    if not half:
        return {"ready": True, "path": str(path), "license": MODEL_LICENSE, "transformer_half": None,
                "message": "ready; run effects/setup-engines.sh --vace once more to add transformer-half/ "
                           "(lower memory on 16 GB Macs)"}
    return {"ready": True, "path": str(path), "license": MODEL_LICENSE, "transformer_half": half, "message": "ready"}


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


def pick_dtype(device: str, path: Optional[Path] = None):
    """Transformer compute dtype: ZEUS_VACE_DTYPE (fp16 / bf16 / fp32), else the dtype of transformer-half/,
    else DEFAULT_HALF. Measured on the M2 Pro (ENGINES.md): fp16 and bf16 both give clean output."""
    import torch
    table = {"fp16": torch.float16, "float16": torch.float16, "half": torch.float16, "f16": torch.float16,
             "bf16": torch.bfloat16, "bfloat16": torch.bfloat16, "fp32": torch.float32, "float32": torch.float32}
    want = os.environ.get("ZEUS_VACE_DTYPE", "").strip().lower()
    if want in table:
        return table[want]
    have = half_ready("transformer", path)
    return table[(have or DEFAULT_HALF).lower()]


# PyTorch's MPS allocator caches freed buffers until it reaches LOW x recommendedMaxWorkingSetSize
# (default 1.4 = ~16.6 GB on a 16 GB Mac) and refuses past HIGH x (default 1.7 = ~20 GB), so on 16 GB
# the cache alone pushes the Mac into swap. These must be in the environment before the first MPS
# allocation; an explicit value in the environment wins.
MPS_HIGH_WATERMARK = "0.9"   # ~10.7 GB hard cap for this process on a 16 GB Mac
MPS_LOW_WATERMARK = "0.5"    # start returning cached buffers above ~5.9 GB


def configure_mps_memory() -> dict:
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", MPS_HIGH_WATERMARK)
    os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", MPS_LOW_WATERMARK)
    try:
        high = float(os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"])
        low = float(os.environ["PYTORCH_MPS_LOW_WATERMARK_RATIO"])
        if high > 0 and low > high:
            os.environ["PYTORCH_MPS_LOW_WATERMARK_RATIO"] = str(high)
    except ValueError:
        pass
    return {k: os.environ[k] for k in ("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "PYTORCH_MPS_LOW_WATERMARK_RATIO")}


def _mps_sync() -> None:
    import torch
    torch.mps.synchronize()


@contextlib.contextmanager
def mps_sync_every(modules: Sequence, every: int = 1, sync: Optional[Callable[[], None]] = None):
    """Wait for the GPU after every `every` forward calls of `modules` (forward hooks, removed on exit).

    MPS runs asynchronously: Python enqueues kernels far ahead of the GPU and every queued kernel keeps
    its buffers (and MPSGraph's own temporaries, the "other allocations" of the OOM message) alive.
    On the Wan VAE that alone reached the 20 GB MPS limit for 9 frames at 640x368; with a sync after
    each causal conv the same encode+decode peaks at ~4.6 GB (ENGINES.md)."""
    sync = sync or _mps_sync
    count = [0]
    if every <= 0:
        yield count
        return

    def hook(_module, _inputs, _output):
        count[0] += 1
        if count[0] % max(1, int(every)) == 0:
            sync()

    handles = [m.register_forward_hook(hook) for m in modules]
    try:
        yield count
    finally:
        for h in handles:
            h.remove()


def vae_convs(vae) -> list:
    return [m for m in vae.modules() if m.__class__.__name__ == "WanCausalConv3d"]


def transformer_blocks(transformer) -> list:
    return [m for name in ("blocks", "vace_blocks") for m in getattr(transformer, name, [])]


def _block_tensors(module) -> list:
    return [t for _, t in module.named_parameters()] + [t for _, t in module.named_buffers()]


_TORCH_ST_DTYPES = {"F32": "float32", "F16": "float16", "BF16": "bfloat16"}


def block_read_plan(weights_file: Path, prefixes: Sequence[str]) -> dict:
    """{prefix: (start, end, {relative name: (offset in range, dtype, shape)})} for blocks whose tensors are
    stored contiguously (safetensors keeps tensors sorted by name, so "blocks.7." is one byte range)."""
    n, header = _read_st_header(weights_file)
    plan = {}
    for prefix in prefixes:
        items = {k[len(prefix):]: v for k, v in header.items() if k != "__metadata__" and k.startswith(prefix)}
        if not items:
            continue
        start = min(v["data_offsets"][0] for v in items.values())
        end = max(v["data_offsets"][1] for v in items.values())
        if sum(v["data_offsets"][1] - v["data_offsets"][0] for v in items.values()) != end - start:
            continue   # not contiguous: leave this block to the mmap path
        plan[prefix] = (8 + n + start, 8 + n + end,
                        {k: (v["data_offsets"][0] - start, v["dtype"], v["shape"]) for k, v in items.items()})
    return plan


@contextlib.contextmanager
def stream_blocks(transformer, device: str, weights_file: Optional[Path] = None,
                  sync: Optional[Callable[[], None]] = None, release: Optional[Callable[[], None]] = None):
    """Copy each transformer block's weights to `device` only while it runs; everything else (embeddings,
    rope, norm_out, proj_out, ~0.1 GB) moves for good.

    With every weight resident on the GPU (4.3 GB fp16 + MPS temporaries, 7.4 GB footprint) the 16 GB Mac
    swapped ~50 GB in 200 s next to the YouTube Zeus app (whisper + Ollama, ~8.6 GB wired). Streaming keeps
    the footprint near 3 GB. The CPU side stays the mmap of transformer-half/ (clean file pages, dropped
    under pressure instead of swapped); with `weights_file` each block is read with one sequential pread
    (~3 GB/s) instead of page faults on that mmap (~0.2 GB/s once macOS has dropped the pages).
    The GPU finishes each block before its weights are released."""
    import torch

    sync = sync or (_mps_sync if device == "mps" else (lambda: None))
    prefixes = {}
    for name in ("blocks", "vace_blocks"):
        for i, blk in enumerate(getattr(transformer, name, [])):
            prefixes[id(blk)] = f"{name}.{i}."
    blocks = transformer_blocks(transformer)
    plan = block_read_plan(Path(weights_file), list(prefixes.values())) if weights_file else {}
    fd = os.open(str(weights_file), os.O_RDONLY) if plan else None
    for name, child in transformer.named_children():
        if name not in ("blocks", "vace_blocks"):
            child.to(device)
    for t in list(transformer.parameters(recurse=False)) + list(transformer.buffers(recurse=False)):
        t.data = t.data.to(device)
    home: dict = {}

    def read_block(prefix: str):
        start, end, items = plan[prefix]
        buf = bytearray(end - start)
        view, got = memoryview(buf), 0
        while got < len(buf):
            k = os.preadv(fd, [view[got:]], start + got)
            if k <= 0:
                raise EngineError(f"Could not read {Path(weights_file).name} ({prefix}).")
            got += k
        out = {}
        for rel, (off, dtype, shape) in items.items():
            dt = getattr(torch, _TORCH_ST_DTYPES[dtype])
            count = int(np.prod(shape)) if shape else 1
            out[rel] = torch.frombuffer(buf, dtype=dt, count=count, offset=off).view(shape)
        return out

    def onload(module, _args):
        named = list(module.named_parameters()) + list(module.named_buffers())
        home[id(module)] = (module, [t.data for _, t in named])
        fresh = read_block(prefixes[id(module)]) if prefixes.get(id(module)) in plan else {}
        for rel, t in named:
            src = fresh.get(rel)
            t.data = (src if src is not None else t.data).to(device=device, dtype=t.dtype)

    def offload(module, _args=None, _output=None):
        sync()
        for t, cpu in zip(_block_tensors(module), home.pop(id(module), (None, []))[1]):
            t.data = cpu
        if release:
            release()

    handles = [h for b in blocks for h in (b.register_forward_pre_hook(onload), b.register_forward_hook(offload))]
    try:
        yield len(blocks)
    finally:
        for h in handles:
            h.remove()
        for module, _cpu in list(home.values()):   # a block that raised mid-forward
            offload(module)
        if fd is not None:
            os.close(fd)


ATTN_BUDGET_MB = 512   # one chunk of attention scores (B x heads x rows x keys), ZEUS_VACE_ATTN_MB overrides


def attention_chunk_rows(b: int, h: int, lq: int, lk: int, itemsize: int, budget_mb: int) -> int:
    """Query rows per chunk so one score matrix stays under the budget (at least 128, at most lq)."""
    per_row = max(1, b * h * lk * itemsize)
    return max(1, min(lq, max(128, (int(budget_mb) * 1024 * 1024) // per_row)))


@contextlib.contextmanager
def chunked_attention(budget_mb: Optional[int] = None):
    """Replace scaled_dot_product_attention on MPS with chunked matmul -> softmax -> matmul.

    diffusers' native attention backend calls torch.nn.functional.scaled_dot_product_attention at call
    time, so swapping the function here needs no diffusers change. Measured on the M2 Pro, fp16,
    12 heads x 128 (Wan 1.3B): 5520 tokens (17 frames 640x368 + ref) 171 ms with PyTorch's MPS SDPA vs
    62 ms here; 15600 tokens (33 frames 832x480 + ref) 103 s (SDPA in query chunks, driver memory
    to the 20 GB limit) vs 0.5 s here. Softmax is per query row, so chunking is exact; max abs error vs
    an fp32 reference 2e-4. Score memory is bounded by ZEUS_VACE_ATTN_MB (default 512 MB per chunk)."""
    import torch
    import torch.nn.functional as F

    budget = int(budget_mb or int(os.environ.get("ZEUS_VACE_ATTN_MB", str(ATTN_BUDGET_MB))))
    original = F.scaled_dot_product_attention

    def sdpa(query, key, value, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None, enable_gqa=False,
             **kwargs):
        # diffusers' native backend always passes enable_gqa=False (Wan has as many K/V heads as Q heads)
        if (query.device.type != "mps" or is_causal or dropout_p or enable_gqa or kwargs or query.dim() != 4
                or key.shape[1] != query.shape[1]):
            if enable_gqa:
                kwargs["enable_gqa"] = enable_gqa
            return original(query, key, value, attn_mask=attn_mask, dropout_p=dropout_p,
                            is_causal=is_causal, scale=scale, **kwargs)
        b, h, lq, d = query.shape
        lk = key.shape[-2]
        chunk = attention_chunk_rows(b, h, lq, lk, query.element_size(), budget)
        q = query * (scale if scale is not None else d ** -0.5)
        kt = key.transpose(-1, -2)
        outs = []
        for i in range(0, lq, chunk):
            s = torch.matmul(q[:, :, i:i + chunk], kt)
            if attn_mask is not None:
                m = attn_mask[..., i:i + chunk, :] if attn_mask.dim() >= 2 and attn_mask.shape[-2] == lq else attn_mask
                s = s.masked_fill(~m, float("-inf")) if m.dtype == torch.bool else s + m
            outs.append(torch.matmul(torch.softmax(s, dim=-1), value))
            del s
        return outs[0] if len(outs) == 1 else torch.cat(outs, dim=2)

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

    conv3d(x, w)[t] = sum_i conv2d(x[t*stride + i*dilation], w[:, :, i]) is exact, and MPS conv2d is far
    better optimised than MPS conv3d: Wan VAE encode+decode of 9 frames at 640x368 (256 px tiles, sync
    after each conv) took 68+88 s with conv3d and 17+29 s with this, same reconstruction error, lower
    peak (4.6 vs 5.6 GB). Swapping torch.nn.functional.conv3d (looked up at call time by nn.Conv3d)
    needs no diffusers change."""
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


TEXT_DTYPE = "torch.bfloat16"   # UMT5 always runs in bf16 (T5 overflows in fp16); part of the cache key


def _cache_path(text: str, dtype=TEXT_DTYPE) -> Path:
    key = hashlib.sha256(f"{MODEL_REPO}|{MAX_SEQUENCE_LENGTH}|{dtype}|{text}".encode("utf-8")).hexdigest()[:40]
    return embed_cache_dir() / f"{key}.pt"


def uncached_texts(texts: Sequence[str]) -> list[str]:
    """Texts whose embeddings are not on disk yet (cheap: file existence only)."""
    return [t for t in dict.fromkeys(texts) if not _cache_path(t).is_file()]


def _cache_load(text: str, dtype=TEXT_DTYPE):
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


def encode_texts(path: Path, texts: Sequence[str], device: str) -> None:
    """Worker (runs in its own process, see get_prompt_embeds): UMT5 -> cached [1, 512, 4096] bf16 embeddings.

    UMT5-XXL is 5.7 B params (11.4 GB bf16 on disk), the biggest single memory cost, so:
      * it only ever lives in this short-lived process;
      * embeddings are cached on disk by text (the fixed negative prompt is encoded once, ever);
      * texts are encoded at their real token length and zero-padded to 512 afterwards — identical to
        the pipeline's own padding (pad tokens are masked, the pipeline zeroes those rows), ~4x less work.
    Encoding goes through the pipeline's public encode_prompt (prompt_clean, tokenizer, trimming)."""
    import torch
    from diffusers import WanVACEPipeline
    from diffusers.pipelines.wan.pipeline_wan_vace import prompt_clean
    from transformers import AutoTokenizer, UMT5EncoderModel

    todo = uncached_texts(texts)
    if not todo:
        return
    dtype = torch.bfloat16
    tokenizer = AutoTokenizer.from_pretrained(str(path / "tokenizer"))
    text_encoder = UMT5EncoderModel.from_pretrained(str(path / "text_encoder"), **{_transformers_dtype_kw(): dtype})
    text_encoder.to(device).eval()
    helper = WanVACEPipeline(tokenizer=tokenizer, text_encoder=text_encoder, vae=None, scheduler=None,
                             transformer=None)
    for t in todo:
        n_tokens = len(tokenizer(prompt_clean(t), add_special_tokens=True, truncation=True,
                                 max_length=MAX_SEQUENCE_LENGTH).input_ids)
        with torch.no_grad():
            emb, _ = helper.encode_prompt(prompt=t, do_classifier_free_guidance=False, num_videos_per_prompt=1,
                                          max_sequence_length=n_tokens, device=torch.device(device), dtype=dtype)
        emb = torch.nn.functional.pad(emb.to("cpu"), (0, 0, 0, MAX_SEQUENCE_LENGTH - emb.shape[1]))
        _cache_save(t, TEXT_DTYPE, emb)


def _effects_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def encode_command(path: Path, texts_file: Path, device: str) -> list[str]:
    """`python -m zfx.engines.vace encode ...` (run with cwd = the effects folder)."""
    import sys
    return [sys.executable, "-m", "zfx.engines.vace", "encode", "--model", str(path),
            "--texts", str(texts_file), "--device", device]


def get_prompt_embeds(path: Path, prompt: str, negative: str, device: str, report: Optional[Progress] = None):
    """Step 1 of the memory plan -> (prompt_embeds, negative_prompt_embeds) on CPU, [1, 512, 4096] bf16.

    Missing embeddings are made by a separate Python process that loads UMT5, writes the cache and exits,
    so this process never holds the text encoder (and never inherits its allocator fragmentation).
    Retries on the CPU if the GPU run fails. Returns (prompt, negative, where) with where in
    {"cache", device, "cpu"}."""
    report = report or (lambda *a, **k: None)
    todo = uncached_texts([prompt, negative])
    where = "cache"
    if todo:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(todo, fh)
            texts_file = Path(fh.name)
        try:
            errors = []
            for dev in dict.fromkeys([device, "cpu"]):
                report("generate", 0.01, f"encoding the prompt (UMT5 text encoder, separate process, {dev})")
                proc = subprocess.run(encode_command(path, texts_file, dev), cwd=str(_effects_dir()),
                                      capture_output=True, text=True)
                if proc.returncode == 0 and not uncached_texts(todo):
                    where = dev
                    break
                errors.append(f"{dev}: " + (proc.stderr or proc.stdout or "").strip()[-400:])
            else:
                raise EngineError("Could not encode the prompt with the UMT5 text encoder. " + " | ".join(errors))
        finally:
            texts_file.unlink(missing_ok=True)
    else:
        report("generate", 0.03, "prompt embeddings from cache (text encoder not loaded)")
    pos, neg = _cache_load(prompt), _cache_load(negative)
    if pos is None or neg is None:
        raise EngineError(f"The prompt embedding cache in {embed_cache_dir()} is unreadable. Delete it and retry.")
    return pos, neg, where


VAE_TILE = 256   # px; tiles overlap by a quarter (diffusers default 256/192)


def vae_dtype(device: str):
    """fp16 on MPS (same reconstruction error as fp32 on the M2 Pro, 1 GB less driver memory, faster),
    fp32 elsewhere; ZEUS_VACE_VAE_DTYPE=fp32|fp16|bf16 overrides."""
    import torch
    want = os.environ.get("ZEUS_VACE_VAE_DTYPE", "").strip().lower()
    table = {"fp32": torch.float32, "float32": torch.float32, "fp16": torch.float16, "float16": torch.float16,
             "bf16": torch.bfloat16, "bfloat16": torch.bfloat16}
    if want in table:
        return table[want]
    return torch.float16 if device == "mps" else torch.float32


def streaming_enabled(device: str) -> bool:
    """Stream transformer blocks (default on MPS); ZEUS_VACE_STREAM=0 keeps every weight on the GPU."""
    return device == "mps" and os.environ.get("ZEUS_VACE_STREAM", "1").strip() not in ("0", "false", "no")


def load_pipeline(path: Path, device: str, dtype):
    """Step 2: transformer (+ VACE blocks) + VAE + scheduler, no text encoder.

    The transformer comes from transformer-half/ when present: diffusers maps the 4.3 GB fp16 file
    (0.2 s, nothing cast, clean file pages) instead of reading and casting 7.1 GB of fp32/bf16 shards.
    On MPS its blocks then stay on the CPU side and are streamed to the GPU one at a time (stream_blocks);
    the VAE (fp16 on MPS, see vae_dtype) lives on the GPU."""
    from diffusers import AutoencoderKLWan, UniPCMultistepScheduler, WanVACEPipeline, WanVACETransformer3DModel

    sub = HALF_DIRS["transformer"] if half_ready("transformer", path) else "transformer"
    transformer = WanVACETransformer3DModel.from_pretrained(str(path), subfolder=sub, torch_dtype=dtype).eval()
    if not streaming_enabled(device):
        transformer.to(device)
    vae = AutoencoderKLWan.from_pretrained(str(path), subfolder="vae", torch_dtype=vae_dtype(device)).to(device).eval()
    scheduler = UniPCMultistepScheduler.from_pretrained(str(path), subfolder="scheduler")
    scheduler = UniPCMultistepScheduler.from_config(scheduler.config, flow_shift=FLOW_SHIFT)
    pipe = WanVACEPipeline(tokenizer=None, text_encoder=None, vae=vae, scheduler=scheduler, transformer=transformer)
    tile = int(os.environ.get("ZEUS_VACE_VAE_TILE", VAE_TILE))
    pipe.vae.enable_tiling(tile, tile, tile * 3 // 4, tile * 3 // 4)
    pipe.vae.enable_slicing()
    pipe.set_progress_bar_config(disable=True)
    pipe.zeus_transformer_weights = sub
    pipe.zeus_weights_file = path / sub / HALF_FILES["transformer"] if sub != "transformer" else None
    return pipe


@contextlib.contextmanager
def scheduler_on_cpu(scheduler):
    """Run scheduler.step on the CPU in float32 and hand the result back on the latents' device.

    With latents on MPS, diffusers' UniPCMultistepScheduler (flow sigmas, torch 2.11) does not converge:
    on an ideal flow model it ends 0.13 mean-abs away from the target (CPU: 6e-5), even at order 1.
    That alone made every VACE/T2V output burned, posterised and blurred, with or without our other
    MPS changes (ENGINES.md). The latents are ~1 MB, so the round trip costs nothing."""
    import torch

    original = scheduler.step

    def step(model_output, timestep, sample, *args, **kwargs):
        out = original(model_output.to("cpu", torch.float32), timestep, sample.to("cpu", torch.float32),
                       *args, **kwargs)
        if isinstance(out, tuple):
            return (out[0].to(sample.device, sample.dtype),) + tuple(out[1:])
        out.prev_sample = out.prev_sample.to(sample.device, sample.dtype)
        return out

    scheduler.step = step
    try:
        yield
    finally:
        del scheduler.step   # back to the class method


@contextlib.contextmanager
def mps_runtime(pipe, device: str):
    """Everything that keeps one VACE call inside 16 GB on MPS (see ENGINES.md for the measurements)."""
    sync_every = int(os.environ.get("ZEUS_VACE_SYNC_EVERY", "1")) if device == "mps" else 0
    with contextlib.ExitStack() as stack:
        if device == "mps":
            stack.enter_context(scheduler_on_cpu(pipe.scheduler))
        stack.enter_context(chunked_attention())
        stack.enter_context(conv3d_as_conv2d())
        stack.enter_context(mps_sync_every(vae_convs(pipe.vae), sync_every))
        if streaming_enabled(device):
            stack.enter_context(stream_blocks(pipe.transformer, device, getattr(pipe, "zeus_weights_file", None)))
        else:
            stack.enter_context(mps_sync_every(transformer_blocks(pipe.transformer), sync_every))
        yield


def gray_control(ctrl: np.ndarray, msk: np.ndarray, base: Optional[np.ndarray] = None) -> np.ndarray:
    """Inside the mask, replace the colour depth (inferno) by its luminance; outside, keep `base` (the
    original work.mp4 frames when given: control.mp4 feathers the depth ~6 px past the mask, and VACE keeps
    whatever is outside the mask, so that halo came back as a yellow glow around the new person).

    VACE was trained with grayscale depth maps (near = bright). Inferno's lightness rises monotonically,
    so its luminance is the depth ordering. Measured (33 frames 640x368, 20 steps): with the inferno
    colours the generated person was smeared with orange/blue depth colours; with grey depth + a cut-out
    reference it was a clean, plausible person following the motion (ENGINES.md).
    ctrl uint8 (N,H,W,3), msk uint8 (N,H,W) with 255 = person."""
    luma = ctrl.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    alpha = (msk.astype(np.float32) / 255.0)[..., None]
    outside = ctrl if base is None or base.shape != ctrl.shape else base
    out = outside.astype(np.float32) * (1.0 - alpha) + luma[..., None] * alpha
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def cutout_on_white(rgb: np.ndarray, person: np.ndarray, margin: float = 0.06) -> Optional[np.ndarray]:
    """Person (soft mask in [0,1]) composited on white and cropped to its box + margin; None if no person.
    VACE's reference images are subjects on a white canvas (the pipeline pads them with white too)."""
    binary = person > 0.5
    if binary.sum() < 0.01 * binary.size:
        return None
    ys, xs = np.nonzero(binary)
    h, w = binary.shape
    my, mx = int(margin * h), int(margin * w)
    y0, y1 = max(0, ys.min() - my), min(h, ys.max() + 1 + my)
    x0, x1 = max(0, xs.min() - mx), min(w, xs.max() + 1 + mx)
    alpha = np.clip(person, 0.0, 1.0)[..., None]
    out = rgb.astype(np.float32) * alpha + 255.0 * (1.0 - alpha)
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)[y0:y1, x0:x1]


def _person_segmenter():
    """Apple Vision person segmentation (zfx.masks) or None when unavailable."""
    try:
        from ..masks import PersonSegmenter
        return PersonSegmenter("accurate").segment
    except Exception:
        return None


def _load_refs(refs: Sequence, segment: Optional[Callable] = None, cutout: Optional[bool] = None) -> list:
    """Reference photos as PIL images; people are cut out onto white (ZEUS_VACE_REF_CUTOUT=0 keeps the
    photo as it is; so does a photo where Vision finds no person)."""
    from PIL import Image, ImageOps

    if cutout is None:
        cutout = os.environ.get("ZEUS_VACE_REF_CUTOUT", "1").strip() not in ("0", "false", "no")
    if cutout and segment is None:
        segment = _person_segmenter()
    images = []
    for r in refs:
        p = Path(r).expanduser()
        try:
            img = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        except Exception as exc:
            raise EngineError(f"Could not open the reference photo {p.name}: {exc}") from exc
        img.thumbnail((1024, 1024))
        if cutout and segment is not None:
            try:
                rgb = np.asarray(img)
                cut = cutout_on_white(rgb, segment(rgb))
                if cut is not None:
                    img = Image.fromarray(cut)
            except Exception:
                pass   # keep the photo as it is
        images.append(img)
    if not images:
        raise EngineError("Full replace needs at least one reference photo (--ref).")
    return images


def job_mode(job_dir: Path) -> str:
    try:
        return str(json.loads((Path(job_dir) / "job.json").read_text()).get("mode") or "depth")
    except (OSError, ValueError):
        return "depth"


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "mps backend out of memory" in text or "failed to allocate" in text


# ----------------------------------------------------------------------------- crops (what the model sees)
CROPS_DIR = "result-crops"
CROPS_MANIFEST = "result-crops.json"


def crop_enabled(value: Optional[bool] = None) -> bool:
    """Crop-and-generate is on unless ZEUS_VACE_CROP=0 (or crop=False): then every window is the whole frame."""
    if value is not None:
        return bool(value)
    return os.environ.get("ZEUS_VACE_CROP", "1").strip().lower() not in ("0", "false", "no", "off")


def inside_mask(ctrl: np.ndarray, msk: np.ndarray, base: np.ndarray) -> np.ndarray:
    """The control inside the mask, `base` (the sharp original crop) outside; for controls kept as they are
    (mesh mode, ZEUS_VACE_CONTROL=as-is)."""
    alpha = (msk.astype(np.float32) / 255.0)[..., None]
    return np.clip(base.astype(np.float32) * (1.0 - alpha) + ctrl.astype(np.float32) * alpha + 0.5, 0, 255).astype(np.uint8)


def _filter_path(path: Path) -> str:
    """A file path inside an ffmpeg filter argument (single-quoted; quotes and backslashes escaped)."""
    return "'" + str(path).replace("\\", "\\\\").replace("'", "'\\''") + "'"


def color_conversion(source: Path, lut: Optional[Path] = None) -> str:
    """ffmpeg filters turning the source's Y'CbCr into RGB with its own matrix and range (+ the Log -> Rec.709 LUT)."""
    from ..hires import _MATRIX, source_format
    fmt = source_format(source)
    mat = _MATRIX.get(fmt["matrix"] or "")
    conv = "scale=iw:ih" + (f":in_color_matrix={mat}" if mat else "") + \
        f":in_range={'pc' if fmt['range'] == 'pc' else 'tv'},format=rgb48le"
    if lut:
        conv += f",lut3d=file={_filter_path(lut)}:interp=tetrahedral"
    return conv


def read_source_crop(source: Path, start: int, end: int, box: Sequence[int], out_w: int, out_h: int,
                     lut: Optional[Path] = None) -> np.ndarray:
    """Frames [start, end) of the source on work.mp4's 16 fps clock (same fps filter as zfx.io.make_work), cropped to
    `box` in source pixels at full resolution, through the Log LUT when given, resized to out_w x out_h (Lanczos)."""
    x0, y0, x1, y1 = (int(v) for v in box)
    vf = (f"fps={FPS}:start_time=0,trim=start_frame={int(start)}:end_frame={int(end)},setpts=PTS-STARTPTS,"
          f"crop={x1 - x0}:{y1 - y0}:{x0}:{y0},{color_conversion(source, lut)},"
          f"scale={out_w}:{out_h}:flags=lanczos,format=rgb24")
    proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-i", str(source), "-an", "-vf", vf,
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if proc.returncode != 0:
        raise EngineError(f"Could not read {source.name}: {proc.stderr.decode(errors='replace')[-300:]}")
    size = out_w * out_h * 3
    n = len(proc.stdout) // size
    frames = np.frombuffer(proc.stdout[: n * size], np.uint8).reshape(n, out_h, out_w, 3)
    want = int(end) - int(start)
    if n == 0:
        raise EngineError(f"{source.name} has no frames in {start}..{end} (16 fps).")
    if n < want:   # the clip ends a frame early on the 16 fps clock: hold the last frame (padded windows too)
        frames = np.concatenate([frames, np.repeat(frames[-1:], want - n, axis=0)])
    return frames[:want]


def _write_png(path: Path, rgb: np.ndarray) -> None:
    import cv2
    if not cv2.imwrite(str(path), np.ascontiguousarray(rgb[..., ::-1] if rgb.ndim == 3 else rgb)):
        raise EngineError(f"Could not write {path}")


def prepare_windows(job_dir: Path, control: Path, mask: Path, windows: Sequence[tuple[int, int]], size: str,
                    control_style: str, crop: bool, lut: Optional[Path] = None,
                    report: Optional[Progress] = None) -> tuple[list[dict], dict]:
    """Per window: crop box (zfx.cropbox), the control/mask crops at the generation size, and the sharp original crop
    (from source.mp4 at full resolution, Log -> Rec.709 when `lut`). All of it is read BEFORE the model is loaded.
    Returns (windows, geometry)."""
    from .. import cropbox
    import cv2

    report = report or (lambda *a, **k: None)
    ww, wh = video_size(control)
    ctrl_full = read_frames(control, ww, wh)
    msk_full = read_frames(mask, ww, wh, gray=True)
    total = min(len(ctrl_full), len(msk_full))
    source = job_dir / "source.mp4"
    use_source = source.is_file()
    src_w, src_h = video_size(source) if use_source else (ww, wh)
    work_full = None
    if not use_source:
        work = job_dir / "work.mp4"
        work_full = read_frames(work if work.is_file() else control, ww, wh)
    gen_sizes = [target_size(2, 1, size), target_size(1, 2, size)]
    plans = cropbox.plan_window_boxes(msk_full[:total], windows, src_w, src_h, gen_sizes, crop=crop,
                                      margin=MARGIN_SHARE)
    sx, sy = ww / float(src_w), wh / float(src_h)
    out = []
    for wi, ((start, end), plan) in enumerate(zip(windows, plans)):
        report("generate", 0.02, f"cropping window {wi + 1}/{len(windows)} around the person")
        gw, gh = plan["gen"]
        box = plan["box"]
        box_w = cropbox.scale_box(box, sx, sy)
        idx = [min(i, total - 1) for i in range(start, end)]
        ctrl = np.stack([cropbox.crop_resize(ctrl_full[i], box_w, gw, gh) for i in idx])
        msk = np.stack([np.where(cropbox.crop_resize(msk_full[i], box_w, gw, gh) > 127, 255, 0).astype(np.uint8)
                        for i in idx])
        if use_source:
            base = read_source_crop(source, start, min(end, total), box, gw, gh, lut)
            if len(base) < len(idx):
                base = np.concatenate([base, np.repeat(base[-1:], len(idx) - len(base), axis=0)])
        else:
            base = np.stack([cropbox.crop_resize(work_full[i], box_w, gw, gh, cv2.INTER_LANCZOS4) for i in idx])
        vid = gray_control(ctrl, msk, base) if control_style == "gray" else inside_mask(ctrl, msk, base)
        out.append({**plan, "index": wi, "start": int(start), "end": int(end), "valid": int(min(end, total) - start),
                    "video": vid, "mask": msk, "base": base, "box_work": [round(v, 2) for v in box_w],
                    "dir": f"w{wi:02d}"})
    geometry = {"source_size": [int(src_w), int(src_h)], "work_size": [int(ww), int(wh)], "total": int(total),
                "base_from": "source.mp4" if use_source else "work.mp4"}
    return out, geometry


def assemble_raw(job_dir: Path, wins: Sequence[dict], results: Sequence[np.ndarray], geometry: dict,
                 lut: Optional[Path] = None) -> np.ndarray:
    """result-raw.mp4 frames at work size: work.mp4 (Rec.709 when Log) with each window's generated crop pasted
    into its box, windows cross-faded over their overlaps in full-frame coordinates (zfx.cropbox.frame_weights)."""
    from .. import cropbox

    ww, wh = geometry["work_size"]
    total = geometry["total"]
    sx, sy = ww / float(geometry["source_size"][0]), wh / float(geometry["source_size"][1])
    work = job_dir / "work.mp4"
    if work.is_file() and lut:
        proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-i", str(work), "-vf",
                               f"{color_conversion(work, lut)},scale={ww}:{wh},format=rgb24", "-f", "rawvideo",
                               "-pix_fmt", "rgb24", "-"], capture_output=True)
        n = len(proc.stdout) // (ww * wh * 3)
        frames = np.frombuffer(proc.stdout[: n * ww * wh * 3], np.uint8).reshape(n, wh, ww, 3).copy()
    elif work.is_file():
        frames = read_frames(work, ww, wh).copy()
    else:
        frames = np.zeros((total, wh, ww, 3), np.uint8)
    if len(frames) < total:
        frames = np.concatenate([frames, np.repeat(frames[-1:], total - len(frames), axis=0)])
    frames = frames[:total]
    cover = cropbox.frame_weights([(w["start"], w["end"]) for w in wins], total)
    for i in range(total):
        acc = frames[i].astype(np.float32)
        for wi, weight in sorted(cover[i], key=lambda t: t[0]):
            win = wins[wi]
            img, cov = cropbox.paste(acc, results[wi][i - win["start"]], cropbox.scale_box(win["box"], sx, sy))
            a = (cov * weight)[..., None]
            acc = acc * (1.0 - a) + img * a
        frames[i] = np.clip(acc + 0.5, 0, 255).astype(np.uint8)
    return frames


# ----------------------------------------------------------------------------- run
def run(job_dir, refs: Sequence, out_path, progress: Optional[Progress] = None, *,
        prompt: str = "", frames: Optional[int] = None, steps: Optional[int] = None,
        size: Optional[str] = None, seed: Optional[int] = None,
        control: Optional[Path] = None, mask: Optional[Path] = None, crop: Optional[bool] = None,
        source_lut: Optional[Path] = None, **_ignored) -> dict:
    """Generate the replacement person. Writes `out_path` (result-raw.mp4, work size, 16 fps) and the per-window
    crops <job>/result-crops/wNN/{gen,base,mask}/NNNN.png + result-crops.json, which zfx.hires composites into the
    original frames at the source resolution."""
    job_dir = Path(job_dir)
    out_path = Path(out_path)
    report: Progress = progress or (lambda *a, **k: None)
    path = check_model()
    control = Path(control) if control else job_dir / "control.mp4"
    mask = Path(mask) if mask else job_dir / "mask.mp4"
    for p in (control, mask):
        if not p.is_file():
            raise EngineError(f"{p.name} is missing in the job folder. Run prepare again.")
    lut = Path(source_lut) if source_lut else None
    if lut is not None and not lut.is_file():
        raise EngineError(f"The Log -> Rec.709 LUT {lut} is missing.")

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    watermarks = configure_mps_memory()   # before the first MPS allocation
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
    if size not in SIZES:
        raise EngineError(f"Unknown size '{size}'. Use one of: {', '.join(SIZES)}.")
    window = normalize_frames(frames or DEFAULT_FRAMES)
    steps = max(1, int(steps or DEFAULT_STEPS))
    seed = int(seed) if seed is not None else random.randint(0, 2**31 - 1)
    device = pick_device()
    dtype = pick_dtype(device, path)
    # UMT5 on the CPU: bf16 weights stay mmapped file pages (0.7 GB footprint, ~20 s for both prompts) instead
    # of 11.4 GB of GPU buffers; same embeddings as on MPS (cosine 0.9996)
    te_device = os.environ.get("ZEUS_VACE_TEXT_DEVICE", "").strip().lower() or "cpu"
    mode = job_mode(job_dir)
    control_style = os.environ.get("ZEUS_VACE_CONTROL", "").strip().lower() or (
        "gray" if mode in ("depth", "depth_mesh") else "as-is")
    cropping = crop_enabled(crop)

    timings: dict = {}
    t_all = time.monotonic()
    # 0) crops: boxes, control/mask/original at the generation size (before the model takes its memory)
    t0 = time.monotonic()
    n_frames = min(len(read_frames(control, 16, 16, gray=True)), len(read_frames(mask, 16, 16, gray=True)))
    windows = plan_windows(n_frames, window, OVERLAP)
    if not windows:
        raise EngineError("control.mp4 / mask.mp4 have no frames. Run prepare again.")
    wins, geometry = prepare_windows(job_dir, control, mask, windows, size, control_style, cropping, lut, report)
    total = geometry["total"]
    timings["crops"] = round(time.monotonic() - t0, 1)
    ref_images = _load_refs(refs)
    full_prompt = (prompt.strip().rstrip(".") + ". " if prompt and prompt.strip() else "") + REPLACE_PROMPT
    boxes_text = "; ".join(f"{w['box'][2] - w['box'][0]}x{w['box'][3] - w['box'][1]}"
                           f"{' (whole frame)' if w['full_frame'] else ''} -> {w['gen'][0]}x{w['gen'][1]}" for w in wins)
    report("generate", 0.03, f"VACE {total} frames in {len(windows)} window(s) of {windows[0][1] - windows[0][0]}, "
           f"{steps} steps, seed {seed}; crops {boxes_text}")

    # 1) text encoder alone, in its own process (skipped when both embeddings are cached)
    t0 = time.monotonic()
    prompt_embeds, negative_prompt_embeds, te_device = get_prompt_embeds(path, full_prompt, NEGATIVE_PROMPT, te_device, report)
    timings["encode_prompt"] = round(time.monotonic() - t0, 1)

    # 2) transformer + VAE
    t0 = time.monotonic()
    report("generate", 0.04, "loading the Wan2.1-VACE transformer and VAE")
    pipe = load_pipeline(path, device, dtype)
    prompt_embeds = prompt_embeds.to(device=device, dtype=dtype)
    negative_prompt_embeds = negative_prompt_embeds.to(device=device, dtype=dtype)
    timings["load"] = round(time.monotonic() - t0, 1)

    crops_root = job_dir / CROPS_DIR
    shutil.rmtree(crops_root, ignore_errors=True)
    (job_dir / CROPS_MANIFEST).unlink(missing_ok=True)
    crops_root.mkdir(parents=True)
    results: list = []
    window_times: list = []
    generator = torch.Generator(device="cpu").manual_seed(seed)
    t0 = time.monotonic()
    lo, hi = 0.06, 0.95
    try:
        from .. import cropbox
        for wi, win in enumerate(wins):
            start, end = win["start"], win["end"]
            gw, gh = win["gen"]
            n = end - start
            vid = win["video"].copy()
            mk = win["mask"].copy()
            if wi > 0:
                prev = wins[wi - 1]
                carry = max(0, min(prev["start"] + prev["valid"], end) - start)
                for j in range(carry):
                    # continue from what the previous window generated (redrawn into this window's box), mask black
                    vid[j] = cropbox.reproject(results[wi - 1][start + j - prev["start"]], prev["box"], win["box"],
                                               gw, gh, fill=win["base"][j])
                    mk[j] = 0
            video_imgs = [Image.fromarray(f) for f in vid]
            mask_imgs = [Image.fromarray(m) for m in mk]  # 2-D uint8 -> mode "L"

            stamps = [time.monotonic()]

            def on_step(pipeline, step, timestep, callback_kwargs, _wi=wi):
                stamps.append(time.monotonic())
                frac = (_wi + (step + 1) / steps) / len(wins)
                report("generate", lo + (hi - lo) * frac,
                       f"window {_wi + 1}/{len(wins)} · step {step + 1}/{steps}")
                return callback_kwargs

            with mps_runtime(pipe, device):
                result = pipe(
                    video=video_imgs,
                    mask=mask_imgs,
                    reference_images=ref_images,
                    prompt_embeds=prompt_embeds,
                    negative_prompt_embeds=negative_prompt_embeds,
                    height=gh,
                    width=gw,
                    num_frames=n,
                    num_inference_steps=steps,
                    guidance_scale=GUIDANCE,
                    generator=generator,
                    output_type="np",
                    callback_on_step_end=on_step,
                    callback_on_step_end_tensor_inputs=["latents"],
                    max_sequence_length=MAX_SEQUENCE_LENGTH,
                ).frames[0]
            stamps.append(time.monotonic())
            # first step includes the VAE encodes (inactive, reactive, reference); the last gap is the decode
            step_s = [b - a for a, b in zip(stamps[1:-2], stamps[2:-1])]
            window_times.append({"encode_and_step1_s": round(stamps[1] - stamps[0], 1),
                                 "step_s": round(sum(step_s) / len(step_s), 1) if step_s else None,
                                 "decode_s": round(stamps[-1] - stamps[-2], 1)})
            res = (np.clip(np.asarray(result, dtype=np.float32), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
            del result
            if res.max() == res.min():
                raise EngineError("VACE produced an empty (flat) video. Try ZEUS_VACE_DTYPE=bf16.")
            if res.shape[1:3] != (gh, gw):
                raise EngineError(f"VACE returned {res.shape[2]}x{res.shape[1]} instead of {gw}x{gh}.")
            results.append(res)
            wdir = crops_root / win["dir"]
            for sub in ("gen", "base", "mask"):
                (wdir / sub).mkdir(parents=True, exist_ok=True)
            for j in range(win["valid"]):
                _write_png(wdir / "gen" / f"{j:04d}.png", res[j])
                _write_png(wdir / "base" / f"{j:04d}.png", win["base"][j])
                _write_png(wdir / "mask" / f"{j:04d}.png", win["mask"][j])
            del video_imgs, mask_imgs
            _empty_cache()
    except RuntimeError as exc:
        if _is_oom(exc):
            raise EngineError("Ran out of memory during VACE generation. Close other apps, then try "
                              "--size 360p and/or --frames 17.") from exc
        raise
    finally:
        transformer_weights = getattr(pipe, "zeus_transformer_weights", "transformer")
        del pipe
        _empty_cache()
    timings["denoise"] = round(time.monotonic() - t0, 1)
    timings["windows"] = window_times

    report("generate", 0.97, "writing result-raw.mp4 and result-crops.json")
    write_frames(assemble_raw(job_dir, wins, results, geometry, lut), out_path, FPS)
    win_records = [{k: w[k] for k in ("index", "start", "end", "valid", "box", "box_work", "gen", "full_frame",
                                      "aspect_error", "mask_bbox", "dir")} for w in wins]
    manifest = {"version": 1, "dir": CROPS_DIR, "fps": FPS, **geometry, "crop": cropping, "margin": MARGIN_SHARE,
                "color": {"lut": str(lut), "domain": "rec709"} if lut else None, "windows": win_records}
    tmp = job_dir / (CROPS_MANIFEST + ".partial")
    tmp.write_text(json.dumps(manifest, indent=1))
    os.replace(tmp, job_dir / CROPS_MANIFEST)
    timings["total"] = round(time.monotonic() - t_all, 1)
    report("generate", 1.0, f"VACE done in {timings['total']:.0f} s")
    return {
        "engine": NAME,
        "model": MODEL_REPO,
        "license": MODEL_LICENSE,
        "seconds": timings["total"],
        "timings": timings,
        "width": wins[0]["gen"][0],
        "height": wins[0]["gen"][1],
        "frames": total,
        "fps": FPS,
        "windows": [list(w) for w in windows],
        "window_frames": windows[0][1] - windows[0][0],
        "overlap": OVERLAP,
        "crop": {"enabled": cropping, "margin": MARGIN_SHARE, "manifest": CROPS_MANIFEST, "dir": CROPS_DIR,
                 "source_size": geometry["source_size"], "base_from": geometry["base_from"],
                 "boxes": [{"window": [w["start"], w["end"]], "box": w["box"], "gen": w["gen"],
                           "full_frame": w["full_frame"], "aspect_error": w["aspect_error"]} for w in win_records]},
        "source_lut": str(lut) if lut else None,
        "steps": steps,
        "guidance_scale": GUIDANCE,
        "flow_shift": FLOW_SHIFT,
        "seed": seed,
        "device": device,
        "text_encoder_device": te_device,
        "dtype": str(dtype).replace("torch.", ""),
        "control": control_style,
        "reference_cutout": os.environ.get("ZEUS_VACE_REF_CUTOUT", "1").strip() not in ("0", "false", "no"),
        "transformer_weights": transformer_weights,
        "vae_dtype": str(vae_dtype(device)).replace("torch.", ""),
        "streamed_blocks": streaming_enabled(device),
        "mps_watermarks": watermarks,
        "prompt": full_prompt,
    }


# ----------------------------------------------------------------------------- helper processes
def _main(argv: Optional[Sequence[str]] = None) -> int:
    """python -m zfx.engines.vace encode|half ...  (cwd = the effects folder)."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m zfx.engines.vace")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encode", help="encode texts with UMT5 into the prompt-embedding cache, then exit")
    e.add_argument("--model", required=True)
    e.add_argument("--texts", required=True, help="JSON file with a list of texts")
    e.add_argument("--device", default="mps")
    h = sub.add_parser("half", help="write transformer-half/ (one-time, 4.3 GB)")
    h.add_argument("--model", default="")
    h.add_argument("--dtype", default="", help=f"fp16 or bf16 for the transformer (default {DEFAULT_HALF})")
    args = ap.parse_args(argv)
    if args.cmd == "encode":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        encode_texts(Path(args.model), json.loads(Path(args.texts).read_text()), args.device)
        return 0
    last = [0.0]

    def rep(p: float, msg: str) -> None:
        if p - last[0] >= 0.1 or p >= 1.0:
            last[0] = p
            print(f"[vace half] {p * 100:3.0f}% {msg}", file=sys.stderr, flush=True)

    print(json.dumps(make_half_copies(Path(args.model) if args.model else None, args.dtype or None, rep)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
