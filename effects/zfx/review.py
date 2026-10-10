"""Review gate: approval bound to SHA-256 hashes of the prepared artifacts.

approve(job)  hashes source.mp4, audio.m4a, work.mp4, depth.mp4, mask.mp4, control.mp4,
              face-landmarks.json (mesh modes) and preview.jpg into job.json.review.
verify(job)   used by generate: raises ReviewError (plain English) unless the job was
              approved and every approved file still has the same hash. Returns the hashes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union

from .jobs import Job, now_iso

HASH_CHUNK = 1 << 20


class ReviewError(RuntimeError):
    """The job is not approved, or a prepared artifact changed after approval."""


def sha256_file(path: Union[str, Path]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def required_artifacts(job: Job) -> list[str]:
    """Artifacts that must exist before approval (by mode)."""
    names = ["source", "work", "depth", "mask", "control"]
    if job.mode in ("mesh", "depth_mesh"):
        names.append("landmarks")
    if job.get("has_audio"):
        names.append("audio")
    return names


def reviewed_artifacts(job: Job) -> list[str]:
    """Everything that gets hashed: required + preview when present."""
    names = required_artifacts(job)
    for optional in ("audio", "preview"):
        if optional not in names and job.artifact(optional) is not None:
            names.append(optional)
    return names


def approve(job: Job) -> dict:
    """Record the review into job.json.review. Raises ReviewError if anything is missing."""
    if job.get("status") in ("preparing", "failed"):
        raise ReviewError(f"Job is '{job.get('status')}', not ready for review. Run prepare again.")
    missing = [n for n in required_artifacts(job) if job.artifact(n) is None]
    if missing:
        raise ReviewError("Cannot approve, missing: " + ", ".join(
            f"{n} ({job.path(n).name})" for n in missing) + ". Run prepare again.")
    hashes = {}
    files = {}
    for name in reviewed_artifacts(job):
        path = job.artifact(name)
        if path is None:
            continue
        hashes[name] = sha256_file(path)
        files[name] = path.name
    review = {"approved_at": now_iso(), "hashes": hashes, "files": files, "mode": job.mode}
    job["review"] = review
    job["status"] = "approved"
    job.save()
    return review


def verify(job: Job) -> dict:
    """Raise ReviewError unless approved and unchanged since approval. Returns {name: sha256}."""
    review = job.get("review") or {}
    hashes = review.get("hashes") or {}
    if not review.get("approved_at") or not hashes:
        raise ReviewError("This job is not approved yet. Review preview.jpg and the videos, then run approve.")
    for name in required_artifacts(job):
        if name not in hashes:
            raise ReviewError(f"The approval does not cover {name}. Run approve again.")
    changed = []
    for name, digest in hashes.items():
        path = job.path(name)
        if not path.is_file():
            changed.append(f"{path.name} is missing")
        elif sha256_file(path) != digest:
            changed.append(f"{path.name} changed")
    if changed:
        raise ReviewError("Prepared files changed after approval (" + "; ".join(changed)
                          + "). Review again and run approve before generating.")
    return dict(hashes)


def is_approved(job: Job) -> tuple[bool, str]:
    try:
        verify(job)
        return True, "approved and unchanged"
    except ReviewError as exc:
        return False, str(exc)


def clear(job: Job, save: bool = True) -> None:
    job["review"] = None
    if save:
        job.save()
