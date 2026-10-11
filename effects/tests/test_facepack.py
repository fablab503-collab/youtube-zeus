"""Face packs (.c101face, effects/FACE-PACK.md): validation, reference picking, CLI and generate --ref-pack.

No weights needed: synthetic packs (small generated JPEGs with fake yaw/pitch/sharpness) and a fake engine.
Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_facepack.py' -v
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import synth  # noqa: F401  (throwaway ZEUS_EFFECTS_HOME, sys.path)
from zfx import facepack, generate
from zfx.facepack import FacePackError, load_pack, pick_refs

# name, yaw, pitch, expression, sharpness
FRAMES = [
    ("y+00_p+00", 1.8, -0.6, "neutral", 412.5),
    ("y+00_p+00b", -2.0, 3.0, "neutral", 300.0),     # frontal too, less sharp
    ("y+00_p+20", 0.5, 21.0, "neutral", 500.0),      # sharpest of the pack, but chin up: not frontal
    ("y+15_p+00", 16.0, 1.0, "neutral", 380.0),
    ("y-15_p+00", -14.0, -2.0, "neutral", 350.0),
    ("y+30_p+00", 31.0, 0.0, "neutral", 200.0),
    ("y+45_p+00", 44.0, 2.0, "neutral", 260.0),
    ("y-30_p+00", -29.0, 1.0, "neutral", 210.0),
    ("y-45_p+00", -43.0, 0.0, "neutral", 90.0),      # under the floor (35 % of 500 = 175)
    ("smile", 2.0, 0.0, "smile", 330.0),
    ("mouth_open", 1.0, 0.0, "mouth_open", 340.0),   # never a reference
]


def write_jpeg(path: Path, seed: int, size=(48, 64)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        rng = random.Random(seed)
        Image.new("RGB", size, (rng.randrange(256), rng.randrange(256), rng.randrange(256))).save(path, "JPEG",
                                                                                                 quality=90)
    except ImportError:
        subprocess.run([synth.ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"color=c=0x{seed * 2654435761 % 0xFFFFFF:06x}:s={size[0]}x{size[1]}", "-frames:v", "1",
                        str(path)], check=True)


def make_pack(root: Path, frames=FRAMES, name="Daniel 2026-10-11 16-00", consent=True, extra=None,
              frame_extra=None, write=True) -> Path:
    pack = root / f"{name}.c101face"
    pack.mkdir(parents=True)
    items = []
    for i, (bin_, yaw, pitch, expr, sharp) in enumerate(frames):
        rel = f"frames/{bin_}.jpg"
        if write:
            write_jpeg(pack / rel, i)
        item = {"file": rel, "bin": bin_, "yaw": yaw, "pitch": pitch, "roll": 0.2, "expression": expr,
                "sharpness": sharp, "width": 48, "height": 64, "time": i * 0.5, "face_box": [0.3, 0.2, 0.4, 0.5]}
        item.update(frame_extra or {})
        items.append(item)
    manifest = {"format": "c101face", "version": 1, "created": "2026-10-11T16:00:00+02:00", "person": "Test Person",
                "consent": consent, "device": "iPhone19,2", "app": "Cinema 101 test", "frames": items}
    manifest.update(extra or {})
    (pack / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return pack


def edit_manifest(pack: Path, fn) -> None:
    data = json.loads((pack / "manifest.json").read_text())
    fn(data)
    (pack / "manifest.json").write_text(json.dumps(data))


class FacePackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-facepack-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PickTests(FacePackTestCase):
    def test_pick_order(self):
        pack = make_pack(self.tmp)
        picks = pick_refs(load_pack(pack))
        self.assertEqual([p["bin"] for p in picks],
                         ["y+00_p+00", "y+15_p+00", "y-15_p+00", "y+45_p+00", "y-30_p+00"])
        self.assertEqual([p["slot"] for p in picks], ["frontal", "yaw+20", "yaw-20", "yaw+40", "yaw-40"])
        for p in picks:
            path = Path(p["path"])
            self.assertTrue(path.is_absolute() and path.is_file())
            self.assertTrue(path.is_relative_to(pack.resolve()))
            self.assertTrue(p["why"])
        self.assertIn("frontal", picks[0]["why"])
        self.assertIn("+20", picks[1]["why"])
        # the smile comes after the angles: in with a sixth slot, never the open mouth
        six = pick_refs(load_pack(pack), max_refs=6)
        self.assertEqual(six[-1]["bin"], "smile")
        self.assertEqual(six[-1]["slot"], "smile")
        self.assertNotIn("mouth_open", [p["bin"] for p in six])
        self.assertEqual([p["bin"] for p in pick_refs(pack, max_refs=2)], ["y+00_p+00", "y+15_p+00"])

    def test_smile_fills_a_missing_angle(self):
        frames = [f for f in FRAMES if not f[0].startswith(("y-30", "y-45"))]
        picks = pick_refs(make_pack(self.tmp, frames=frames))
        self.assertEqual([p["slot"] for p in picks], ["frontal", "yaw+20", "yaw-20", "yaw+40", "smile"])

    def test_sharpness_floor(self):
        frames = [("front", 0.0, 0.0, "neutral", 400.0), ("left", -40.0, 0.0, "neutral", 100.0),
                  ("happy", 1.0, 0.0, "smile", 120.0), ("right", 21.0, 0.0, "neutral", 150.0)]
        pack = load_pack(make_pack(self.tmp, frames=frames))
        self.assertEqual([p["bin"] for p in pick_refs(pack)], ["front", "right"])          # 25 % and 30 % skipped
        self.assertEqual([p["bin"] for p in pick_refs(pack, min_sharpness=0.2)], ["front", "right", "left", "happy"])
        info = facepack.describe(pack, pick_refs(pack))
        self.assertEqual((info["frames"], info["usable"], info["blurry"]), (4, 2, 2))
        self.assertEqual(info["sharpness_floor"], 140.0)
        # the CLI's own floor applies to the default run of generate too
        self.assertEqual(facepack.MIN_SHARPNESS, 0.35)

    def test_stable_ordering_and_ties(self):
        frames = [("front", 0.0, 0.0, "neutral", 300.0), ("r25", 25.0, 0.0, "neutral", 300.0),
                  ("r18", 18.0, 0.0, "neutral", 300.0), ("r19", 19.0, 0.0, "neutral", 300.0),
                  ("front2", 0.0, 1.0, "neutral", 300.0)]
        first = pick_refs(make_pack(self.tmp / "a", frames=frames))
        self.assertEqual([p["bin"] for p in first], ["front", "r19"])   # equal sharpness: closest to the angle
        shuffled = list(frames)
        random.Random(7).shuffle(shuffled)
        again = pick_refs(make_pack(self.tmp / "b", frames=shuffled))
        self.assertEqual([p["bin"] for p in again], ["front", "r19"])
        self.assertEqual(pick_refs(make_pack(self.tmp / "c", frames=frames)), pick_refs(self.tmp / "c" /
                         "Daniel 2026-10-11 16-00.c101face"))

    def test_frontal_fallback_and_no_angles(self):
        frames = [("near", 11.0, -3.0, "neutral", 200.0), ("side", 38.0, 0.0, "neutral", 300.0)]
        picks = pick_refs(make_pack(self.tmp / "a", frames=frames))
        self.assertEqual([p["bin"] for p in picks], ["near", "side"])
        self.assertIn("closest to", picks[0]["why"])
        # no angles at all: still the sharpest neutral frame
        pack = make_pack(self.tmp / "b", frames=[("a", 0, 0, "neutral", 1.0), ("b", 0, 0, "neutral", 2.0)])
        edit_manifest(pack, lambda d: [f.pop(k) for f in d["frames"] for k in ("yaw", "pitch")])
        self.assertEqual([p["bin"] for p in pick_refs(pack)], ["b"])

    def test_missing_frame_is_skipped(self):
        pack = make_pack(self.tmp)
        (pack / "frames" / "y+15_p+00.jpg").unlink()
        loaded = load_pack(pack)
        self.assertEqual(loaded.skipped, [{"file": "frames/y+15_p+00.jpg", "reason": "missing"}])
        # nothing else within ±10° of +20 (yaw 31 is 11° away): that slot stays empty, the smile comes in
        self.assertEqual([p["slot"] for p in pick_refs(loaded)], ["frontal", "yaw-20", "yaw+40", "yaw-40", "smile"])


class ValidationTests(FacePackTestCase):
    def assertRefused(self, pack, *words):
        with self.assertRaises(FacePackError) as ctx:
            load_pack(pack)
        for word in words:
            self.assertIn(word, str(ctx.exception).lower())

    def test_consent_refusal(self):
        for i, value in enumerate((False, "true", 1, None)):
            pack = make_pack(self.tmp / str(i), consent=value)
            self.assertRefused(pack, "consent")
        pack = make_pack(self.tmp / "missing")
        edit_manifest(pack, lambda d: d.pop("consent"))
        self.assertRefused(pack, "consent")

    def test_path_escape_refusal(self):
        outside = self.tmp / "outside.jpg"
        write_jpeg(outside, 99)
        cases = {
            "dotdot": lambda d: d["frames"][0].update(file="../outside.jpg"),
            "nested": lambda d: d["frames"][0].update(file="frames/../../outside.jpg"),
            "absolute": lambda d: d["frames"][0].update(file=str(outside)),
            "home": lambda d: d["frames"][0].update(file="~/outside.jpg"),
            "depth": lambda d: d["frames"][0].update(depth="../depth.png"),
            "mesh": lambda d: d.update(mesh="/etc/hosts"),
            "blendshapes": lambda d: d.update(blendshapes="../../blend.json"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                pack = make_pack(self.tmp / name)
                edit_manifest(pack, change)
                self.assertRefused(pack, "outside" if name not in ("home",) else "not a path inside")
        # a link inside the pack that leads out of it
        pack = make_pack(self.tmp / "link")
        os.symlink(outside, pack / "frames" / "link.jpg")
        edit_manifest(pack, lambda d: d["frames"][0].update(file="frames/link.jpg"))
        self.assertRefused(pack, "outside")
        # a link that stays inside is fine
        pack = make_pack(self.tmp / "inner-link")
        os.symlink(pack / "frames" / "smile.jpg", pack / "frames" / "alias.jpg")
        edit_manifest(pack, lambda d: d["frames"].append(dict(d["frames"][-2], file="frames/alias.jpg", bin="alias")))
        self.assertEqual(len(load_pack(pack).frames), len(FRAMES))   # the alias is the smile listed twice

    def test_unknown_fields_are_ignored(self):
        pack = make_pack(self.tmp, extra={"future": {"lens": "TrueDepth"}, "scanner": 2, "notes": ["x"]},
                         frame_extra={"landmarks": [[0.1, 0.2]], "exposure": 1.2, "eyes_open": True})
        picks = pick_refs(pack)
        self.assertEqual(len(picks), 5)
        self.assertEqual(picks[0]["bin"], "y+00_p+00")
        # optional parts absent, odd values tolerated
        edit_manifest(pack, lambda d: (d.pop("mesh", None), d.pop("blendshapes", None),
                                       d["frames"][3].update(yaw="sixteen"), d["frames"].append("junk"),
                                       d["frames"].append({"bin": "no-file"})))
        loaded = load_pack(pack)
        self.assertEqual([s["reason"] for s in loaded.skipped], ["frame 12 is not an object", "frame 13 names no file"])
        self.assertNotIn("y+15_p+00", [p["bin"] for p in pick_refs(loaded)])   # no usable yaw: not an angle

    def test_format_and_version(self):
        for name, change, word in (("format", lambda d: d.update(format="other"), "format"),
                                   ("v2", lambda d: d.update(version=2), "version"),
                                   ("vstr", lambda d: d.update(version="1"), "version"),
                                   ("frames", lambda d: d.update(frames={}), "frames")):
            with self.subTest(name):
                pack = make_pack(self.tmp / name)
                edit_manifest(pack, change)
                self.assertRefused(pack, word)
        no_manifest = self.tmp / "empty.c101face"
        no_manifest.mkdir()
        self.assertRefused(no_manifest, "manifest")
        self.assertRefused(self.tmp / "nowhere.c101face", "not found")
        broken = make_pack(self.tmp / "broken")
        (broken / "manifest.json").write_text("{nope")
        self.assertRefused(broken, "could not be read")

    def test_manifest_path_and_name(self):
        pack = make_pack(self.tmp)
        loaded = load_pack(pack / "manifest.json")
        self.assertEqual(loaded.dir, pack.resolve())
        self.assertEqual(loaded.name, "Daniel 2026-10-11 16-00")


def run_cli(*args, timeout=120):
    proc = subprocess.run([sys.executable, str(synth.CLI)] + list(args), capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ))
    lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    return proc.returncode, lines, proc


class CLITests(FacePackTestCase):
    def test_facepack_json(self):
        pack = make_pack(self.tmp)
        code, lines, proc = run_cli("facepack", "--pack", str(pack), "--json")
        self.assertEqual(code, 0, proc.stderr)
        last = lines[-1]
        self.assertTrue(last["done"])
        result = last["result"]
        self.assertEqual(result["name"], "Daniel 2026-10-11 16-00")
        self.assertEqual(result["pack"], str(pack.resolve()))
        self.assertEqual((result["frames"], result["usable"], result["blurry"]), (11, 10, 1))
        self.assertEqual([p["file"] for p in result["picked"]],
                         ["frames/y+00_p+00.jpg", "frames/y+15_p+00.jpg", "frames/y-15_p+00.jpg",
                          "frames/y+45_p+00.jpg", "frames/y-30_p+00.jpg"])
        self.assertTrue(all(Path(p["path"]).is_absolute() and p["why"] for p in result["picked"]))
        code, lines, _ = run_cli("facepack", "--pack", str(pack), "--json", "--max-refs", "6")
        self.assertEqual(lines[-1]["result"]["picked"][-1]["slot"], "smile")
        code, _, proc = run_cli("facepack", "--pack", str(pack))
        self.assertEqual(code, 0)
        self.assertIn("would use 5", proc.stdout)

    def test_facepack_refusal_is_a_json_error(self):
        pack = make_pack(self.tmp, consent=False)
        code, lines, _ = run_cli("facepack", "--pack", str(pack), "--json")
        self.assertEqual(code, 1)
        self.assertIn("consent", lines[-1]["error"].lower())
        self.assertEqual(lines[-1]["stage"], "facepack")

    def test_generate_parser_accepts_a_pack_alone(self):
        sys.path.insert(0, str(synth.EFFECTS))
        import zeus_effects
        args = zeus_effects.build_parser().parse_args(
            ["generate", "--job", "x", "--engine", "faceswap", "--ref-pack", "p.c101face", "--consent"])
        self.assertEqual((args.ref, args.ref_pack), (None, "p.c101face"))
        args = zeus_effects.build_parser().parse_args(
            ["generate", "--job", "x", "--engine", "faceswap", "--ref", "a.jpg", "--ref-pack", "p.c101face"])
        self.assertEqual((args.ref, args.ref_pack), (["a.jpg"], "p.c101face"))


class RecordingEngine:
    NAME = "faceswap"
    LABEL = "fake"

    def __init__(self):
        self.refs = None

    def status(self):
        return {"ready": True, "message": "ready"}

    def run(self, job_dir, refs, out_path, progress, **options):
        from test_engines import make_raw
        self.refs = [Path(r) for r in refs]
        make_raw(Path(out_path), frames=32)
        return {"engine": "fake"}


def gen_args(**kw):
    base = dict(engine="faceswap", ref=[], ref_pack=None, prompt="", consent=True, frames=None, steps=None,
                size=None, seed=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class GenerateTests(FacePackTestCase):
    def setUp(self):
        super().setUp()
        from test_engines import make_job, make_ref
        self.job = make_job(self.tmp / "jobs")
        self.photo = make_ref(self.tmp)

    def test_generate_with_pack_records_the_frames(self):
        pack = make_pack(self.tmp)
        fake = RecordingEngine()
        with mock.patch.object(generate, "get_engine", return_value=fake):
            result = generate.run_generate(gen_args(ref_pack=str(pack), ref=[str(self.photo)]), self.job,
                                           lambda *a: None)
        self.assertEqual([r.name for r in fake.refs],
                         ["pack-1.jpg", "pack-2.jpg", "pack-3.jpg", "pack-4.jpg", "pack-5.jpg", "ref-1.png"])
        self.assertTrue(all(r.parent == self.job / "refs" for r in fake.refs))
        self.assertEqual(result["face_pack"]["name"], "Daniel 2026-10-11 16-00")
        rec = json.loads((self.job / "job.json").read_text())["generations"][-1]
        fp = rec["face_pack"]
        self.assertEqual(fp["pack"], str(pack.resolve()))
        self.assertEqual(fp["version"], 1)
        self.assertEqual([p["file"] for p in fp["picked"]][0], "frames/y+00_p+00.jpg")
        self.assertEqual([p["ref"] for p in fp["picked"]], [f"refs/pack-{i}.jpg" for i in range(1, 6)])
        self.assertTrue(all(p["why"] for p in fp["picked"]))
        self.assertEqual(rec["refs"][:5], [f"refs/pack-{i}.jpg" for i in range(1, 6)])
        self.assertEqual(len(rec["ref_sha256"]), 6)
        self.assertTrue((self.job / "final.mp4").is_file())

    def test_pack_alone_and_vace_cap(self):
        pack = make_pack(self.tmp)
        refs, record = generate.prepare_pack_refs(pack, self.job, "vace")
        self.assertEqual([r.name for r in refs], ["pack-1.jpg", "pack-2.jpg"])
        self.assertEqual([p["slot"] for p in record["picked"]], ["frontal", "yaw+20"])
        refs, _ = generate.prepare_pack_refs(pack, self.job, "faceswap")
        self.assertEqual(len(refs), 5)
        fake = RecordingEngine()
        with mock.patch.object(generate, "get_engine", return_value=fake):
            generate.run_generate(gen_args(ref_pack=str(pack)), self.job, lambda *a: None)
        self.assertEqual(len(fake.refs), 5)
        self.assertEqual(sorted(p.name for p in (self.job / "refs").glob("pack-*")),
                         [f"pack-{i}.jpg" for i in range(1, 6)])

    def test_generate_refuses_pack_without_consent(self):
        pack = make_pack(self.tmp, consent=False)
        fake = RecordingEngine()
        with mock.patch.object(generate, "get_engine", return_value=fake):
            with self.assertRaises(generate.GenerateError) as ctx:
                generate.run_generate(gen_args(ref_pack=str(pack), ref=[str(self.photo)]), self.job, lambda *a: None)
        self.assertIn("consent", str(ctx.exception).lower())
        self.assertIsNone(fake.refs)
        self.assertFalse((self.job / "final.mp4").exists())

    def test_generate_needs_photo_or_pack(self):
        with mock.patch.object(generate, "get_engine", return_value=RecordingEngine()):
            with self.assertRaises(generate.GenerateError) as ctx:
                generate.run_generate(gen_args(), self.job, lambda *a: None)
        self.assertIn("--ref-pack", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
