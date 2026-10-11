"""Crop boxes, full-resolution composite, colour/grain matching, Log LUT inverse (no model weights needed)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

EFFECTS = Path(__file__).resolve().parents[1]
for _p in (EFFECTS, EFFECTS / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402

import synth  # noqa: E402,F401  (throwaway ZEUS_EFFECTS_HOME)
from zfx import cropbox, generate, hires, logcolor, upscale  # noqa: E402

try:
    import cv2  # noqa: F401
except ImportError:  # pragma: no cover
    cv2 = None


def _bin(name: str) -> str:
    for cand in (f"/opt/homebrew/bin/{name}", shutil.which(name)):
        if cand and Path(cand).exists():
            return cand
    raise unittest.SkipTest(f"{name} not installed")


def _run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def audio_packets_sha(path: Path) -> str:
    out = subprocess.run([_bin("ffmpeg"), "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy",
                          "-f", "data", "-"], check=True, capture_output=True).stdout
    assert out, "no audio packets"
    return hashlib.sha256(out).hexdigest()


def decode(path: Path, w: int, h: int, extra: str = "") -> np.ndarray:
    vf = ["-vf", extra] if extra else []
    data = subprocess.run([_bin("ffmpeg"), "-v", "error", "-i", str(path)] + vf +
                          ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    return np.frombuffer(data, np.uint8)[: len(data) // (w * h * 3) * w * h * 3].reshape(-1, h, w, 3).astype(np.int32)


# ----------------------------------------------------------------------------- boxes
@unittest.skipIf(cv2 is None, "cv2 not available")
class CropBoxTests(unittest.TestCase):
    def test_mask_bbox(self):
        m = np.zeros((40, 60), np.uint8)
        self.assertIsNone(cropbox.mask_bbox(m))
        m[10:20, 30:45] = 255
        self.assertEqual(cropbox.mask_bbox(m), (30, 10, 45, 20))

    def test_box_contains_person_matches_aspect_and_is_inside(self):
        for (bbox, w, h) in [((800, 300, 1100, 900), 1920, 1080), ((900, 100, 1000, 300), 1920, 1080),
                             ((1600, 600, 3000, 2160), 3840, 2160), ((653, 258, 1920, 1080), 1920, 1080),
                             ((10, 10, 200, 400), 1920, 1080), ((100, 0, 700, 1080), 1920, 1080)]:
            plan = cropbox.plan_box(bbox, w, h)
            x0, y0, x1, y1 = plan["box"]
            self.assertTrue(0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h, (bbox, plan))
            self.assertTrue(x0 % 2 == 0 and y0 % 2 == 0 and x1 % 2 == 0 and y1 % 2 == 0, plan)
            self.assertTrue(x0 <= bbox[0] and y0 <= bbox[1] and x1 >= bbox[2] and y1 >= bbox[3], (bbox, plan))
            gw, gh = plan["gen"]
            self.assertEqual((gw % 16, gh % 16), (0, 0))
            self.assertLessEqual(plan["aspect_error"], 0.03, (bbox, plan))   # no visible stretch
            self.assertAlmostEqual((x1 - x0) / (y1 - y0), gw / gh, delta=0.035 * gw / gh)

    def test_orientation_and_margin(self):
        tall = cropbox.plan_box((900, 300, 1000, 700), 1920, 1080)
        self.assertEqual(tall["gen"], [368, 640])
        wide = cropbox.plan_box((700, 500, 1300, 650), 1920, 1080)
        self.assertEqual(wide["gen"], [640, 368])
        # 12 % margin on the sides inside the frame; none past the frame edge (the person leaves the frame there)
        edge = cropbox.plan_box((1000, 400, 1920, 1080), 1920, 1080, margin=0.12)
        self.assertEqual((edge["box"][2], edge["box"][3]), (1920, 1080))
        self.assertLessEqual(edge["box"][1], 400 - 0.12 * 680 + 2)

    def test_whole_frame_when_person_fills_it_or_is_missing(self):
        self.assertTrue(cropbox.plan_box(None, 1920, 1080)["full_frame"])
        self.assertTrue(cropbox.plan_box((50, 50, 1870, 1030), 1920, 1080)["full_frame"])
        whole = cropbox.plan_box((0, 500, 1920, 700), 1920, 1080)
        self.assertEqual(whole["box"], [0, 0, 1920, 1080])

    def test_one_stable_box_per_window(self):
        """A person walking right: the box of a window is the union of all its frames (no jitter inside a window),
        consecutive windows follow the person, every frame's mask lies inside its window's box."""
        n, h, w = 60, 90, 160
        masks = np.zeros((n, h, w), np.uint8)
        for i in range(n):
            x = 10 + i                                   # moves 1 px per frame
            masks[i, 30:80, x:x + 20] = 255
            masks[i, 30 + (i % 3), x + 5] = 255          # 1 px jitter that must not move the box
        windows = [(0, 33), (25, 58), (27, 60)]
        plans = cropbox.plan_window_boxes(masks, windows, 1920, 1080)
        sx, sy = 1920 / w, 1080 / h
        for (start, end), plan in zip(windows, plans):
            x0, y0, x1, y1 = plan["box"]
            for i in range(start, min(end, n)):
                bx0, by0, bx1, by1 = cropbox.mask_bbox(masks[i])
                self.assertTrue(x0 <= bx0 * sx and x1 >= bx1 * sx and y0 <= by0 * sy and y1 >= by1 * sy)
        self.assertLess(plans[0]["box"][0], plans[1]["box"][0])   # follows the motion
        again = cropbox.plan_window_boxes(masks, windows, 1920, 1080)
        self.assertEqual([p["box"] for p in plans], [p["box"] for p in again])
        off = cropbox.plan_window_boxes(masks, windows, 1920, 1080, crop=False)
        self.assertTrue(all(p["full_frame"] and p["box"] == [0, 0, 1920, 1080] for p in off))

    def test_stray_blob_does_not_stretch_the_box(self):
        """A face on a screen far from the presenter (35 % of their blob, kept by prepare) stays outside the box;
        a hand the mask cut off next to the body stays inside."""
        m = np.zeros((468, 832), np.uint8)
        m[281:468, 501:751] = 255          # presenter
        m[112:223, 283:393] = 255          # face on a screen, far away
        m[300:330, 455:490] = 255          # detached hand, close to the body
        self.assertEqual(cropbox.person_bbox(m), (455, 281, 751, 468))
        plan = cropbox.plan_window_boxes(m[None], [(0, 1)], 1920, 1080)[0]
        self.assertGreater(plan["box"][0], 393 * 1920 / 832)
        self.assertGreater(plan["outside_mask_share"], 0.15)
        two = np.zeros((468, 832), np.uint8)
        two[200:468, 100:250] = 255
        two[220:468, 600:740] = 255         # a second person of similar size steers the box too
        self.assertEqual(cropbox.person_bbox(two), (100, 200, 740, 468))

    def test_adapted_generation_size_instead_of_stretch(self):
        g = cropbox.adapt_gen(744 / 1080)
        self.assertEqual((g[0] % 16, g[1] % 16), (0, 0))
        self.assertLessEqual(g[0] * g[1], 640 * 368 * 1.05)
        self.assertAlmostEqual(g[0] / g[1], 744 / 1080, delta=0.03)

    def test_frame_weights_match_vace_blend(self):
        from zfx.engines import vace
        total = 70
        windows = vace.plan_windows(total, 33, 8)
        cover = cropbox.frame_weights(windows, total)
        for parts in cover:
            self.assertAlmostEqual(sum(w for _, w in parts), 1.0, places=6)
        # the second window ramps in with vace.blend_weights over the overlap
        s1 = windows[1][0]
        overlap = windows[0][1] - s1
        ramp = vace.blend_weights(overlap)
        for j in range(overlap):
            weights = dict(cover[s1 + j])
            self.assertAlmostEqual(weights[1], float(ramp[j]), places=5)

    def test_crop_resize_and_reproject(self):
        frame = np.zeros((100, 200, 3), np.uint8)
        frame[40:60, 90:110] = 255
        box = (80, 30, 120, 70)                          # 40x40 around the square
        crop = cropbox.crop_resize(frame, box, 40, 40, cv2.INTER_NEAREST)
        self.assertTrue(np.array_equal(crop, frame[30:70, 80:120]))
        # a patch of box A drawn into box B: same pixels where they overlap, fill elsewhere
        patch = frame[30:70, 80:120].copy()
        fill = np.full((40, 40, 3), 7, np.uint8)
        out = cropbox.reproject(patch, (80, 30, 120, 70), (100, 30, 140, 70), 40, 40, fill)
        self.assertTrue(np.array_equal(out[:, :19], frame[30:70, 100:119]))
        self.assertTrue((out[:, 21:] == 7).all())


# ----------------------------------------------------------------------------- composite
@unittest.skipIf(cv2 is None, "cv2 not available")
class CompositeTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.base = rng.integers(0, 256, (120, 200, 3), dtype=np.uint8)
        self.box = (40, 20, 160, 110)
        self.mask = np.zeros((90, 120), bool)
        self.mask[15:75, 30:90] = True
        self.patch = np.full((90, 120, 3), 0.25, np.float32)

    def test_inside_is_patch_outside_is_bit_exact(self):
        out = hires.composite_region(self.base, self.patch, self.box, self.mask, feather_px=6)
        x0, y0, x1, y1 = self.box
        alpha = hires.feather_alpha(self.mask, 6)
        outside = np.ones(self.base.shape[:2], bool)
        outside[y0:y1, x0:x1] = alpha <= 0
        self.assertTrue(np.array_equal(out[outside], self.base[outside]))          # bit-exact
        core = np.zeros(self.base.shape[:2], bool)
        core[y0:y1, x0:x1] = alpha >= 1.0
        self.assertTrue(core.any())
        self.assertTrue((out[core] == round(0.25 * 255)).all())                     # the patch
        self.assertEqual(out.dtype, self.base.dtype)

    def test_feather_is_monotonic_from_the_edge(self):
        alpha = hires.feather_alpha(self.mask, 8)
        self.assertEqual(float(alpha[~self.mask].max()), 0.0)
        row = alpha[45, 30:61]                                                       # edge -> centre
        self.assertTrue(np.all(np.diff(row) >= -1e-6))
        self.assertGreater(row[0], 0.0)
        self.assertLess(row[0], 0.2)
        self.assertEqual(float(row[-1]), 1.0)
        d = hires._dist_inside(self.mask)
        self.assertTrue(np.all(np.diff(alpha[self.mask][np.argsort(d[self.mask])]) >= -1e-6))
        # a mask that leaves through the left border has no edge there
        m = np.zeros((20, 30), bool)
        m[2:18, 0:20] = True
        self.assertLess(hires.feather_alpha(m, 4)[10, 0], 0.2)
        self.assertEqual(hires.feather_alpha(m, 4, open_edges=("left",))[10, 0], 1.0)

    def test_16_bit_base(self):
        base16 = self.base.astype(np.uint16) * 257
        out = hires.composite_region(base16, self.patch, self.box, self.mask, feather_px=4)
        self.assertEqual(out.dtype, np.uint16)
        self.assertEqual(int(out[60, 100, 0]), round(0.25 * 65535))
        self.assertTrue(np.array_equal(out[:20], base16[:20]))

    def test_color_transfer_moves_patch_toward_the_ring(self):
        rng = np.random.default_rng(3)
        mask = np.zeros((80, 80), bool)
        mask[20:60, 20:60] = True
        orig = np.clip(rng.normal([0.45, 0.40, 0.35], 0.05, (80, 80, 3)), 0, 1).astype(np.float32)
        # the model's drift: brighter, bluer, flatter
        gen = np.clip((orig - orig.mean((0, 1))) * 0.7 + orig.mean((0, 1)) + [0.08, 0.06, 0.15], 0, 1).astype(np.float32)
        sel = hires.ring(mask, 6)
        params = hires.color_params(gen, orig, sel)
        fixed = hires.apply_color(gen, params)
        lab_o, lab_g, lab_f = (hires.to_lab(x)[sel] for x in (orig, gen, fixed))
        for c in range(3):
            self.assertLess(abs(lab_f[:, c].mean() - lab_o[:, c].mean()), abs(lab_g[:, c].mean() - lab_o[:, c].mean()))
        self.assertLess(abs(lab_f[:, 0].std() - lab_o[:, 0].std()), abs(lab_g[:, 0].std() - lab_o[:, 0].std()))
        self.assertLess(np.abs(fixed - orig)[sel].mean(), np.abs(gen - orig)[sel].mean() * 0.5)
        # chroma spread is clamped: a grey ring cannot desaturate a colourful person by more than 1/1.15
        self.assertTrue(np.all(hires.color_gain(params)[1:] >= 1 / 1.15 - 1e-6))
        self.assertIsNone(hires.color_params(gen, orig, np.zeros_like(sel)))
        sm = hires.smooth_params([params, None, params * 1.1])
        self.assertTrue(np.allclose(sm[1], (params + params * 1.1) / 2))

    def test_grain_estimate_on_synthetic_noise(self):
        rng = np.random.default_rng(5)
        yy, xx = np.mgrid[0:540, 0:960]
        smooth = (60 + 0.1 * xx + 0.05 * yy + 20 * np.sin(xx / 90.0)).astype(np.float32)
        edges = smooth.copy()
        edges[:, 480:] += 60                                                          # a hard edge
        for sigma in (1.0, 3.0, 6.0):
            noisy = edges + rng.normal(0, sigma, edges.shape).astype(np.float32)
            est = hires.estimate_noise(noisy)
            self.assertAlmostEqual(est, sigma, delta=0.12 * sigma + 0.1, msg=f"sigma {sigma}: {est}")
        self.assertLess(hires.estimate_noise(smooth), 0.2)
        # crushed black carries no noise and is ignored
        crushed = np.zeros_like(smooth)
        crushed[:, 480:] = 100 + rng.normal(0, 4, (540, 480))
        self.assertAlmostEqual(hires.estimate_noise(crushed), 4.0, delta=0.5)
        # added grain is measured back at the requested level
        img = np.full((300, 300, 3), 0.5, np.float32)
        g = hires.add_grain(img, 4 / 255.0, np.random.default_rng(0))
        self.assertAlmostEqual(np.std(g - img) * 255, 4.0, delta=0.2)
        self.assertAlmostEqual(hires.estimate_noise(g[..., 1] * 255), 4.0, delta=0.4)

    def test_light_wrap_only_touches_the_edge(self):
        patch = np.zeros((60, 60, 3), np.float32)
        orig = np.ones((60, 60, 3), np.float32)
        mask = np.zeros((60, 60), bool)
        mask[10:50, 10:50] = True
        out = hires.light_wrap(patch, orig, mask, radius=3, strength=0.5)
        self.assertGreater(out[30, 10, 0], 0.1)          # edge picks up the background light
        self.assertLess(out[30, 30, 0], 1e-3)            # centre unchanged
        self.assertIs(hires.light_wrap(patch, orig, mask, 3, 0.0), patch)

    def test_fps_map_matches_ffmpeg(self):
        """The composite picks the 16 fps frame for each output frame exactly as ffmpeg's fps filter does."""
        ff = _bin("ffmpeg")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "idx.mp4"
            frames = np.stack([np.full((16, 16), i * 7, np.uint8) for i in range(33)])
            subprocess.run([ff, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "16x16", "-r", "16",
                            "-i", "-", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", "-video_track_timescale",
                            "16", str(src)], input=frames.tobytes(), check=True)
            for rate, dur in (("30/1", 2.0), ("25/1", 2.0), ("60/1", 2.0), ("30000/1001", 2.0)):
                out = subprocess.run([ff, "-v", "error", "-i", str(src), "-vf",
                                      f"setpts=PTS-STARTPTS,fps={rate},tpad=stop_mode=clone:stop_duration=1,"
                                      f"trim=end={dur}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                     check=True, capture_output=True).stdout
                got = [int(round(v / 7)) for v in np.frombuffer(out, np.uint8).reshape(-1, 256)[:, 0]]
                self.assertEqual(hires.fps_map(len(got), Fraction(rate), 33), got, rate)
                self.assertEqual(len(got), math.ceil(dur * float(Fraction(rate)) - 1e-6))


# ----------------------------------------------------------------------------- Log
class LogColorTests(unittest.TestCase):
    M = np.array([[0.8, 0.15, 0.05], [0.1, 0.8, 0.1], [0.05, 0.15, 0.8]], np.float32)

    def synthetic_forward(self, x):
        """A camera-like Log curve (10 stops), a gamut matrix and a display tone curve."""
        lin = (0.18 * np.exp2((np.asarray(x, np.float32) - 0.45) * 10.0)) @ self.M.T
        return np.power(lin / (lin + 0.3), 1 / 1.1)

    def test_cube_round_trip(self):
        lut = logcolor.lut_from_function(self.synthetic_forward, 9)
        with tempfile.TemporaryDirectory() as tmp:
            p = logcolor.write_cube(lut, Path(tmp) / "x.cube", "test")
            back = logcolor.read_cube(p)
        self.assertTrue(np.allclose(back, lut, atol=1e-6))
        ident = logcolor.identity_lut(5)
        x = np.random.default_rng(0).uniform(0, 1, (100, 3)).astype(np.float32)
        self.assertTrue(np.allclose(logcolor.apply_lut(x, ident), x, atol=1e-6))

    def test_inverse_round_trip_under_one_percent(self):
        forward = logcolor.lut_from_function(self.synthetic_forward, 33)
        inverse, stats = logcolor.invert_lut(forward, 33)
        rt = logcolor.round_trip_error(forward, inverse, lo=0.1, hi=0.9, chroma=0.1)
        self.assertLess(rt["log_max"], 0.01, rt)        # Log code value back within 1 %
        self.assertLess(rt["display_max"], 0.01, rt)
        exact = logcolor.LogInverter(forward, inverse)
        rt2 = logcolor.round_trip_error(forward, exact, samples=4000, lo=0.1, hi=0.9, chroma=0.1)
        self.assertLess(rt2["display_p99"], 0.006, rt2)  # per-colour solve: 8-bit quantisation only
        self.assertGreater(exact.solved, 100)
        with tempfile.TemporaryDirectory() as tmp:
            p = logcolor.save_inverse(inverse, Path(tmp) / "inv.npz")
            again = logcolor.load_inverse(p)
        y = logcolor.apply_lut(np.full((4, 3), 0.5, np.float32), forward)
        self.assertTrue(np.allclose(logcolor.apply_inverse(y, again), 0.5, atol=0.005))

    def test_crushed_black_comes_back_at_the_cameras_black_level(self):
        """A LUT that maps every Log code below 0.2 to display black: the inverse of black is 0.2 (where real black
        footage sits), not 0, so generated blacks do not print a darker hole into the Log shadows."""
        def crush(x):
            return np.clip((np.asarray(x, np.float32) - 0.2) / 0.7, 0.0, 1.0)
        forward = logcolor.lut_from_function(crush, 33)
        inverse, _ = logcolor.invert_lut(forward)
        exact = logcolor.LogInverter(forward, inverse)
        black = exact(np.zeros((1, 3), np.float32))
        # 33-point LUT: the last grid code still mapping to black is 6/32 = 0.1875
        self.assertTrue(np.allclose(black, 0.1875, atol=0.01), black)
        self.assertTrue(np.allclose(logcolor.apply_inverse(np.zeros((1, 3), np.float32), inverse), 0.1875, atol=0.01))

    def test_apple_log_curve(self):
        # Apple's published constants: 18 % grey encodes to ~0.488, 0 -> ~0.15
        k = logcolor.APPLE_LOG
        enc = lambda r: k["gamma"] * math.log2(r + k["beta"]) + k["delta"]   # noqa: E731
        self.assertAlmostEqual(float(logcolor.apple_log_decode(np.float32(enc(0.18)))), 0.18, places=4)
        self.assertAlmostEqual(enc(0.18), 0.488, places=3)
        y = logcolor.apple_log_to_rec709(np.array([[0.2] * 3, [0.4877] * 3, [0.8] * 3], np.float32))
        self.assertTrue(np.all(np.diff(y[:, 1]) > 0) and 0.3 < y[1, 1] < 0.6)

    def test_parse_c101_probe(self):
        rec709 = ("source.mp4\n  1920×1080 30.000 fps · avc1 · 0:02 · 1 Mb/s · \n"
                  "  tags: ITU_R_709_2 / ITU_R_709_2 / ITU_R_709_2 · video levels\n"
                  "  profile: Rec.709 video (BT.1886) [generic.rec709] — No log tag found "
                  "(Assumed — pick the profile if this is log)\n")
        info = logcolor.parse_c101_probe(rec709)
        self.assertEqual(info["profile"], "generic.rec709")
        self.assertFalse(info["log"])
        applelog = ("IMG_0001.MOV\n  3840×2160 29.970 fps · hvc1 · 0:10 · 80 Mb/s · 10-bit\n"
                    "  tags: ITU_R_2020 / - / ITU_R_2020 · log com.apple.rec2020.apple-log · video levels\n"
                    "  camera: iPhone 16 Pro\n"
                    "  profile: Apple Log [apple.log] — Apple Log key in the file (From the file's own tags)\n")
        info = logcolor.parse_c101_probe(applelog)
        self.assertEqual((info["profile"], info["log"]), ("apple.log", True))
        self.assertEqual(info["log_tag"], "com.apple.rec2020.apple-log")
        hlg = "x\n  tags: a / b / c · video levels\n  profile: iPhone HDR [apple.hdr] — HLG transfer tag (x)\n"
        self.assertFalse(logcolor.parse_c101_probe(hlg)["log"])
        self.assertIsNone(logcolor.parse_c101_probe("garbage"))

    def test_detect_forced_and_plain_files(self):
        with mock.patch.dict(os.environ, {"ZEUS_FX_LOG_PROFILE": "apple.log"}):
            d = logcolor.detect([])
            self.assertTrue(d["log"])
            self.assertEqual(d["profile"], "apple.log")
        with mock.patch.dict(os.environ, {"ZEUS_FX_LOG_PROFILE": "none"}):
            self.assertFalse(logcolor.detect(["/nope"])["log"])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ZEUS_C101": "/nonexistent/c101"}):
            clip = Path(tmp) / "plain.mp4"
            _run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=10:duration=0.3",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-color_trc", "bt709", str(clip)])
            d = logcolor.detect([clip])
            self.assertFalse(d["log"])
            self.assertEqual(d["via"], "tags")
            rec = logcolor.prepare(Path(tmp), [clip])
            self.assertFalse(rec["log"])
            self.assertFalse((Path(tmp) / "color").exists())

    def test_prepare_apple_log_without_cinema101(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"ZEUS_FX_LOG_PROFILE": "apple.log", "ZEUS_C101": "/nonexistent/c101"}):
            rec = logcolor.prepare(Path(tmp), [])
            self.assertTrue(rec["log"])
            self.assertIn("approximate", rec["forward_how"])
            self.assertTrue((Path(tmp) / rec["forward_lut"]).is_file())
            self.assertTrue((Path(tmp) / rec["inverse_lut"]).is_file())
            self.assertLess(rec["round_trip"]["per_colour"]["display_p99"], 0.02)


# ----------------------------------------------------------------------------- upscale
# the real install (the tests run with a throwaway ZEUS_EFFECTS_HOME)
INSTALLED_ESRGAN = Path(os.environ.get("ZEUS_REALESRGAN_DIR", "") or
                        Path.home() / "Library/Application Support/YouTube Zeus/Effects/tools/realesrgan")


class UpscaleTests(unittest.TestCase):
    def test_model_choice(self):
        self.assertIsNone(upscale.model_for("fast", 1.05))
        self.assertEqual(upscale.model_for("fast", 1.8), ("realesr-animevideov3", 2))
        self.assertEqual(upscale.model_for("fast", 2.6), ("realesr-animevideov3", 3))
        self.assertEqual(upscale.model_for("fast", 5.0), ("realesr-animevideov3", 4))
        self.assertEqual(upscale.model_for("best", 1.8), ("realesrgan-x4plus", 4))
        self.assertIsNone(upscale.model_for("lanczos", 4.0))

    @unittest.skipIf(cv2 is None, "cv2 not available")
    def test_falls_back_to_lanczos_without_the_binary(self):
        frames = [np.full((16, 24, 3), 100, np.uint8)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"ZEUS_REALESRGAN_DIR": tmp}):
            out, info = upscale.upscale_frames(frames, 48, 32, "fast")
        self.assertEqual(out[0].shape, (32, 48, 3))
        self.assertIn("not installed", info["fallback"])
        self.assertEqual(info["model"], "lanczos")

    @unittest.skipIf(cv2 is None or not (INSTALLED_ESRGAN / upscale.BINARY).is_file(),
                     "Real-ESRGAN not installed (setup-engines.sh --upscaler)")
    def test_realesrgan_runs(self):
        img = np.zeros((32, 48, 3), np.uint8)
        img[8:24, 12:36] = 200
        with mock.patch.dict(os.environ, {"ZEUS_REALESRGAN_DIR": str(INSTALLED_ESRGAN)}):
            out, info = upscale.upscale_frames([img], 96, 64, "fast")
        self.assertNotIn("fallback", info)
        self.assertEqual((info["model"], info["scale"]), ("realesr-animevideov3", 2))
        self.assertEqual(out[0].shape, (64, 96, 3))
        self.assertGreater(int(out[0][32, 48, 0]), 150)


# ----------------------------------------------------------------------------- render (ffmpeg end to end)
def make_crops_job(root: Path, gen_color=(230, 20, 20), window=(0, 17)) -> tuple[Path, dict]:
    """A job with source.mp4 (320x180, 30 fps, audio), mask.mp4 (work size 160x90, 16 fps, a box) and the crops
    vace would write: one window, box around the mask, generated frames solid `gen_color`."""
    job = root / "job"
    job.mkdir(parents=True)
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi",
          "-i", "testsrc=size=320x180:rate=30:duration=1,noise=alls=12:allf=t",   # every frame different
          "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
          "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "aac", "-shortest", str(job / "source.mp4")])
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(job / "source.mp4"), "-vn", "-c:a", "copy",
          str(job / "audio.m4a")])
    _run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:size=160x90:rate=16",
          "-frames:v", "17", "-vf", "drawbox=x=60:y=20:w=40:h=50:color=white:t=fill",
          "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(job / "mask.mp4")])
    masks = hires.read_gray_frames(job / "mask.mp4", 160, 90)
    plans = cropbox.plan_window_boxes(masks, [window], 320, 180)
    plan = plans[0]
    gw, gh = plan["gen"]
    gw, gh = gw // 4, gh // 4          # small generation size keeps the test fast (the code does not care)
    wdir = job / "result-crops" / "w00"
    for sub in ("gen", "base", "mask"):
        (wdir / sub).mkdir(parents=True)
    for j in range(window[1] - window[0]):
        gen = np.zeros((gh, gw, 3), np.uint8)
        gen[:] = gen_color
        cv2.imwrite(str(wdir / "gen" / f"{j:04d}.png"), gen[:, :, ::-1])
        cv2.imwrite(str(wdir / "base" / f"{j:04d}.png"), gen[:, :, ::-1])
        m = cropbox.crop_resize(masks[min(j, len(masks) - 1)], cropbox.scale_box(plan["box"], 0.5, 0.5), gw, gh)
        cv2.imwrite(str(wdir / "mask" / f"{j:04d}.png"), np.where(m > 127, 255, 0).astype(np.uint8))
    manifest = {"version": 1, "dir": "result-crops", "fps": 16, "source_size": [320, 180], "work_size": [160, 90],
                "total": 17, "windows": [{**plan, "gen": [gw, gh], "index": 0, "start": window[0], "end": window[1],
                                          "valid": window[1] - window[0], "dir": "w00"}]}
    (job / "result-crops.json").write_text(json.dumps(manifest))
    return job, manifest


@unittest.skipIf(cv2 is None, "cv2 not available")
class VaceRunCropTests(unittest.TestCase):
    """vace.run end to end with the model replaced by a stand-in: boxes, crops, carry-over between windows in
    different boxes, result-raw.mp4, result-crops.json and the returned info (no weights, no GPU)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-vacerun-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_with_a_stand_in_pipeline(self):
        try:
            import torch
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("torch / Pillow not available")
        from zfx.engines import vace
        job = self.tmp / "job"
        job.mkdir()
        # 3 s source 640x360 30 fps; work/control/mask 320x180 16 fps (48 frames); a person moving right
        _run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=3",
              "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(job / "source.mp4")])
        _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", str(job / "source.mp4"), "-vf",
              "fps=16:start_time=0,scale=320:180", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
              str(job / "work.mp4")])
        shutil.copyfile(job / "work.mp4", job / "control.mp4")
        masks = np.zeros((48, 180, 320), np.uint8)
        for i in range(48):
            masks[i, 40:140, 40 + 4 * i:80 + 4 * i] = 255
        subprocess.run([_bin("ffmpeg"), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "320x180",
                        "-r", "16", "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        str(job / "mask.mp4")], input=masks.tobytes(), check=True)
        (job / "job.json").write_text(json.dumps({"mode": "depth"}))
        calls = []

        class Out:
            def __init__(self, frames):
                self.frames = [frames]

        class FakePipe:
            zeus_transformer_weights = "transformer-half"

            def __call__(self, video, mask, height, width, num_frames, callback_on_step_end=None, **kw):
                vid = np.stack([np.asarray(v) for v in video]).astype(np.float32)
                calls.append({"video": vid, "mask": np.stack([np.asarray(m) for m in mask]), "size": (width, height),
                              "n": num_frames})
                for step in range(kw["num_inference_steps"]):
                    callback_on_step_end(self, step, 0, {})
                return Out(np.clip(vid / 255.0 * 0.5 + 0.25, 0, 1))   # recognisable: contrast halved

        with mock.patch.object(vace, "check_model", return_value=self.tmp), \
                mock.patch.object(vace, "get_prompt_embeds",
                                  return_value=(torch.zeros(1, 512, 4096), torch.zeros(1, 512, 4096), "cache")), \
                mock.patch.object(vace, "load_pipeline", return_value=FakePipe()), \
                mock.patch.object(vace, "pick_device", return_value="cpu"), \
                mock.patch.object(vace, "mps_runtime", return_value=__import__("contextlib").nullcontext()), \
                mock.patch.object(vace, "_load_refs", return_value=[Image.new("RGB", (64, 64), "white")]):
            info = vace.run(job, ["ref.jpg"], job / "result-raw.mp4", None, frames=17, steps=2, seed=1)
        windows = vace.plan_windows(48, 17, vace.OVERLAP)
        self.assertEqual(len(calls), len(windows))
        man = json.loads((job / "result-crops.json").read_text())
        self.assertEqual(man["source_size"], [640, 360])
        self.assertEqual(man["base_from"], "source.mp4")
        self.assertEqual(len(man["windows"]), len(windows))
        self.assertTrue(info["crop"]["enabled"])
        self.assertEqual([b["box"] for b in info["crop"]["boxes"]], [w["box"] for w in man["windows"]])
        boxes = [w["box"] for w in man["windows"]]
        self.assertTrue(all(not w["full_frame"] for w in man["windows"]))
        self.assertLess(boxes[0][0], boxes[-1][0])                          # the box follows the person
        for call, w in zip(calls, man["windows"]):
            self.assertEqual(call["size"], tuple(w["gen"]))
            self.assertEqual(call["n"], 17)
            wdir = job / "result-crops" / w["dir"]
            self.assertEqual(len(list((wdir / "gen").glob("*.png"))), w["valid"])
            self.assertEqual(len(list((wdir / "base").glob("*.png"))), w["valid"])
        # window 2 starts from window 1's generated frames redrawn into its own box, mask black there
        w0, w1 = man["windows"][0], man["windows"][1]
        carry = w0["start"] + w0["valid"] - w1["start"]
        self.assertGreater(carry, 0)
        self.assertTrue((calls[1]["mask"][:carry] == 0).all())
        self.assertTrue((calls[1]["mask"][carry:] > 0).any())
        gen0 = cv2.imread(str(job / "result-crops" / "w00" / "gen" / f"{w1['start'] - w0['start']:04d}.png"))[:, :, ::-1]
        expect = cropbox.reproject(gen0, w0["box"], w1["box"], w1["gen"][0], w1["gen"][1],
                                   fill=calls[1]["video"][0].astype(np.uint8))
        self.assertLess(np.abs(calls[1]["video"][0] - expect.astype(np.float32)).mean(), 1.5)
        # result-raw.mp4: work size, 16 fps, every frame
        probe = generate.probe_video(job / "result-raw.mp4")
        self.assertEqual((probe["width"], probe["height"], probe["frames"]), (320, 180, 48))
        self.assertEqual(probe["fps"], Fraction(16))


@unittest.skipIf(cv2 is None, "cv2 not available")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zfx-hires-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_render_composites_at_source_size_keeps_audio_and_clock(self):
        job, manifest = make_crops_job(self.tmp)
        out = job / "final.mp4"
        src_i = generate.probe_video(job / "source.mp4")
        info = hires.render(job, manifest, job / "source.mp4", src_i, job / "mask.mp4", job / "audio.m4a", out,
                            "AI-generated with YouTube Zeus Effects (vace)", upscale_mode="lanczos")
        self.assertTrue(info["composited_in_mask"] and info["hires"])
        self.assertEqual((info["width"], info["height"]), (320, 180))
        self.assertEqual(audio_packets_sha(out), audio_packets_sha(job / "audio.m4a"))   # untouched audio
        probe = generate.probe_video(out)
        self.assertEqual(probe["fps"], Fraction(30))
        self.assertAlmostEqual(probe["duration"], src_i["duration"], delta=0.05)
        self.assertEqual(info["color_tags"]["matrix"], "bt709")
        f, s = decode(out, 320, 180), decode(job / "source.mp4", 320, 180)
        self.assertEqual(len(f), len(s))
        k = 15
        centre = f[k, 90, 160]                    # mask box centre in source pixels: (120+40, 40+50)
        self.assertGreater(centre[0], 180)
        self.assertLess(centre[1], 80)
        # outside the mask: the original (only the final H.264 encode differs)
        self.assertLess(np.abs(f[k, 0:30, 0:100] - s[k, 0:30, 0:100]).mean(), 3.0)
        self.assertLess(np.abs(f[k, 150:, 250:] - s[k, 150:, 250:]).mean(), 3.0)
        # no time stretch: every final frame still shows the source frame of the same index outside the mask
        for k in (2, 17, 29):
            diffs = [np.abs(f[k, :30, :100] - s[j, :30, :100]).mean() for j in range(len(s))]
            self.assertEqual(int(np.argmin(diffs)), k)

    def test_generate_uses_the_full_resolution_composite_for_vace_crops(self):
        """run_generate with a stand-in vace engine that writes crops: final.mp4 comes from zfx.hires, the record
        keeps the colour check and the composite details."""
        from test_engines import make_job, make_ref, args as gen_args
        job = make_job(self.tmp)
        crops_job, manifest = make_crops_job(self.tmp / "crops")
        # the stand-in's crops refer to a 320x180 source; the job's source is 640x360: scale the boxes
        manifest["source_size"] = [640, 360]
        for w in manifest["windows"]:
            w["box"] = [v * 2 for v in w["box"]]
        manifest["work_size"] = [416, 240]

        class FakeVace:
            NAME, LABEL = "vace", "fake vace"

            def status(self):
                return {"ready": True, "message": "ready"}

            def run(self, job_dir, refs, out_path, progress, **options):
                self.options = options
                shutil.copytree(crops_job / "result-crops", Path(job_dir) / "result-crops", dirs_exist_ok=True)
                (Path(job_dir) / "result-crops.json").write_text(json.dumps(manifest))
                shutil.copyfile(Path(job_dir) / "work.mp4", out_path)
                return {"engine": "vace", "crop": {"enabled": True}}

        fake = FakeVace()
        with mock.patch.object(generate, "get_engine", return_value=fake), \
                mock.patch.dict(os.environ, {"ZEUS_C101": "/nonexistent/c101", "ZEUS_REALESRGAN_DIR": str(self.tmp)}):
            result = generate.run_generate(gen_args(engine="vace", ref=[str(make_ref(self.tmp))], upscale="fast"),
                                           job, lambda *a, **k: None)
        self.assertTrue(result["mux"]["hires"])
        self.assertIn("not installed", result["mux"]["upscale"][0]["fallback"])
        self.assertEqual(fake.options.get("upscale"), "fast")
        self.assertNotIn("source_lut", fake.options)
        rec = json.loads((job / "job.json").read_text())["generations"][-1]
        self.assertFalse(rec["color"]["log"])
        self.assertTrue(rec["mux"]["composited_in_mask"])
        self.assertEqual(audio_packets_sha(job / "final.mp4"), audio_packets_sha(job / "audio.m4a"))


if __name__ == "__main__":
    unittest.main()
