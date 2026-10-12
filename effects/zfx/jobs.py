"""Job folders and job.json (atomic writes).

job.json:
  {id, created, updated, input, start, duration, mode, fps, size, status,
   artifacts{name: file name}, timings{stage: seconds}, review{approved_at, hashes}, generations[...]}

Artifact paths are stored relative to the job folder (the folder can be moved);
`job.path(name)` / `job.artifact(name)` give absolute paths.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from . import jobs_dir

JOB_FILE = "job.json"

# Standard artifact names -> file names in the job folder.
ARTIFACT_FILES = {
    "source": "source.mp4",
    "audio": "audio.m4a",
    "work": "work.mp4",
    "depth": "depth.mp4",
    "mask": "mask.mp4",
    "control": "control.mp4",
    "landmarks": "face-landmarks.json",
    "motion": "motion.json",
    "bvh": "motion.bvh",
    "pose": "pose.mp4",
    "preview": "preview.jpg",
    # the parts map (zfx.parts, effects/PARTS.md): mode depth_parts, prepare --parts, or the parts command
    "parts": "parts.mp4",
    "parts_index": "parts.npz",
    "parts_json": "parts.json",
    "control_parts": "control-parts.mp4",
    "preview_parts": "preview-parts.jpg",
    "result_raw": "result-raw.mp4",
    "final": "final.mp4",
}


class JobError(RuntimeError):
    """Plain-English job problem (missing folder, bad job.json...)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; job files are ordinary user files
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Union[str, Path], data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _slug(text: str, limit: int = 24) -> str:
    text = re.sub(r"^https?://(www\.)?", "", text or "")
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", text) or re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", text)
    if m:
        text = m.group(1)
    else:
        text = Path(text).stem if "/" in text or "." in text else text
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")
    return (text[:limit] or "clip").strip("-") or "clip"


def new_job_id(input_ref: str = "") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_slug(input_ref)}-{secrets.token_hex(2)}"


class Job:
    """A job folder plus its job.json (dict-like access to the data)."""

    def __init__(self, directory: Union[str, Path], data: Optional[dict] = None):
        self.dir = Path(directory).expanduser().resolve()
        self.data: dict = data if data is not None else {}

    # -- create / load ---------------------------------------------------
    @classmethod
    def create(cls, input_ref: str, start: float, duration: float, mode: str,
               job_dir: Optional[Union[str, Path]] = None) -> "Job":
        if job_dir:
            directory = Path(job_dir).expanduser()
            job_id = directory.name
        else:
            job_id = new_job_id(input_ref)
            directory = jobs_dir() / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = cls(directory, {
            "id": job_id,
            "created": now_iso(),
            "updated": now_iso(),
            "input": input_ref,
            "start": float(start),
            "duration": float(duration),
            "mode": mode,
            "fps": None,
            "size": None,
            "status": "preparing",
            "artifacts": {},
            "timings": {},
            "review": None,
            "generations": [],
        })
        job.save()
        return job

    @classmethod
    def load(cls, directory: Union[str, Path]) -> "Job":
        directory = Path(directory).expanduser()
        if not directory.is_dir():
            # Accept a bare job id too.
            candidate = jobs_dir() / str(directory)
            if candidate.is_dir():
                directory = candidate
            else:
                raise JobError(f"Job folder not found: {directory}")
        path = directory / JOB_FILE
        if not path.is_file():
            raise JobError(f"No job.json in {directory}. Run prepare first.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JobError(f"Could not read {path}: {exc}") from exc
        return cls(directory, data)

    def reload(self) -> "Job":
        self.data = Job.load(self.dir).data
        return self

    def save(self) -> None:
        self.data["updated"] = now_iso()
        atomic_write_json(self.dir / JOB_FILE, self.data)

    # -- dict-like -------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def id(self) -> str:
        return self.data.get("id") or self.dir.name

    @property
    def mode(self) -> str:
        return self.data.get("mode") or "depth"

    @property
    def json_path(self) -> Path:
        return self.dir / JOB_FILE

    # -- artifacts -------------------------------------------------------
    def path(self, name: str) -> Path:
        """Absolute path for an artifact name ('work') or a file name ('work.mp4')."""
        rel = self.data.get("artifacts", {}).get(name) or ARTIFACT_FILES.get(name) or name
        p = Path(rel)
        return p if p.is_absolute() else self.dir / p

    def artifact(self, name: str) -> Optional[Path]:
        """Absolute path of a recorded artifact that exists on disk, else None."""
        if name not in self.data.get("artifacts", {}):
            return None
        p = self.path(name)
        return p if p.exists() else None

    def set_artifact(self, name: str, path: Union[str, Path], save: bool = True) -> None:
        p = Path(path)
        try:
            rel = str(p.resolve().relative_to(self.dir))
        except ValueError:
            rel = str(p)
        self.data.setdefault("artifacts", {})[name] = rel
        if save:
            self.save()

    def set_status(self, status: str, **fields: Any) -> None:
        self.data["status"] = status
        self.data.update(fields)
        self.save()

    def set_timing(self, stage: str, seconds: float, save: bool = True) -> None:
        self.data.setdefault("timings", {})[stage] = round(float(seconds), 2)
        if save:
            self.save()

    def add_generation(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("created", now_iso())
        self.data.setdefault("generations", []).append(entry)
        self.save()

    def summary(self) -> dict:
        return {
            "id": self.id,
            "dir": str(self.dir),
            "created": self.data.get("created"),
            "input": self.data.get("input"),
            "start": self.data.get("start"),
            "duration": self.data.get("duration"),
            "mode": self.data.get("mode"),
            "status": self.data.get("status"),
            "approved": bool((self.data.get("review") or {}).get("approved_at")),
            "generations": len(self.data.get("generations") or []),
        }


def iter_jobs(root: Optional[Path] = None) -> Iterator[Job]:
    root = root or jobs_dir()
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir() and (child / JOB_FILE).is_file():
            try:
                yield Job.load(child)
            except JobError:
                continue


def list_jobs(root: Optional[Path] = None) -> list[Job]:
    """Jobs under Jobs/, newest first."""
    def key(job: Job) -> float:
        try:
            return (job.dir / JOB_FILE).stat().st_ctime if not job.get("created") else \
                datetime.fromisoformat(job.get("created")).timestamp()
        except (OSError, ValueError):
            return 0.0
    return sorted(iter_jobs(root), key=key, reverse=True)

