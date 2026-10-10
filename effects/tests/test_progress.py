"""JSON-lines contract: progress lines, done line, error line, and CLI refusals (no weights)."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import synth
from zfx.progress import Progress


def run_cli(*args, home: Path, timeout=300):
    env = dict(os.environ, ZEUS_EFFECTS_HOME=str(home))
    proc = subprocess.run([sys.executable, str(synth.CLI)] + list(args), capture_output=True, text=True,
                          env=env, timeout=timeout)
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    return proc.returncode, lines, proc


class ProgressFormatTests(unittest.TestCase):
    def test_lines_are_single_json_objects(self):
        buf = io.StringIO()
        p = Progress(stream=buf)
        p.update("depth", 0.42, "frame 40/96")
        p("masks", 1.7, "clamped")
        p.span("composite", 0.5, 1.0)(0.5, "half of the second half")
        p.done(Path("/tmp/job"), {"frames": 64, "path": Path("/x")})
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        objs = [json.loads(l) for l in lines]
        self.assertEqual(objs[0], {"stage": "depth", "progress": 0.42, "message": "frame 40/96"})
        self.assertEqual(objs[1]["progress"], 1.0)
        self.assertAlmostEqual(objs[2]["progress"], 0.75)
        self.assertEqual(objs[3], {"done": True, "job": "/tmp/job", "result": {"frames": 64, "path": "/x"}})
        for l in lines:
            self.assertNotIn("\n", l)

    def test_error_line(self):
        buf = io.StringIO()
        p = Progress(stream=buf)
        p.update("depth", 0.1)
        p.error("Not enough memory")
        last = json.loads(buf.getvalue().splitlines()[-1])
        self.assertEqual(last, {"error": "Not enough memory", "stage": "depth"})
        self.assertTrue(p.failed)


class CLIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="zfx-cli-"))
        cls.clip = synth.make_clip(cls.tmp / "clip.mp4", seconds=2.0, size="640x360", rate=24)

    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def test_list_json_empty(self):
        code, lines, _ = run_cli("list", "--json", home=self.home)
        self.assertEqual(code, 0)
        self.assertTrue(lines[-1]["done"])
        self.assertEqual(lines[-1]["result"]["jobs"], [])

    def test_status_missing_job_is_error_line(self):
        code, lines, _ = run_cli("status", "--job", str(self.home / "nope"), "--json", home=self.home)
        self.assertEqual(code, 1)
        self.assertIn("error", lines[-1])
        self.assertEqual(lines[-1]["stage"], "status")

    def test_doctor_json_never_downloads(self):
        code, lines, _ = run_cli("doctor", "--json", home=self.home)
        self.assertEqual(code, 0)
        rep = lines[-1]["result"]
        for key in ("python", "torch", "ffmpeg", "yt_dlp", "vision", "weights", "memory", "disk"):
            self.assertIn(key, rep)
        self.assertFalse(rep["weights"]["vda"]["present"])        # empty home: nothing fetched
        self.assertFalse((self.home / "models").exists())

    def test_prepare_without_weights_fails_cleanly_after_io(self):
        job_dir = self.home / "Jobs" / "t1"
        code, lines, _ = run_cli("prepare", "--input", str(self.clip), "--start", "0.5", "--duration", "1",
                                 "--mode", "depth", "--job", str(job_dir), home=self.home)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["stage"], "depth")
        self.assertIn("setup.sh", lines[-1]["error"])
        for obj in lines[:-1]:
            self.assertEqual(set(obj) - {"overall"}, {"stage", "progress", "message"})
        self.assertTrue((job_dir / "source.mp4").is_file())
        self.assertTrue((job_dir / "audio.m4a").is_file())
        self.assertTrue((job_dir / "work.mp4").is_file())
        data = json.loads((job_dir / "job.json").read_text())
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["frames"], 16)

    def test_prepare_rejects_long_duration(self):
        code, lines, _ = run_cli("prepare", "--input", str(self.clip), "--duration", "11", "--mode", "depth",
                                 home=self.home)
        self.assertEqual(code, 1)
        self.assertIn("between", lines[-1]["error"])

    def test_generate_refuses_without_consent_or_approval(self):
        from test_review import fake_prepared_job
        job = fake_prepared_job(self.home)
        ref = self.tmp / "me.jpg"
        ref.write_bytes(b"\xff\xd8\xff")
        code, lines, _ = run_cli("generate", "--job", str(job.dir), "--engine", "faceswap", "--ref", str(ref),
                                 home=self.home)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["stage"], "consent")
        code, lines, _ = run_cli("generate", "--job", str(job.dir), "--engine", "faceswap", "--ref", str(ref),
                                 "--consent", home=self.home)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["stage"], "review")
        self.assertIn("not approved", lines[-1]["error"])

    def test_generate_refuses_changed_after_approval(self):
        from test_review import fake_prepared_job
        job = fake_prepared_job(self.home)
        code, lines, _ = run_cli("approve", "--job", str(job.dir), home=self.home)
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[-1]["done"])
        job.path("control").write_bytes(b"tampered")
        ref = self.tmp / "me.jpg"
        ref.write_bytes(b"\xff\xd8\xff")
        code, lines, _ = run_cli("generate", "--job", str(job.dir), "--engine", "vace", "--ref", str(ref),
                                 "--consent", home=self.home)
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1]["stage"], "review")
        self.assertIn("changed", lines[-1]["error"])


if __name__ == "__main__":
    unittest.main()
