"""Make it me without weights: the self-video frame selection on synthetic measurements, the hat skip rule, the step
planner and resume (markers), the makeme options, cancel on a finished job.

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_makeme.py' -v
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import synth  # noqa: F401  (throwaway ZEUS_EFFECTS_HOME, sys.path)
from zfx import makeme as M
from zfx import refs as R
from zfx.engines import hat as H
from zfx.progress import Progress


def measure(index, yaw, pitch, ear=0.3, sharp=200.0, mouth=0.6, time=None):
    return R.Measure(index=index, time=time if time is not None else index / 2.0, yaw=yaw, pitch=pitch, ear=ear,
                     sharpness=sharp, mouth_ratio=mouth, width=1080, height=1920)


class SelfVideoSelectionTests(unittest.TestCase):
    def test_bins_keep_the_sharpest_frame(self):
        ms = [measure(0, 0.0, 0.0, sharp=150), measure(1, 2.0, -1.0, sharp=300), measure(2, 14.0, 0.0, sharp=200),
              measure(3, 17.0, 2.0, sharp=250), measure(4, -31.0, 0.0, sharp=220), measure(5, 1.0, 21.0, sharp=180)]
        chosen, stats = R.select_frames(ms)
        self.assertEqual(sorted(chosen), ["y+00_p+00", "y+00_p+20", "y+15_p+00", "y-30_p+00"])
        self.assertEqual(chosen["y+00_p+00"].index, 1)      # sharpest of the two frontal frames
        self.assertEqual(chosen["y+15_p+00"].index, 3)      # 14 and 17 deg share the +15 bin: the sharper one
        self.assertEqual(stats["bins"], 4)
        self.assertEqual(stats["smiles"], 0)

    def test_closed_eyes_are_never_kept(self):
        ms = [measure(0, 0.0, 0.0, ear=0.06, sharp=900), measure(1, 1.0, 0.0, ear=0.31, sharp=200),
              measure(2, 30.0, 0.0, ear=0.09, sharp=500)]
        chosen, stats = R.select_frames(ms)
        self.assertEqual([m.index for m in chosen.values()], [1])
        self.assertEqual(stats["eyes_open"], 1)
        # the threshold follows the person: squinting eyes (0.12-0.19) are open, a blink (0.08) is not
        squint = [measure(i, 0.0, 0.0, ear=e, sharp=100 + i) for i, e in enumerate((0.13, 0.18, 0.15, 0.08, 0.12))]
        chosen, stats = R.select_frames(squint)
        self.assertEqual(stats["eyes_open"], 4)
        self.assertEqual(chosen["y+00_p+00"].index, 4)      # the sharpest with open eyes (0.12 passes, the 0.08 blink not)

    def test_sharpness_floor(self):
        ms = [measure(0, 0.0, 0.0, sharp=400), measure(1, 30.0, 0.0, sharp=100), measure(2, -30.0, 0.0, sharp=150)]
        chosen, stats = R.select_frames(ms)
        self.assertEqual(sorted(chosen), ["y+00_p+00", "y-30_p+00"])       # 100 < 35 % of 400
        self.assertAlmostEqual(stats["sharpness_floor"], 140.0)
        self.assertEqual(stats["sharp_enough"], 2)

    def test_smile_from_mouth_corners(self):
        ms = [measure(i, 0.0, 0.0, mouth=0.60, sharp=200) for i in range(5)]
        ms.append(measure(5, 3.0, 0.0, mouth=0.75, sharp=180))               # wider mouth: the smile
        ms.append(measure(6, 35.0, 0.0, mouth=0.80, sharp=300))              # a smile too far to the side: not kept
        chosen, stats = R.select_frames(ms)
        self.assertEqual(chosen["smile"].index, 5)
        self.assertEqual(stats["smiles"], 2)
        self.assertEqual(R.expression_of(chosen["smile"], chosen), "smile")
        self.assertEqual(R.expression_of(chosen["y+00_p+00"], chosen), "neutral")

    def test_out_of_range_angles_dropped_and_bin_names(self):
        self.assertEqual(R.bin_name(0.0, 0.0), "y+00_p+00")
        self.assertEqual(R.bin_name(-22.0, 11.0), "y-15_p+20")
        self.assertEqual(R.bin_name(44.0, -30.0), "y+45_p-20")
        ms = [measure(0, 62.0, 5.0, sharp=500), measure(1, 0.0, 0.0, sharp=100)]
        chosen, stats = R.select_frames(ms)
        self.assertEqual(stats["in_range"], 1)
        self.assertEqual(sorted(chosen), ["y+00_p+00"])

    def test_pack_written_into_the_job_is_a_valid_face_pack(self):
        from zfx.facepack import load_pack, pick_refs
        tmp = Path(tempfile.mkdtemp(prefix="zfx-refs-"))
        try:
            ms = [measure(0, 0.0, 0.0, sharp=300), measure(1, 21.0, 0.0, sharp=250), measure(2, 2.0, 0.0, mouth=0.9, sharp=200)]
            chosen, stats = R.select_frames(ms)
            frames = {i: np.full((64, 48, 3), 40 * (i + 1), np.uint8) for i in range(3)}
            pack = R.write_pack(tmp / "me.c101face", chosen, frames, person="Test", consent=True,
                                source=Path("self.mov"), stats=stats)
            manifest = json.loads((pack / "manifest.json").read_text())
            self.assertEqual((manifest["format"], manifest["version"], manifest["consent"]), ("c101face", 1, True))
            self.assertEqual(sorted(f["bin"] for f in manifest["frames"]), ["smile", "y+00_p+00", "y+15_p+00"])
            for f in manifest["frames"]:
                self.assertTrue((pack / f["file"]).is_file())
                for key in ("yaw", "pitch", "roll", "expression", "sharpness", "width", "height", "time", "face_box"):
                    self.assertIn(key, f)
            picks = pick_refs(load_pack(pack))
            self.assertEqual([p["slot"] for p in picks], ["frontal", "yaw+20", "smile"])
            self.assertEqual(R.eye_aspect_ratio(np.array([[0, 0], [1, 1], [2, 1], [3, 0], [2, -1], [1, -1]], float),
                                                range(6)), 4 / 6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HatSkipRuleTests(unittest.TestCase):
    def test_skip_under_a_tenth_of_a_percent(self):
        found, msg = H.hat_found({"mean_cover": 0.0004, "frames": 96, "hat_frames": 3})
        self.assertFalse(found)
        self.assertIn("No hat or cap found", msg)
        self.assertIn("skipped", msg)
        found, msg = H.hat_found({"mean_cover": 0.012, "frames": 96, "hat_frames": 96})
        self.assertTrue(found)
        self.assertIn("1.20 %", msg)
        self.assertFalse(H.hat_found({})[0])

    def test_postprocess_numbers(self):
        h, w = 64, 64
        soft = np.zeros((6, h, w), np.uint8)
        soft[:, 20:30, 20:30] = 255                      # a steady 10 x 10 hat
        soft[2, 40:60, 40:60] = 255                      # one-frame flicker: EMA 0.6 passes the 0.4 threshold once
        masks = list(H.postprocess(soft))
        self.assertEqual(len(masks), 6)
        self.assertTrue((masks[0][20:30, 20:30] == 255).all())
        self.assertTrue(masks[0][20 - 8, 25] == 255 and masks[0][20 - 9, 25] == 0)    # 17 x 17 ellipse: 8 px out
        self.assertTrue((masks[2][45:55, 45:55] == 255).all())                       # EMA 0.6 x 1 = 0.6 > 0.4
        self.assertFalse((masks[3][45:55, 45:55] == 255).any())                      # EMA 0.4 x 0.6 = 0.24 < 0.4: gone
        self.assertFalse((masks[4][45:55, 45:55] == 255).any())
        stats = H.coverage_stats(masks)
        self.assertEqual(stats["frames"], 6)
        self.assertGreater(stats["mean_cover"], 0.01)
        empty = H.coverage_stats(list(H.postprocess(np.zeros((4, h, w), np.uint8))))
        self.assertEqual(empty["mean_cover"], 0.0)
        self.assertFalse(H.hat_found(empty)[0])

    def test_head_crop_and_boxes(self):
        self.assertEqual(H.head_crop([100, 100, 200, 150], 1000, 1000), (40, 50, 260, 160))
        self.assertEqual(H.head_crop([10, 10, 60, 60], 100, 100), (0, 0, 90, 70))
        lm = {"width": 100, "height": 50, "frames": [None, [[0.1, 0.2, 0], [0.5, 0.8, 0]]]}
        boxes = H.face_boxes(lm)
        self.assertIsNone(boxes[0])
        self.assertEqual([round(v) for v in boxes[1]], [10, 10, 50, 40])
        self.assertEqual(H.scale_boxes(boxes, (100, 50), (200, 100))[1], [20.0, 20.0, 100.0, 80.0])
        self.assertIn("hat", H.hair_prompt(None))
        self.assertTrue(H.hair_prompt("short dark hair").startswith("short dark hair,"))


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-makeme-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan(self):
        base = dict(input="clip.mp4", consent=True)
        self.assertEqual(M.plan_steps(M.Options(**base, refs=["a.jpg"])),
                         ["prepare", "refs", "approve", "faceswap", "final"])
        self.assertEqual(M.plan_steps(M.Options(**base, self_video="me.mov", hat="remove")),
                         ["prepare", "refs", "approve", "faceswap", "hat", "voice", "final"])
        self.assertEqual(M.plan_steps(M.Options(**base, self_video="me.mov", no_voice=True)),
                         ["prepare", "refs", "approve", "faceswap", "final"])
        self.assertEqual(M.plan_steps(M.Options(**base, ref_pack="p.c101face", voice="v.m4a")),
                         ["prepare", "refs", "approve", "faceswap", "voice", "final"])

    def test_options_validation(self):
        photo = self.tmp / "a.jpg"
        photo.write_bytes(b"x")
        with self.assertRaises(M.MakeMeError) as cm:
            M.validate(M.Options(input="c.mp4", refs=[str(photo)]))
        self.assertEqual(cm.exception.step, "consent")
        with self.assertRaises(M.MakeMeError):
            M.validate(M.Options(input="c.mp4", consent=True))                        # nobody to become
        with self.assertRaises(M.MakeMeError):
            M.validate(M.Options(input="c.mp4", consent=True, refs=[str(photo)], self_video=str(photo)))
        with self.assertRaises(M.MakeMeError):
            M.validate(M.Options(input="c.mp4", consent=True, refs=[str(photo)], hat="maybe"))
        with self.assertRaises(M.MakeMeError):
            M.validate(M.Options(input="c.mp4", consent=True, self_video=str(self.tmp / "missing.mov")))
        M.validate(M.Options(input="c.mp4", consent=True, refs=[str(photo)]))
        opts = M.Options.from_args(SimpleNamespace(input="c.mp4", start=1, duration=None, ref_pack=None,
                                                   self_video="me.mov", ref=None, voice=None, no_voice=False,
                                                   quality="natural", hat="remove", consent=True, job=None, hair=None,
                                                   seed=None, person=None, cookies=None, enhance=False, swapjob=None))
        self.assertEqual((opts.me_source, opts.wants_voice, opts.hat), ("self-video", True, "remove"))
        self.assertEqual(opts.public()["voice_on"], True)

    def _pipeline(self, log, outputs_by_step, fail=None, versions=None):
        """Fake steps: each writes one file and records that it ran; `versions` changes a step's inputs."""
        versions = versions or {}

        def step(name):
            def inputs(ctx):
                return M.digest(name, versions.get(name, 1), *(ctx.state.get(f"{name}_dep") or []))

            def run(ctx, reporter):
                if fail == name:
                    raise M.MakeMeError(f"{name} broke", name)
                log.append(name)
                reporter.update(name, 0.5, f"{name} half")
                out = ctx.job_dir / outputs_by_step[name]
                out.write_text(f"{name} v{versions.get(name, 1)}")
                ctx.state[f"{name}_out"] = M.file_digest(out)
                ctx.state[f"final_dep"] = [ctx.state.get("faceswap_out")]
                return [out], {"message": f"{name} ok", "v": versions.get(name, 1)}, name == "hat"

            def restore(ctx, marker):
                out = ctx.job_dir / outputs_by_step[name]
                ctx.state[f"{name}_out"] = M.file_digest(out)
                ctx.state["final_dep"] = [ctx.state.get("faceswap_out")]
            return M.Step(name, inputs, run, restore)
        return [step(n) for n in outputs_by_step]

    def test_resume_skips_done_steps_and_reruns_changed_ones(self):
        job = self.tmp / "job"
        job.mkdir()
        (job / "job.json").write_text(json.dumps({"id": "job", "mode": "depth_mesh"}))
        outputs = {"prepare": "source.mp4", "refs": "refs.txt", "faceswap": "final.mp4", "hat": "hat.txt",
                   "final": "compare.mp4"}
        opts = M.Options(input="clip.mp4", consent=True, refs=["a.jpg"], hat="remove", job=str(job))

        def run(fail=None, versions=None):
            log: list = []
            out = io.StringIO()
            progress = Progress(stream=out)
            ctx = M.Context(job_dir=job, opts=opts, progress=progress)
            try:
                M.run_pipeline(ctx, self._pipeline(log, outputs, fail, versions))
            except M.MakeMeError as exc:
                return log, ctx, out.getvalue(), exc
            return log, ctx, out.getvalue(), None

        log, ctx, text, err = run()
        self.assertIsNone(err)
        self.assertEqual(log, ["prepare", "refs", "faceswap", "hat", "final"])
        lines = [json.loads(ln) for ln in text.splitlines()]
        self.assertEqual(lines[0]["stage"], "plan")
        self.assertEqual(lines[0]["steps"], ["prepare", "refs", "faceswap", "hat", "final"])
        self.assertTrue(all(0.0 <= ln.get("overall", 0) <= 1.0 for ln in lines))
        self.assertEqual([ln["step"] for ln in lines if ln.get("progress") == 0.5],
                         ["prepare", "refs", "faceswap", "hat", "final"])
        self.assertTrue(all((job / ".makeme" / f"{s}.json").is_file() for s in outputs))
        self.assertEqual(ctx.steps["hat"]["status"], "skipped")               # the fake hat says "no hat"
        self.assertEqual(ctx.steps["final"]["status"], "done")
        saved = json.loads((job / "job.json").read_text())["makeme"]
        self.assertEqual(saved["plan"], ["prepare", "refs", "faceswap", "hat", "final"])
        self.assertEqual(saved["steps"]["faceswap"]["status"], "done")
        self.assertEqual(saved["id" if False else "options"]["hat"], "remove")

        # same inputs again: nothing runs, everything is "already done"
        log, ctx, text, err = run()
        self.assertIsNone(err)
        self.assertEqual(log, [])
        self.assertTrue(all(ctx.steps[s].get("resumed") for s in outputs))
        self.assertIn("already done", text)

        # the face swap's inputs change: it re-runs, and `final` follows (its inputs include the swap's output)
        log, ctx, text, err = run(versions={"faceswap": 2})
        self.assertIsNone(err)
        self.assertEqual(log, ["faceswap", "final"])
        self.assertTrue(ctx.steps["prepare"].get("resumed"))

        # a failure stops the job at that step; the marker of the failed step is not written
        (job / "compare.mp4").unlink()
        log, ctx, text, err = run(fail="final", versions={"faceswap": 2})
        self.assertIsNotNone(err)
        self.assertEqual(err.step, "final")
        self.assertEqual(log, [])
        self.assertEqual(ctx.steps["final"]["status"], "failed")
        self.assertIn("broke", ctx.steps["final"]["error"])
        # a missing output of a done step re-runs it
        log, ctx, text, err = run(versions={"faceswap": 2})
        self.assertEqual(log, ["final"])

    def test_a_finished_run_clears_an_earlier_failure(self):
        """A failed attempt leaves error/failed_step in job.json's makeme section; the resumed, finished run
        clears them (seen on the real job of 2026-10-12: status done next to a stale AttributeError)."""
        job = self.tmp / "job"
        job.mkdir()
        (job / "job.json").write_text(json.dumps({"id": "job", "mode": "depth_mesh"}))
        outputs = {"prepare": "source.mp4", "final": "compare.mp4"}
        opts = M.Options(input="clip.mp4", consent=True, refs=["a.jpg"], job=str(job))

        def attempt(fail=None):
            log: list = []
            ctx = M.Context(job_dir=job, opts=opts, progress=Progress(stream=io.StringIO()))
            try:
                M.run_pipeline(ctx, self._pipeline(log, outputs, fail))
            except M.MakeMeError as exc:          # what run_makeme does
                ctx.save("failed", {"error": str(exc), "failed_step": exc.step})
            else:
                ctx.save("done", {"result": "final.mp4"})
            return log, json.loads((job / "job.json").read_text())["makeme"]

        log, section = attempt(fail="final")
        self.assertEqual(log, ["prepare"])
        self.assertEqual((section["status"], section["failed_step"]), ("failed", "final"))
        self.assertIn("final broke", section["error"])
        self.assertIn("finished", section)
        log, section = attempt()
        self.assertEqual(log, ["final"])                      # prepare was kept, final ran again
        self.assertEqual(section["status"], "done")
        self.assertNotIn("error", section)
        self.assertNotIn("failed_step", section)
        self.assertEqual(section["steps"]["final"]["status"], "done")
        self.assertEqual(section["result"], "final.mp4")
        self.assertIn("finished", section)

    def test_markers(self):
        job = self.tmp
        self.assertIsNone(M.is_done(job, "refs", "abc"))
        (job / "refs.txt").write_text("x")
        M.mark_done(job, "refs", "abc", [job / "refs.txt"], {"n": 1}, 2.5)
        marker = M.is_done(job, "refs", "abc")
        self.assertEqual(marker["outputs"], ["refs.txt"])
        self.assertEqual(marker["info"], {"n": 1})
        self.assertIsNone(M.is_done(job, "refs", "other"))
        (job / "refs.txt").unlink()
        self.assertIsNone(M.is_done(job, "refs", "abc"))
        M.clear_markers(job)
        self.assertIsNone(M.read_marker(job, "refs"))

    def test_cancel_on_a_finished_job(self):
        job = self.tmp / "job"
        job.mkdir()
        (job / "job.json").write_text(json.dumps({"id": "job", "makeme": {"status": "done", "pid": 1}}))
        result = M.cancel_job(job)
        self.assertFalse(result["cancelled"])
        with self.assertRaises(M.MakeMeError):
            M.cancel_job(self.tmp / "nowhere")

    def test_step_reporter_lines(self):
        out = io.StringIO()
        rep = M.StepReporter(Progress(stream=out), "prepare", 0.0, 0.1)
        rep.update("depth", 0.5, "frame 1", overall=0.25)
        rep.span("masks", 0.5, 1.0)(0.5, "half")
        rep.done("job", {"ok": True})
        lines = [json.loads(ln) for ln in out.getvalue().splitlines()]
        self.assertEqual(lines[0], {"stage": "prepare", "step": "prepare", "progress": 0.25, "overall": 0.025,
                                    "message": "frame 1", "substage": "depth"})
        self.assertEqual(lines[1]["progress"], 0.75)
        self.assertEqual(rep.result, {"ok": True})
        with self.assertRaises(M.MakeMeError):
            rep.error("boom", "depth")


class CLITests(unittest.TestCase):
    def test_makeme_refuses_without_consent_and_cancel_needs_a_job(self):
        import subprocess
        import sys
        proc = subprocess.run([sys.executable, str(synth.CLI), "makeme", "--input", "x.mp4", "--ref", "a.jpg"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 1)
        last = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(last["stage"], "consent")
        self.assertIn("consent", last["error"])
        proc = subprocess.run([sys.executable, str(synth.CLI), "cancel", "--job", "/nonexistent/job"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1])["stage"], "cancel")


if __name__ == "__main__":
    unittest.main()
