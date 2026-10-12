"""Hat / cap removal (Make it me, step `hat`): BiSeNet "hat" mask -> VACE inpaint of hair -> hi-res composite.

The recipe that gave the good result on 2026-10-11 (effects/recipes/{boxes,capmask,run_cap_v2}.py, v2.sh), as one
engine with the same numbers:

    1. head crop per frame from the face landmarks (MediaPipe, prepare's face-landmarks.json): the face box widened
       by 0.6 x its width on each side, 1.0 x its height above and 0.2 x below;
    2. BiSeNet face parser (FaceFusion's bisenet_resnet_34.onnx, MIT; CelebAMask-HQ classes, 18 = hat) on the crop
       resized to 512 x 512 (ImageNet mean/std), class 18 -> a soft hat map put back at the crop's place;
    3. temporal EMA 0.6 (new) / 0.4 (old), threshold 0.4, dilate with a 17 x 17 ellipse -> mask.mp4
       (white = regenerate); control.mp4 = the frame with grey 127 inside the mask (VACE sees it as is);
    4. skip rule: when the mask covers on average < 0.1 % of the frame area, there is no hat: nothing to do;
    5. VACE (Wan2.1-VACE-1.3B, zfx/engines/vace.py) with ZEUS_VACE_CONTROL=as-is, 33 frames per window, 20 steps,
       360p, the frontal reference frame, a hair prompt and the negative prompt extended with
       ", hat, cap, baseball cap, beanie, headband, fabric, cloth on the head, plaid, checkered pattern, blue patch";
    6. zfx.generate.composite_final: the generated crops back into the full-resolution frames with the audio.

The ONNX parser needs onnxruntime, which only FaceFusion's venv has: it runs there as a subprocess
(`<facefusion>/.venv/bin/python -m zfx.engines.hat parse <task.json>`, importing nothing heavy from zfx) and hands
back a soft hat map per frame (uint8, work size) in a .npy; the EMA/threshold/dilate and VACE run in the main venv.

Layout inside the job: <job>/hat/{source.mp4 (the video to fix), work.mp4, hat-soft.npy, mask.mp4, control.mp4,
job.json (mode mesh), result-raw.mp4, result-crops/, final.mp4, hat.log}. Nothing the review hashed is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from . import EngineError

NAME = "hat"
LABEL = "Hat removal (BiSeNet mask + VACE hair, local)"
PARSER_FILE = "bisenet_resnet_34.onnx"      # FaceFusion's face parser (yakhyo, MIT), ~1.7 GB models set
HAT_CLASS = 18                               # CelebAMask-HQ: 17 hair, 18 hat
PARSER_SIZE = 512
PARSER_MEAN = (0.485, 0.456, 0.406)
PARSER_STD = (0.229, 0.224, 0.225)
CROP_SIDES = 0.6                             # x face width, each side
CROP_ABOVE = 1.0                             # x face height, above
CROP_BELOW = 0.2                             # x face height, below
EMA_NEW = 0.6                                # ema = 0.6 * new + 0.4 * old
THRESHOLD = 0.4
DILATE = 17                                  # 17 x 17 ellipse
CONTROL_GREY = 127
MIN_COVER = 0.001                            # skip when the mask covers < 0.1 % of the frame area on average
NEGATIVE_EXTRA = (", hat, cap, baseball cap, beanie, headband, fabric, cloth on the head, plaid, checkered pattern, "
                  "blue patch")
DEFAULT_HAIR = "natural hair like the reference"
PROMPT_TAIL = ", no hat, no cap, natural hairline and fringe, real hair texture, same soft studio light, photorealistic"
VACE_FRAMES = 33
VACE_STEPS = 20
VACE_SIZE = "360p"
DEFAULT_SEED = 20261011
HAT_DIR = "hat"
PARSE_TIMEOUT_S = 1800

Progress = Callable[[str, float, str], None]


# ----------------------------------------------------------------------------- paths / status
def parser_model() -> Path:
    from .faceswap import facefusion_dir
    env = os.environ.get("ZEUS_HAT_PARSER", "").strip()
    return Path(env).expanduser() if env else facefusion_dir() / ".assets" / "models" / PARSER_FILE


def parser_python() -> Path:
    from .faceswap import facefusion_python
    env = os.environ.get("ZEUS_HAT_PYTHON", "").strip()
    return Path(env).expanduser() if env else facefusion_python()


def status() -> dict:
    """Readiness without importing anything heavy: FaceFusion's venv + parser model, VACE weights."""
    from . import vace
    py, model = parser_python(), parser_model()
    if not py.is_file():
        return {"ready": False, "message": "FaceFusion's Python is missing (the hat parser runs there): "
                                           "run effects/setup-engines.sh --faceswap"}
    if not model.is_file():
        return {"ready": False, "message": f"The face parser {PARSER_FILE} is missing in FaceFusion's models "
                                           "(it downloads on FaceFusion's first run)."}
    v = vace.status()
    if not v.get("ready"):
        return {"ready": False, "message": "Hat removal needs the full replace engine: " + str(v.get("message"))}
    return {"ready": True, "message": "ready", "parser": str(model), "python": str(py)}


# ----------------------------------------------------------------------------- geometry
def face_boxes(landmarks: dict) -> list:
    """Pixel face boxes [x0, y0, x1, y1] (or None) per frame from prepare's face-landmarks.json (normalized points)."""
    w, h = float(landmarks.get("width") or 0), float(landmarks.get("height") or 0)
    boxes = []
    for frame in landmarks.get("frames") or []:
        if not frame:
            boxes.append(None)
            continue
        pts = np.asarray(frame, dtype=np.float32)[:, :2]
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        boxes.append([float(x0 * w), float(y0 * h), float(x1 * w), float(y1 * h)])
    return boxes


def head_crop(box: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    """The parser's crop around a face box: widened 0.6 x sides, 1.0 x above, 0.2 x below, inside the frame."""
    x0, y0, x1, y1 = box
    fw, fh = x1 - x0, y1 - y0
    bx0 = int(max(0, x0 - CROP_SIDES * fw))
    bx1 = int(min(width, x1 + CROP_SIDES * fw))
    by0 = int(max(0, y0 - CROP_ABOVE * fh))
    by1 = int(min(height, y1 + CROP_BELOW * fh))
    return bx0, by0, bx1, by1


def scale_boxes(boxes: list, from_size: tuple, to_size: tuple) -> list:
    """Boxes measured on one frame size, expressed on another (the hat work video may differ by a pixel)."""
    fw, fh = from_size
    tw, th = to_size
    if (fw, fh) == (tw, th) or not fw or not fh:
        return boxes
    sx, sy = tw / float(fw), th / float(fh)
    return [[b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy] if b else None for b in boxes]


# ----------------------------------------------------------------------------- mask post-processing (main venv)
def postprocess(soft_frames, threshold: float = THRESHOLD, alpha: float = EMA_NEW, dilate: int = DILATE):
    """Soft hat maps (uint8 0..255 or float 0..1) -> binary uint8 masks (0/255): EMA, threshold, dilate."""
    import cv2
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate, dilate)) if dilate > 1 else None
    ema: Optional[np.ndarray] = None
    for soft in soft_frames:
        m = np.asarray(soft)
        m = m.astype(np.float32) / 255.0 if m.dtype == np.uint8 else m.astype(np.float32)
        ema = m if ema is None else alpha * m + (1.0 - alpha) * ema
        binary = (ema > threshold).astype(np.uint8) * 255
        if kernel is not None:
            binary = cv2.dilate(binary, kernel)
        yield binary


def coverage_stats(masks) -> dict:
    """{"frames", "hat_frames", "mean_cover", "max_cover"} from binary masks (0/255)."""
    covers = [float((np.asarray(m) > 0).mean()) for m in masks]
    return {"frames": len(covers), "hat_frames": int(sum(c > 0.001 for c in covers)),
            "mean_cover": round(float(np.mean(covers)) if covers else 0.0, 5),
            "max_cover": round(max(covers) if covers else 0.0, 5)}


def hat_found(stats: dict, min_cover: float = MIN_COVER) -> tuple[bool, str]:
    """The skip rule: (found, plain-English message)."""
    cover = float(stats.get("mean_cover") or 0.0)
    frames, hat_frames = int(stats.get("frames") or 0), int(stats.get("hat_frames") or 0)
    if cover < min_cover:
        return False, (f"No hat or cap found: the hat mask covers {cover * 100:.3f} % of the frame on average "
                       f"(under {min_cover * 100:g} %), in {hat_frames} of {frames} frames. Hat step skipped.")
    return True, (f"Hat found: the mask covers {cover * 100:.2f} % of the frame on average, "
                  f"in {hat_frames} of {frames} frames.")


# ----------------------------------------------------------------------------- the parser worker (FaceFusion venv)
def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def parse_worker(task: dict) -> dict:
    """Runs in FaceFusion's venv: work video + boxes -> <out>.npy with one uint8 soft hat map per frame."""
    import cv2
    import onnxruntime as ort

    work, model, out = task["work"], task["model"], task["out"]
    boxes = task.get("boxes") or []
    providers = task.get("providers") or ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    available = ort.get_available_providers()
    providers = [p for p in providers if p in available] or ["CPUExecutionProvider"]
    sess = ort.InferenceSession(model, providers=providers)
    inp = sess.get_inputs()[0].name
    cap = cv2.VideoCapture(work)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open {work}")
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or len(boxes)
    mean = np.array(PARSER_MEAN, dtype=np.float32)
    std = np.array(PARSER_STD, dtype=np.float32)
    maps: list = []
    i = 0
    t0 = time.monotonic()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        soft = np.zeros((h, w), np.uint8)
        box = boxes[i] if i < len(boxes) else None
        if box:
            bx0, by0, bx1, by1 = head_crop(box, w, h)
            if bx1 - bx0 >= 8 and by1 - by0 >= 8:
                crop = frame[by0:by1, bx0:bx1]
                x = cv2.resize(crop, (PARSER_SIZE, PARSER_SIZE))[:, :, ::-1].astype(np.float32) / 255.0
                x = (x - mean) / std
                y = sess.run(None, {inp: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]
                cls = y.argmax(0)
                hat = cv2.resize((cls == HAT_CLASS).astype(np.float32), (bx1 - bx0, by1 - by0),
                                 interpolation=cv2.INTER_LINEAR)
                soft[by0:by1, bx0:bx1] = np.clip(hat * 255.0 + 0.5, 0, 255).astype(np.uint8)
        maps.append(soft)
        i += 1
        if i % 8 == 0 or i == total:
            _emit({"progress": min(1.0, i / max(1, total)), "message": f"face parser: frame {i}/{total}"})
    cap.release()
    arr = np.stack(maps) if maps else np.zeros((0, h, w), np.uint8)
    np.save(out, arr)
    return {"frames": int(arr.shape[0]), "width": w, "height": h, "providers": providers,
            "boxes": int(sum(1 for b in boxes[: len(maps)] if b)), "seconds": round(time.monotonic() - t0, 1)}


def run_parser(work: Path, boxes: list, out_npy: Path, log_path: Path, report: Progress, lo: float = 0.0,
               hi: float = 1.0) -> dict:
    """Start the parser worker in FaceFusion's venv and relay its progress into [lo, hi] of stage 'hat'."""
    py, model = parser_python(), parser_model()
    if not py.is_file() or not model.is_file():
        raise EngineError(status()["message"])
    effects = Path(__file__).resolve().parents[2]
    task_file = out_npy.with_suffix(".task.json")
    task_file.write_text(json.dumps({"work": str(work), "model": str(model), "out": str(out_npy), "boxes": boxes}))
    env = dict(os.environ)
    env.update(PYTHONPATH=str(effects), PYTHONUNBUFFERED="1", PYTHONWARNINGS="ignore")
    env.pop("VIRTUAL_ENV", None)
    result, error = None, None
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n== hat parse {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log.flush()
        proc = subprocess.Popen([str(py), "-m", "zfx.engines.hat", "parse", str(task_file)], cwd=str(effects),
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
                    report("hat", lo + (hi - lo) * float(obj.get("progress") or 0), obj.get("message", ""))
                if time.monotonic() - t0 > PARSE_TIMEOUT_S:
                    proc.kill()
                    error = f"the face parser took longer than {PARSE_TIMEOUT_S} s"
                    break
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            task_file.unlink(missing_ok=True)
    if error or result is None or not out_npy.is_file():
        raise EngineError((error or f"the face parser stopped without a result (exit {proc.returncode})")
                          + f". Details: {log_path}")
    return result


# ----------------------------------------------------------------------------- detection (mask + control videos)
def detect(hat_dir: Path, work: Path, boxes: list, report: Progress, lo: float = 0.0, hi: float = 1.0) -> dict:
    """work.mp4 + face boxes -> hat/mask.mp4 + hat/control.mp4 (+ hat-soft.npy). Returns the coverage stats."""
    from .. import io as zio
    hat_dir.mkdir(parents=True, exist_ok=True)
    soft_path = hat_dir / "hat-soft.npy"
    log_path = hat_dir / "hat.log"
    span = hi - lo
    report("hat", lo, "looking for a hat or cap (BiSeNet face parser)")
    parsed = run_parser(work, boxes, soft_path, log_path, report, lo, lo + span * 0.8)
    soft = np.load(soft_path, mmap_mode="r")
    info = zio.probe(work)
    w, h = info["width"], info["height"]
    masks = list(postprocess(soft))
    stats = coverage_stats(masks)
    stats.update(parser=parsed, ema=[EMA_NEW, round(1.0 - EMA_NEW, 2)], threshold=THRESHOLD, dilate=DILATE,
                 hat_class=HAT_CLASS, crop=[CROP_SIDES, CROP_ABOVE, CROP_BELOW])
    report("hat", lo + span * 0.85, "writing the hat mask and the control video")
    with zio.VideoWriter(hat_dir / "mask.mp4", w, h, zio.WORK_FPS, crf=12) as mw, \
            zio.VideoWriter(hat_dir / "control.mp4", w, h, zio.WORK_FPS, crf=12) as cw:
        for i, frame in enumerate(zio.iter_frames(work)):
            m = masks[i] if i < len(masks) else np.zeros((h, w), np.uint8)
            mw.write(m)
            ctrl = frame.copy()
            ctrl[m > 0] = CONTROL_GREY
            cw.write(ctrl)
    found, message = hat_found(stats)
    stats["found"] = found
    stats["message"] = message
    report("hat", hi, message)
    return stats


# ----------------------------------------------------------------------------- the whole step
def hair_prompt(hair: Optional[str]) -> str:
    text = (hair or "").strip().rstrip(".,;")
    return (text or DEFAULT_HAIR) + PROMPT_TAIL


def run(job_dir, refs: Sequence, out_path, progress: Optional[Progress] = None, *, base: Optional[Path] = None,
        landmarks: Optional[Path] = None, audio: Optional[Path] = None, hair: Optional[str] = None,
        seed: Optional[int] = None, frames: Optional[int] = None, steps: Optional[int] = None,
        size: Optional[str] = None, engine_label: str = "vace hair", **_ignored) -> dict:
    """Remove the hat from `base` (default <job>/final.mp4) -> out_path (default <job>/hat/final.mp4, with audio).

    refs: the reference frames (the first, frontal one is what VACE gets). Returns a record with "skipped": True and
    the reason when no hat is found; raises EngineError on failure."""
    from .. import generate
    from .. import io as zio
    from . import vace

    report: Progress = progress or (lambda *a, **k: None)
    job_dir = Path(job_dir)
    hat_dir = job_dir / HAT_DIR
    hat_dir.mkdir(parents=True, exist_ok=True)
    base = Path(base) if base else job_dir / "final.mp4"
    if not base.is_file():
        raise EngineError(f"{base.name} is missing: the face swap must run before the hat step.")
    landmarks = Path(landmarks) if landmarks else job_dir / "face-landmarks.json"
    if not landmarks.is_file():
        raise EngineError("face-landmarks.json is missing: prepare the clip in a mesh mode (depth_mesh) first.")
    audio = Path(audio) if audio else (job_dir / "audio.m4a" if (job_dir / "audio.m4a").is_file() else None)
    out_path = Path(out_path) if out_path else hat_dir / "final.mp4"
    refs = [Path(r) for r in refs if r]
    if not refs:
        raise EngineError("Hat removal needs a reference frame (the frontal one).")
    st = status()
    if not st["ready"]:
        raise EngineError(st["message"])
    timings: dict = {}
    t_all = time.monotonic()

    # 1) the mini job: source = the video to fix, work.mp4 at 16 fps, job.json in mode mesh (control used as is)
    t0 = time.monotonic()
    report("hat", 0.0, "preparing the hat job (work video at 16 fps)")
    src = hat_dir / "source.mp4"
    if not src.is_file() or src.stat().st_size != base.stat().st_size:
        shutil.copyfile(base, src)
    work_info = zio.make_work(src, hat_dir / "work.mp4")
    lm = json.loads(landmarks.read_text(encoding="utf-8"))
    boxes = scale_boxes(face_boxes(lm), (lm.get("width") or work_info["width"], lm.get("height") or work_info["height"]),
                        (work_info["width"], work_info["height"]))
    (hat_dir / "job.json").write_text(json.dumps({
        "id": f"{job_dir.name}/hat", "mode": "mesh", "parent": str(job_dir), "base": base.name,
        "note": "Make it me hat step: mask = BiSeNet hat class, control = grey inside the mask (used as is)",
        "artifacts": {"source": "source.mp4", "work": "work.mp4", "mask": "mask.mp4", "control": "control.mp4"}},
        indent=2))
    timings["work"] = round(time.monotonic() - t0, 1)

    # 2) detection
    t0 = time.monotonic()
    stats = detect(hat_dir, hat_dir / "work.mp4", boxes, report, 0.01, 0.08)
    timings["detect"] = round(time.monotonic() - t0, 1)
    record = {"engine": NAME, "base": base.name, "work": work_info, "faces": int(sum(1 for b in boxes if b)),
              "mask": stats, "timings": timings}
    if not stats["found"]:
        timings["total"] = round(time.monotonic() - t_all, 1)
        record.update(skipped=True, reason=stats["message"], seconds=timings["total"])
        return record

    # 3) VACE inpaints hair where the hat was (control as is, the recipe's negative prompt)
    t0 = time.monotonic()
    prompt = hair_prompt(hair)
    seed = int(seed) if seed is not None else DEFAULT_SEED
    old_env = os.environ.get("ZEUS_VACE_CONTROL")
    old_negative = vace.NEGATIVE_PROMPT
    os.environ["ZEUS_VACE_CONTROL"] = "as-is"
    vace.NEGATIVE_PROMPT = old_negative + NEGATIVE_EXTRA
    raw = hat_dir / "result-raw.mp4"
    try:
        report("hat", 0.08, "VACE: drawing hair where the hat was")
        vinfo = vace.run(hat_dir, [refs[0]], raw, lambda s, p, m="": report("hat", 0.08 + 0.87 * float(p or 0), m),
                         prompt=prompt, frames=int(frames or VACE_FRAMES), steps=int(steps or VACE_STEPS),
                         size=size or VACE_SIZE, seed=seed, control=hat_dir / "control.mp4", mask=hat_dir / "mask.mp4")
    finally:
        vace.NEGATIVE_PROMPT = old_negative
        if old_env is None:
            os.environ.pop("ZEUS_VACE_CONTROL", None)
        else:
            os.environ["ZEUS_VACE_CONTROL"] = old_env
    timings["vace"] = round(time.monotonic() - t0, 1)

    # 4) back into the full-resolution frames, with the audio
    t0 = time.monotonic()
    report("hat", 0.95, "compositing the new hair into the full-resolution frames")
    crops = hat_dir / vace.CROPS_MANIFEST
    try:
        if vinfo.get("crop") and crops.is_file():
            mux = generate.composite_final(hat_dir, crops, src, hat_dir / "mask.mp4", audio, out_path, engine_label,
                                           "fast", None, lambda s, p, m="": report("hat", 0.95 + 0.04 * float(p or 0), m))
        else:
            mux = generate.mux_final(raw, src, audio, out_path, "vace", mask=hat_dir / "mask.mp4")
    except generate.GenerateError as exc:
        raise EngineError(str(exc)) from exc
    timings["composite"] = round(time.monotonic() - t0, 1)
    timings["total"] = round(time.monotonic() - t_all, 1)
    report("hat", 1.0, f"hat removed in {timings['total']:.0f} s")
    record.update(skipped=False, prompt=prompt, negative_extra=NEGATIVE_EXTRA.strip(", "), seed=seed,
                  control="as-is", reference=str(refs[0]), vace={k: vinfo.get(k) for k in
                  ("seconds", "timings", "width", "height", "frames", "windows", "steps", "seed", "device", "dtype")},
                  mux=mux, final=str(out_path), seconds=timings["total"])
    return record


# ----------------------------------------------------------------------------- `python -m zfx.engines.hat parse`
def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m zfx.engines.hat")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="BiSeNet hat maps for a work video (runs in FaceFusion's venv)")
    p.add_argument("task", help="task.json: {work, model, out, boxes}")
    args = ap.parse_args(argv)
    try:
        task = json.loads(Path(args.task).read_text(encoding="utf-8"))
        _emit({"result": parse_worker(task)})
        return 0
    except Exception as exc:  # noqa: BLE001 - one JSON error line for the parent
        _emit({"error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
