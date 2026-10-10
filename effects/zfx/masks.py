"""mask.mp4: people masks (white = person) with Apple Vision, on this Mac only.

VNGeneratePersonSegmentationRequest (qualityLevel accurate) through pyobjc, per frame of
work.mp4, through one VNSequenceRequestHandler (Vision keeps temporal state), then
temporal smoothing (EMA 0.6 on the soft mask), threshold 0.5, dilation 8 px.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

import numpy as np

from . import WORK_FPS
from .io import VideoWriter, iter_frames, probe
from .progress import get_logger

log = get_logger()

DILATE_PX = 8
EMA_ALPHA = 0.6     # weight of the current frame
THRESHOLD = 0.5


class MaskError(RuntimeError):
    pass


def vision_available() -> tuple[bool, str]:
    try:
        import Quartz  # noqa: F401
        import Vision
        Vision.VNGeneratePersonSegmentationRequest  # noqa: B018
        return True, "ok"
    except Exception as exc:  # pragma: no cover - depends on the Mac
        return False, f"{type(exc).__name__}: {exc}"


class PersonSegmenter:
    """Apple Vision person segmentation; segment(rgb uint8 HxWx3) -> float32 HxW in [0, 1]."""

    def __init__(self, quality: str = "accurate"):
        ok, why = vision_available()
        if not ok:
            raise MaskError(f"Apple Vision is not available through pyobjc ({why}). Run: bash effects/setup.sh")
        import Quartz
        import Vision
        self.Q, self.V = Quartz, Vision
        levels = {
            "accurate": Vision.VNGeneratePersonSegmentationRequestQualityLevelAccurate,
            "balanced": Vision.VNGeneratePersonSegmentationRequestQualityLevelBalanced,
            "fast": Vision.VNGeneratePersonSegmentationRequestQualityLevelFast,
        }
        # VNStatefulRequest: plain -init is NS_UNAVAILABLE in recent SDKs.
        self.request = Vision.VNGeneratePersonSegmentationRequest.alloc().initWithCompletionHandler_(None)
        self.request.setQualityLevel_(levels[quality])
        self.request.setOutputPixelFormat_(Quartz.kCVPixelFormatType_OneComponent8)
        self.handler = Vision.VNSequenceRequestHandler.alloc().init()
        self.colorspace = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)

    def _cgimage(self, rgb: np.ndarray):
        from Foundation import NSData
        Q = self.Q
        h, w = rgb.shape[:2]
        rgbx = np.empty((h, w, 4), dtype=np.uint8)
        rgbx[:, :, :3] = rgb
        rgbx[:, :, 3] = 255
        data = NSData.dataWithBytes_length_(rgbx.tobytes(), rgbx.nbytes)
        provider = Q.CGDataProviderCreateWithCFData(data)
        return Q.CGImageCreate(w, h, 8, 32, w * 4, self.colorspace, Q.kCGImageAlphaNoneSkipLast,
                               provider, None, False, Q.kCGRenderingIntentDefault)

    def segment(self, rgb: np.ndarray) -> np.ndarray:
        import cv2
        import objc
        Q = self.Q
        h, w = rgb.shape[:2]
        with objc.autorelease_pool():
            image = self._cgimage(rgb)
            ok, err = self.handler.performRequests_onCGImage_error_([self.request], image, None)
            if not ok:
                raise MaskError(f"Vision person segmentation failed: {err}")
            results = self.request.results() or []
            if not results:
                return np.zeros((h, w), dtype=np.float32)
            buffer = results[0].pixelBuffer()
            Q.CVPixelBufferLockBaseAddress(buffer, Q.kCVPixelBufferLock_ReadOnly)
            try:
                bh, bw = Q.CVPixelBufferGetHeight(buffer), Q.CVPixelBufferGetWidth(buffer)
                stride = Q.CVPixelBufferGetBytesPerRow(buffer)
                base = Q.CVPixelBufferGetBaseAddress(buffer)
                raw = np.frombuffer(base.as_buffer(stride * bh), dtype=np.uint8).reshape(bh, stride)[:, :bw].copy()
            finally:
                Q.CVPixelBufferUnlockBaseAddress(buffer, Q.kCVPixelBufferLock_ReadOnly)
        soft = raw.astype(np.float32) / 255.0
        if soft.shape != (h, w):
            soft = cv2.resize(soft, (w, h), interpolation=cv2.INTER_LINEAR)
        return soft


def postprocess(soft_masks: Iterable[np.ndarray], dilate_px: int = DILATE_PX, alpha: float = EMA_ALPHA,
                threshold: float = THRESHOLD) -> Iterator[np.ndarray]:
    """Soft masks in [0,1] -> uint8 masks (0/255): EMA over time, threshold, dilate."""
    import cv2
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate_px + 1, 2 * dilate_px + 1)) \
        if dilate_px > 0 else None
    ema: Optional[np.ndarray] = None
    for soft in soft_masks:
        soft = np.asarray(soft, dtype=np.float32)
        ema = soft if ema is None else alpha * soft + (1.0 - alpha) * ema
        binary = (ema >= threshold).astype(np.uint8) * 255
        if kernel is not None:
            binary = cv2.dilate(binary, kernel)
        yield binary


def run_masks(work_path: Path, out_path: Path, report: Callable[[float, str], None],
              quality: str = "accurate") -> dict:
    """work.mp4 -> mask.mp4. Returns {frames, person_frames, coverage}."""
    info = probe(work_path)
    total = info.get("frames") or 0
    w, h = info["width"], info["height"]
    segmenter = PersonSegmenter(quality)
    report(0.0, f"Apple Vision person segmentation ({quality})")
    stats = {"frames": 0, "person_frames": 0, "coverage": 0.0}

    def soft_stream() -> Iterator[np.ndarray]:
        for i, frame in enumerate(iter_frames(work_path)):
            yield segmenter.segment(frame)
            if total:
                report((i + 1) / total, f"frame {i + 1}/{total}")
            elif i % 8 == 0:
                report(0.5, f"frame {i + 1}")

    coverage = []
    with VideoWriter(out_path, w, h, WORK_FPS, crf=12) as writer:
        for mask in postprocess(soft_stream()):
            frac = float((mask > 127).mean())
            coverage.append(frac)
            stats["person_frames"] += int(frac > 0.002)
            writer.write(mask)
    stats["frames"] = len(coverage)
    stats["coverage"] = round(float(np.mean(coverage)) if coverage else 0.0, 4)
    report(1.0, f"{stats['frames']} masks, person in {stats['person_frames']} frames")
    return stats
