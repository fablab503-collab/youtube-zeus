"""review.py: approval bound to SHA-256; any change after approval blocks generate."""

import json
import unittest

import synth
from zfx.jobs import Job, list_jobs
from zfx.review import ReviewError, approve, sha256_file, verify


def fake_prepared_job(home, mode="depth", audio=True) -> Job:
    job = Job.create("/tmp/in.mp4", 1.0, 2.0, mode)
    files = {"source": b"src", "work": b"work", "depth": b"depth", "mask": b"mask", "control": b"control",
             "preview": b"jpg"}
    if audio:
        files["audio"] = b"aac"
    if mode in ("mesh", "depth_mesh"):
        files["landmarks"] = b'{"frames": []}'
    for name, data in files.items():
        path = job.path(name)
        path.write_bytes(data)
        job.set_artifact(name, path, save=False)
    job["has_audio"] = audio
    job["status"] = "prepared"
    job.save()
    return job


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def test_job_lives_under_home_and_saves_atomically(self):
        job = fake_prepared_job(self.home)
        self.assertEqual(job.dir.parent, (self.home / "Jobs").resolve())
        data = json.loads((job.dir / "job.json").read_text())
        self.assertEqual(data["status"], "prepared")
        self.assertEqual(data["artifacts"]["work"], "work.mp4")  # relative to the job folder
        self.assertEqual([p.name for p in job.dir.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual([j.id for j in list_jobs()], [job.id])

    def test_not_approved_refused(self):
        job = fake_prepared_job(self.home)
        with self.assertRaises(ReviewError):
            verify(job)

    def test_approve_then_verify_ok(self):
        job = fake_prepared_job(self.home)
        review = approve(job)
        self.assertEqual(review["hashes"]["work"], sha256_file(job.path("work")))
        self.assertIn("audio", review["hashes"])
        self.assertIn("preview", review["hashes"])
        reloaded = Job.load(job.dir)
        self.assertEqual(reloaded["status"], "approved")
        self.assertEqual(verify(reloaded), review["hashes"])

    def test_modified_file_fails_verify(self):
        job = fake_prepared_job(self.home)
        approve(job)
        job.path("mask").write_bytes(b"mask edited after review")
        with self.assertRaises(ReviewError) as ctx:
            verify(Job.load(job.dir))
        self.assertIn("mask.mp4 changed", str(ctx.exception))

    def test_deleted_file_fails_verify(self):
        job = fake_prepared_job(self.home)
        approve(job)
        job.path("control").unlink()
        with self.assertRaises(ReviewError):
            verify(Job.load(job.dir))

    def test_missing_artifact_cannot_be_approved(self):
        job = fake_prepared_job(self.home, mode="depth_mesh")
        job.path("landmarks").unlink()
        with self.assertRaises(ReviewError):
            approve(job)

    def test_mesh_mode_hashes_landmarks(self):
        job = fake_prepared_job(self.home, mode="mesh")
        review = approve(job)
        self.assertIn("landmarks", review["hashes"])

    def test_no_audio_clip_can_be_approved(self):
        job = fake_prepared_job(self.home, audio=False)
        review = approve(job)
        self.assertNotIn("audio", review["hashes"])
        verify(job)


if __name__ == "__main__":
    unittest.main()
