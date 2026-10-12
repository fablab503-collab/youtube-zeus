"""The shared-folder watcher (zeus_effects.py watch) on a temp folder with a fake makeme: request -> status.json ->
result.mp4 / compare.mp4 / preview.jpg / done; cancel; a half-copied job (no `request`) is ignored; errors land in
status.json; the makeme command line built from a .swapjob.

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_swapjobs.py' -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import synth
from zfx import swapjobs as S

FAKE_MAKEME = r'''
import json, os, shutil, sys, time
from pathlib import Path
args = sys.argv[1:]
job = Path(args[args.index("--job") + 1])
job.mkdir(parents=True, exist_ok=True)
(job / "argv.json").write_text(json.dumps(args))
def emit(o):
    print(json.dumps(o), flush=True)
steps = ["prepare", "refs", "approve", "faceswap", "final"]
emit({"stage": "plan", "step": "plan", "progress": 0, "overall": 0, "steps": steps, "job": str(job), "message": "plan"})
slow = os.environ.get("FAKE_SLOW") == "1"
for i, s in enumerate(steps):
    emit({"stage": s, "step": s, "progress": 0.5, "overall": (i + 0.5) / len(steps), "message": s + " half"})
    time.sleep(2.0 if slow else 0.05)
    if os.environ.get("FAKE_FAIL") == s:
        emit({"error": s + " broke", "stage": s})
        sys.exit(1)
clip = Path(os.environ["FAKE_CLIP"])
shutil.copyfile(clip, job / "final.mp4")
shutil.copyfile(clip, job / "compare.mp4")
(job / "job.json").write_text(json.dumps({"id": job.name, "makeme": {"status": "done"}}))
emit({"done": True, "job": str(job), "result": {"result": str(job / "final.mp4"), "compare": str(job / "compare.mp4"),
      "comment": "AI-generated with YouTube Zeus Effects (faceswap)", "seconds": 1.5, "timings": {"faceswap": 1.0},
      "warnings": []}})
'''


class WatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="zfx-swap-")).resolve()
        cls.clip = cls.root / "clip.mp4"
        synth.make_clip(cls.clip, seconds=1.0, size="320x180", rate=10)
        cls.fake = cls.root / "fake_makeme.py"
        cls.fake.write_text(FAKE_MAKEME)
        cls.old_env = {k: os.environ.get(k) for k in ("ZEUS_MAKEME_COMMAND", "FAKE_CLIP", "FAKE_FAIL", "FAKE_SLOW",
                                                       "ZEUS_SWAP_BUSY_CHECK")}
        os.environ["ZEUS_MAKEME_COMMAND"] = json.dumps([sys.executable, str(cls.fake)])
        os.environ["ZEUS_SWAP_BUSY_CHECK"] = "0"       # a real job on this Mac must not make the tests wait
        S.BUSY_CHECK = False
        os.environ["FAKE_CLIP"] = str(cls.clip)
        os.environ.pop("FAKE_FAIL", None)
        os.environ.pop("FAKE_SLOW", None)

    @classmethod
    def tearDownClass(cls):
        S.BUSY_CHECK = True
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.folder = self.root / f"Jobs-{self._testMethodName}"
        self.folder.mkdir()
        self.jobs_root = self.root / f"ZeusJobs-{self._testMethodName}"
        self.lock = self.root / f"{self._testMethodName}.lock"

    def make_swapjob(self, name="20261012-101500-Daniel", request=True, consent=True, me="pack", voice=True,
                     options=None):
        job = self.folder / f"{name}.swapjob"
        job.mkdir()
        shutil.copyfile(self.clip, job / "clip.mp4")
        if me == "pack":
            (job / "me.c101face").mkdir()
            (job / "me.c101face" / "manifest.json").write_text(json.dumps({"format": "c101face", "version": 1,
                                                                            "consent": True, "frames": []}))
        elif me == "photos":
            (job / "photos").mkdir()
            (job / "photos" / "b.jpg").write_bytes(b"x")
            (job / "photos" / "a.jpg").write_bytes(b"x")
        if voice:
            (job / "voice.m4a").write_bytes(b"x")
        opts = {"quality": "natural", "hat": "remove", "voice": True, "consent": consent, "device": "iPhone19,2",
                "app": "The Swap 1.0", "version": 1}
        opts.update(options or {})
        (job / "job.json").write_text(json.dumps(opts))
        if request:
            (job / "request").write_bytes(b"")
        return job

    def status(self, job):
        return json.loads((job / "status.json").read_text())

    # -- reading ------------------------------------------------------------------------------------------------
    def test_read_job_and_command(self):
        job = self.make_swapjob(options={"start": 2, "duration": 4, "hair": "short dark hair", "person": "Daniel"})
        read = S.read_job(job)
        self.assertEqual(read["me"]["kind"], "face-pack")
        self.assertEqual((read["quality"], read["hat"], read["voice_on"], read["consent"]), ("natural", "remove", True, True))
        self.assertEqual((read["start"], read["duration"]), (2.0, 4.0))
        cmd = S.makeme_command(read, self.jobs_root / read["name"], job)
        self.assertEqual(cmd[:2], [sys.executable, str(self.fake)])
        self.assertEqual(cmd[2], "makeme")
        self.assertIn("--ref-pack", cmd)
        self.assertIn("--consent", cmd)
        self.assertIn("--hat", cmd)
        self.assertEqual(cmd[cmd.index("--hat") + 1], "remove")
        self.assertEqual(cmd[cmd.index("--hair") + 1], "short dark hair")
        self.assertEqual(cmd[cmd.index("--job") + 1], str(self.jobs_root / "20261012-101500-Daniel"))
        self.assertEqual(cmd[cmd.index("--swapjob") + 1], str(job))
        self.assertNotIn("--no-voice", cmd)
        photos = self.make_swapjob(name="20261012-101600-Photos", me="photos", voice=False,
                                   options={"hat": "keep", "voice": False})
        read = S.read_job(photos)
        self.assertEqual(read["me"]["kind"], "photos")
        self.assertEqual([Path(p).name for p in read["me"]["paths"]], ["a.jpg", "b.jpg"])
        cmd = S.makeme_command(read, self.jobs_root / read["name"], photos)
        self.assertEqual(cmd.count("--ref"), 2)
        self.assertIn("--no-voice", cmd)
        self.assertEqual(cmd[cmd.index("--hat") + 1], "keep")
        link = self.make_swapjob(name="20261012-101700-Link", me="pack")
        (link / "clip.mp4").unlink()
        (link / "clip.txt").write_text("https://www.youtube.com/watch?v=d6spCMcfkew\nstart: 598\nduration: 6\n")
        read = S.read_job(link)
        self.assertEqual((read["input"], read["start"], read["duration"]), ("https://www.youtube.com/watch?v=d6spCMcfkew", 598.0, 6.0))
        self.assertEqual(S.parse_clip_link("https://x.y/v 12 3.5"), {"input": "https://x.y/v", "start": 12.0, "duration": 3.5})
        empty = self.make_swapjob(name="20261012-101800-Empty")
        shutil.rmtree(empty / "me.c101face")
        with self.assertRaises(S.SwapJobError):
            S.read_job(empty)

    def test_pending_ignores_half_copied_and_finished_jobs(self):
        half = self.make_swapjob(name="20261012-100000-Half", request=False)      # the phone is still copying
        done = self.make_swapjob(name="20261012-100100-Done")
        (done / "done").write_bytes(b"")
        failed = self.make_swapjob(name="20261012-100200-Failed")
        S.write_status(failed, {"step": "error", "error": "x", "finished": "now"})
        ready = self.make_swapjob(name="20261012-100300-Ready")
        later = self.make_swapjob(name="20261012-100250-Earlier")
        self.assertEqual([p.name for p in S.pending_jobs([self.folder])],
                         ["20261012-100250-Earlier.swapjob", "20261012-100300-Ready.swapjob"])
        self.assertNotIn(half, S.pending_jobs([self.folder]))
        (self.folder / "notes.txt").write_text("x")
        (self.folder / "Other.folder").mkdir()
        self.assertEqual(len(S.pending_jobs([self.folder])), 2)
        self.assertEqual(S.pending_jobs([self.root / "missing"]), [])
        _ = later

    # -- the whole protocol -------------------------------------------------------------------------------------
    def test_request_status_result_done(self):
        job = self.make_swapjob()
        events: list = []
        summary = S.watch([self.folder], once=True, poll=0.2, report=events.append, jobs_root=self.jobs_root,
                          lock_path=self.lock)
        self.assertEqual(len(summary["jobs"]), 1)
        self.assertEqual(summary["jobs"][0]["status"], "done")
        for name in ("result.mp4", "compare.mp4", "preview.jpg", "done", "status.json", "mac.log"):
            self.assertTrue((job / name).is_file(), name)
        status = self.status(job)
        self.assertEqual((status["step"], status["progress"], status["error"]), ("done", 1.0, None))
        self.assertEqual(status["steps"], ["prepare", "refs", "approve", "faceswap", "final"])
        self.assertEqual(status["steps_done"], ["prepare", "refs", "approve", "faceswap", "final"])
        self.assertEqual(status["result"]["result"], "result.mp4")
        self.assertIn("AI-generated", status["comment"])
        for key in ("started", "updated", "finished", "zeus_job", "device", "app"):
            self.assertIn(key, status)
        self.assertEqual((job / "result.mp4").stat().st_size, self.clip.stat().st_size)
        # the Zeus job sits under Jobs/ with the .swapjob's name, so the app lists it
        zeus_job = self.jobs_root / "20261012-101500-Daniel"
        self.assertTrue((zeus_job / "final.mp4").is_file())
        argv = json.loads((zeus_job / "argv.json").read_text())
        self.assertIn("--consent", argv)
        self.assertEqual(argv[argv.index("--swapjob") + 1], str(job))
        kinds = [e.get("stage") for e in events]
        self.assertEqual(kinds[0], "watch")
        self.assertIn("job", kinds)
        statuses = [e.get("status") for e in events if e.get("stage") == "job"]
        self.assertEqual(statuses[0], "picked")
        self.assertEqual(statuses[-1], "done")
        self.assertIn("running", statuses)
        # done twice? no: a second pass finds nothing
        events.clear()
        summary = S.watch([self.folder], once=True, poll=0.2, report=events.append, jobs_root=self.jobs_root,
                          lock_path=self.lock)
        self.assertEqual(summary["jobs"], [])
        self.assertEqual(events[-1]["message"], "nothing pending")

    def test_error_goes_into_status_json(self):
        job = self.make_swapjob()
        os.environ["FAKE_FAIL"] = "faceswap"
        try:
            summary = S.watch([self.folder], once=True, poll=0.2, jobs_root=self.jobs_root, lock_path=self.lock)
        finally:
            os.environ.pop("FAKE_FAIL", None)
        self.assertEqual(summary["jobs"][0]["status"], "error")
        status = self.status(job)
        self.assertEqual(status["step"], "error")
        self.assertIn("faceswap broke", status["error"])
        self.assertIn("finished", status)
        self.assertFalse((job / "done").exists())
        self.assertFalse((job / "result.mp4").exists())
        self.assertEqual(S.pending_jobs([self.folder]), [])            # not retried
        # no consent: refused before anything runs
        refused = self.make_swapjob(name="20261012-102000-NoConsent", consent=False)
        S.watch([self.folder], once=True, poll=0.2, jobs_root=self.jobs_root, lock_path=self.lock)
        self.assertIn("consent", self.status(refused)["error"])
        self.assertFalse((self.jobs_root / "20261012-102000-NoConsent").exists())

    def test_cancel_marker_stops_the_job(self):
        job = self.make_swapjob()
        os.environ["FAKE_SLOW"] = "1"
        try:
            def cancel_soon():
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    st = S.read_status(job)
                    if st.get("pid") and st.get("step") not in ("starting", "waiting", "plan", None):
                        break
                    time.sleep(0.1)
                (job / "cancel").write_bytes(b"")
            t = threading.Thread(target=cancel_soon, daemon=True)
            t.start()
            t0 = time.monotonic()
            summary = S.watch([self.folder], once=True, poll=0.2, jobs_root=self.jobs_root, lock_path=self.lock)
            t.join(timeout=5)
        finally:
            os.environ.pop("FAKE_SLOW", None)
        self.assertLess(time.monotonic() - t0, 9.0)                      # the fake would take 10 s
        self.assertEqual(summary["jobs"][0]["status"], "cancelled")
        self.assertTrue((job / "cancelled").is_file())
        self.assertFalse((job / "done").exists())
        self.assertFalse((job / "result.mp4").exists())
        status = self.status(job)
        self.assertEqual(status["step"], "cancelled")
        pid = status.get("pid")
        if pid:
            time.sleep(0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid), 0)
        self.assertEqual(S.pending_jobs([self.folder]), [])

    def test_watch_cli_once(self):
        job = self.make_swapjob(name="20261012-103000-CLI")
        env = dict(os.environ, ZEUS_EFFECTS_HOME=str(self.root / "home-cli"))
        proc = subprocess.run([sys.executable, str(synth.CLI), "watch", "--folder", str(self.folder), "--once",
                               "--poll", "0.5"], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = [json.loads(ln) for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
        self.assertEqual(lines[0]["stage"], "watch")
        self.assertTrue(lines[-1].get("done"))
        self.assertEqual(lines[-1]["result"]["jobs"][0]["status"], "done")
        self.assertTrue((job / "done").is_file())
        self.assertTrue((self.root / "home-cli" / "Jobs" / "20261012-103000-CLI" / "final.mp4").is_file())
        proc = subprocess.run([sys.executable, str(synth.CLI), "watch", "--folder", str(self.root / "nope"), "--once"],
                              capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Folder not found", json.loads(proc.stdout.strip().splitlines()[-1])["error"])

    def test_lock_and_cleaning(self):
        with S.WatchLock(self.lock):
            with self.assertRaises(S.SwapJobError):
                S.watch([self.folder], once=True, jobs_root=self.jobs_root, lock_path=self.lock)
        old = self.make_swapjob(name="20250101-000000-Old")
        (old / "done").write_bytes(b"")
        fresh = self.make_swapjob(name="20261012-104000-Fresh")
        (fresh / "done").write_bytes(b"")
        unfinished = self.make_swapjob(name="20250101-000100-Unfinished")
        removed = S.clean_old([self.folder], 14)
        self.assertEqual([Path(p).name for p in removed], ["20250101-000000-Old.swapjob"])
        self.assertTrue(fresh.exists() and unfinished.exists())
        self.assertEqual(S.clean_old([self.folder], 0), [])

    def test_busy_processes_and_default_folders(self):
        self.assertIsInstance(S.busy_processes(), list)
        env = os.environ.get("ZEUS_SWAP_FOLDERS")
        os.environ["ZEUS_SWAP_FOLDERS"] = os.pathsep.join([str(self.folder), str(self.root / "missing")])
        try:
            self.assertEqual(S.default_folders(), [self.folder])
        finally:
            if env is None:
                os.environ.pop("ZEUS_SWAP_FOLDERS", None)
            else:
                os.environ["ZEUS_SWAP_FOLDERS"] = env


if __name__ == "__main__":
    unittest.main()
