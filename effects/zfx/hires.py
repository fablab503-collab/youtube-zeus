"""Composite VACE's generated person into the ORIGINAL frames at the source resolution (4K stays 4K).

VACE generates a crop around the person at 640x368 / 368x640 (zfx/cropbox.py, zfx/engines/vace.py). This module
turns those crops into final.mp4:

    1. upscale each window's crops (Real-ESRGAN, zfx/upscale.py), resize to the crop box in source pixels;
    2. colour: mean/std transfer in Lab, estimated per frame from a ring just outside the mask (generated ring
       -> original ring, both at generation size, smoothed over +-3 frames) and applied to the whole patch, so the
       model's colour/exposure drift is removed while the new person keeps their own colours;
    3. Log footage: the crops were generated in Rec.709 (vace read them through the forward LUT); the patch goes back
       to the source's Log encoding with zfx.logcolor.LogInverter before it is merged;
    4. grain: the source's noise level is measured in flat areas (Immerkaer's operator on the flattest blocks) and the
       patch, which comes out of the upscaler almost noise-free, gets the missing grain, fresh every frame;
    5. merge with a distance-based feather inside the mask (~1.5 % of the box's short side): alpha 0 on the mask
       edge rising to 1, so every pixel outside the mask is the original, bit for bit; optional light wrap;
    6. frames on the source's clock (the same fps rule ffmpeg's fps filter uses, never a time stretch), encoded with
       the source's colour tags, and the original audio stream-copied untouched.

The numpy functions (feather_alpha, ring, color_params, apply_color, estimate_noise, add_grain, light_wrap,
composite_region, fps_map) work on plain arrays and are unit-tested; render() does the ffmpeg I/O.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

FEATHER_SHARE = 0.015    # feather width = 1.5 % of the crop box's short side
RING_SHARE = 0.03        # colour ring = 3 % of the generation size's short side, just outside the mask
COLOR_MAX_GAIN = (1.5, 1.15, 1.15)   # std ratio clamp for L, a, b: the ring is background, so it may correct
                                     # the model's contrast drift but must not desaturate the new person
COLOR_SMOOTH = 3         # +- frames
GRAIN_CHROMA = 0.3       # share of the grain variance that is per channel (the rest is luma)
MIN_RING_PIXELS = 200
GEN_FPS = 16

Progress = Callable[[str, float, str], None]


# ----------------------------------------------------------------------------- masks
def _dist_inside(mask: np.ndarray, open_edges: Sequence[str] = (), reach: int = 0) -> np.ndarray:
    """Euclidean distance of each mask pixel to the nearest non-mask pixel (0 outside). `open_edges` ('left',
    'top', 'right', 'bottom'): the mask continues past that border (the frame edge), so the border is not an edge;
    distances there are exact up to `reach` px (enough for a feather)."""
    import cv2
    m = np.asarray(mask, bool)
    h, w = m.shape
    big = max(1, int(reach)) if open_edges else 0
    pt, pb = (big if "top" in open_edges else 0), (big if "bottom" in open_edges else 0)
    pl, pr = (big if "left" in open_edges else 0), (big if "right" in open_edges else 0)
    p = np.pad(m, ((pt, pb), (pl, pr)), mode="edge") if big else m
    p = np.pad(p, 1, constant_values=False)
    d = cv2.distanceTransform(p.astype(np.uint8), cv2.DIST_L2, 5)
    return d[1 + pt:1 + pt + h, 1 + pl:1 + pl + w]


def feather_alpha(mask: np.ndarray, feather_px: float, open_edges: Sequence[str] = ()) -> np.ndarray:
    """Alpha in [0,1]: 0 outside the mask, smoothstep of (distance inside the mask / feather_px), so it rises
    monotonically from the mask edge and is 1 deeper than feather_px."""
    d = _dist_inside(mask, open_edges, reach=int(math.ceil(feather_px)) + 2)
    t = np.clip(d / max(float(feather_px), 1e-6), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def ring(mask: np.ndarray, width: float) -> np.ndarray:
    """Pixels outside the mask within `width` px of it."""
    import cv2
    m = np.asarray(mask, bool)
    d = cv2.distanceTransform((~m).astype(np.uint8), cv2.DIST_L2, 5)
    return (~m) & (d <= width)


# ----------------------------------------------------------------------------- colour
def to_lab(rgb01: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(np.ascontiguousarray(rgb01, np.float32), cv2.COLOR_RGB2Lab)


def from_lab(lab: np.ndarray) -> np.ndarray:
    import cv2
    return np.clip(cv2.cvtColor(np.ascontiguousarray(lab, np.float32), cv2.COLOR_Lab2RGB), 0.0, 1.0)


def color_params(gen01: np.ndarray, orig01: np.ndarray, sel: np.ndarray) -> Optional[np.ndarray]:
    """(4, 3): Lab mean and std of the generated pixels, then of the original pixels, over `sel` (the ring).
    None when the ring is too small to say anything."""
    if int(np.count_nonzero(sel)) < MIN_RING_PIXELS:
        return None
    g, o = to_lab(gen01)[sel], to_lab(orig01)[sel]
    return np.stack([g.mean(0), g.std(0), o.mean(0), o.std(0)]).astype(np.float32)


def smooth_params(params: Sequence[Optional[np.ndarray]], radius: int = COLOR_SMOOTH) -> list[Optional[np.ndarray]]:
    """Centred moving average over +-radius frames (frames without a ring borrow from their neighbours)."""
    have = [p for p in params if p is not None]
    if not have:
        return list(params)
    out = []
    for i in range(len(params)):
        near = [params[j] for j in range(max(0, i - radius), min(len(params), i + radius + 1)) if params[j] is not None]
        out.append(np.mean(near, axis=0) if near else np.mean(have, axis=0))
    return out


def color_gain(params: np.ndarray, max_gain=COLOR_MAX_GAIN) -> np.ndarray:
    mg = np.asarray(max_gain, np.float32) * np.ones(3, np.float32)
    return np.clip(params[3] / np.maximum(params[1], 1e-3), 1.0 / mg, mg)


def apply_color(rgb01: np.ndarray, params: Optional[np.ndarray], strength: float = 1.0,
                max_gain=COLOR_MAX_GAIN) -> np.ndarray:
    """Mean/std transfer in Lab: (lab - gen_mean) * clip(orig_std / gen_std) + orig_mean, blended by strength."""
    if params is None or strength <= 0:
        return rgb01
    gm, gs, om, os_ = params
    gain = color_gain(params, max_gain)
    lab = to_lab(rgb01)
    moved = (lab - gm) * gain + om
    if strength < 1.0:
        moved = lab + (moved - lab) * strength
    return from_lab(moved)


# ----------------------------------------------------------------------------- grain
_IMMERKAER = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)


def estimate_noise(gray: np.ndarray, block: int = 16, keep: float = 0.3, exclude: Optional[np.ndarray] = None,
                   top: float = 255.0) -> float:
    """Noise sigma of a greyscale image (same units as the image), from its flattest blocks.

    Immerkaer (1996): sigma = sqrt(pi/2) * mean|I * N| / 6 with N the 3x3 second-difference kernel above, which
    cancels flat areas and linear ramps; the mean runs over the `keep` share of blocks with the least structure
    (Sobel magnitude). Sobel is odd and N even, so for Gaussian noise the two are independent and picking blocks by
    Sobel does not bias the estimate. Clipped blocks (mean within 3 % of 0 or `top`: crushed blacks, blown
    highlights) carry no noise and are skipped."""
    import cv2
    g = np.asarray(gray, np.float32)
    h, w = g.shape
    resp = np.abs(cv2.filter2D(g, cv2.CV_32F, _IMMERKAER, borderType=cv2.BORDER_REFLECT))
    grad = np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3))
    hb, wb = (h - 2) // block, (w - 2) // block
    if hb < 1 or wb < 1:
        return float(math.sqrt(math.pi / 2.0) * resp[1:-1, 1:-1].mean() / 6.0) if h > 2 and w > 2 else 0.0
    sl = (slice(1, 1 + hb * block), slice(1, 1 + wb * block))
    r = resp[sl].reshape(hb, block, wb, block).mean(axis=(1, 3))
    s = grad[sl].reshape(hb, block, wb, block).mean(axis=(1, 3))
    level = g[sl].reshape(hb, block, wb, block).mean(axis=(1, 3))
    ok = (level > 0.03 * top) & (level < 0.97 * top)
    if exclude is not None:
        ok &= ~np.asarray(exclude, bool)[sl].reshape(hb, block, wb, block).any(axis=(1, 3))
    if not ok.any():
        ok[:] = True
    cut = np.quantile(s[ok], keep)
    chosen = ok & (s <= cut)
    return float(math.sqrt(math.pi / 2.0) * r[chosen].mean() / 6.0)


def add_grain(rgb01: np.ndarray, sigma01: float, rng: np.random.Generator, chroma: float = GRAIN_CHROMA) -> np.ndarray:
    """Gaussian grain with per-channel standard deviation sigma01 (in 0..1 units): mostly shared by the three
    channels (luma grain), `chroma` of the variance per channel."""
    if sigma01 <= 0:
        return rgb01
    h, w = rgb01.shape[:2]
    luma = rng.standard_normal((h, w, 1), dtype=np.float32) * math.sqrt(1.0 - chroma)
    per = rng.standard_normal((h, w, 3), dtype=np.float32) * math.sqrt(chroma)
    return rgb01 + (luma + per) * np.float32(sigma01)


def light_wrap(patch01: np.ndarray, orig01: np.ndarray, mask: np.ndarray, radius: float, strength: float) -> np.ndarray:
    """Let the original background's light spill a little over the patch's edge: near the mask edge (inside),
    mix toward the blurred background by up to `strength`."""
    import cv2
    if strength <= 0:
        return patch01
    m = np.asarray(mask, bool)
    bg = (~m).astype(np.float32)
    k = max(3, int(radius * 3) | 1)
    wsum = cv2.GaussianBlur(bg, (k, k), radius)
    wrap = cv2.GaussianBlur(orig01 * bg[..., None], (k, k), radius) / np.maximum(wsum, 1e-4)[..., None]
    edge = (wsum * m)[..., None]
    return patch01 + strength * edge * (wrap - patch01)


# ----------------------------------------------------------------------------- the merge
def composite_region(base: np.ndarray, patch01: np.ndarray, box: Sequence[int], mask: np.ndarray, feather_px: float,
                     *, grain_sigma01: float = 0.0, rng: Optional[np.random.Generator] = None,
                     lightwrap: float = 0.0, open_edges: Sequence[str] = ()) -> np.ndarray:
    """base (H, W, 3) uint8/uint16 with patch01 (h, w, 3) float in [0,1] merged into box [x0, y0, x1, y1) inside the
    feathered `mask` (h, w). Returns a new array; every pixel where alpha is 0 (all of the frame outside the mask)
    is the base pixel unchanged."""
    x0, y0, x1, y1 = (int(v) for v in box)
    out = np.array(base, copy=True)
    region = base[y0:y1, x0:x1]
    if region.shape[:2] != patch01.shape[:2] or region.shape[:2] != np.shape(mask):
        raise ValueError(f"patch {patch01.shape[:2]} / mask {np.shape(mask)} do not match box {region.shape[:2]}")
    alpha = feather_alpha(mask, feather_px, open_edges)
    on = alpha > 0
    if not on.any():
        return out
    ys, xs = np.nonzero(on)
    a0, a1, b0, b1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1      # work only where the mask is
    top = float(np.iinfo(base.dtype).max) if np.issubdtype(base.dtype, np.integer) else 1.0
    orig = region[a0:a1, b0:b1].astype(np.float32) / top
    p = np.asarray(patch01[a0:a1, b0:b1], np.float32)
    m = np.asarray(mask, bool)[a0:a1, b0:b1]
    if lightwrap > 0:
        p = light_wrap(p, orig, m, max(2.0, feather_px), lightwrap)
    if grain_sigma01 > 0:
        p = add_grain(p, grain_sigma01, rng or np.random.default_rng(0))
    al = alpha[a0:a1, b0:b1, None]
    mixed = orig * (1.0 - al) + np.clip(p, 0.0, 1.0) * al
    vals = np.clip(mixed * top + 0.5, 0, top).astype(base.dtype) if top != 1.0 else mixed.astype(base.dtype)
    sub = out[y0 + a0:y0 + a1, x0 + b0:x0 + b1]
    keep = ~on[a0:a1, b0:b1]
    vals[keep] = sub[keep]
    out[y0 + a0:y0 + a1, x0 + b0:x0 + b1] = vals
    return out


# ----------------------------------------------------------------------------- time
def fps_map(n_out: int, out_fps, n_in: int, in_fps: int = GEN_FPS) -> list[int]:
    """Input frame shown at each output frame, as ffmpeg's fps filter does it (round=near): input frame i (time
    i/in_fps) lands on output slot round(i * out_fps / in_fps), slot k shows the last input whose slot <= k, and
    the last input is held. No time stretch: input frame i stays at time i/in_fps."""
    out_fps = Fraction(out_fps)
    slots = [int(math.floor(i * out_fps / in_fps + Fraction(1, 2))) for i in range(n_in)]
    res, i = [], 0
    for k in range(n_out):
        while i + 1 < n_in and slots[i + 1] <= k:
            i += 1
        res.append(i)
    return res


# ----------------------------------------------------------------------------- ffmpeg I/O
def _tool(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", shutil.which(name)):
        if cand and Path(cand).is_file():
            return cand
    raise RuntimeError(f"{name} is not installed (brew install ffmpeg).")


_MATRIX = {"bt709": "bt709", "bt470bg": "bt601", "smpte170m": "bt601", "bt2020nc": "bt2020", "bt2020c": "bt2020",
           "smpte240m": "smpte240m", "fcc": "fcc"}


def source_format(path: Path) -> dict:
    """Pixel format, bit depth and colour tags of the first video stream."""
    proc = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=pix_fmt,color_space,color_transfer,color_primaries,color_range,bits_per_raw_sample",
                           "-of", "json", str(path)], capture_output=True, text=True)
    s = (json.loads(proc.stdout or "{}").get("streams") or [{}])[0]
    pix = str(s.get("pix_fmt") or "yuv420p")
    deep = any(t in pix for t in ("10", "12", "16")) or str(s.get("bits_per_raw_sample") or "8") not in ("", "8")
    known = lambda v: v if v and v not in ("unknown", "reserved", "unspecified") else None   # noqa: E731
    return {"pix_fmt": pix, "deep": deep, "matrix": known(s.get("color_space")),
            "transfer": known(s.get("color_transfer")), "primaries": known(s.get("color_primaries")),
            "range": known(s.get("color_range"))}


def _read_exact(stream, n: int) -> Optional[bytes]:
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = stream.readinto(view[got:])
        if not k:
            return None
        got += k
    return bytes(buf)


def read_gray_frames(path: Path, width: int, height: int) -> np.ndarray:
    proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-i", str(path), "-vf",
                           f"scale={width}:{height}:flags=bilinear", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Could not read {path.name}: {proc.stderr.decode(errors='replace')[-200:]}")
    n = len(proc.stdout) // (width * height)
    return np.frombuffer(proc.stdout[: n * width * height], np.uint8).reshape(n, height, width)


def _frame_size(path: Path) -> tuple[int, int]:
    out = subprocess.run([_tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True)
    w, h = (int(v) for v in out.stdout.strip().split("\n")[0].split("x")[:2])
    return w, h


def _load_png(path: Path) -> np.ndarray:
    import cv2
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Could not read {path}")
    return img[:, :, ::-1]


# ----------------------------------------------------------------------------- render
def render(job_dir: Path, manifest: dict, source: Path, src_info: dict, mask_path: Path, audio: Optional[Path],
           out: Path, comment: str, *, upscale_mode: str = "fast", color: Optional[dict] = None,
           lightwrap: Optional[float] = None, report: Optional[Progress] = None) -> dict:
    """final.mp4 from vace's crops (manifest = result-crops.json) over source.mp4 at its own size/fps/clock.

    src_info: generate.probe_video(source) (width, height, fps Fraction, container_duration, video_start)."""
    import cv2
    from . import cropbox, upscale
    from . import logcolor

    report = report or (lambda *a, **k: None)
    t_start = time.monotonic()
    job_dir = Path(job_dir)
    crops_root = job_dir / manifest.get("dir", "result-crops")
    if lightwrap is None:
        try:
            lightwrap = float(os.environ.get("ZEUS_FX_LIGHTWRAP", "0") or 0)
        except ValueError:
            lightwrap = 0.0
    fmt = source_format(source)
    W, H = src_info["width"] - src_info["width"] % 2, src_info["height"] - src_info["height"] % 2
    src_w, src_h = manifest["source_size"]
    if (src_w, src_h) != (src_info["width"], src_info["height"]):
        raise RuntimeError("result-crops.json does not match source.mp4 (size changed); generate again.")
    rate = src_info["fps"] or Fraction(30)
    src_dur = float(src_info["container_duration"])
    n_out = max(1, int(math.ceil(src_dur * float(rate) - 1e-6)))
    total = int(manifest["total"])
    windows = manifest["windows"]
    cover = cropbox.frame_weights([(w["start"], w["end"]) for w in windows], total)
    fmap = fps_map(n_out, rate, total)

    # masks at work size
    ww, wh = manifest["work_size"]
    masks = read_gray_frames(mask_path, ww, wh)
    if len(masks) < total:
        masks = np.concatenate([masks, np.repeat(masks[-1:], total - len(masks), 0)])
    sx, sy = ww / float(src_w), wh / float(src_h)

    # Log
    inverter = None
    if color and color.get("inverse_lut") and color.get("forward_lut"):
        forward = logcolor.read_cube(job_dir / color["forward_lut"])
        inverter = logcolor.LogInverter(forward, logcolor.load_inverse(job_dir / color["inverse_lut"]))

    tmp_root = Path(tempfile.mkdtemp(prefix=".hires-", dir=str(job_dir)))
    up_infos, colour_summary = [], []
    try:
        # 1) upscale + 2) colour parameters, per window
        params: list[list] = []
        for wi, w in enumerate(windows):
            report("composite", 0.02 + 0.2 * wi / max(1, len(windows)), f"upscaling window {wi + 1}/{len(windows)}")
            wdir = crops_root / w["dir"]
            x0, y0, x1, y1 = w["box"]
            gw, gh = w["gen"]
            ratio = max((x1 - x0) / float(gw), (y1 - y0) / float(gh))
            info = upscale.upscale_dir(wdir / "gen", tmp_root / w["dir"], ratio, upscale_mode)
            info["window"] = wi
            up_infos.append(info)
            ring_px = max(3.0, RING_SHARE * min(gw, gh))
            raw = []
            for j in range(w["valid"]):
                gen = _load_png(wdir / "gen" / f"{j:04d}.png").astype(np.float32) / 255.0
                base = _load_png(wdir / "base" / f"{j:04d}.png").astype(np.float32) / 255.0
                mk = cv2.imread(str(wdir / "mask" / f"{j:04d}.png"), cv2.IMREAD_GRAYSCALE) > 127
                raw.append(color_params(gen, base, ring(mk, ring_px) if mk.any() else np.zeros_like(mk)))
            sm = smooth_params(raw)
            params.append(sm)
            have = [p for p in sm if p is not None]
            if have:
                shift = np.mean([p[2] - p[0] for p in have], axis=0)
                gain = np.mean([color_gain(p) for p in have], axis=0)
                colour_summary.append({"window": wi, "frames_with_ring": len([p for p in raw if p is not None]),
                                       "mean_shift_Lab": [round(float(v), 2) for v in shift],
                                       "gain_Lab": [round(float(v), 3) for v in gain]})

        # 4) grain: the source's noise (flattest blocks of a few frames), in source code values
        top = 65535.0 if fmt["deep"] else 255.0
        pix_in = "rgb48le" if fmt["deep"] else "rgb24"
        dtype = np.uint16 if fmt["deep"] else np.uint8
        lead = max(0.0, float(src_info.get("video_start") or 0.0))
        rate_s = f"{rate.numerator}/{rate.denominator}"
        hold = f"tpad=stop_mode=clone:stop_duration=1,trim=end={src_dur:.6f}"
        mat = _MATRIX.get(fmt["matrix"] or "")
        rng_in = {"tv": "tv", "pc": "pc"}.get(fmt["range"] or "", "tv")
        conv = f"scale={W}:{H}" + (f":in_color_matrix={mat}" if mat else "") + f":in_range={rng_in}"
        base_vf = (f"setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration={lead:.6f},fps={rate_s},{hold},"
                   f"{conv},format={pix_in}")
        sigmas = []
        for frac in (0.25, 0.5, 0.75):
            t = min(src_dur * frac, max(0.0, src_dur - 0.05))
            proc = subprocess.run([_tool("ffmpeg"), "-v", "error", "-ss", f"{t:.3f}", "-i", str(source), "-frames:v", "1",
                                   "-vf", f"{conv},format=gray16le" if fmt["deep"] else f"{conv},format=gray",
                                   "-f", "rawvideo", "-"], capture_output=True)
            if proc.returncode == 0 and len(proc.stdout) >= W * H * (2 if fmt["deep"] else 1):
                g = np.frombuffer(proc.stdout[: W * H * (2 if fmt["deep"] else 1)],
                                  np.uint16 if fmt["deep"] else np.uint8).reshape(H, W).astype(np.float32)
                sigmas.append(estimate_noise(g, top=top) / top)
        src_sigma = float(np.median(sigmas)) if sigmas else 0.0
        patch_sigma: dict = {}

        # 5) + 6) stream the source, merge, encode
        tag_args = []
        for flag, key in (("-color_primaries", "primaries"), ("-color_trc", "transfer"), ("-colorspace", "matrix")):
            if fmt[key]:
                tag_args += [flag, fmt[key]]
        out_mat = f":out_color_matrix={mat}" if mat else ""
        if fmt["deep"]:
            enc = ["-c:v", "libx265", "-preset", "fast", "-crf", "16", "-tag:v", "hvc1", "-x265-params", "log-level=error"]
            out_pix = "yuv420p10le"
        else:
            enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "17"]
            out_pix = "yuv420p"
        tmp_out = out.with_name(out.stem + ".partial" + out.suffix)
        reader = subprocess.Popen([_tool("ffmpeg"), "-v", "error", "-i", str(source), "-an", "-vf", base_vf,
                                   "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        wcmd = [_tool("ffmpeg"), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", pix_in, "-s", f"{W}x{H}",
                "-r", rate_s, "-i", "-"]
        if audio is not None:
            wcmd += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
        else:
            wcmd += ["-map", "0:v:0"]
        wcmd += ["-vf", f"scale=in_range=pc:out_range={rng_in}{out_mat},format={out_pix}"] + enc + tag_args
        wcmd += ["-color_range", rng_in, "-map_metadata", "-1", "-metadata", f"comment={comment}",
                 "-movflags", "+faststart", str(tmp_out)]
        err_file = tempfile.TemporaryFile()
        writer = subprocess.Popen(wcmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_file)
        frame_bytes = W * H * 3 * (2 if fmt["deep"] else 1)
        cache: dict = {}
        mask_cache: dict = {}
        feathers = []
        last_base = None
        try:
            for k in range(n_out):
                data = _read_exact(reader.stdout, frame_bytes)
                if data is None:
                    if last_base is None:
                        raise RuntimeError("Could not decode source.mp4 for the composite.")
                    base = last_base
                else:
                    base = np.frombuffer(data, dtype).reshape(H, W, 3)
                    last_base = base
                g = fmap[k]
                parts = cover[g] if g < len(cover) else []
                frame = base
                if parts:
                    boxes = [windows[wi]["box"] for wi, _ in parts]
                    ux0, uy0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
                    ux1, uy1 = min(W, max(b[2] for b in boxes)), min(H, max(b[3] for b in boxes))
                    ubw, ubh = ux1 - ux0, uy1 - uy0
                    mkey = (g, ux0, uy0, ux1, uy1)
                    if mkey not in mask_cache:
                        mask_cache.clear()
                        mw = cropbox.crop_resize(masks[g], (ux0 * sx, uy0 * sy, ux1 * sx, uy1 * sy), ubw, ubh)
                        mask_cache[mkey] = mw > 127
                    m = mask_cache[mkey]
                    if m.any():
                        # work only on the mask's bounding box: alpha is 0 everywhere else
                        rows, cols = np.nonzero(m.any(axis=1))[0], np.nonzero(m.any(axis=0))[0]
                        ma0, ma1, mb0, mb1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
                        sx0, sy0, sx1, sy1 = ux0 + mb0, uy0 + ma0, ux0 + mb1, uy0 + ma1   # source pixels
                        canvas = np.zeros((sy1 - sy0, sx1 - sx0, 3), np.float32)
                        wsum = np.zeros((sy1 - sy0, sx1 - sx0, 1), np.float32)
                        for wi, wgt in parts:
                            win = windows[wi]
                            bx0, by0, bx1, by1 = win["box"]
                            ix0, iy0, ix1, iy1 = max(bx0, sx0), max(by0, sy0), min(bx1, sx1), min(by1, sy1)
                            if ix1 <= ix0 or iy1 <= iy0:
                                continue
                            key = (wi, g)
                            if key not in cache:
                                while len(cache) >= 6:
                                    cache.pop(next(iter(cache)))
                                j = g - win["start"]
                                up = _load_png(tmp_root / win["dir"] / f"{j:04d}.png")
                                full = upscale.resize(up, bx1 - bx0, by1 - by0)
                                sub = full[iy0 - by0:iy1 - by0, ix0 - bx0:ix1 - bx0].astype(np.float32) / 255.0
                                sub = apply_color(sub, params[wi][j] if j < len(params[wi]) else None)
                                if inverter is not None:
                                    sub = inverter(sub)
                                if wi not in patch_sigma:
                                    gray = (sub @ np.array([0.299, 0.587, 0.114], np.float32)) * top
                                    patch_sigma[wi] = estimate_noise(gray, top=top) / top
                                cache[key] = (ix0, iy0, sub)
                            px0, py0, sub = cache[key]
                            h_, w_ = sub.shape[:2]
                            canvas[py0 - sy0:py0 - sy0 + h_, px0 - sx0:px0 - sx0 + w_] += sub * wgt
                            wsum[py0 - sy0:py0 - sy0 + h_, px0 - sx0:px0 - sx0 + w_] += wgt
                        region = base[sy0:sy1, sx0:sx1].astype(np.float32) / top
                        canvas = np.where(wsum > 0, canvas / np.maximum(wsum, 1e-6), region)
                        feather = max(2.0, FEATHER_SHARE * min(ubw, ubh))
                        feathers.append(feather)
                        edges = [e for e, hit in (("left", sx0 <= 0), ("top", sy0 <= 0), ("right", sx1 >= W),
                                                  ("bottom", sy1 >= H)) if hit]
                        ps = max(patch_sigma.get(wi, 0.0) for wi, _ in parts)
                        grain = math.sqrt(max(0.0, src_sigma ** 2 - ps ** 2))
                        frame = composite_region(base, canvas, (sx0, sy0, sx1, sy1), m[ma0:ma1, mb0:mb1], feather,
                                                 grain_sigma01=grain, rng=np.random.default_rng(1_000_003 + k),
                                                 lightwrap=float(lightwrap), open_edges=edges)
                writer.stdin.write(np.ascontiguousarray(frame).tobytes())
                if k % 10 == 0 or k == n_out - 1:
                    report("composite", 0.25 + 0.73 * (k + 1) / n_out, f"frame {k + 1}/{n_out} at {W}x{H}")
            writer.stdin.close()
            code = writer.wait()
        except BaseException:
            writer.kill()
            writer.wait()
            tmp_out.unlink(missing_ok=True)
            raise
        finally:
            reader.kill()
            reader.wait()
        if code != 0 or not tmp_out.is_file():
            err_file.seek(0)
            tmp_out.unlink(missing_ok=True)
            raise RuntimeError("Final encode failed: " + err_file.read().decode(errors="replace").strip()[-300:])
        os.replace(tmp_out, out)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return {"retime_factor": 1.0, "fps": f"{rate.numerator}/{rate.denominator}", "duration": round(src_dur, 3),
            "video_offset": 0.0, "width": W, "height": H, "frames": n_out, "video_stream_copy": False,
            "composited_in_mask": True, "hires": True, "audio": audio.name if audio is not None else None,
            "comment": comment, "pix_fmt": out_pix, "color_tags": {k: fmt[k] for k in ("primaries", "transfer", "matrix", "range")},
            "upscale": up_infos, "color_match": colour_summary,
            "grain": {"source_sigma_8bit": round(src_sigma * 255, 3),
                      "patch_sigma_8bit": {str(k): round(v * 255, 3) for k, v in patch_sigma.items()},
                      "method": "Immerkaer on the flattest 30 % of 16 px blocks"},
            "feather_px": round(float(np.median(feathers)), 1) if feathers else None,
            "light_wrap": float(lightwrap), "log_inverse": bool(inverter is not None),
            "log_colours_solved": inverter.solved if inverter is not None else 0,
            "seconds": round(time.monotonic() - t_start, 1)}
