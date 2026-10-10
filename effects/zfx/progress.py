"""Progress and logging.

stdout carries only JSON lines, one object per line:
    {"stage":"depth","progress":0.42,"message":"frame 40/96"}
and ends with either
    {"done":true,"job":"<dir>","result":{...}}
or
    {"error":"<plain-English message>","stage":"..."}   (exit code 1)

Human-readable logs go to stderr and to $ZEUS_EFFECTS_HOME/effects.log.
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from typing import Any, Optional, TextIO

from . import log_path

_LOGGER_NAME = "zeus_effects"


def setup_logging(verbose: bool = False) -> logging.Logger:
    """stderr + effects.log handlers (idempotent)."""
    logger = logging.getLogger(_LOGGER_NAME)
    if getattr(logger, "_zfx_configured", False):
        return logger
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.DEBUG if verbose else logging.INFO)
    err.setFormatter(fmt)
    logger.addHandler(err)
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass  # read-only home: stderr only
    logger._zfx_configured = True  # type: ignore[attr-defined]
    return logger


def get_logger() -> logging.Logger:
    return setup_logging()


def _clean(value: Any) -> Any:
    """Make values JSON-safe (Paths, numpy scalars, NaN)."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if hasattr(value, "item"):  # numpy scalar
        try:
            return _clean(value.item())
        except Exception:
            pass
    return str(value)


class Progress:
    """JSON-lines progress reporter.

    progress.update("depth", 0.42, "frame 40/96")   # or progress("depth", 0.42, "...")
    progress.log("text")                             # stderr + effects.log only
    progress.done(job_dir, {...});  progress.error("message", "stage")
    progress.span("depth", 0.2, 0.6) -> callable(fraction, message) mapped into that range
    """

    def __init__(self, stream: Optional[TextIO] = None, min_interval: float = 0.0):
        self.stream = stream or sys.stdout
        self.logger = get_logger()
        self.min_interval = min_interval
        self._last_emit: dict[str, float] = {}
        self.stage = "start"
        self.finished = False
        self.failed = False

    # -- low level -------------------------------------------------------
    def emit(self, obj: dict) -> None:
        line = json.dumps(_clean(obj), ensure_ascii=False, separators=(",", ":"))
        self.stream.write(line + "\n")
        self.stream.flush()

    # -- progress --------------------------------------------------------
    def update(self, stage: str, progress: float, message: str = "", **extra: Any) -> None:
        self.stage = stage
        p = max(0.0, min(1.0, float(progress)))
        now = time.monotonic()
        if self.min_interval and p < 1.0 and now - self._last_emit.get(stage, 0.0) < self.min_interval:
            return
        self._last_emit[stage] = now
        obj = {"stage": stage, "progress": round(p, 4), "message": message}
        obj.update(extra)
        self.emit(obj)
        if message:
            self.logger.debug("[%s %3.0f%%] %s", stage, p * 100, message)

    __call__ = update

    def span(self, stage: str, lo: float, hi: float):
        """Return f(fraction, message) that reports into [lo, hi] of `stage`."""
        def report(fraction: float, message: str = "") -> None:
            self.update(stage, lo + (hi - lo) * max(0.0, min(1.0, fraction)), message)
        return report

    def log(self, message: str, level: int = logging.INFO) -> None:
        self.logger.log(level, message)

    info = log

    def warn(self, message: str) -> None:
        self.logger.warning(message)

    # -- terminal lines --------------------------------------------------
    def done(self, job: Any = None, result: Optional[dict] = None) -> None:
        self.finished = True
        self.emit({"done": True, "job": str(job) if job is not None else None, "result": result or {}})

    def error(self, message: str, stage: Optional[str] = None) -> None:
        self.finished = True
        self.failed = True
        self.logger.error("%s (stage: %s)", message, stage or self.stage)
        self.emit({"error": message, "stage": stage or self.stage})


class Timer:
    """with Timer() as t: ...; t.seconds"""

    def __enter__(self) -> "Timer":
        self.start = time.monotonic()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.seconds = round(time.monotonic() - self.start, 2)
