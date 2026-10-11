"""Camera Log footage for vace: detect it, give the model a Rec.709 picture, put the result back in Log.

Detection (first answer wins; ZEUS_FX_LOG_PROFILE=<profile id> forces one, =none turns Log handling off):
    1. Cinema 101's CLI, `c101 probe FILE` (ZEUS_C101, default /Volumes/Volume1/SecondBrain/cinema101/build/c101):
       the profile in brackets, e.g. `profile: Apple Log [apple.log] — Apple Log key in the file (From the file's
       own tags)`. Cinema 101 reads Apple's LogTransferFunction key, DJI/Sony tags and sidecars. Profiles that are
       not Log (generic.*, *.hlg, apple.hdr, dji.normal) mean "no Log".
    2. The file's colour tags (ffprobe): a log transfer or an Apple Log tag -> apple.log.
   The job's original input file is probed before source.mp4, because prepare re-encodes source.mp4 with ffmpeg,
   which drops Apple's LogTransferFunction key.

Conversion:
    forward  = `c101 bake --profile <id> --out <job>/color/<id>-to-rec709.cube` (Cinema 101's own maths, neutral
               grade, input = legal-range signal normalised 0..1, i.e. what ffmpeg's yuv -> rgb gives), applied to the
               crops the model sees with ffmpeg lut3d. Without Cinema 101, Apple Log only: an approximate LUT from
               Apple's published Apple Log curve (BT.2020 -> BT.709, simple highlight roll-off).
    inverse  = the forward LUT inverted numerically: a 1-D shaper (inverse of its neutral axis) + a 3-D grid solved
               by damped Gauss-Newton per point, saved as rec709-to-<id>.npz; Cinema 101's CLI has no inverse/encode command (its Swift LogCurve has
               encode/decode, but the baked LUT also tone-maps, so the LUT itself is what has to be inverted).
               Where the forward LUT clips (crushed shadows, rolled-off highlights) the inverse returns the closest
               Log value, which maps back to the same Rec.709 colour.
Everything here is numpy; LUTs are arrays (S, S, S, 3) indexed [r, g, b] with values in [0, 1].
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

C101_DEFAULT = "/Volumes/Volume1/SecondBrain/cinema101/build/c101"
# Cinema 101 profile ids that are not camera Log (display-referred SDR / HDR video)
NOT_LOG = {"generic.rec709", "generic.hlg", "generic.pq", "apple.hdr", "dji.hlg", "sony.hlg", "dji.normal"}
LUT_SIZE = 33          # forward bake size (c101 default is 65; 33 is plenty for a smooth transform)
INVERSE_SIZE = 33


class ColorError(RuntimeError):
    """Plain-English colour problem."""


# ----------------------------------------------------------------------------- .cube files
def read_cube(path) -> np.ndarray:
    """3-D .cube -> float32 (S, S, S, 3) indexed [r, g, b]. DOMAIN_MIN/MAX other than 0..1 are refused."""
    size = None
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split()[0].upper()
        if head == "LUT_3D_SIZE":
            size = int(line.split()[1])
        elif head == "DOMAIN_MIN":
            if any(abs(float(v)) > 1e-6 for v in line.split()[1:4]):
                raise ColorError(f"{Path(path).name}: DOMAIN_MIN other than 0 is not supported.")
        elif head == "DOMAIN_MAX":
            if any(abs(float(v) - 1.0) > 1e-6 for v in line.split()[1:4]):
                raise ColorError(f"{Path(path).name}: DOMAIN_MAX other than 1 is not supported.")
        elif head in ("TITLE", "LUT_1D_SIZE", "LUT_1D_INPUT_RANGE", "LUT_3D_INPUT_RANGE"):
            if head == "LUT_1D_SIZE":
                raise ColorError(f"{Path(path).name} is a 1-D LUT; a 3-D LUT is needed.")
            continue
        else:
            try:
                rows.append([float(v) for v in line.split()[:3]])
            except ValueError:
                continue
    if not size or len(rows) != size ** 3:
        raise ColorError(f"{Path(path).name} is not a complete 3-D .cube LUT.")
    # file order: red changes fastest -> reshape gives [b][g][r]
    return np.ascontiguousarray(np.asarray(rows, np.float32).reshape(size, size, size, 3).transpose(2, 1, 0, 3))


def write_cube(lut: np.ndarray, path, title: str = "") -> Path:
    size = lut.shape[0]
    flat = np.asarray(lut, np.float32).transpose(2, 1, 0, 3).reshape(-1, 3)
    lines = [f'TITLE "{title}"' if title else "# YouTube Zeus Effects", f"LUT_3D_SIZE {size}",
             "DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in flat]
    path = Path(path)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def identity_lut(size: int = 17) -> np.ndarray:
    t = np.linspace(0.0, 1.0, size, dtype=np.float32)
    r, g, b = np.meshgrid(t, t, t, indexing="ij")
    return np.stack([r, g, b], axis=-1)


def lut_from_function(fn, size: int = LUT_SIZE) -> np.ndarray:
    """Sample fn((N,3) float32 in [0,1]) -> (N,3) on a size^3 grid."""
    grid = identity_lut(size)
    return np.asarray(fn(grid.reshape(-1, 3)), np.float32).reshape(size, size, size, 3)


# ----------------------------------------------------------------------------- applying
def apply_lut(rgb: np.ndarray, lut: np.ndarray, chunk: int = 1 << 20) -> np.ndarray:
    """Trilinear 3-D LUT on float RGB in [0,1] (any leading shape). Returns float32, same shape."""
    lut = np.asarray(lut, np.float32)
    s = lut.shape[0]
    flat = np.asarray(rgb, np.float32).reshape(-1, 3)
    out = np.empty_like(flat)
    for a in range(0, len(flat), chunk):
        x = np.clip(flat[a:a + chunk], 0.0, 1.0) * (s - 1)
        i0 = np.minimum(x.astype(np.int32), s - 2)
        f = x - i0
        r0, g0, b0 = i0[:, 0], i0[:, 1], i0[:, 2]
        fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
        c00 = lut[r0, g0, b0] * (1 - fr) + lut[r0 + 1, g0, b0] * fr
        c10 = lut[r0, g0 + 1, b0] * (1 - fr) + lut[r0 + 1, g0 + 1, b0] * fr
        c01 = lut[r0, g0, b0 + 1] * (1 - fr) + lut[r0 + 1, g0, b0 + 1] * fr
        c11 = lut[r0, g0 + 1, b0 + 1] * (1 - fr) + lut[r0 + 1, g0 + 1, b0 + 1] * fr
        c0 = c00 * (1 - fg) + c10 * fg
        c1 = c01 * (1 - fg) + c11 * fg
        out[a:a + chunk] = c0 * (1 - fb) + c1 * fb
    return out.reshape(np.shape(rgb))


# ----------------------------------------------------------------------------- inverting
SHAPER_POINTS = 4097


def neutral_axis(lut: np.ndarray, points: int = SHAPER_POINTS) -> tuple[np.ndarray, np.ndarray]:
    """(t, axis): the LUT's neutral axis F(t, t, t) per channel, made strictly increasing (for np.interp).

    The tiny ramp is (t - 1) * 1e-6, i.e. negative inside a flat stretch, so a display value a flat stretch maps to
    comes back at that stretch's END: display black returns the highest Log code the LUT crushes to black (about where
    a camera's real black sits, Apple Log ~0.12-0.15), not Log 0, which would print a darker hole into the shadows of
    the Log footage once someone grades it differently (seen on a synthetic Apple Log 4K clip)."""
    t = np.linspace(0.0, 1.0, points, dtype=np.float32)
    axis = apply_lut(np.repeat(t[:, None], 3, axis=1), lut).T.copy()
    for c in range(3):
        axis[c] = np.maximum.accumulate(axis[c]) + (t - 1.0) * 1e-6
    return t, axis


def _shape(y: np.ndarray, t: np.ndarray, axis: np.ndarray) -> np.ndarray:
    """Display values -> shaper coordinates: per-channel inverse of the neutral axis (exact on greys)."""
    u = np.empty(np.shape(y), np.float32)
    for c in range(3):
        u[..., c] = np.interp(y[..., c], axis[c], t)
    return u


def _unshape(u: np.ndarray, t: np.ndarray, axis: np.ndarray) -> np.ndarray:
    y = np.empty(np.shape(u), np.float32)
    for c in range(3):
        y[..., c] = np.interp(u[..., c], t, axis[c])
    return y


def _solve(lut: np.ndarray, y: np.ndarray, x0: np.ndarray, iters: int, tol: float) -> tuple[np.ndarray, np.ndarray]:
    """Damped Gauss-Newton (Levenberg-Marquardt) for F(x) = y, one 3x3 system per point, x kept in [0,1]^3.
    Returns (x, squared residual)."""
    x = np.array(x0, np.float32)
    lam = np.full(len(y), 1e-3, np.float32)
    h = 0.5 / (lut.shape[0] - 1)
    eye = np.eye(3, dtype=np.float32)
    r = apply_lut(x, lut) - y
    err = np.einsum("ij,ij->i", r, r)
    for _ in range(iters):
        jac = np.empty((len(y), 3, 3), np.float32)
        for k in range(3):
            d = np.zeros(3, np.float32)
            d[k] = h
            hi, lo = np.clip(x + d, 0.0, 1.0), np.clip(x - d, 0.0, 1.0)
            span = np.maximum(hi[:, k] - lo[:, k], 1e-9)[:, None]
            jac[:, :, k] = (apply_lut(hi, lut) - apply_lut(lo, lut)) / span
        jt = jac.transpose(0, 2, 1)
        step = np.linalg.solve(jt @ jac + lam[:, None, None] * eye, (jt @ r[:, :, None]))[:, :, 0]
        x_new = np.clip(x - step, 0.0, 1.0)
        r_new = apply_lut(x_new, lut) - y
        err_new = np.einsum("ij,ij->i", r_new, r_new)
        better = err_new < err
        x[better], r[better], err[better] = x_new[better], r_new[better], err_new[better]
        lam = np.where(better, np.maximum(lam * 0.3, 1e-7), np.minimum(lam * 10.0, 1e4)).astype(np.float32)
        if float(np.sqrt(err.max())) < tol:
            break
    return x, err


def _nearest_samples(lut: np.ndarray, y: np.ndarray, chunk: int = 1024) -> np.ndarray:
    """For each target colour, the LUT input whose stored output is closest (brute force, chunked)."""
    s = lut.shape[0]
    xs = identity_lut(s).reshape(-1, 3)
    ys = lut.reshape(-1, 3)
    ys2 = np.einsum("ij,ij->i", ys, ys)
    out = np.empty_like(y)
    for a in range(0, len(y), chunk):
        q = y[a:a + chunk]
        d = ys2[None, :] - 2.0 * (q @ ys.T)
        out[a:a + chunk] = xs[np.argmin(d, axis=1)]
    return out


def harmonic_fill(grid: np.ndarray, known: np.ndarray, iters: int = 400) -> np.ndarray:
    """Replace the unknown points of a (S,S,S,C) grid by the harmonic extension of the known ones (Jacobi
    iterations of the 6-neighbour average, edges replicated); known points stay as they are."""
    g = np.array(grid, np.float32)
    unknown = ~known
    if not unknown.any() or not known.any():
        return g
    for _ in range(iters):
        p = np.pad(g, ((1, 1), (1, 1), (1, 1), (0, 0)), mode="edge")
        avg = (p[2:, 1:-1, 1:-1] + p[:-2, 1:-1, 1:-1] + p[1:-1, 2:, 1:-1] + p[1:-1, :-2, 1:-1]
               + p[1:-1, 1:-1, 2:] + p[1:-1, 1:-1, :-2]) / 6.0
        g[unknown] = avg[unknown]
    return g


REACHABLE = 0.01   # display units (2.5 code values of 8 bits): a grid colour F can produce


def invert_lut(lut: np.ndarray, size: int = INVERSE_SIZE, iters: int = 40, tol: float = 1e-5) -> tuple[dict, dict]:
    """Numerical inverse of a 3-D LUT F (Log -> display): G(y) = argmin_x |F(x) - y| over x in [0,1]^3.

    G is stored as a 1-D shaper (the inverse of F's neutral axis, per channel, 4097 points) followed by a 3-D LUT
    over the shaper's output, so the 3-D part is close to the identity and trilinear interpolation stays accurate
    in the shadows, where a Log curve is steep. Each 3-D grid point is solved with damped Gauss-Newton
    (Levenberg-Marquardt) on the trilinear F with central-difference Jacobians, twice: from the shaper (exact on
    greys) and from the LUT sample whose output is nearest (catches the gamut matrix's strongly coupled corners);
    the better solution wins. Grid colours F never outputs (saturated corners, below its black) have no true inverse
    and their least-squares solutions jump around (e.g. a Log red of 0 next to 0.72), which would wreck trilinear
    interpolation for reachable colours next to them, so they are replaced by the harmonic extension of the reachable
    solutions. Returns (inverse = {"t", "axis", "lut"}, stats); stats["reachable_share"] = share of reachable points."""
    lut = np.asarray(lut, np.float32)
    t, axis = neutral_axis(lut)
    u = identity_lut(size).reshape(-1, 3)
    y = _unshape(u, t, axis)                 # the display colour each 3-D grid point stands for
    x, err = _solve(lut, y, u, iters, tol)
    x2, err2 = _solve(lut, y, _nearest_samples(lut, y), iters, tol)
    better = err2 < err - 1e-10
    x[better], err[better] = x2[better], err2[better]
    res = np.sqrt(err)
    known = (res < REACHABLE).reshape(size, size, size)
    grid = harmonic_fill(x.reshape(size, size, size, 3), known)
    stats = {"size": size, "shaper_points": len(t), "median_residual": round(float(np.median(res)), 6),
             "reachable_share": round(float(known.mean()), 4)}
    return {"t": t, "axis": axis, "lut": np.clip(grid, 0.0, 1.0).astype(np.float32)}, stats


def apply_inverse(rgb: np.ndarray, inverse: dict) -> np.ndarray:
    """Display RGB in [0,1] -> Log RGB with an inverse from invert_lut (shaper, then the 3-D LUT)."""
    return apply_lut(_shape(np.asarray(rgb, np.float32), inverse["t"], inverse["axis"]), inverse["lut"])


def save_inverse(inverse: dict, path) -> Path:
    path = Path(path)
    tmp = path.with_name(path.stem + ".partial.npz")
    np.savez_compressed(tmp, t=inverse["t"], axis=inverse["axis"], lut=inverse["lut"])
    os.replace(tmp, path)
    return path


def load_inverse(path) -> dict:
    with np.load(path) as data:
        return {"t": data["t"], "axis": data["axis"], "lut": data["lut"]}


class LogInverter:
    """Exact per-colour inverse for the composite: display RGB -> Log RGB with F(G(y)) = y wherever F can produce y.

    A grid inverse alone is not enough for Cinema 101's LUTs: their tonescale desaturates highlights, so the colours F
    can produce form a thin shell there and any 3-D grid cell straddling its edge interpolates wrong values (measured on
    Apple Log -> Rec.709: 37 % of natural bright colours off by more than 1 %). So each distinct 8-bit display colour is
    solved with Gauss-Newton on F itself, started from the grid inverse, and cached in a 2^24 table (a 4K patch has
    ~10^5 distinct colours, most repeat from frame to frame)."""

    def __init__(self, forward: np.ndarray, inverse: Optional[dict] = None, iters: int = 12):
        self.forward = np.asarray(forward, np.float32)
        self.inverse = inverse if inverse is not None else invert_lut(self.forward)[0]
        self.iters = iters
        self.table = np.zeros((1 << 24, 3), np.float32)
        self.known = np.zeros(1 << 24, bool)
        self.solved = 0

    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        shape = np.shape(rgb)
        q = np.clip(np.asarray(rgb, np.float32).reshape(-1, 3) * 255.0 + 0.5, 0, 255).astype(np.uint32)
        keys = (q[:, 0] << 16) | (q[:, 1] << 8) | q[:, 2]
        todo = np.unique(keys[~self.known[keys]])
        if len(todo):
            y = np.stack([(todo >> 16) & 255, (todo >> 8) & 255, todo & 255], axis=1).astype(np.float32) / 255.0
            x, err = _solve(self.forward, y, apply_inverse(y, self.inverse), self.iters, 1e-4)
            bad = np.sqrt(err) > 0.004
            if bad.any():   # second start: the LUT sample whose output is nearest
                xb, errb = _solve(self.forward, y[bad], _nearest_samples(self.forward, y[bad]), self.iters, 1e-4)
                fix = errb < err[bad]
                idx = np.nonzero(bad)[0][fix]
                x[idx] = xb[fix]
            self.table[todo] = x
            self.known[todo] = True
            self.solved += len(todo)
        return self.table[keys].reshape(shape)


def round_trip_error(forward: np.ndarray, inverse, samples: int = 20000, seed: int = 0,
                     lo: float = 0.0, hi: float = 1.0, chroma: Optional[float] = None) -> dict:
    """Log x -> y = F(x) -> x' = G(y) -> y' = F(x'): max/mean |x' - x| (Log side) and |y' - y| (display side).
    `inverse` is a grid inverse (dict from invert_lut) or a callable such as LogInverter.
    x is uniform in [lo, hi]^3, or with `chroma`, a grey in [lo, hi] plus per-channel offsets within +-chroma
    (natural colours: skin, clothes and walls are a few Log code values off grey)."""
    rng = np.random.default_rng(seed)
    if chroma is None:
        x = rng.uniform(lo, hi, (samples, 3)).astype(np.float32)
    else:
        x = (rng.uniform(lo, hi, (samples, 1)) + rng.uniform(-chroma, chroma, (samples, 3))).astype(np.float32)
        x = np.clip(x, 0.0, 1.0)
    y = apply_lut(x, forward)
    x2 = inverse(y) if callable(inverse) else apply_inverse(y, inverse)
    y2 = apply_lut(x2, forward)
    dx, dy = np.abs(x2 - x), np.abs(y2 - y)
    return {"log_max": float(dx.max()), "log_mean": float(dx.mean()),
            "display_max": float(dy.max()), "display_mean": float(dy.mean()),
            "display_p99": float(np.percentile(dy.max(axis=1), 99))}


# ----------------------------------------------------------------------------- Apple Log fallback
APPLE_LOG = {"R0": -0.05641088, "Rt": 0.01, "c": 47.28711236, "beta": 0.00964052, "gamma": 0.08550479,
             "delta": 0.69336945}   # Apple Log Profile white paper (2023)
BT2020_TO_BT709 = np.array([[1.6605, -0.5876, -0.0728],
                            [-0.1246, 1.1329, -0.0083],
                            [-0.0182, -0.1006, 1.1187]], np.float32)


def apple_log_decode(p: np.ndarray) -> np.ndarray:
    """Apple Log code value (0..1) -> scene-linear reflectance (0.18 = grey)."""
    k = APPLE_LOG
    p = np.asarray(p, np.float32)
    pt = k["c"] * (k["Rt"] - k["R0"]) ** 2
    hi = np.exp2((p - k["delta"]) / k["gamma"]) - k["beta"]
    lo = np.sqrt(np.maximum(p, 0.0) / k["c"]) + k["R0"]
    return np.where(p >= pt, hi, np.where(p > 0, lo, k["R0"])).astype(np.float32)


def _bt709_oetf(lin: np.ndarray) -> np.ndarray:
    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin < 0.018, 4.5 * lin, 1.099 * np.power(lin, 0.45) - 0.099).astype(np.float32)


def apple_log_to_rec709(rgb: np.ndarray) -> np.ndarray:
    """Approximate Apple Log (BT.2020) -> SDR Rec.709 display signal, used only when Cinema 101 is not available:
    decode, gamut matrix, exposure so grey lands near 42 %, a soft highlight roll-off, BT.709 OETF."""
    lin = apple_log_decode(rgb) @ BT2020_TO_BT709.T
    lin = np.maximum(lin, 0.0) * 1.1
    rolled = lin * (1.0 + lin / 64.0) / (1.0 + lin)      # extended Reinhard, white at 8.0
    return _bt709_oetf(rolled * 1.6)


# ----------------------------------------------------------------------------- detection
def c101_path() -> Optional[str]:
    cand = os.environ.get("ZEUS_C101", "").strip() or C101_DEFAULT
    try:
        return cand if Path(cand).is_file() and os.access(cand, os.X_OK) else None
    except OSError:
        return None


_PROFILE_RE = re.compile(r"profile:\s*(?P<name>.+?)\s*\[(?P<id>[\w.\-]+)\]\s*(?:—|-)\s*(?P<reason>.+?)(?:\s*\((?P<conf>[^()]*)\))?\s*$")


def parse_c101_probe(text: str) -> Optional[dict]:
    """The first file in `c101 probe` output -> {profile, name, reason, confidence, log_tag, log}."""
    info: dict = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("tags:"):
            m = re.search(r"· log (.+?) ·", s)
            info["log_tag"] = m.group(1).strip() if m else None
            info["tags"] = s[5:].strip()
        elif s.startswith("profile:"):
            m = _PROFILE_RE.match(s)
            if m:
                info.update(profile=m.group("id"), name=m.group("name"), reason=m.group("reason").strip(),
                            confidence=(m.group("conf") or "").strip())
            break
    if "profile" not in info:
        return None
    pid = info["profile"]
    info["log"] = bool(info.get("log_tag")) or (pid not in NOT_LOG and not pid.startswith("generic."))
    return info


def probe_c101(path, c101: Optional[str] = None, timeout: float = 60.0) -> Optional[dict]:
    c101 = c101 or c101_path()
    if not c101 or not Path(path).is_file():
        return None
    try:
        proc = subprocess.run([c101, "probe", str(path)], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return parse_c101_probe(proc.stdout)


def _ffprobe() -> Optional[str]:
    for cand in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", shutil.which("ffprobe")):
        if cand and Path(cand).is_file():
            return cand
    return None


def probe_tags(path) -> Optional[dict]:
    """Colour tags of the first video stream: {primaries, transfer, matrix, range, pix_fmt, log, profile}."""
    ffprobe = _ffprobe()
    if not ffprobe or not Path(path).is_file():
        return None
    proc = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-print_format", "json",
                           "-show_streams", "-show_format", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return None
    v = streams[0]
    trc = str(v.get("color_transfer") or "")
    texts = [trc] + [str(x) for x in (v.get("tags") or {}).values()] + \
            [str(x) for x in ((data.get("format") or {}).get("tags") or {}).values()]
    apple = any(re.search(r"apple[ ._-]?log", t, re.I) for t in texts)
    log = apple or trc.startswith("log")
    return {"primaries": v.get("color_primaries"), "transfer": v.get("color_transfer"),
            "matrix": v.get("color_space"), "range": v.get("color_range"), "pix_fmt": v.get("pix_fmt"),
            "log": log, "profile": "apple.log" if apple else None}


def detect(paths: Sequence, c101: Optional[str] = None) -> dict:
    """{"log": bool, "profile", "name", "via", "file", "detail"} for the first file that says something."""
    forced = os.environ.get("ZEUS_FX_LOG_PROFILE", "").strip()
    if forced:
        if forced.lower() in ("none", "off", "0"):
            return {"log": False, "profile": None, "via": "forced", "detail": "ZEUS_FX_LOG_PROFILE=none"}
        return {"log": True, "profile": forced, "name": forced, "via": "forced",
                "detail": f"ZEUS_FX_LOG_PROFILE={forced}"}
    files = [Path(p) for p in paths if p and Path(str(p)).is_file()]
    c101 = c101 or c101_path()
    first = None
    if c101:
        for f in files:
            info = probe_c101(f, c101)
            if info is None:
                continue
            first = first or (f, info)
            if info["log"]:
                return {"log": True, "profile": info["profile"], "name": info.get("name"), "via": "c101",
                        "file": str(f), "detail": f"{info.get('reason')} ({info.get('confidence')})"}
    for f in files:
        tags = probe_tags(f)
        if tags and tags["log"]:
            return {"log": True, "profile": tags["profile"], "name": "Apple Log" if tags["profile"] else "log",
                    "via": "tags", "file": str(f), "detail": f"transfer {tags['transfer']}"}
    if first:
        f, info = first
        return {"log": False, "profile": info["profile"], "name": info.get("name"), "via": "c101", "file": str(f),
                "detail": f"{info.get('reason')} ({info.get('confidence')})"}
    return {"log": False, "profile": None, "via": "tags" if files else "none",
            "detail": "no log transfer in the colour tags" if files else "no file to probe"}


# ----------------------------------------------------------------------------- job preparation
def bake_forward(profile: str, out: Path, c101: Optional[str] = None, size: int = LUT_SIZE) -> tuple[Path, str]:
    """Forward Log -> Rec.709 LUT: Cinema 101's bake, else the Apple Log fallback. Returns (path, how)."""
    c101 = c101 or c101_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    if c101:
        proc = subprocess.run([c101, "bake", "--profile", profile, "--out", str(out), "--size", str(size)],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0 and out.is_file():
            return out, "c101 bake"
        if profile != "apple.log":
            raise ColorError(f"Cinema 101 could not bake {profile}: {(proc.stderr or proc.stdout).strip()[-200:]}")
    if profile == "apple.log":
        write_cube(lut_from_function(apple_log_to_rec709, size), out, "Apple Log -> Rec.709 (approximate, Zeus)")
        return out, "Apple Log published curve (approximate; Cinema 101 not available)"
    raise ColorError(f"{profile} footage needs Cinema 101's c101 CLI to convert it (not found).")


def prepare(job_dir, files: Sequence, report=None) -> dict:
    """Detect Log for a job and, when found, write <job>/color/<profile>-to-rec709.cube (forward) and
    rec709-to-<profile>.npz (inverse: shaper + 3-D grid). Returns the record stored in job.json (log False: nothing changes)."""
    job_dir = Path(job_dir)
    info = detect(files)
    record = {k: v for k, v in info.items() if v is not None}
    if not info.get("log"):
        return record
    profile = info.get("profile")
    if not profile:
        record["warning"] = "Log footage of an unknown profile: the model sees it as it is (pick a profile with " \
                            "ZEUS_FX_LOG_PROFILE)."
        return record
    color_dir = job_dir / "color"
    fwd_path, how = bake_forward(profile, color_dir / f"{profile}-to-rec709.cube")
    forward = read_cube(fwd_path)
    if report:
        report("color", 0.5, f"{info.get('name') or profile}: building the inverse LUT")
    inverse, stats = invert_lut(forward, INVERSE_SIZE)
    inv_path = save_inverse(inverse, color_dir / f"rec709-to-{profile}.npz")
    grid_rt = round_trip_error(forward, inverse, lo=0.15, hi=0.85, chroma=0.08)
    exact_rt = round_trip_error(forward, LogInverter(forward, inverse), samples=5000, lo=0.15, hi=0.85, chroma=0.08)
    record.update(forward_lut=str(fwd_path.relative_to(job_dir)), inverse_lut=str(inv_path.relative_to(job_dir)),
                  forward_how=how, inverse=stats,
                  round_trip={"grid": {k: round(v, 5) for k, v in grid_rt.items()},
                              "per_colour": {k: round(v, 5) for k, v in exact_rt.items()}})
    return record
