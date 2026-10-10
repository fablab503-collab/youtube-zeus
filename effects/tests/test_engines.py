"""Engine layer tests: no model weights, no FaceFusion needed (synthetic clips + a fake engine).

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_engines.py' -v
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

EFFECTS = Path(__file__).resolve().parents[1]
if str(EFFECTS) not in sys.path:
    sys.path.insert(0, str(EFFECTS))

from zfx import generate  # noqa: E402
from zfx.engines import EngineError, faceswap  # noqa: E402

try:
    from zfx.engines import vace  # needs numpy only (torch is imported lazily)
except ImportError:  # pragma: no cover
    vace = None


def _bin(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", shutil.which(name)):
        if cand and Path(cand).exists():
            return cand
    raise unittest.SkipTest(f"{name} not installed")


def _run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def make_source(path: Path, seconds: float = 2.0, size: str = "640x360", rate: str = "30") -> None:
    _run([_bin("ffmpeg"), "-y", "-v", "error",
          "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}",
          "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "128k", "-shortest", str(path)])


def make_raw(path: Path, frames: int = 32, size: str = "416x240", rate: int = 16) -> None:
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
          "-i", f"testsrc2=size={size}:rate={rate}", "-frames:v", str(frames),
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(path)])


def audio_packets_sha(path: Path) -> str:
    """SHA-256 of the raw audio packets (stream copy, no decoding)."""
    out = subprocess.run([_bin("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy",
                          "-f", "data", "-"], check=True, capture_output=True).stdout
    assert out, "no audio packets"
    return hashlib.sha256(out).hexdigest()


def decoded_audio_md5(path: Path) -> str:
    out = subprocess.run([_bin("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
                         check=True, capture_output=True, text=True).stdout.strip()
    return out


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run([_bin("ffprobe"), "-v", "error", "-print_format", "json", "-show_streams",
                          "-show_format", str(path)], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_job(root: Path, approve: bool = True) -> Path:
    """Job folder shaped like `prepare` output (synthetic media), optionally approved."""
    job = root / "job-test"
    job.mkdir(parents=True)
    make_source(job / "source.mp4")
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(job / "source.mp4"), "-vn", "-c:a", "copy",
          str(job / "audio.m4a")])
    make_raw(job / "work.mp4")
    for name in ("depth.mp4", "mask.mp4", "control.mp4"):
        shutil.copyfile(job / "work.mp4", job / name)
    (job / "preview.jpg").write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    artifacts = {"source": "source.mp4", "audio": "audio.m4a", "work": "work.mp4", "depth": "depth.mp4",
                 "mask": "mask.mp4", "control": "control.mp4", "preview": "preview.jpg"}
    data = {"id": job.name, "created": "2026-10-10T12:00:00+02:00", "input": "synthetic", "start": 0.0,
            "duration": 2.0, "mode": "depth", "fps": 30.0, "size": [640, 360], "status": "prepared",
            "has_audio": True, "artifacts": artifacts, "timings": {}, "review": None, "generations": []}
    (job / "job.json").write_text(json.dumps(data, indent=2))
    if approve:
        try:
            from zfx import review
            from zfx.jobs import Job
            review.approve(Job.load(job))
        except ImportError:
            data["review"] = {"approved_at": "2026-10-10T12:01:00+02:00",
                              "hashes": {k: sha(job / v) for k, v in artifacts.items()}}
            data["status"] = "approved"
            (job / "job.json").write_text(json.dumps(data, indent=2))
    return job


def make_ref(root: Path) -> Path:
    ref = root / "me.png"
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64", "-frames:v", "1",
          str(ref)])
    return ref


def args(**kw):
    base = dict(engine="faceswap", ref=[], prompt="", consent=True, frames=None, steps=None, size=None, seed=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class FakeEngine:
    """Stands in for an engine: writes a 16 fps 416x240 result-raw.mp4 (2 s)."""
    NAME = "faceswap"
    LABEL = "fake"

    def __init__(self):
        self.calls = 0

    def status(self):
        return {"ready": True, "message": "ready"}

    def run(self, job_dir, refs, out_path, progress, **options):
        self.calls += 1
        progress("generate", 0.5, "fake engine working")
        make_raw(Path(out_path), frames=32)
        return {"engine": "fake", "seconds": 0.0}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-engines-"))
        self.events = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def progress(self, stage, p, message=""):
        self.events.append((stage, p, message))

    def test_consent_refusal(self):
        job = make_job(self.tmp)
        fake = FakeEngine()
        with mock.patch.object(generate, "get_engine", return_value=fake):
            with self.assertRaises(generate.GenerateError) as ctx:
                generate.run_generate(args(consent=False, ref=[str(make_ref(self.tmp))]), job, self.progress)
        self.assertIn("consent", str(ctx.exception).lower())
        self.assertIn(generate.CONSENT_TEXT, str(ctx.exception))
        self.assertEqual(fake.calls, 0)
        self.assertFalse((job / "final.mp4").exists())

    def test_refuses_unapproved_job(self):
        job = make_job(self.tmp, approve=False)
        fake = FakeEngine()
        with mock.patch.object(generate, "get_engine", return_value=fake):
            with self.assertRaises(generate.GenerateError) as ctx:
                generate.run_generate(args(ref=[str(make_ref(self.tmp))]), job, self.progress)
        self.assertIn("not approved", str(ctx.exception).lower())
        self.assertEqual(fake.calls, 0)

    def test_refuses_when_artifact_changed_after_approval(self):
        job = make_job(self.tmp)
        with open(job / "control.mp4", "ab") as fh:
            fh.write(b"tampered")
        fake = FakeEngine()
        with mock.patch.object(generate, "get_engine", return_value=fake):
            with self.assertRaises(generate.GenerateError) as ctx:
                generate.run_generate(args(ref=[str(make_ref(self.tmp))]), job, self.progress)
        self.assertIn("changed", str(ctx.exception).lower())
        self.assertEqual(fake.calls, 0)

    def test_fallback_verify_matches_contract(self):
        job = make_job(self.tmp)
        ok, msg = generate._fallback_verify(job)
        self.assertTrue(ok, msg)
        (job / "mask.mp4").write_bytes(b"changed")
        ok, msg = generate._fallback_verify(job)
        self.assertFalse(ok)
        self.assertIn("mask.mp4 changed", msg)

    def test_refuses_missing_reference(self):
        job = make_job(self.tmp)
        with mock.patch.object(generate, "get_engine", return_value=FakeEngine()):
            with self.assertRaises(generate.GenerateError):
                generate.run_generate(args(ref=[str(self.tmp / "nope.jpg")]), job, self.progress)

    def test_accepts_job_object_from_cli(self):
        job = make_job(self.tmp)
        try:
            from zfx.jobs import Job
        except ImportError:
            self.skipTest("zfx.jobs not available")
        self.assertEqual(generate.resolve_job_dir(Job.load(job)), job.resolve())

    def test_unknown_engine(self):
        job = make_job(self.tmp)
        with self.assertRaises(generate.GenerateError):
            generate.run_generate(args(engine="seedance"), job, self.progress)


class MuxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-mux-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generate_mux_keeps_original_audio_and_timing(self):
        job = make_job(self.tmp)
        fake = FakeEngine()
        events = []
        with mock.patch.object(generate, "get_engine", return_value=fake):
            result = generate.run_generate(args(ref=[str(make_ref(self.tmp))]), job,
                                           lambda s, p, m="": events.append((s, p, m)))
        self.assertEqual(fake.calls, 1)
        final = job / "final.mp4"
        self.assertTrue(final.is_file())
        # audio: identical packets (stream copy) and identical decoded samples
        self.assertEqual(audio_packets_sha(final), audio_packets_sha(job / "audio.m4a"))
        self.assertEqual(decoded_audio_md5(final), decoded_audio_md5(job / "audio.m4a"))
        info = ffprobe_json(final)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["width"], v["height"]), (640, 360))           # source size
        self.assertEqual(v["avg_frame_rate"], "30/1")                       # source fps
        self.assertAlmostEqual(float(v["duration"]), 2.0, delta=0.05)       # source duration
        self.assertEqual(a["codec_name"], "aac")
        self.assertEqual(info["format"]["tags"].get("comment"), "AI-generated with YouTube Zeus Effects (faceswap)")
        data = json.loads((job / "job.json").read_text())
        self.assertEqual(len(data["generations"]), 1)
        rec = data["generations"][0]
        self.assertEqual(rec["engine"], "faceswap")
        self.assertTrue(rec["consent"])
        self.assertEqual(rec["final"], "final.mp4")
        self.assertEqual(data["status"], "generated")
        self.assertEqual(result["generation"], 1)
        self.assertTrue(any(s == "mux" and p == 1.0 for s, p, _ in events))
        # the review still holds after generating (results are not reviewed artifacts)
        ok, msg = generate.check_review(job)
        self.assertTrue(ok, msg)

    def test_mux_stream_copies_matching_video(self):
        src = self.tmp / "source.mp4"
        make_source(src, seconds=1.0)
        audio = self.tmp / "audio.m4a"
        _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-c:a", "copy", str(audio)])
        raw = self.tmp / "raw.mp4"
        _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-an", "-c:v", "copy", str(raw)])
        out = self.tmp / "final.mp4"
        mux = generate.mux_final(raw, src, audio, out, "faceswap")
        self.assertTrue(mux["video_stream_copy"])
        self.assertEqual(audio_packets_sha(out), audio_packets_sha(audio))


@unittest.skipIf(vace is None, "numpy not available")
class WindowPlanTests(unittest.TestCase):
    def check_plan(self, total, window, overlap=8):
        plan = vace.plan_windows(total, window, overlap)
        self.assertTrue(plan)
        n = vace.normalize_frames(window)
        for s, e in plan:
            self.assertEqual((e - s - 1) % 4, 0, f"window {s}-{e} not 4k+1")
        if total > n:
            for s, e in plan:
                self.assertEqual(e - s, n)
            self.assertEqual(plan[0][0], 0)
            self.assertEqual(plan[-1][1], total)
            for (s0, e0), (s1, e1) in zip(plan, plan[1:]):
                self.assertGreater(s1, s0)
                self.assertGreaterEqual(e0 - s1, min(overlap, (n - 1) // 2), "overlap too small")
                self.assertLessEqual(s1, e0, "gap between windows")
        else:
            self.assertEqual(len(plan), 1)
            self.assertGreaterEqual(plan[0][1], total)
        covered = set()
        for s, e in plan:
            covered.update(range(s, min(e, total)))
        self.assertEqual(covered, set(range(total)))
        return plan

    def test_rule_4k_plus_1(self):
        self.assertEqual(vace.normalize_frames(33), 33)
        self.assertEqual(vace.normalize_frames(32), 29)
        self.assertEqual(vace.normalize_frames(17), 17)
        self.assertEqual(vace.normalize_frames(1), 5)
        self.assertEqual(vace.frames_up(20), 21)
        self.assertEqual(vace.frames_up(17), 17)

    def test_ten_second_clip(self):
        plan = self.check_plan(160, 33)
        self.assertEqual(plan[:3], [(0, 33), (25, 58), (50, 83)])
        self.assertEqual(plan[-1], (127, 160))

    def test_various(self):
        for total in (1, 5, 16, 17, 20, 33, 34, 48, 81, 100, 159, 160, 161):
            for window in (17, 21, 33, 81):
                with self.subTest(total=total, window=window):
                    self.check_plan(total, window)

    def test_short_clip_single_window(self):
        self.assertEqual(vace.plan_windows(20, 33), [(0, 21)])
        self.assertEqual(vace.plan_windows(33, 33), [(0, 33)])
        self.assertEqual(vace.plan_windows(0, 33), [])

    def test_blend_weights(self):
        w = vace.blend_weights(8)
        self.assertEqual(len(w), 8)
        self.assertTrue(all(0 < x < 1 for x in w))
        self.assertTrue(all(b > a for a, b in zip(w, w[1:])))

    def test_target_size(self):
        self.assertEqual(vace.target_size(1920, 1080, "480p"), (832, 480))
        self.assertEqual(vace.target_size(1080, 1920, "480p"), (480, 832))
        self.assertEqual(vace.target_size(640, 360, "360p"), (640, 368))
        with self.assertRaises(EngineError):
            vace.target_size(640, 360, "4k")

    def test_missing_weights_message(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"ZEUS_VACE_DIR": d}):
            with self.assertRaises(EngineError) as ctx:
                vace.check_model()
            self.assertIn("setup-engines.sh --vace", str(ctx.exception))
            self.assertFalse(vace.status()["ready"])


class FaceSwapCommandTests(unittest.TestCase):
    def test_command_flags(self):
        cmd = faceswap.build_command(Path("/ff"), [Path("/r/a.jpg"), Path("/r/b.jpg")], Path("/j/source.mp4"),
                                     Path("/j/out.mp4"), Path("/j/tmp"), Path("/j/jobs"))
        self.assertEqual(cmd[1:3], ["facefusion.py", "headless-run"])
        i = cmd.index("--source-paths")
        self.assertEqual(cmd[i + 1:i + 3], ["/r/a.jpg", "/r/b.jpg"])
        self.assertEqual(cmd[cmd.index("--target-path") + 1], "/j/source.mp4")
        self.assertEqual(cmd[cmd.index("--output-path") + 1], "/j/out.mp4")
        i = cmd.index("--processors")
        self.assertEqual(cmd[i + 1:i + 3], ["face_swapper", "face_enhancer"])
        i = cmd.index("--execution-providers")
        self.assertEqual(cmd[i + 1:i + 3], ["coreml", "cpu"])

    def test_progress_regex(self):
        line = "processing:  78%|=======   | 70/90 [03:16<00:31,  1.57s/frame, execution_providers=['coreml']]"
        m = faceswap._TQDM.search(line)
        self.assertEqual((m.group(1), m.group(2), m.group(3), m.group(4)), ("processing", "78", "70", "90"))
        m = faceswap._TQDM.search("downloading:  13%|=         | 9.96M/76.7M [00:00<00:01, 56.9MB/s]")
        self.assertEqual(m.group(1), "downloading")

    def test_not_installed_message(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"ZEUS_FACEFUSION_DIR": d}):
            st = faceswap.status()
            self.assertFalse(st["ready"])
            self.assertIn("setup-engines.sh --faceswap", st["message"])


@unittest.skipIf(vace is None, "numpy not available")
class ChunkedAttentionTests(unittest.TestCase):
    def test_chunked_matches_full(self):
        try:
            import torch
            import torch.nn.functional as F
        except ImportError:
            self.skipTest("torch not installed")
        if not torch.backends.mps.is_available():
            self.skipTest("no MPS")
        g = torch.Generator().manual_seed(0)
        q, k, v = (torch.randn(1, 2, 700, 32, generator=g).to("mps") for _ in range(3))
        full = F.scaled_dot_product_attention(q, k, v)
        with vace.chunked_attention(budget_mb=1):  # forces chunking (700*700*2*4 B > 1 MB)
            chunked = torch.nn.functional.scaled_dot_product_attention(query=q, key=k, value=v)
        self.assertEqual(F.scaled_dot_product_attention.__name__, "scaled_dot_product_attention")
        self.assertLess((full - chunked).abs().max().item(), 1e-4)


if __name__ == "__main__":
    unittest.main()
