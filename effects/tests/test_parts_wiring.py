"""The parts map wired into the engine (effects/PARTS.md, 4.0): `prepare --parts` / mode depth_parts run
zfx.parts.run_parts after the composite (every heavy step faked here), the review hashes the five new artifacts,
the vace engine follows control-parts.mp4 for a depth_parts job or `--control parts`, Make it me's `--parts`,
the `parts` command and the doctor's clipseg / faceparse checks.

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_parts_wiring.py' -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import synth
import zeus_effects
from zfx import MESH_MODES, MODES, PARTS_MODES, POSE_MODES
from zfx import makeme as M
from zfx import parts_cli
from zfx.engines import EngineError, vace
from zfx.jobs import ARTIFACT_FILES, Job
from zfx.parts import PartsError
from zfx.progress import Progress
from zfx.review import OPTIONAL_REVIEWED, ReviewError, approve, required_artifacts, verify

PARTS_NAMES = ("parts", "parts_index", "parts_json", "control_parts", "preview_parts")
PARTS_FILES = ("parts.mp4", "parts.npz", "parts.json", "control-parts.mp4", "preview-parts.jpg")


def _touch(path, data: bytes = b"x") -> Path:
    path = Path(path)
    path.write_bytes(data)
    return path


class FakeSteps:
    """Stand-ins for every heavy step cmd_prepare runs (input, depth, masks, mesh, pose, composite, parts): each
    writes the small file the real one would and records that it ran; run_parts records how it was called."""

    def __init__(self, parts_error: Exception | None = None):
        self.steps: list[str] = []
        self.parts_calls: list[dict] = []
        self.parts_error = parts_error

    def prepare_input(self, input_ref, start, duration, job_dir, cookies, say):
        job_dir = Path(job_dir)
        for name in ("source.mp4", "work.mp4", "audio.m4a"):
            _touch(job_dir / name, name.encode())
        say("trim", 1.0, "trimmed")
        self.steps.append("input")
        return {"source": {"fps": 30.0, "fps_rational": "30/1", "width": 640, "height": 360, "duration": 3.0},
                "audio": str(job_dir / "audio.m4a"), "work": {"width": 640, "height": 360, "frames": 48},
                "timings": {"trim": 0.1, "normalize": 0.1}}

    def run_depth(self, work, out, report, device=None, input_size=None):
        _touch(out, b"depth")
        self.steps.append("depth")
        return {"device": "cpu", "frames": 48}

    def run_masks(self, work, out, report, quality=None, min_person_ratio=None):
        _touch(out, b"mask")
        self.steps.append("masks")
        return {"person_frames": 48, "frames": 48}

    def detect_landmarks(self, work, report, mask_path=None, **_):
        self.steps.append("mesh")
        return {"faces_found": 48, "frames": [None] * 48, "search": "person_crop", "crop": None}

    def write_landmarks(self, data, path):
        return _touch(path, b"{}")

    def detect_motion(self, work, report, mask_path=None, face=None, source=None):
        self.steps.append("pose")
        return {"fake": True}

    def write_motion(self, motion, path):
        return _touch(path, b"{}")

    def write_pose_video(self, motion, path, report):
        _touch(path, b"pose")
        return {"frames": 48, "frames_with_body": 48}

    def write_bvh(self, motion, path):
        _touch(path, b"HIERARCHY")
        return {"frames": 48, "joints": 19, "channels": 60, "held_frames": 0, "root": "Hips"}

    def summarize(self, motion):
        return {"frames": 48, "body_frames": 48, "left_hand_frames": 10, "right_hand_frames": 12, "face_frames": 48,
                "average_visibility": 0.9, "limbs_tracked": {"left_arm": 1.0}, "search": "person_crop"}

    def build_control(self, work, depth, mask, out, mode, report, landmarks=None, preview_path=None, motion=None):
        _touch(out, b"control")
        if preview_path:
            _touch(preview_path, b"jpg")
        self.steps.append("composite")
        return {"frames": 48, "mode": mode, "preview_columns": ["work", "depth", "mask", "control"]}

    def run_parts(self, job, things=(), gaze=False, over_person=False, device=None, report=None, **_):
        self.parts_calls.append({"things": list(things), "gaze": gaze, "over_person": over_person, "device": device,
                                 "status": job.get("status"), "control_first": job.artifact("control") is not None})
        self.steps.append("parts")
        if self.parts_error is not None:
            raise self.parts_error
        if report:
            report("load", 0.5, "reading work.mp4")
            report("render", 1.0, "parts.mp4")
        for name, file in zip(PARTS_NAMES, PARTS_FILES):
            job.set_artifact(name, _touch(job.dir / file, file.encode()), save=False)
        things = list(things)
        warnings = ["'cap' was not found in any frame (CLIPSeg below 0.4)."] if "cap" in things else []
        job["parts_info"] = {"frames": 48, "classes": {"torso": 3.4, "head": 0.7},
                             "things": {t: {"frames": 48, "rate": 1.0} for t in things}, "things_prompt": ", ".join(things),
                             "gaze": {"directions": {"centre": 48}, "blink_frames": 2} if gaze else None,
                             "warnings": warnings}
        job.save()
        return {"id": job.id, "classes": {"torso": 3.4, "head": 0.7}, "warnings": warnings, "timings": {"total": 1.5}}

    def patches(self) -> list:
        return [mock.patch("zfx.io.prepare_input", self.prepare_input),
                mock.patch("zfx.depth.run_depth", self.run_depth),
                mock.patch("zfx.masks.run_masks", self.run_masks),
                mock.patch("zfx.mesh.detect_landmarks", self.detect_landmarks),
                mock.patch("zfx.mesh.write_landmarks", self.write_landmarks),
                mock.patch("zfx.pose.detect_motion", self.detect_motion),
                mock.patch("zfx.pose.write_motion", self.write_motion),
                mock.patch("zfx.pose.write_pose_video", self.write_pose_video),
                mock.patch("zfx.pose.summarize", self.summarize),
                mock.patch("zfx.bvh.write_bvh", self.write_bvh),
                mock.patch("zfx.composite.build_control", self.build_control),
                mock.patch("zfx.parts.run_parts", self.run_parts)]


def prepare_args(home: Path, **overrides) -> SimpleNamespace:
    """What build_parser gives cmd_prepare, with every option at its default."""
    clip = _touch(home / "in.mp4", b"clip")
    base = dict(input=str(clip), start=0.0, duration=3.0, mode="depth_parts", job=None, cookies=None, device=None,
                depth_size=zeus_effects.DEPTH_INPUT_SIZE, mask_quality="accurate", min_person_ratio=0.25,
                motion=False, parts=False, things="", gaze=False, things_over_person=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def run_prepare(fakes: FakeSteps, args: SimpleNamespace):
    out = io.StringIO()
    progress = Progress(stream=out)
    with contextlib.ExitStack() as stack:
        for patch in fakes.patches():
            stack.enter_context(patch)
        code = zeus_effects.cmd_prepare(args, progress)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


# --------------------------------------------------------------------------- tables and the parser
class TablesTests(unittest.TestCase):
    def test_depth_parts_is_a_pose_and_mesh_mode(self):
        self.assertIn("depth_parts", MODES)
        self.assertEqual(PARTS_MODES, ("depth_parts",))
        self.assertIn("depth_parts", POSE_MODES)
        self.assertIn("depth_parts", MESH_MODES)

    def test_artifacts_and_review_know_the_five_files(self):
        for name, file in zip(PARTS_NAMES, PARTS_FILES):
            self.assertEqual(ARTIFACT_FILES[name], file)
            self.assertIn(name, OPTIONAL_REVIEWED)
        job = Job(Path("/tmp/none"), {"artifacts": {}})
        self.assertEqual(job.path("control_parts").name, "control-parts.mp4")

    def test_parser_takes_the_parts_flags(self):
        p = zeus_effects.build_parser()
        a = p.parse_args(["prepare", "--input", "x.mp4", "--mode", "depth_parts", "--things", "laptop, cap", "--gaze",
                          "--parts", "--things-over-person"])
        self.assertEqual((a.mode, a.things, a.gaze, a.parts, a.things_over_person),
                         ("depth_parts", "laptop, cap", True, True, True))
        a = p.parse_args(["prepare", "--input", "x.mp4", "--mode", "depth"])
        self.assertEqual((a.things, a.gaze, a.parts, a.things_over_person), ("", False, False, False))
        a = p.parse_args(["parts", "--job", "j", "--things", "a, b", "--gaze", "--no-preview"])
        self.assertEqual((a.command, a.job, a.things, a.gaze, a.no_preview, a.no_things), ("parts", "j", "a, b", True, True, False))
        self.assertIn("parts", zeus_effects.COMMANDS)
        a = p.parse_args(["generate", "--job", "j", "--engine", "vace", "--control", "parts"])
        self.assertEqual(a.control, "parts")
        self.assertEqual(p.parse_args(["generate", "--job", "j", "--engine", "vace"]).control, "auto")
        a = p.parse_args(["makeme", "--input", "x.mp4", "--ref", "a.jpg", "--parts", "--consent"])
        self.assertTrue(a.parts)
        self.assertFalse(p.parse_args(["makeme", "--input", "x.mp4", "--ref", "a.jpg"]).parts)

    def test_overall_fraction_runs_through_the_sub_stages(self):
        self.assertEqual(parts_cli.overall_fraction("load", 0.0), 0.0)
        self.assertEqual(parts_cli.overall_fraction("preview", 1.0), 1.0)
        last = -1.0
        for stage in parts_cli.STAGE_WEIGHTS:
            for frac in (0.0, 0.5, 1.0):
                value = parts_cli.overall_fraction(stage, frac)
                self.assertGreaterEqual(value, last)
                last = value
        self.assertEqual(parts_cli.overall_fraction("elsewhere", 0.3), 0.3)


# --------------------------------------------------------------------------- prepare plumbing (fake run_parts)
class PrepareFlagTests(unittest.TestCase):
    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def test_depth_parts_runs_the_map_after_the_composite_with_the_gaze(self):
        fakes = FakeSteps()
        code, lines = run_prepare(fakes, prepare_args(self.home, mode="depth_parts", things="laptop, Cap"))
        self.assertEqual(code, 0)
        self.assertEqual(fakes.steps, ["input", "depth", "masks", "mesh", "pose", "composite", "parts"])
        self.assertEqual(fakes.parts_calls, [{"things": ["laptop", "cap"], "gaze": True, "over_person": False,
                                              "device": None, "status": "preparing", "control_first": True}])
        done = lines[-1]
        self.assertTrue(done["done"])
        result = done["result"]
        self.assertEqual(result["mode"], "depth_parts")
        for name, file in zip(PARTS_NAMES, PARTS_FILES):
            self.assertEqual(Path(result["artifacts"][name]).name, file)
        self.assertEqual(result["parts"]["classes"], {"torso": 3.4, "head": 0.7})
        self.assertEqual(result["parts"]["things_prompt"], "laptop, cap")
        self.assertEqual(result["warnings"], ["parts: 'cap' was not found in any frame (CLIPSeg below 0.4)."])
        self.assertIn("preview-parts.jpg", result["next"])
        self.assertIn("parts", result["timings"])
        # the progress lines: the parts stage comes last, its overall share grows and stays inside [0, 1]
        stages = [ln["stage"] for ln in lines if "stage" in ln and "done" not in ln]
        self.assertEqual(stages[-1], "parts")
        self.assertLess(stages.index("composite"), stages.index("parts"))
        parts_lines = [ln for ln in lines if ln.get("stage") == "parts"]
        self.assertGreaterEqual(len(parts_lines), 3)
        overall = [ln["overall"] for ln in parts_lines]
        self.assertEqual(overall, sorted(overall))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in overall))
        self.assertEqual(overall[-1], 1.0)
        self.assertIn("load: reading work.mp4", [ln["message"] for ln in parts_lines])
        # job.json: prepared, the map recorded, the approval hashes it (required in this mode)
        job = Job.load(done["job"])
        self.assertEqual(job["status"], "prepared")
        self.assertEqual(job["parts_info"]["gaze"]["directions"], {"centre": 48})
        self.assertEqual(job.get("warnings"), result["warnings"])
        self.assertTrue(set(PARTS_NAMES[:4]) <= set(required_artifacts(job)))
        review = approve(job)
        for name in PARTS_NAMES:
            self.assertIn(name, review["hashes"])
        verify(Job.load(job.dir))
        job.path("control_parts").write_bytes(b"re-rendered after the review")
        with self.assertRaises(ReviewError) as ctx:
            verify(Job.load(job.dir))
        self.assertIn("control-parts.mp4 changed", str(ctx.exception))

    def test_parts_flag_on_a_depth_mode_without_the_gaze(self):
        fakes = FakeSteps()
        code, lines = run_prepare(fakes, prepare_args(self.home, mode="depth_mesh", parts=True, things="", device="cpu",
                                                      things_over_person=True))
        self.assertEqual(code, 0)
        self.assertEqual(fakes.steps, ["input", "depth", "masks", "mesh", "composite", "parts"])   # no pose step: zfx.parts computes it
        self.assertEqual(fakes.parts_calls[0]["things"], [])
        self.assertFalse(fakes.parts_calls[0]["gaze"])
        self.assertTrue(fakes.parts_calls[0]["over_person"])
        self.assertEqual(fakes.parts_calls[0]["device"], "cpu")
        job = Job.load(lines[-1]["job"])
        self.assertEqual(job.mode, "depth_mesh")
        self.assertFalse(set(PARTS_NAMES) & set(required_artifacts(job)))     # optional outside depth_parts
        review = approve(job)
        for name in PARTS_NAMES:
            self.assertIn(name, review["hashes"])                                 # but hashed when present
        code, lines = run_prepare(fakes, prepare_args(self.home, mode="depth_mesh", parts=True, gaze=True))
        self.assertTrue(fakes.parts_calls[1]["gaze"])

    def test_without_the_flag_nothing_changes(self):
        fakes = FakeSteps()
        code, lines = run_prepare(fakes, prepare_args(self.home, mode="depth_pose"))
        self.assertEqual(code, 0)
        self.assertEqual(fakes.parts_calls, [])
        self.assertNotIn("parts", [ln.get("stage") for ln in lines])
        result = lines[-1]["result"]
        self.assertNotIn("parts", result)
        self.assertNotIn("preview-parts", result["next"])
        self.assertFalse(set(PARTS_NAMES) & set(result["artifacts"]))

    def test_parts_need_a_depth_mode_and_at_most_24_things(self):
        fakes = FakeSteps()
        with self.assertRaises(zeus_effects.CLIError) as ctx:
            run_prepare(fakes, prepare_args(self.home, mode="pose", parts=True))
        self.assertEqual(ctx.exception.stage, "start")
        self.assertIn("depth mode", str(ctx.exception))
        with self.assertRaises(zeus_effects.CLIError) as ctx:
            run_prepare(fakes, prepare_args(self.home, mode="depth_parts", things=", ".join(f"t{i}" for i in range(25))))
        self.assertEqual(ctx.exception.stage, "start")
        self.assertIn("24", str(ctx.exception))
        self.assertEqual(fakes.steps, [])

    def test_a_failing_map_marks_the_job(self):
        fakes = FakeSteps(parts_error=PartsError("The face parser failed (exit 1): boom"))
        with self.assertRaises(zeus_effects.CLIError) as ctx:
            run_prepare(fakes, prepare_args(self.home, mode="depth_parts"))
        self.assertEqual(ctx.exception.stage, "parts")
        self.assertIn("face parser", str(ctx.exception))
        jobs = [p for p in (self.home / "Jobs").iterdir() if (p / "job.json").is_file()]
        self.assertEqual(len(jobs), 1)
        job = Job.load(jobs[0])
        self.assertEqual((job["status"], job["failed_stage"]), ("failed", "parts"))
        self.assertTrue(job.artifact("control") is not None)      # the composite before it is kept


# --------------------------------------------------------------------------- review
def fake_job(mode: str, parts: bool, audio: bool = False) -> Job:
    job = Job.create("/tmp/in.mp4", 1.0, 2.0, mode)
    files = {"source": b"src", "work": b"work", "depth": b"depth", "mask": b"mask", "control": b"control",
             "preview": b"jpg", "landmarks": b"{}", "motion": b"{}", "pose": b"pose", "bvh": b"HIERARCHY"}
    if audio:
        files["audio"] = b"aac"
    if parts:
        for name, file in zip(PARTS_NAMES, PARTS_FILES):
            files[name] = file.encode()
    for name, data in files.items():
        job.set_artifact(name, _touch(job.path(name), data), save=False)
    job["has_audio"] = audio
    job["status"] = "prepared"
    job.save()
    return job


class ReviewPartsTests(unittest.TestCase):
    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def test_depth_parts_needs_the_map_to_be_approved(self):
        job = fake_job("depth_parts", parts=False)
        names = required_artifacts(job)
        for name in ("parts", "parts_index", "parts_json", "control_parts", "motion", "pose", "landmarks", "depth"):
            self.assertIn(name, names)
        self.assertNotIn("preview_parts", names)        # the contact sheet is optional
        with self.assertRaises(ReviewError) as ctx:
            approve(job)
        self.assertIn("control_parts (control-parts.mp4)", str(ctx.exception))
        job = fake_job("depth_parts", parts=True)
        review = approve(job)
        self.assertEqual(review["mode"], "depth_parts")
        for name, file in zip(PARTS_NAMES, PARTS_FILES):
            self.assertIn(name, review["hashes"])
            self.assertEqual(review["files"][name], file)
        verify(Job.load(job.dir))
        job.path("parts_index").write_bytes(b"another index")
        with self.assertRaises(ReviewError) as ctx:
            verify(Job.load(job.dir))
        self.assertIn("parts.npz changed", str(ctx.exception))

    def test_map_present_before_approval_is_hashed_in_any_mode(self):
        job = fake_job("depth", parts=True)
        self.assertFalse(set(PARTS_NAMES) & set(required_artifacts(job)))
        review = approve(job)
        for name in PARTS_NAMES:
            self.assertIn(name, review["hashes"])
        job.path("preview_parts").unlink()
        with self.assertRaises(ReviewError) as ctx:
            verify(Job.load(job.dir))
        self.assertIn("preview-parts.jpg is missing", str(ctx.exception))

    def test_map_made_after_approval_leaves_the_approval_valid(self):
        job = fake_job("depth_pose", parts=False, audio=True)
        review = approve(job)
        self.assertFalse(set(PARTS_NAMES) & set(review["hashes"]))
        for name, file in zip(PARTS_NAMES, PARTS_FILES):        # what the parts command adds: new files only
            job.set_artifact(name, _touch(job.path(name), file.encode()), save=False)
        job.save()
        verify(Job.load(job.dir))


# --------------------------------------------------------------------------- vace: which control it follows
class VaceControlTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="zfx-vace-control-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _job(self, mode: str, parts: bool, artifacts: dict | None = None) -> Path:
        if parts:
            _touch(self.dir / "control-parts.mp4", b"parts control")
        _touch(self.dir / "control.mp4", b"control")
        data = {"id": "j", "mode": mode, "artifacts": artifacts if artifacts is not None else
                ({"control_parts": "control-parts.mp4"} if parts else {})}
        (self.dir / "job.json").write_text(json.dumps(data))
        return self.dir

    def test_an_explicit_path_wins(self):
        self._job("depth_parts", parts=True)
        self.assertEqual(vace.control_video(self.dir, control="/x/own.mp4", kind="parts"), (Path("/x/own.mp4"), None))

    def test_auto_follows_the_mode(self):
        self._job("depth_parts", parts=True)
        self.assertEqual(vace.control_video(self.dir), (self.dir / "control-parts.mp4", "as-is"))
        self.assertEqual(vace.control_video(self.dir, kind="auto"), (self.dir / "control-parts.mp4", "as-is"))
        self.assertEqual(vace.control_video(self.dir, kind="control"), (self.dir / "control.mp4", None))
        self._job("depth_pose", parts=True)
        self.assertEqual(vace.control_video(self.dir), (self.dir / "control.mp4", None))
        self.assertEqual(vace.control_video(self.dir, kind="parts"), (self.dir / "control-parts.mp4", "as-is"))
        self._job("depth", parts=False)
        self.assertEqual(vace.control_video(self.dir), (self.dir / "control.mp4", None))

    def test_recorded_artifact_name_is_followed(self):
        self._job("depth", parts=False, artifacts={"control_parts": "maps/tones.mp4"})
        (self.dir / "maps").mkdir()
        _touch(self.dir / "maps" / "tones.mp4", b"tones")
        self.assertEqual(vace.control_video(self.dir, kind="parts"), (self.dir / "maps" / "tones.mp4", "as-is"))
        self.assertEqual(vace.job_artifact(self.dir, "control_parts"), self.dir / "maps" / "tones.mp4")
        self.assertIsNone(vace.job_artifact(self.dir, "nothing"))

    def test_missing_map_and_unknown_kind_are_plain_errors(self):
        self._job("depth_parts", parts=False)
        with self.assertRaises(EngineError) as ctx:
            vace.control_video(self.dir)
        self.assertIn("control-parts.mp4 is missing", str(ctx.exception))
        self.assertIn("parts --job", str(ctx.exception))
        with self.assertRaises(EngineError):
            vace.control_video(self.dir, kind="tones")

    def test_run_takes_the_control_kind(self):
        """run() asks control_video before anything heavy; a missing map stops it with the plain error."""
        self._job("depth_parts", parts=False)
        with mock.patch.object(vace, "check_model", return_value=self.dir):
            with self.assertRaises(EngineError) as ctx:
                vace.run(self.dir, [self.dir / "ref.png"], self.dir / "out.mp4", control_kind="parts")
        self.assertIn("control-parts.mp4 is missing", str(ctx.exception))
        import inspect
        params = inspect.signature(vace.run).parameters
        self.assertIn("control_kind", params)
        self.assertIsNone(params["control_kind"].default)


class GenerateControlTests(unittest.TestCase):
    """`generate --control` reaches the vace engine as control_kind (recorded in the generation), never faceswap."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-generate-control-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_control_kind_reaches_vace_only(self):
        import shutil
        from test_engines import FakeEngine, args as gen_args, make_job, make_ref
        from zfx import generate

        class Spy(FakeEngine):
            def __init__(self, name):
                super().__init__()
                self.NAME = name
                self.options: list = []

            def run(self, job_dir, refs, out_path, progress, **options):
                self.options.append(options)
                return super().run(job_dir, refs, out_path, progress, **options)

        def fake_mux(raw, source, audio, final, engine, **_):
            shutil.copyfile(raw, final)
            return {"fake": True}

        job = make_job(self.tmp)
        ref = str(make_ref(self.tmp))
        for engine_name, control, expected in (("vace", "parts", "parts"), ("vace", "control", "control"),
                                               ("vace", "auto", None), ("vace", None, None), ("faceswap", "parts", None)):
            spy = Spy(engine_name)
            with mock.patch.object(generate, "get_engine", return_value=spy), \
                    mock.patch.object(generate, "prepare_color", return_value={}), \
                    mock.patch.object(generate, "mux_final", fake_mux):
                generate.run_generate(gen_args(engine=engine_name, ref=[ref], control=control), job, lambda *a, **k: None)
            self.assertEqual(spy.options[0].get("control_kind"), expected, (engine_name, control))
            record = json.loads((job / "job.json").read_text())["generations"][-1]
            self.assertEqual(record["engine"], engine_name)
            self.assertEqual(record["options"].get("control_kind"), expected, (engine_name, control))


# --------------------------------------------------------------------------- make it me
class MakeMePartsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-makeme-parts-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _opts(self, **kw) -> M.Options:
        return M.Options(input=str(_touch(self.tmp / "clip.mp4", b"clip")), consent=True, refs=["a.jpg"], **kw)

    def test_option_is_off_by_default_and_only_changes_the_hash_when_on(self):
        ns = SimpleNamespace(input="c.mp4", start=0, duration=None, ref_pack=None, self_video=None, ref=["a.jpg"],
                             voice=None, no_voice=False, quality="natural", hat="keep", consent=True, job=None,
                             hair=None, seed=None, person=None, cookies=None, enhance=False, swapjob=None)
        self.assertFalse(M.Options.from_args(ns).parts)                 # an older caller without the attribute
        ns.parts = True
        opts = M.Options.from_args(ns)
        self.assertTrue(opts.parts)
        self.assertTrue(opts.public()["parts"])
        off, on = self._opts(), self._opts(parts=True)
        ctx_off = M.Context(job_dir=self.tmp, opts=off, progress=Progress(stream=io.StringIO()), state={"duration": 3.0})
        ctx_on = M.Context(job_dir=self.tmp, opts=on, progress=Progress(stream=io.StringIO()), state={"duration": 3.0})
        self.assertNotEqual(M._prepare_inputs(ctx_off), M._prepare_inputs(ctx_on))
        src = M.file_digest(self.tmp / "clip.mp4")
        self.assertEqual(M._prepare_inputs(ctx_off), M.digest("prepare", src, 0.0, 3.0, M.PREPARE_MODE))  # as before 4.0

    def test_prepare_step_passes_parts_to_cmd_prepare(self):
        seen: list = []

        def fake_prepare(ns, reporter):
            seen.append(ns)
            for name in ("source.mp4", "work.mp4", "mask.mp4", "control.mp4", "preview.jpg", "job.json") + PARTS_FILES:
                _touch(self.tmp / name, name.encode())
            reporter.update("composite", 1.0, "control.mp4")
            reporter.done(self.tmp, {"frames": 48, "fps": 30.0, "size": [640, 360], "duration": 3.0, "has_audio": True,
                                     "timings": {"parts": 1.5}, "warnings": ["No person found"],
                                     "parts": {"classes": {"torso": 3.4}, "body_frames": 48, "face_frames": 48,
                                               "face_detail_frames": 0, "things": {}, "gaze": None,
                                               "warnings": ["Face detail skipped: no venv"]}})
            return 0

        ctx = M.Context(job_dir=self.tmp, opts=self._opts(parts=True), progress=Progress(stream=io.StringIO()),
                        state={"duration": 3.0})
        reporter = M.StepReporter(ctx.progress, "prepare", 0.0, 0.1)
        with mock.patch.object(zeus_effects, "cmd_prepare", fake_prepare):
            outputs, info, skipped = M._prepare_run(ctx, reporter)
        ns = seen[0]
        self.assertEqual((ns.mode, ns.parts, ns.things, ns.gaze, ns.things_over_person), ("depth_mesh", True, "", False, False))
        self.assertFalse(skipped)
        self.assertTrue({self.tmp / f for f in PARTS_FILES} <= set(outputs))
        self.assertEqual(info["parts"]["classes"], {"torso": 3.4})
        self.assertEqual(info["parts"]["warnings"], ["Face detail skipped: no venv"])
        self.assertTrue(info["message"].endswith("with the parts map"))
        self.assertEqual(ctx.warnings, ["prepare: No person found"])
        # off: no parts files among the outputs, no parts in the info, the message as before
        ctx = M.Context(job_dir=self.tmp, opts=self._opts(), progress=Progress(stream=io.StringIO()), state={"duration": 3.0})
        with mock.patch.object(zeus_effects, "cmd_prepare", fake_prepare):
            outputs, info, _ = M._prepare_run(ctx, M.StepReporter(ctx.progress, "prepare", 0.0, 0.1))
        self.assertFalse(seen[1].parts)
        self.assertFalse({self.tmp / f for f in PARTS_FILES} & set(outputs))
        self.assertNotIn("parts", info)
        self.assertFalse(info["message"].endswith("with the parts map"))


# --------------------------------------------------------------------------- the parts command and the doctor
class PartsCommandTests(unittest.TestCase):
    def test_parts_subcommand_reports_like_the_others(self):
        with synth.TempHome():
            job = Job.create("/tmp/in.mp4", 1.0, 2.0, "depth")           # prepared nothing: no work/mask/depth
            proc = subprocess.run([sys.executable, str(synth.CLI), "parts", "--job", str(job.dir), "--no-preview"],
                                  capture_output=True, text=True, timeout=120, env=dict(os.environ))
            self.assertEqual(proc.returncode, 1, proc.stderr)
            last = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(last["stage"], "parts")
            self.assertIn("missing", last["error"])
            proc = subprocess.run([sys.executable, str(synth.CLI), "parts", "--job", "/nonexistent/job"],
                                  capture_output=True, text=True, timeout=120, env=dict(os.environ))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("not found", json.loads(proc.stdout.strip().splitlines()[-1])["error"])

    def test_run_with_a_fake_map(self):
        """parts_cli.run (what cmd_parts calls) with a stand-in run_parts: the done line and the summary."""
        with synth.TempHome():
            job = Job.create("/tmp/in.mp4", 1.0, 2.0, "depth")
            out = io.StringIO()
            args = parts_cli.build_parser().parse_args(["--job", str(job.dir), "--things", "cup", "--gaze"])
            calls: list = []

            def fake_run_parts(job, **kw):
                calls.append(kw)
                kw["report"]("render", 1.0, "parts.mp4")
                return {"id": job.id, "frames": 4, "size": [8, 8], "fps": 16, "person_coverage": 0.1,
                        "body_frames": 4, "face_frames": 4, "face_detail_frames": 0, "classes": {}, "things": {},
                        "gaze": None, "timings": {"total": 0.1}, "warnings": [], "files": {}}

            with mock.patch("zfx.parts.run_parts", fake_run_parts):
                code = parts_cli.run(args, Progress(stream=out))
            self.assertEqual(code, 0)
            self.assertEqual((calls[0]["things"], calls[0]["gaze"]), (["cup"], True))
            lines = [json.loads(ln) for ln in out.getvalue().splitlines()]
            self.assertEqual(lines[0]["stage"], "render")
            self.assertEqual(lines[0]["overall"], 1.0 - parts_cli.STAGE_WEIGHTS["preview"])
            self.assertTrue(lines[-1]["done"])
            self.assertEqual(lines[-1]["result"]["frames"], 4)


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_clipseg_and_the_face_parser(self):
        with synth.TempHome() as home:
            report = zeus_effects.doctor_report()
        weights, engines = report["weights"], report["engines"]
        self.assertEqual(weights["clipseg"]["path"], str(home / "models" / "clipseg-rd64-refined"))
        self.assertFalse(weights["clipseg"]["present"])
        self.assertEqual(Path(weights["faceparse"]["path"]).name, "bisenet_resnet_34.onnx")
        self.assertFalse(weights["faceparse"]["present"])
        self.assertIn("setup", weights["faceparse"]["detail"])
        for key in ("parts", "parts_face_detail", "parts_things"):
            self.assertIsInstance(engines[key], bool)
        self.assertFalse(engines["parts_things"])
        self.assertFalse(engines["parts_face_detail"])
        self.assertEqual(engines["parts"], engines["pose"])


if __name__ == "__main__":
    unittest.main()
