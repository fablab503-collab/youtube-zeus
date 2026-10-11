"""Face packs (`.c101face`, v1): Cinema 101's "Scan my face" -> face-swap references.

Contract: effects/FACE-PACK.md. A pack is a folder (a document package on iOS):

    <Name> <yyyy-MM-dd HH-mm>.c101face/
      manifest.json         {"format": "c101face", "version": 1, "consent": true, "frames": [...], ...}
      frames/<bin>.jpg      upright RGB stills
      depth/<bin>.png       optional
      mesh.obj              optional here (not used yet)
      blendshapes.json      optional here (not used yet)

    load_pack(path) -> FacePack
        Validates: manifest.json is a JSON object, format "c101face", version 1, consent exactly true, and every
        path the manifest names (frames[].file, frames[].depth, mesh, blendshapes) stays inside the pack folder
        (no absolute paths, no "..", no symlink leading out): any of these failing refuses the whole pack.
        A frame whose file is missing or is not a photo is skipped (listed in FacePack.skipped).
        Unknown fields are ignored: the iPhone side may add more.

    pick_refs(pack, max_refs=5, min_sharpness=0.35) -> [pick dict]
        Up to max_refs frames, in this priority order:
          1. the sharpest frontal neutral frame (|yaw| < 8, |pitch| < 10),
          2. the sharpest neutral frame near +20 deg yaw, then near -20,
          3. the sharpest near +40, then near -40,
          4. the sharpest smile.
        Frames whose sharpness is under min_sharpness x the sharpest frame of the pack are skipped. Ties go to the
        frame closest to the slot's angle, then to the file name, so the same pack always gives the same picks.
        Each pick: {"path" (absolute), "file", "bin", "slot", "why", "yaw", "pitch", "expression", "sharpness",
        "relative_sharpness"}.

    describe(pack, picks) -> dict   what `zeus_effects.py facepack --json` prints and what a generation records.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Optional

FORMAT = "c101face"
EXTENSION = ".c101face"
SUPPORTED_VERSIONS = (1,)
MAX_REFS = 5
MIN_SHARPNESS = 0.35          # skip frames under 35 % of the pack's sharpest frame
MANIFEST_MAX_BYTES = 8 << 20
MAX_FRAMES = 2000
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}

FRONTAL_YAW = 8.0
FRONTAL_PITCH = 10.0
FRONTAL_FALLBACK = 15.0       # no frame inside the frontal window: the most frontal one within 15 deg
SIDE_PITCH = 15.0             # side slots want a level head
NEUTRAL = {"", "neutral", "none", "rest"}
SMILE = {"smile", "smiling", "happy", "grin"}


class FacePackError(ValueError):
    """Plain-English problem with a face pack (refused, unreadable, or nothing usable)."""


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    target: Optional[float] = None    # yaw
    tolerance: float = 0.0


SLOTS = (
    Slot("frontal", "frontal neutral"),
    Slot("yaw+20", "near +20° yaw", 20.0, 10.0),
    Slot("yaw-20", "near −20° yaw", -20.0, 10.0),
    Slot("yaw+40", "near +40° yaw", 40.0, 13.0),
    Slot("yaw-40", "near −40° yaw", -40.0, 13.0),
    Slot("smile", "smile"),
)


@dataclass
class PackFrame:
    index: int                      # position in manifest.frames
    file: str                       # as written in the manifest (relative to the pack)
    path: Path                      # absolute, resolved, inside the pack
    bin: str = ""
    yaw: Optional[float] = None
    pitch: Optional[float] = None
    roll: Optional[float] = None
    expression: str = ""
    sharpness: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None

    @property
    def neutral(self) -> bool:
        return self.expression in NEUTRAL

    @property
    def smile(self) -> bool:
        return self.expression in SMILE or self.expression.startswith("smil")


@dataclass
class FacePack:
    dir: Path
    manifest: dict
    frames: list[PackFrame] = field(default_factory=list)     # files present inside the pack
    skipped: list[dict] = field(default_factory=list)         # {"file", "reason"}
    total: int = 0                                            # frames listed in the manifest

    @property
    def name(self) -> str:
        n = self.dir.name
        return n[: -len(EXTENSION)] if n.lower().endswith(EXTENSION) else n

    @property
    def version(self) -> Any:
        return self.manifest.get("version")

    def text(self, key: str) -> Optional[str]:
        v = self.manifest.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None


# ----------------------------------------------------------------------------- small helpers
def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _int(value: Any) -> Optional[int]:
    f = _number(value)
    return int(f) if f is not None and f >= 0 else None


def _signed(value: Optional[float]) -> str:
    if value is None:
        return "?"
    return f"{'+' if value >= 0 else '−'}{abs(value):.0f}°"


def inside(root: Path, rel: Any, what: str) -> Path:
    """`rel` resolved under `root` (already resolved); FacePackError if it is not a plain relative path that stays
    inside the pack, symlinks included."""
    if not isinstance(rel, str) or not rel.strip():
        raise FacePackError(f"The face pack's manifest has an empty or invalid path for {what}.")
    if "\x00" in rel or rel.startswith("~"):
        raise FacePackError(f"Refused: the face pack's {what} path “{rel}” is not a path inside the pack.")
    parts = PurePosixPath(rel).parts
    if PurePosixPath(rel).is_absolute() or ".." in parts:
        raise FacePackError(f"Refused: the face pack's {what} path “{rel}” points outside the pack folder.")
    full = (root / rel).resolve()
    if full != root and not full.is_relative_to(root):
        raise FacePackError(f"Refused: the face pack's {what} “{rel}” leads outside the pack folder (a link).")
    return full


def pack_dir(path) -> Path:
    """The pack folder from what the user gave: the folder itself, or its manifest.json."""
    p = Path(str(path)).expanduser()
    if p.name == "manifest.json" and p.is_file():
        p = p.parent
    if not p.exists():
        raise FacePackError(f"Face pack not found: {p}")
    if not p.is_dir():
        raise FacePackError(f"{p.name} is not a face pack: choose the .c101face folder from Cinema 101 › Scan my face.")
    return p.resolve()


# ----------------------------------------------------------------------------- load + validate
def load_pack(path) -> FacePack:
    root = pack_dir(path)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FacePackError(f"{root.name} has no manifest.json: it is not a Cinema 101 face pack.")
    if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
        raise FacePackError(f"The manifest of {root.name} is too large to be a face pack.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise FacePackError(f"The manifest of {root.name} could not be read: {exc}") from exc
    if not isinstance(manifest, dict):
        raise FacePackError(f"The manifest of {root.name} is not a JSON object.")
    if manifest.get("format") != FORMAT:
        raise FacePackError(f"{root.name} is not a Cinema 101 face pack (format is not “{FORMAT}”).")
    version = manifest.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in SUPPORTED_VERSIONS:
        raise FacePackError(f"{root.name} is face pack version {version!r}; this copy of YouTube Zeus reads version "
                            + ", ".join(map(str, SUPPORTED_VERSIONS)) + ". Update YouTube Zeus.")
    if manifest.get("consent") is not True:
        raise FacePackError(f"Refused: {root.name} was not saved with consent (its manifest's “consent” is not true). "
                            "Cinema 101 asks “This scan is of me (or of someone who agreed)” before saving a scan.")

    # every path the manifest names must stay inside the pack (checked even for files not used yet)
    for key in ("mesh", "blendshapes"):
        if manifest.get(key) not in (None, ""):
            inside(root, manifest.get(key), key)

    raw_frames = manifest.get("frames")
    if not isinstance(raw_frames, list):
        raise FacePackError(f"The manifest of {root.name} has no list of frames.")
    pack = FacePack(dir=root, manifest=manifest, total=len(raw_frames))
    seen: set[Path] = set()
    for i, item in enumerate(raw_frames[:MAX_FRAMES]):
        if not isinstance(item, dict):
            pack.skipped.append({"file": None, "reason": f"frame {i + 1} is not an object"})
            continue
        rel = item.get("file")
        if not isinstance(rel, str) or not rel.strip():
            pack.skipped.append({"file": None, "reason": f"frame {i + 1} names no file"})
            continue
        full = inside(root, rel, f"frame {i + 1}")
        if item.get("depth") not in (None, ""):
            inside(root, item.get("depth"), f"frame {i + 1} depth")
        if full.suffix.lower() not in IMAGE_EXTENSIONS:
            pack.skipped.append({"file": rel, "reason": "not a photo"})
            continue
        if not full.is_file():
            pack.skipped.append({"file": rel, "reason": "missing"})
            continue
        if full in seen:
            pack.skipped.append({"file": rel, "reason": "listed twice"})
            continue
        seen.add(full)
        expression = item.get("expression")
        sharp = _number(item.get("sharpness"))
        pack.frames.append(PackFrame(
            index=i, file=str(rel), path=full,
            bin=str(item.get("bin") or PurePosixPath(str(rel)).stem),
            yaw=_number(item.get("yaw")), pitch=_number(item.get("pitch")), roll=_number(item.get("roll")),
            expression=expression.strip().lower() if isinstance(expression, str) else "",
            sharpness=sharp if sharp is not None and sharp >= 0 else None,
            width=_int(item.get("width")), height=_int(item.get("height")),
        ))
    if len(raw_frames) > MAX_FRAMES:
        pack.skipped.append({"file": None, "reason": f"{len(raw_frames) - MAX_FRAMES} frames over the {MAX_FRAMES} limit"})
    return pack


# ----------------------------------------------------------------------------- picking
def sharpness_floor(pack: FacePack, min_sharpness: float = MIN_SHARPNESS) -> Optional[float]:
    """Absolute floor (min_sharpness x the sharpest frame), or None when the pack has no sharpness values."""
    best = max((f.sharpness for f in pack.frames if f.sharpness is not None), default=None)
    if not best or best <= 0:
        return None
    return best * max(0.0, float(min_sharpness))


def sharp_enough(frame: PackFrame, floor: Optional[float]) -> bool:
    if floor is None:
        return True
    return frame.sharpness is not None and frame.sharpness >= floor


def _matches(slot: Slot, f: PackFrame) -> bool:
    if slot.key == "smile":
        return f.smile
    if not f.neutral or f.yaw is None:
        return False
    if slot.key == "frontal":
        return abs(f.yaw) < FRONTAL_YAW and (f.pitch is None or abs(f.pitch) < FRONTAL_PITCH)
    return abs(f.yaw - slot.target) <= slot.tolerance and (f.pitch is None or abs(f.pitch) <= SIDE_PITCH)


def _distance(slot: Slot, f: PackFrame) -> float:
    if slot.target is not None:
        return abs((f.yaw or 0.0) - slot.target)
    return math.hypot(f.yaw or 0.0, f.pitch or 0.0)


def _pick(slot: Slot, f: PackFrame, why: str, best: Optional[float]) -> dict:
    rel = round(f.sharpness / best, 3) if best and f.sharpness is not None else None
    return {"path": str(f.path), "file": f.file, "bin": f.bin, "slot": slot.key, "why": why,
            "yaw": f.yaw, "pitch": f.pitch, "expression": f.expression or None, "sharpness": f.sharpness,
            "relative_sharpness": rel}


def _why(slot: Slot, f: PackFrame, best: Optional[float], prefix: str = "sharpest") -> str:
    bits = [f"yaw {_signed(f.yaw)}"] if f.yaw is not None else []
    if f.pitch is not None:
        bits.append(f"pitch {_signed(f.pitch)}")
    if f.sharpness is not None:
        share = f", {f.sharpness / best:.0%} of the sharpest" if best else ""
        bits.append(f"sharpness {f.sharpness:g}{share}")
    return f"{prefix} {slot.label} frame ({', '.join(bits)})" if bits else f"{prefix} {slot.label} frame"


def pick_refs(pack, max_refs: int = MAX_REFS, min_sharpness: float = MIN_SHARPNESS) -> list[dict]:
    """Up to max_refs reference frames (see the module docstring for the order). Accepts a FacePack or a path."""
    if not isinstance(pack, FacePack):
        pack = load_pack(pack)
    max_refs = max(0, int(max_refs))
    floor = sharpness_floor(pack, min_sharpness)
    best = max((f.sharpness for f in pack.frames if f.sharpness is not None), default=None)
    pool = [f for f in pack.frames if sharp_enough(f, floor)]
    picks: list[dict] = []
    used: set[int] = set()

    def order(slot: Slot):
        return lambda f: (-(f.sharpness or 0.0), _distance(slot, f), f.file)

    for slot in SLOTS:
        if len(picks) >= max_refs:
            break
        cands = sorted((f for f in pool if f.index not in used and _matches(slot, f)), key=order(slot))
        if cands:
            f = cands[0]
            picks.append(_pick(slot, f, _why(slot, f, best), best))
            used.add(f.index)
        elif slot.key == "frontal":
            # nothing inside the frontal window: the most frontal neutral frame within FRONTAL_FALLBACK degrees
            near = [f for f in pool if f.index not in used and f.neutral and f.yaw is not None
                    and abs(f.yaw) <= FRONTAL_FALLBACK and abs(f.pitch or 0.0) <= FRONTAL_FALLBACK]
            if near:
                f = sorted(near, key=lambda f: (_distance(slot, f), -(f.sharpness or 0.0), f.file))[0]
                picks.append(_pick(slot, f, _why(slot, f, best, "closest to") + f": none within ±{FRONTAL_YAW:g}° yaw "
                                   f"and ±{FRONTAL_PITCH:g}° pitch", best))
                used.add(f.index)
    if not picks and max_refs and pool:
        # a pack without angles: the sharpest neutral frame (or the sharpest frame at all)
        rest = [f for f in pool if f.neutral] or pool
        f = sorted(rest, key=lambda f: (-(f.sharpness or 0.0), f.file))[0]
        slot = Slot("sharpest", "available")
        picks.append(_pick(slot, f, _why(slot, f, best) + ": the pack gives no usable angles", best))
    return picks


# ----------------------------------------------------------------------------- reports
def manifest_sha256(pack: FacePack) -> str:
    return hashlib.sha256((pack.dir / "manifest.json").read_bytes()).hexdigest()


def describe(pack: FacePack, picks: list[dict], min_sharpness: float = MIN_SHARPNESS) -> dict:
    floor = sharpness_floor(pack, min_sharpness)
    usable = [f for f in pack.frames if sharp_enough(f, floor)]
    best = max((f.sharpness for f in pack.frames if f.sharpness is not None), default=None)
    return {
        "pack": str(pack.dir),
        "name": pack.name,
        "format": FORMAT,
        "version": pack.version,
        "person": pack.text("person"),
        "created": pack.text("created"),
        "device": pack.text("device"),
        "app": pack.text("app"),
        "consent": True,
        "frames": pack.total,
        "present": len(pack.frames),
        "usable": len(usable),
        "blurry": len(pack.frames) - len(usable),
        "best_sharpness": best,
        "min_sharpness": min_sharpness,
        "sharpness_floor": round(floor, 3) if floor is not None else None,
        "skipped": pack.skipped,
        "picked": picks,
        "manifest_sha256": manifest_sha256(pack),
    }
