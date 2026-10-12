"""Named things in the picture: CLIPSeg on sampled frames of work.mp4 (one tone per name in the parts map).

The user names things in a prompt ("microphone, laptop, cap"); zfx.parts gives every name its own tone. CLIPSeg
(Lüddecke & Ecker, CVPR 2022; weights CIDAS/clipseg-rd64-refined on Hugging Face, Apache-2.0) segments an image
from a text prompt: a CLIP ViT-B/16 image encoder, the CLIP text encoder and a small transformer decoder
("rd64-refined": reduce dimension 64, trained on PhraseCut+) that returns a 352 x 352 logit map per prompt.
It runs through `transformers` on torch "mps" (else the CPU) in fp32. The weights (~600 MB, model.safetensors +
tokenizer files, pytorch_model.bin is skipped) are downloaded once at a pinned revision into
$ZEUS_EFFECTS_HOME/models/clipseg-rd64-refined/; ZEUS_EFFECTS_NO_DOWNLOAD=1 forbids the download.

Per clip: the frames are sampled at THINGS_FPS (2 fps = every 8th frame of the 16 fps work.mp4, plus the last
one); each sampled frame goes through the model once per prompt, and - when the person is small in the frame -
once more on a crop around the person (the "zoom" pass: the union of the people-mask boxes plus a 60 % margin),
whose map is merged by maximum, so the things next to a presenter in a corner get a sharper look. The
probability maps (sigmoid of the logits, 352 x 352) are interpolated linearly in time to every frame, so a thing
moves smoothly between samples and flicker is damped. zfx.parts resizes them to the frame, keeps per thing the
blobs above THRESHOLD that contain a pixel above THRESHOLD + HYSTERESIS and are at least MIN_AREA of the frame
(hysteresis: no scattered specks), takes the most likely thing per pixel, keeps things outside the person mask
unless asked, and smooths them with the rest of the map.

Free and local; nothing is sent anywhere. Limits: CLIPSeg is an open-vocabulary guess, not a detector - a dark
round microphone next to a head can score as "cap" too, graphics on a slide can score as anything at ~0.4; check
the found rates and mean probabilities in parts.json, raise --things-threshold, or name the thing more precisely
("microphone on a boom arm").
"""

from __future__ import annotations

import gc
import os
import re
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import WORK_FPS, models_dir
from .progress import get_logger

log = get_logger()

CLIPSEG_REPO = "CIDAS/clipseg-rd64-refined"
CLIPSEG_REVISION = "999e0328d9e10b484360c477313983f9afdd7050"   # pinned 2026-10-12 (main)
CLIPSEG_LICENSE = "Apache-2.0"
CLIPSEG_FILES = ("config.json", "model.safetensors", "preprocessor_config.json", "merges.txt", "vocab.json",
                 "special_tokens_map.json", "tokenizer_config.json")
CLIPSEG_MIN_BYTES = 500_000_000      # model.safetensors is 603 MB
CLIPSEG_SIZE = 352                   # the model's map (and input) size
THINGS_FPS = 2.0                     # sampled frames per second of work.mp4
THRESHOLD = 0.4                      # probability above which a pixel belongs to the thing
HYSTERESIS = 0.1                     # a blob needs one pixel above THRESHOLD + HYSTERESIS
MIN_AREA = 0.0002                    # a blob needs at least this fraction of the frame (78 px at 832 x 468)
TEMPLATE = "{}"                      # prompt per name; "a photo of a {}" is the alternative CLIPSeg was tuned on
MAX_THINGS = 24                      # legend ids 40..63 in zfx.parts
ZOOM_MARGIN = 0.6                    # zoom pass: the person box grown by this fraction on every side
ZOOM_MAX_FRACTION = 0.4              # no zoom pass when the person box already covers this much of the frame
PROB_BUDGET_BYTES = 320_000_000      # per-frame maps are halved (176 px) when N x T x 352^2 would exceed this


class ThingsError(RuntimeError):
    """Plain-English problem with the things step (missing weights, bad prompt...)."""


# --------------------------------------------------------------------------- prompt
def parse_things(text: Optional[str]) -> list[str]:
    """'microphone, laptop,  Cap' -> ['microphone', 'laptop', 'cap']: split on commas, semicolons and new lines,
    trimmed, lower-cased, duplicates dropped (first wins), empty names dropped. At most MAX_THINGS."""
    names: list[str] = []
    for raw in re.split(r"[,;\n]+", text or ""):
        name = re.sub(r"\s+", " ", raw).strip().lower()
        if name and name not in names:
            names.append(name)
    if len(names) > MAX_THINGS:
        raise ThingsError(f"At most {MAX_THINGS} things can be named ({len(names)} given).")
    return names


# --------------------------------------------------------------------------- weights
def clipseg_dir() -> Path:
    return models_dir() / "clipseg-rd64-refined"


def clipseg_present(directory: Optional[Path] = None) -> bool:
    d = Path(directory) if directory else clipseg_dir()
    weights = d / "model.safetensors"
    return all((d / f).is_file() for f in CLIPSEG_FILES) and weights.stat().st_size >= CLIPSEG_MIN_BYTES


def ensure_clipseg(report: Optional[Callable[[float, str], None]] = None) -> Path:
    """The local CLIPSeg folder, downloaded once (pinned revision, only the files needed) when missing."""
    d = clipseg_dir()
    if clipseg_present(d):
        return d
    hint = (f"Run `hf download {CLIPSEG_REPO} --revision {CLIPSEG_REVISION} --local-dir \"{d}\"` "
            "(or run the parts step with downloads allowed).")
    if os.environ.get("ZEUS_EFFECTS_NO_DOWNLOAD", "").strip() in ("1", "true", "yes"):
        raise ThingsError(f"CLIPSeg weights are missing ({d}). {hint}")
    if report:
        report(0.0, f"downloading CLIPSeg ({CLIPSEG_REPO}, ~600 MB, first use)")
    log.info("downloading %s@%s into %s", CLIPSEG_REPO, CLIPSEG_REVISION[:12], d)
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(CLIPSEG_REPO, revision=CLIPSEG_REVISION, allow_patterns=list(CLIPSEG_FILES),
                          local_dir=str(d))
    except Exception as exc:
        raise ThingsError(f"Could not download CLIPSeg ({type(exc).__name__}: {exc}). {hint}") from exc
    if not clipseg_present(d):
        raise ThingsError(f"The CLIPSeg download is incomplete ({d}). {hint}")
    return d


# --------------------------------------------------------------------------- model
class ClipSegBackend:
    """segment(frames (K,H,W,3) uint8 RGB, prompts) -> (K, T, 352, 352) float32 probabilities in [0, 1]."""

    name = "clipseg"
    size = CLIPSEG_SIZE

    def __init__(self, device: Optional[str] = None, report: Optional[Callable[[float, str], None]] = None):
        import torch
        from transformers import CLIPSegForImageSegmentation, CLIPSegProcessor
        folder = ensure_clipseg(report)
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        if report:
            report(0.0, f"loading CLIPSeg on {self.device}")
        self.processor = CLIPSegProcessor.from_pretrained(str(folder))
        self.model = CLIPSegForImageSegmentation.from_pretrained(str(folder), dtype=torch.float32)
        self.model = self.model.to(self.device).eval()
        self.revision = CLIPSEG_REVISION
        self.repo = CLIPSEG_REPO

    def _forward(self, frame: np.ndarray, prompts: Sequence[str]):
        import torch
        inputs = self.processor(text=list(prompts), images=[frame] * len(prompts), padding=True,
                                return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.inference_mode():
            logits = self.model(**inputs).logits
        return logits.reshape(len(prompts), self.size, self.size)

    def segment(self, frames: np.ndarray, prompts: Sequence[str]) -> np.ndarray:
        import torch
        out = np.zeros((len(frames), len(prompts), self.size, self.size), np.float32)
        for k, frame in enumerate(frames):
            frame = np.ascontiguousarray(frame)
            try:
                logits = self._forward(frame, prompts)
            except RuntimeError as exc:
                if self.device == "cpu":
                    raise
                log.warning("CLIPSeg failed on %s (%s); continuing on the CPU", self.device, exc)
                self.device = "cpu"
                self.model = self.model.to("cpu")
                logits = self._forward(frame, prompts)
            out[k] = torch.sigmoid(logits).float().cpu().numpy()
        return out

    def close(self) -> None:
        self.model = None
        self.processor = None
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass


# --------------------------------------------------------------------------- per clip
def sample_indices(n: int, fps: float = THINGS_FPS, work_fps: float = WORK_FPS) -> list[int]:
    """Frames sent to the model: every round(work_fps / fps)-th frame plus the last one."""
    if n <= 0:
        return []
    step = max(1, int(round(work_fps / max(fps, 1e-6))))
    picks = sorted(set(range(0, n, step)) | {n - 1})
    return picks


def interpolate(sampled: np.ndarray, picks: Sequence[int], n: int) -> np.ndarray:
    """(K, ...) maps at the frames `picks` -> (n, ...) uint8 maps, linear in time between samples."""
    sampled = np.asarray(sampled, dtype=np.float32)
    out = np.zeros((n,) + sampled.shape[1:], np.uint8)
    if not len(picks):
        return out
    for j in range(len(picks)):
        a = picks[j]
        b = picks[j + 1] if j + 1 < len(picks) else None
        out[a] = np.clip(sampled[j] * 255.0 + 0.5, 0, 255).astype(np.uint8)
        if b is None or b <= a + 1:
            continue
        for i in range(a + 1, b):
            t = (i - a) / float(b - a)
            mix = (1.0 - t) * sampled[j] + t * sampled[j + 1]
            out[i] = np.clip(mix * 255.0 + 0.5, 0, 255).astype(np.uint8)
    first, last = picks[0], picks[-1]
    for i in range(0, first):
        out[i] = out[first]
    for i in range(last + 1, n):
        out[i] = out[last]
    return out


def zoom_box(masks: Optional[np.ndarray], width: int, height: int, margin: float = ZOOM_MARGIN,
             max_fraction: float = ZOOM_MAX_FRACTION) -> Optional[tuple[int, int, int, int]]:
    """The zoom pass crop (x0, y0, x1, y1): the union of the person boxes grown by `margin` on every side; None
    without a person or when the person box already covers more than max_fraction of the frame."""
    if masks is None or not len(masks):
        return None
    union = np.any(np.asarray(masks, bool), axis=0)
    ys, xs = np.nonzero(union)
    if not len(xs):
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    if (x1 - x0) * (y1 - y0) > max_fraction * width * height:
        return None
    mx, my = int(round(margin * (x1 - x0))), int(round(margin * (y1 - y0)))
    box = (max(0, x0 - mx), max(0, y0 - my), min(width, x1 + mx), min(height, y1 + my))
    if box[2] - box[0] < 32 or box[3] - box[1] < 32:
        return None
    return box


def merge_zoom(full: np.ndarray, crop: np.ndarray, box: tuple[int, int, int, int], width: int, height: int
               ) -> np.ndarray:
    """(T, s, s) full-frame maps + (T, s, s) maps of the crop `box` -> the full maps with the crop's values merged
    by maximum into the box's area (resized to the box's footprint in map pixels)."""
    import cv2
    t, s = full.shape[0], full.shape[1]
    x0, y0, x1, y1 = box
    mx0, my0 = int(round(x0 * s / width)), int(round(y0 * s / height))
    mx1, my1 = max(mx0 + 1, int(round(x1 * s / width))), max(my0 + 1, int(round(y1 * s / height)))
    mx1, my1 = min(mx1, s), min(my1, s)
    out = full.copy()
    for k in range(t):
        patch = cv2.resize(crop[k], (mx1 - mx0, my1 - my0), interpolation=cv2.INTER_LINEAR)
        out[k, my0:my1, mx0:mx1] = np.maximum(out[k, my0:my1, mx0:mx1], patch)
    return out


def detect_things(frames: np.ndarray, names: Sequence[str], report: Callable[[float, str], None],
                  backend=None, fps: float = THINGS_FPS, template: str = TEMPLATE,
                  device: Optional[str] = None, masks: Optional[np.ndarray] = None, zoom: bool = True) -> dict:
    """CLIPSeg over the sampled frames -> {"probs": (N, T, s, s) uint8 (0..255 = probability), "names",
    "prompts", "sampled": [frame indices], "size": s, "model", "revision", "device", "max_prob": (N, T),
    "zoom": box or None}. With `masks` (N, H, W) and zoom=True a second pass runs on the person crop (zoom_box).
    `backend` (tests) must offer segment(frames, prompts) -> (K, T, size, size) floats in [0, 1]."""
    names = [str(n) for n in names]
    n = int(len(frames))
    if not names or n == 0:
        return {"probs": np.zeros((n, 0, 1, 1), np.uint8), "names": names, "prompts": [], "sampled": [],
                "size": 1, "model": None, "revision": None, "device": None, "max_prob": np.zeros((n, 0)),
                "zoom": None}
    prompts = [template.format(name) if "{}" in template else f"{template} {name}".strip() for name in names]
    picks = sample_indices(n, fps)
    height, width = frames.shape[1], frames.shape[2]
    box = zoom_box(masks, width, height) if zoom else None
    own = backend is None
    if own:
        backend = ClipSegBackend(device, lambda f, m: report(0.0, m))
    size = int(getattr(backend, "size", CLIPSEG_SIZE))
    report(0.0, f"CLIPSeg: {len(names)} thing(s) on {len(picks)} sampled frames ({fps:g} fps)"
                + (", with a zoom pass on the person" if box else ""))
    maps = np.zeros((len(picks), len(prompts), size, size), np.float32)
    chunk = 4
    try:
        for start in range(0, len(picks), chunk):
            idx = picks[start:start + chunk]
            got = backend.segment(frames[idx], prompts)
            if box is not None:
                x0, y0, x1, y1 = box
                crops = np.stack([np.ascontiguousarray(frames[i][y0:y1, x0:x1]) for i in idx])
                zoomed = backend.segment(crops, prompts)
                got = np.stack([merge_zoom(got[k], zoomed[k], box, width, height) for k in range(len(idx))])
            maps[start:start + len(idx)] = got
            done = start + len(idx)
            report(done / len(picks), f"things: sampled frame {done}/{len(picks)}")
    finally:
        if own and hasattr(backend, "close"):
            backend.close()
    store = size
    if n * len(prompts) * size * size > PROB_BUDGET_BYTES and size >= 64:
        store = size // 2
        import cv2
        small = np.zeros((len(picks), len(prompts), store, store), np.float32)
        for k in range(len(picks)):
            for t in range(len(prompts)):
                small[k, t] = cv2.resize(maps[k, t], (store, store), interpolation=cv2.INTER_AREA)
        maps = small
    probs = interpolate(maps, picks, n)
    max_prob = probs.reshape(n, len(prompts), -1).max(axis=2).astype(np.float32) / 255.0
    return {"probs": probs, "names": names, "prompts": prompts, "sampled": picks, "size": store,
            "model": getattr(backend, "repo", None) or getattr(backend, "name", "backend"),
            "revision": getattr(backend, "revision", None), "device": getattr(backend, "device", None),
            "fps": fps, "max_prob": max_prob, "zoom": list(box) if box else None}


def things_probabilities(frame_probs: np.ndarray, width: int, height: int) -> np.ndarray:
    """(T, s, s) uint8 maps of one frame -> (T, height, width) float32 probabilities at the frame size."""
    import cv2
    t = frame_probs.shape[0]
    out = np.zeros((t, height, width), np.float32)
    for k in range(t):
        out[k] = cv2.resize(frame_probs[k], (width, height), interpolation=cv2.INTER_LINEAR) / 255.0
    return out


def things_regions(probs: np.ndarray, threshold: float = THRESHOLD, hysteresis: float = HYSTERESIS,
                   min_area: float = MIN_AREA) -> np.ndarray:
    """(T, H, W) probabilities -> (T, H, W) bool: per thing the blobs above `threshold` that contain a pixel
    above threshold + hysteresis and cover at least min_area of the frame."""
    import cv2
    t, h, w = probs.shape
    out = np.zeros((t, h, w), bool)
    min_px = max(1, int(round(min_area * h * w)))
    high = min(0.999, threshold + hysteresis)
    for k in range(t):
        low = probs[k] >= threshold
        if not low.any():
            continue
        count, labels, stats, _ = cv2.connectedComponentsWithStats(low.astype(np.uint8), connectivity=8)
        if count < 2:
            continue
        peak = np.zeros(count, np.float32)
        np.maximum.at(peak, labels.ravel(), probs[k].ravel())
        keep = np.flatnonzero((stats[:, cv2.CC_STAT_AREA] >= min_px) & (peak >= high))
        keep = keep[keep != 0]
        if len(keep):
            out[k] = np.isin(labels, keep)
    return out
