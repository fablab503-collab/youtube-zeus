"""Engine layer tests: no model weights, no FaceFusion needed (synthetic clips + a fake engine).

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_engines.py' -v
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
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


def write_safetensors(path: Path, tensors: dict) -> None:
    """Minimal safetensors writer (numpy only): {name: ndarray} as F32, names containing "bf16" as BF16."""
    import numpy as np
    header, blobs, offset = {}, [], 0
    for name in sorted(tensors):
        a = np.ascontiguousarray(tensors[name], dtype="<f4")
        dtype, raw = "F32", a.tobytes()
        if "bf16" in name:
            dtype, raw = "BF16", (a.view("<u4") >> 16).astype("<u2").tobytes()
        header[name] = {"dtype": dtype, "shape": list(a.shape), "data_offsets": [offset, offset + len(raw)]}
        blobs.append(raw)
        offset += len(raw)
    raw = json.dumps(header).encode()
    raw += b" " * (-len(raw) % 8)
    with open(path, "wb") as fh:
        fh.write(len(raw).to_bytes(8, "little"))
        fh.write(raw)
        for b in blobs:
            fh.write(b)


def read_safetensors(path: Path) -> dict:
    """{name: (dtype, ndarray as float32)} for F32/F16/BF16 files."""
    import numpy as np
    data = path.read_bytes()
    n = int.from_bytes(data[:8], "little")
    header = json.loads(data[8:8 + n])
    out = {}
    for name, info in header.items():
        if name == "__metadata__":
            continue
        s, e = info["data_offsets"]
        raw = data[8 + n + s:8 + n + e]
        if info["dtype"] == "F32":
            a = np.frombuffer(raw, "<f4")
        elif info["dtype"] == "F16":
            a = np.frombuffer(raw, "<f2").astype(np.float32)
        else:
            a = (np.frombuffer(raw, "<u2").astype(np.uint32) << 16).view(np.float32)
        out[name] = (info["dtype"], a.reshape(info["shape"]))
    return out


def make_fake_model(root: Path) -> Path:
    """Wan-like folder: transformer/ (2 shards + index) and text_encoder/ (1 shard + index), fp32."""
    import numpy as np
    rng = np.random.default_rng(0)
    model = root / "Wan2.1-VACE-1.3B-diffusers"
    tr = {"blocks.0.attn1.to_q.weight": rng.standard_normal((8, 4)),
          "blocks.0.norm2.weight": rng.standard_normal(4),
          "condition_embedder.time_embedder.linear_1.weight": rng.standard_normal((4, 4)),
          "scale_shift_table": rng.standard_normal((1, 6, 4)),
          "vace_blocks.0.proj_out.weight": rng.standard_normal((4, 4)) * 1e-3,
          "vace_blocks.0.bf16": np.array([0.25, -1.5, 3.0, 0.0078125], dtype=np.float32),  # stored BF16
          "patch_embedding.bias": np.array([1e6, -2.0, 0.5], dtype=np.float32)}  # > fp16 range
    te = {"encoder.block.0.layer.0.SelfAttention.q.weight": rng.standard_normal((4, 4)),
          "shared.weight": rng.standard_normal((6, 4))}
    for sub, tensors, shards in (("transformer", tr, 2), ("text_encoder", te, 1)):
        d = model / sub
        d.mkdir(parents=True)
        names = sorted(tensors)
        base = "diffusion_pytorch_model" if sub == "transformer" else "model"
        weight_map = {}
        for k in range(shards):
            fname = f"{base}-{k + 1:05d}-of-{shards:05d}.safetensors"
            part = {n: tensors[n] for i, n in enumerate(names) if i % shards == k}
            write_safetensors(d / fname, part)
            weight_map.update({n: fname for n in part})
        (d / f"{base}.safetensors.index.json").write_text(json.dumps({"metadata": {}, "weight_map": weight_map}))
        (d / "config.json").write_text(json.dumps({"_class_name": sub}))
    return model


@unittest.skipIf(vace is None, "numpy not available")
class HalfCopyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-half-"))
        self.model = make_fake_model(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keep_fp32_modules(self):
        self.assertTrue(vace.keep_fp32("blocks.0.norm2.weight"))
        self.assertTrue(vace.keep_fp32("condition_embedder.time_embedder.linear_1.weight"))
        self.assertTrue(vace.keep_fp32("scale_shift_table"))
        self.assertFalse(vace.keep_fp32("blocks.0.attn1.to_q.weight"))
        self.assertFalse(vace.keep_fp32("blocks.0.attn1.norm_q.weight"))   # norm_q is not norm1/2/3

    def test_half_bytes(self):
        import numpy as np
        a = np.array([0.0, 1.0, -2.5, 3.14159, 1e-3, 65504.0], dtype="<f4")
        f16 = np.frombuffer(vace.f32_to_half_bytes(a.tobytes(), "F16"), "<f2")
        self.assertTrue(np.array_equal(f16, a.astype(np.float16)))
        bf16 = np.frombuffer(vace.f32_to_half_bytes(a.tobytes(), "BF16"), "<u2")
        self.assertEqual(int(bf16[1]), 0x3F80)                       # 1.0
        self.assertEqual(int(bf16[3]), 0x4049)                       # pi, rounded to nearest
        back = (bf16.astype(np.uint32) << 16).view(np.float32)
        self.assertTrue(np.allclose(back, a, rtol=1 / 128))
        try:
            import torch
        except ImportError:
            return
        ref = torch.from_numpy(a.copy()).to(torch.bfloat16).view(torch.int16).numpy().view("<u2")
        self.assertTrue(np.array_equal(bf16, ref))

    def test_plan_header_offsets(self):
        tensors = {"b.norm1.weight": ("F32", (3,)), "a.w": ("F32", (2, 2)), "c.half": ("F16", (5,)),
                   "d.vace": ("BF16", (2,))}
        h = vace.plan_half_header(tensors, "F16", vace.keep_fp32)
        self.assertEqual(list(h), ["a.w", "b.norm1.weight", "c.half", "d.vace"])
        self.assertEqual(h["a.w"], {"dtype": "F16", "shape": [2, 2], "data_offsets": [0, 8]})
        self.assertEqual(h["b.norm1.weight"], {"dtype": "F32", "shape": [3], "data_offsets": [8, 20]})
        self.assertEqual(h["c.half"], {"dtype": "F16", "shape": [5], "data_offsets": [20, 30]})
        self.assertEqual(h["d.vace"], {"dtype": "F16", "shape": [2], "data_offsets": [30, 34]})

    def test_convert_transformer_fp16(self):
        src = read_safetensors(next((self.model / "transformer").glob("*00001-of-*.safetensors")))
        src.update(read_safetensors(next((self.model / "transformer").glob("*00002-of-*.safetensors"))))
        out = vace.convert_half(self.model / "transformer", vace.half_dir("transformer", self.model),
                                vace.HALF_FILES["transformer"], "fp16", vace.keep_fp32)
        self.assertTrue(vace._shard_complete(out))
        got = read_safetensors(out)
        self.assertEqual(set(got), set(src))
        self.assertEqual(got["blocks.0.attn1.to_q.weight"][0], "F16")
        self.assertEqual(got["vace_blocks.0.proj_out.weight"][0], "F16")
        for kept in ("blocks.0.norm2.weight", "scale_shift_table", "condition_embedder.time_embedder.linear_1.weight",
                     "patch_embedding.bias"):   # fp32 modules + a tensor beyond the fp16 range
            self.assertEqual(got[kept][0], "F32", kept)
            self.assertTrue((got[kept][1] == src[kept][1]).all(), kept)
        import numpy as np
        for name, (_dt, a) in got.items():
            self.assertTrue(np.isfinite(a).all(), name)
            self.assertTrue(np.allclose(a, src[name][1], rtol=1e-3, atol=1e-6), name)
        self.assertTrue((vace.half_dir("transformer", self.model) / "config.json").is_file())
        self.assertFalse(vace.half_dir("transformer", self.model).with_name("transformer-half.partial").exists())
        self.assertEqual(vace.half_ready("transformer", self.model), "F16")
        marker = json.loads((vace.half_dir("transformer", self.model) / vace.HALF_MARKER).read_text())
        self.assertIn("patch_embedding.bias", marker["kept"])
        self.assertEqual(got["vace_blocks.0.bf16"][0], "F16")                 # bf16 VACE blocks -> fp16
        self.assertTrue(np.allclose(got["vace_blocks.0.bf16"][1], src["vace_blocks.0.bf16"][1], rtol=1e-3))

    def test_make_half_copies_idempotent(self):
        self.assertIsNone(vace.half_ready("transformer", self.model))
        done = vace.make_half_copies(self.model, "bf16")
        self.assertEqual(done, {"transformer": "BF16"})
        tr = vace.half_dir("transformer", self.model) / vace.HALF_FILES["transformer"]
        self.assertIn("BF16", {d for d, _ in read_safetensors(tr).values()})
        mtime = tr.stat().st_mtime_ns
        self.assertEqual(vace.make_half_copies(self.model, "bf16"), done)
        self.assertEqual(tr.stat().st_mtime_ns, mtime)                 # nothing rewritten
        self.assertFalse((self.model / "text_encoder-half").exists())   # UMT5 is bf16 already
        # a truncated copy is not "ready" and gets rebuilt
        with open(tr, "r+b") as fh:
            fh.truncate(tr.stat().st_size - 4)
        self.assertIsNone(vace.half_ready("transformer", self.model))
        vace.make_half_copies(self.model, "bf16")
        self.assertEqual(vace.half_ready("transformer", self.model), "BF16")

    def test_half_cli(self):
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(vace._main(["half", "--model", str(self.model), "--dtype", "fp16"]), 0)
        self.assertEqual(vace.half_ready("transformer", self.model), "F16")

    def test_missing_files_ignores_half_copies(self):
        vace.make_half_copies(self.model, "fp16")
        self.assertNotIn("transformer-half", " ".join(vace.missing_files(self.model, deep=False)))


@unittest.skipIf(vace is None, "numpy not available")
class PromptProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-prompt-"))
        self.env = mock.patch.dict("os.environ", {"ZEUS_EFFECTS_HOME": str(self.tmp)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_key_unchanged(self):
        # existing caches (bf16 UMT5, 512 tokens) stay valid
        key = hashlib.sha256(f"{vace.MODEL_REPO}|512|torch.bfloat16|hello".encode()).hexdigest()[:40]
        self.assertEqual(vace._cache_path("hello").name, f"{key}.pt")

    def test_uncached_texts(self):
        self.assertEqual(vace.uncached_texts(["a", "b", "a"]), ["a", "b"])
        p = vace._cache_path("a")
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x")
        self.assertEqual(vace.uncached_texts(["a", "b"]), ["b"])

    def test_encode_command(self):
        cmd = vace.encode_command(Path("/m"), Path("/t.json"), "mps")
        self.assertEqual(cmd[1:4], ["-m", "zfx.engines.vace", "encode"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "/m")
        self.assertEqual(cmd[cmd.index("--texts") + 1], "/t.json")
        self.assertEqual(cmd[cmd.index("--device") + 1], "mps")
        self.assertTrue((vace._effects_dir() / "zfx" / "engines" / "vace.py").is_file())

    def test_prompt_encoding_runs_in_a_separate_process(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        calls = []

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            device = cmd[cmd.index("--device") + 1]
            calls.append(device)
            self.assertEqual(Path(cwd), vace._effects_dir())
            if device == "mps":   # GPU attempt fails -> CPU retry
                return subprocess.CompletedProcess(cmd, 1, "", "MPS backend out of memory")
            for t in json.loads(Path(cmd[cmd.index("--texts") + 1]).read_text()):
                vace._cache_save(t, vace.TEXT_DTYPE, torch.ones(1, vace.MAX_SEQUENCE_LENGTH, 4096))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(vace.subprocess, "run", side_effect=fake_run):
            pos, neg, where = vace.get_prompt_embeds(Path("/m"), "p", "n", "mps")
            self.assertEqual(calls, ["mps", "cpu"])
            self.assertEqual(where, "cpu")
            self.assertEqual(tuple(pos.shape), (1, vace.MAX_SEQUENCE_LENGTH, 4096))
            calls.clear()
            _pos, _neg, where = vace.get_prompt_embeds(Path("/m"), "p", "n", "mps")
            self.assertEqual((calls, where), ([], "cache"))   # cached: no process, no text encoder

    def test_prompt_encoding_failure_message(self):
        with mock.patch.object(vace.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 1, "", "boom")):
            with self.assertRaises(EngineError) as ctx:
                vace.get_prompt_embeds(Path("/m"), "p", "n", "mps")
        self.assertIn("boom", str(ctx.exception))


@unittest.skipIf(vace is None, "numpy not available")
class MpsHelperTests(unittest.TestCase):
    def test_configure_mps_memory(self):
        keys = ("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "PYTORCH_MPS_LOW_WATERMARK_RATIO")
        with mock.patch.dict("os.environ", {}, clear=False):
            for k in keys:
                os.environ.pop(k, None)
            got = vace.configure_mps_memory()
            self.assertEqual(got, {keys[0]: vace.MPS_HIGH_WATERMARK, keys[1]: vace.MPS_LOW_WATERMARK})
        with mock.patch.dict("os.environ", {keys[0]: "0.3"}, clear=False):
            os.environ.pop(keys[1], None)
            got = vace.configure_mps_memory()
            self.assertEqual(got[keys[0]], "0.3")                      # explicit value wins
            self.assertLessEqual(float(got[keys[1]]), 0.3)           # low never above high

    def test_sync_hooks(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        mods = [torch.nn.Linear(2, 2) for _ in range(3)]
        seq = torch.nn.Sequential(*mods)
        synced = []
        with vace.mps_sync_every(mods, every=2, sync=lambda: synced.append(1)) as count:
            seq(torch.ones(1, 2))
            seq(torch.ones(1, 2))
        self.assertEqual(count[0], 6)
        self.assertEqual(len(synced), 3)
        self.assertTrue(all(len(m._forward_hooks) == 0 for m in mods))
        with vace.mps_sync_every(mods, every=0, sync=lambda: synced.append(1)):
            seq(torch.ones(1, 2))
        self.assertEqual(len(synced), 3)

    def test_pick_dtype(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"ZEUS_VACE_DTYPE": ""}):
            self.assertEqual(vace.pick_dtype("mps", Path(d)),
                             torch.float16 if vace.DEFAULT_HALF == "F16" else torch.bfloat16)
            os.environ["ZEUS_VACE_DTYPE"] = "bf16"
            self.assertEqual(vace.pick_dtype("mps", Path(d)), torch.bfloat16)

    def test_conv3d_as_conv2d_matches(self):
        try:
            import torch
            import torch.nn.functional as F
        except ImportError:
            self.skipTest("torch not installed")
        if not torch.backends.mps.is_available():
            self.skipTest("no MPS")
        g = torch.Generator().manual_seed(0)
        x = torch.randn(1, 4, 6, 10, 12, generator=g)
        w = torch.randn(5, 4, 3, 3, 3, generator=g)
        b = torch.randn(5, generator=g)
        ref = F.conv3d(x, w, b, stride=(1, 1, 1), padding=(1, 1, 1))
        with vace.conv3d_as_conv2d():
            got = torch.nn.functional.conv3d(x.to("mps"), w.to("mps"), b.to("mps"), (1, 1, 1), (1, 1, 1)).cpu()
        self.assertEqual(F.conv3d.__name__, "conv3d")
        self.assertLess((ref - got).abs().max().item(), 1e-3)


@unittest.skipIf(vace is None, "numpy not available")
class StreamBlocksTests(unittest.TestCase):
    """Block streaming on the CPU (device "cpu"): same maths, weights read from the file per block."""

    def setUp(self):
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        self.torch = torch
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-stream-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tiny(self):
        torch = self.torch

        class Tiny(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.patch_embedding = torch.nn.Linear(4, 4)
                self.blocks = torch.nn.ModuleList([torch.nn.Linear(4, 4) for _ in range(3)])
                self.vace_blocks = torch.nn.ModuleList([torch.nn.Linear(4, 4)])
                self.scale_shift_table = torch.nn.Parameter(torch.zeros(1, 4))

            def forward(self, x):
                x = self.patch_embedding(x) + self.scale_shift_table
                for b in list(self.blocks) + list(self.vace_blocks):
                    x = b(x)
                return x

        torch.manual_seed(0)
        return Tiny().eval()

    def test_block_read_plan(self):
        m = self.tiny()
        f = self.tmp / "w.safetensors"
        write_safetensors(f, {k: v.detach().numpy() for k, v in m.state_dict().items()})
        plan = vace.block_read_plan(f, ["blocks.0.", "blocks.2.", "vace_blocks.0.", "missing."])
        self.assertEqual(set(plan), {"blocks.0.", "blocks.2.", "vace_blocks.0."})
        start, end, items = plan["blocks.0."]
        self.assertEqual(set(items), {"weight", "bias"})
        self.assertEqual(end - start, (16 + 4) * 4)
        self.assertEqual(items["weight"][1:], ("F32", [4, 4]))

    def test_stream_reads_each_block_from_the_file_and_restores(self):
        torch = self.torch
        m = self.tiny()
        x = torch.randn(2, 4)
        with torch.no_grad():
            want = m(x)
        f = self.tmp / "w.safetensors"
        write_safetensors(f, {k: v.detach().numpy() for k, v in m.state_dict().items()})
        with torch.no_grad():                       # in-memory block weights now differ from the file
            for b in list(m.blocks) + list(m.vace_blocks):
                b.weight.zero_()
        synced = []
        with torch.no_grad(), vace.stream_blocks(m, "cpu", f, sync=lambda: synced.append(1)) as n:
            got = m(x)
        self.assertEqual(n, 4)
        self.assertEqual(len(synced), 4)                                  # one wait per block
        self.assertTrue(torch.allclose(got, want, atol=1e-6))              # file weights were used
        self.assertTrue(all(float(b.weight.abs().sum()) == 0 for b in m.blocks))   # CPU side restored
        self.assertTrue(all(len(b._forward_hooks) == 0 and len(b._forward_pre_hooks) == 0 for b in m.blocks))

    def test_stream_without_file_matches(self):
        torch = self.torch
        m = self.tiny()
        x = torch.randn(2, 4)
        with torch.no_grad():
            want = m(x)
            with vace.stream_blocks(m, "cpu", None, sync=lambda: None):
                got = m(x)
        self.assertTrue(torch.allclose(got, want))

    def test_streaming_switch(self):
        with mock.patch.dict("os.environ", {"ZEUS_VACE_STREAM": ""}):
            os.environ.pop("ZEUS_VACE_STREAM")
            self.assertTrue(vace.streaming_enabled("mps"))
            self.assertFalse(vace.streaming_enabled("cpu"))
            os.environ["ZEUS_VACE_STREAM"] = "0"
            self.assertFalse(vace.streaming_enabled("mps"))


@unittest.skipIf(vace is None, "numpy not available")
class SchedulerTests(unittest.TestCase):
    """UniPC (flow sigmas, Wan config) must converge to the clean sample on an ideal flow model."""

    def trajectory(self, device, wrap):
        try:
            import torch
            from diffusers import UniPCMultistepScheduler
        except ImportError:
            self.skipTest("torch/diffusers not installed")
        if device == "mps" and not torch.backends.mps.is_available():
            self.skipTest("no MPS")
        s = UniPCMultistepScheduler(num_train_timesteps=1000, prediction_type="flow_prediction", use_flow_sigmas=True,
                                    flow_shift=vace.FLOW_SHIFT, solver_order=2, final_sigmas_type="zero")
        s.set_timesteps(20, device=device)
        g = torch.Generator().manual_seed(0)
        x0 = torch.randn(1, 16, 3, 8, 8, generator=g)
        lat = torch.randn(1, 16, 3, 8, 8, generator=g).to(device)
        ctx = vace.scheduler_on_cpu(s) if wrap else contextlib.nullcontext()
        with ctx:
            for i, t in enumerate(s.timesteps):
                v = (lat - x0.to(device)) / s.sigmas[i].to(device)     # ideal flow prediction
                lat = s.step(v.to(torch.float16), t, lat, return_dict=False)[0]
            self.assertEqual(lat.device.type, device)
        self.assertFalse("step" in s.__dict__)                          # patch removed
        return (lat.float().cpu() - x0).abs().mean().item()

    def test_cpu_reference_converges(self):
        self.assertLess(self.trajectory("cpu", False), 1e-3)

    def test_mps_latents_converge_with_cpu_step(self):
        self.assertLess(self.trajectory("mps", True), 1e-3)


@unittest.skipIf(vace is None, "numpy not available")
class ConditioningTests(unittest.TestCase):
    def test_gray_control_only_inside_the_mask(self):
        import numpy as np
        ctrl = np.zeros((2, 4, 4, 3), dtype=np.uint8)
        ctrl[..., 0], ctrl[..., 1], ctrl[..., 2] = 250, 140, 20          # inferno-ish orange everywhere
        msk = np.zeros((2, 4, 4), dtype=np.uint8)
        msk[:, :2] = 255
        out = vace.gray_control(ctrl, msk)
        luma = round(0.299 * 250 + 0.587 * 140 + 0.114 * 20)
        self.assertTrue((out[:, :2] == luma).all())                      # person: grey depth
        self.assertTrue((out[:, 2:] == ctrl[:, 2:]).all())               # background untouched
        # inferno lightness rises with depth, so the order survives
        dark, bright = np.array([[[[40, 10, 80]]]], np.uint8), np.array([[[[250, 220, 60]]]], np.uint8)
        m = np.full((1, 1, 1), 255, np.uint8)
        self.assertLess(int(vace.gray_control(dark, m)[0, 0, 0, 0]), int(vace.gray_control(bright, m)[0, 0, 0, 0]))
        # with the original frames, the depth halo outside the mask is replaced by the original background
        work = np.full_like(ctrl, 7)
        out = vace.gray_control(ctrl, msk, work)
        self.assertTrue((out[:, 2:] == 7).all())
        self.assertTrue((out[:, :2] == luma).all())
        self.assertTrue((vace.gray_control(ctrl, msk, work[:1]) == vace.gray_control(ctrl, msk)).all())  # bad shape

    def test_cutout_on_white(self):
        import numpy as np
        rgb = np.full((100, 80, 3), 30, dtype=np.uint8)
        person = np.zeros((100, 80), dtype=np.float32)
        person[40:70, 30:50] = 1.0
        cut = vace.cutout_on_white(rgb, person)
        self.assertLess(cut.shape[0], 100)
        self.assertLess(cut.shape[1], 80)
        self.assertEqual(int(cut[0, 0, 0]), 255)                         # white around the person
        self.assertEqual(int(cut[cut.shape[0] // 2, cut.shape[1] // 2, 0]), 30)
        self.assertIsNone(vace.cutout_on_white(rgb, np.zeros((100, 80), np.float32)))

    def test_load_refs_cutout_and_fallback(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            ref = Path(d) / "me.png"
            Image.fromarray(np.full((60, 40, 3), 30, np.uint8)).save(ref)

            def seg(rgb):
                m = np.zeros(rgb.shape[:2], np.float32)
                m[20:40, 10:30] = 1.0
                return m

            img = vace._load_refs([ref], segment=seg, cutout=True)[0]
            self.assertLess(img.size, (40, 60))
            self.assertEqual(img.getpixel((0, 0)), (255, 255, 255))
            raw = vace._load_refs([ref], segment=lambda rgb: np.zeros(rgb.shape[:2], np.float32), cutout=True)[0]
            self.assertEqual(raw.size, (40, 60))                           # no person found: photo as is

            def boom(_rgb):
                raise RuntimeError("vision failed")

            self.assertEqual(vace._load_refs([ref], segment=boom, cutout=True)[0].size, (40, 60))
            self.assertEqual(vace._load_refs([ref], segment=seg, cutout=False)[0].size, (40, 60))
            with self.assertRaises(EngineError):
                vace._load_refs([Path(d) / "missing.png"], cutout=False)

    def test_job_mode(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(vace.job_mode(Path(d)), "depth")
            (Path(d) / "job.json").write_text(json.dumps({"mode": "mesh"}))
            self.assertEqual(vace.job_mode(Path(d)), "mesh")


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

    def test_diffusers_call_uses_the_chunked_path(self):
        """diffusers' native backend passes enable_gqa=False: that must not fall back to MPS SDPA."""
        try:
            import torch
            import torch.nn.functional as F
        except ImportError:
            self.skipTest("torch not installed")
        if not torch.backends.mps.is_available():
            self.skipTest("no MPS")
        g = torch.Generator().manual_seed(1)
        q, k, v = (torch.randn(1, 2, 300, 16, generator=g).to("mps", torch.float16) for _ in range(3))
        full = F.scaled_dot_product_attention(q.float(), k.float(), v.float())
        calls = []
        real = F.scaled_dot_product_attention
        with mock.patch.object(F, "scaled_dot_product_attention",
                               side_effect=lambda *a, **kw: calls.append(1) or real(*a, **kw)):
            with vace.chunked_attention(budget_mb=1):
                out = torch.nn.functional.scaled_dot_product_attention(
                    query=q, key=k, value=v, attn_mask=None, dropout_p=0.0, is_causal=False, scale=None,
                    enable_gqa=False)
        self.assertEqual(calls, [])                                   # never reached PyTorch's SDPA
        self.assertLess((full - out.float()).abs().max().item(), 5e-3)
        self.assertEqual(vace.attention_chunk_rows(1, 12, 5520, 5520, 2, 512), 4052)
        self.assertEqual(vace.attention_chunk_rows(1, 12, 100, 100, 2, 512), 100)


if __name__ == "__main__":
    unittest.main()
