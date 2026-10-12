"""zfx.parts, zfx.things and zfx.parts_cli on synthetic data: no weights, no network, no FaceFusion venv.

Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_parts.py' -v
"""

from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path

import numpy as np

import synth
from zfx import parts, things
from zfx.io import VideoWriter, count_frames
from zfx.jobs import Job
from zfx.pose import B, build_motion, write_motion
from zfx.review import approve, verify

os.environ["ZEUS_EFFECTS_NO_DOWNLOAD"] = "1"   # never fetch models from a test

W, H = 160, 120


# --------------------------------------------------------------------------- synthetic people
def stick_figure(vis: float = 1.0) -> np.ndarray:
    """33 x [x, y, z, visibility] normalized landmarks of a person standing in the middle, facing the camera
    (the person's left side is on the picture's right)."""
    p = np.zeros((33, 4))
    p[:, 3] = vis

    def put(name, x, y):
        p[B[name], :2] = (x, y)

    put("nose", 0.50, 0.20)
    put("left_eye", 0.53, 0.18), put("right_eye", 0.47, 0.18)
    put("left_eye_inner", 0.515, 0.18), put("right_eye_inner", 0.485, 0.18)
    put("left_eye_outer", 0.545, 0.18), put("right_eye_outer", 0.455, 0.18)
    put("left_ear", 0.56, 0.20), put("right_ear", 0.44, 0.20)
    put("mouth_left", 0.52, 0.24), put("mouth_right", 0.48, 0.24)
    put("left_shoulder", 0.60, 0.35), put("right_shoulder", 0.40, 0.35)
    put("left_elbow", 0.70, 0.50), put("right_elbow", 0.30, 0.50)
    put("left_wrist", 0.75, 0.65), put("right_wrist", 0.25, 0.65)
    put("left_index", 0.77, 0.70), put("right_index", 0.23, 0.70)
    put("left_pinky", 0.78, 0.69), put("right_pinky", 0.22, 0.69)
    put("left_thumb", 0.75, 0.69), put("right_thumb", 0.25, 0.69)
    put("left_hip", 0.56, 0.60), put("right_hip", 0.44, 0.60)
    put("left_knee", 0.56, 0.80), put("right_knee", 0.44, 0.80)
    put("left_ankle", 0.56, 0.95), put("right_ankle", 0.44, 0.95)
    put("left_heel", 0.56, 0.97), put("right_heel", 0.44, 0.97)
    put("left_foot_index", 0.60, 0.98), put("right_foot_index", 0.40, 0.98)
    return p


def right_hand21() -> np.ndarray:
    """21 x [x, y, z] normalized points of the right hand hanging at the right wrist of stick_figure()."""
    hand = np.zeros((21, 3))
    hand[0, :2] = (0.25, 0.65)
    tips = [(0.21, 0.67), (0.22, 0.72), (0.245, 0.74), (0.27, 0.73), (0.29, 0.70)]   # thumb .. pinky
    for f, tip in enumerate(tips):
        base = np.array([0.25, 0.65])
        for j in range(4):
            t = (j + 1) / 4.0
            hand[1 + f * 4 + j, :2] = base + t * (np.array(tip) - base)
    return hand


def face478(iris_dx: float = 0.0, lid_gap: float = 0.02) -> np.ndarray:
    """478 x [x, y, z] normalized mesh with only the points zfx.parts reads placed deliberately: the eye corners
    and lids, the irises (shifted by iris_dx x the eye's half width), the chin, the forehead, the jaw sides."""
    f = np.zeros((478, 3))
    f[:, :2] = (0.5, 0.2)
    f[10, :2] = (0.5, 0.13)            # forehead
    f[152, :2] = (0.5, 0.27)           # chin
    f[234, :2] = (0.44, 0.20)          # jaw sides
    f[454, :2] = (0.56, 0.20)
    for outer, inner, upper, lower, iris, cx in ((33, 133, (160, 158), (153, 144), 468, 0.47),
                                                 (263, 362, (385, 387), (373, 380), 473, 0.53)):
        half_w = 0.015
        f[outer, :2] = (cx - half_w if outer == 33 else cx + half_w, 0.18)
        f[inner, :2] = (cx + half_w if outer == 33 else cx - half_w, 0.18)
        for k in upper:
            f[k, :2] = (cx, 0.18 - lid_gap / 2.0)
        for k in lower:
            f[k, :2] = (cx, 0.18 + lid_gap / 2.0)
        f[iris, :2] = (cx + iris_dx * half_w, 0.18)
    return f


def person_mask() -> np.ndarray:
    m = np.zeros((H, W), bool)
    m[2:H, int(0.18 * W):int(0.82 * W)] = True      # from the top of the hat down to the feet
    return m


# --------------------------------------------------------------------------- legend and palette
class LegendTests(unittest.TestCase):
    def test_ids_fixed_and_names_unique(self):
        entries = parts.legend()
        self.assertEqual([e["id"] for e in entries], list(range(29)))
        self.assertEqual(len({e["name"] for e in entries}), 29)
        self.assertEqual(parts.NAME_OF[parts.TORSO], "torso")
        self.assertEqual(parts.ID_OF["left-hand"], parts.L_HAND)
        self.assertEqual(entries[parts.BG_FAR]["name"], "background-far")
        self.assertEqual(entries[parts.GAZE]["name"], "gaze")

    def test_things_append_without_moving_the_base(self):
        base = parts.legend()
        with_things = parts.legend(["cup", "laptop"])
        self.assertEqual(with_things[:29], base)
        self.assertEqual([e["id"] for e in with_things[29:]], [40, 41])
        self.assertEqual([e["name"] for e in with_things[29:]], ["thing:cup", "thing:laptop"])
        self.assertEqual(with_things[29]["prompt"], "cup")
        self.assertNotEqual(with_things[29]["hue"], with_things[30]["hue"])
        self.assertEqual(with_things[29]["color"], [255, 0, 255])    # magenta at value 1
        with self.assertRaises(parts.PartsError):
            parts.legend([f"t{i}" for i in range(parts.MAX_THINGS + 1)])

    def test_colours_are_deterministic_hsv_at_full_value(self):
        for entry in parts.legend():
            self.assertEqual(entry["color"], parts._rgb(entry["hue"], entry["saturation"]))
            self.assertEqual(entry["hex"], "#%02x%02x%02x" % tuple(entry["color"]))
        self.assertEqual(parts.legend()[parts.BODY_OTHER]["color"], [255, 255, 255])   # grey: no saturation
        hue, sat = parts.class_table(["cup"])
        self.assertEqual(hue.shape, (256,))
        self.assertEqual(sat[40], 1.0)
        self.assertEqual(sat[parts.BODY_OTHER], 0.0)

    def test_render_value_follows_depth_and_gaze_is_full(self):
        hue, sat = parts.class_table()
        index = np.full((4, 6), parts.TORSO, np.uint8)
        index[0, 0] = parts.GAZE
        depth = np.zeros((4, 6), np.float32)
        depth[:, 3:] = 1.0
        rgb = parts.render_map(index, depth, hue, sat, feather_px=0)
        self.assertEqual(rgb[2, 5].tolist(), parts.legend()[parts.TORSO]["color"])      # nearest: legend colour
        self.assertLess(int(rgb[2, 1].max()), int(rgb[2, 5].max()))                    # farthest: darker
        self.assertAlmostEqual(rgb[2, 1].max() / 255.0, parts.V_FLOOR, delta=0.01)
        self.assertEqual(rgb[0, 0].tolist(), parts.legend()[parts.GAZE]["color"])      # gaze: full value at depth 0

    def test_feathered_edge_is_a_blend(self):
        hue, sat = parts.class_table()
        index = np.full((20, 40), parts.TORSO, np.uint8)
        index[:, 20:] = parts.L_HAND
        depth = np.ones((20, 40), np.float32)
        rgb = parts.render_map(index, depth, hue, sat, feather_px=2)
        a, b = np.array(parts.legend()[parts.TORSO]["color"]), np.array(parts.legend()[parts.L_HAND]["color"])
        edge = rgb[10, 20].astype(int)
        self.assertFalse(np.array_equal(edge, a) or np.array_equal(edge, b), edge)
        self.assertEqual(rgb[10, 5].tolist(), a.tolist())                              # interior untouched
        self.assertEqual(rgb[10, 35].tolist(), b.tolist())

    def test_control_keeps_original_outside_and_tints_inside(self):
        hue, sat = parts.class_table()
        work = np.full((20, 40, 3), (10, 200, 30), np.uint8)
        index = np.full((20, 40), parts.BG_MID, np.uint8)
        region = np.zeros((20, 40), bool)
        region[:, 20:] = True
        index[region] = parts.TORSO
        index[5, 30] = parts.GAZE
        depth = np.full((20, 40), 0.8, np.float32)
        out = parts.render_control(work, index, depth, region, hue, sat, feather_px=0)
        self.assertEqual(out[10, 5].tolist(), [10, 200, 30])
        inside = out[10, 30]
        self.assertGreater(int(inside[0]), int(inside[1]))                             # red tint over grey depth
        self.assertAlmostEqual(int(inside[0]) / 255.0, 0.8, delta=0.02)                # value = the depth
        self.assertEqual(out[5, 30].tolist(), [255, 255, 255])                         # gaze marker: white


# --------------------------------------------------------------------------- rasterization
class RasterTests(unittest.TestCase):
    def px(self, x, y):
        return int(round(y * H)), int(round(x * W))

    def test_parts_land_where_the_landmarks_are(self):
        canvas, unit = parts.rasterize_body(H, W, stick_figure(), None, right_hand21(), face478())
        self.assertAlmostEqual(unit, 0.2 * W, delta=0.5)                               # shoulder width
        self.assertEqual(canvas[self.px(0.5, 0.47)], parts.TORSO)
        self.assertEqual(canvas[self.px(0.65, 0.425)], parts.L_UPPER_ARM)
        self.assertEqual(canvas[self.px(0.35, 0.425)], parts.R_UPPER_ARM)
        self.assertEqual(canvas[self.px(0.725, 0.575)], parts.L_FOREARM)
        self.assertEqual(canvas[self.px(0.275, 0.575)], parts.R_FOREARM)
        self.assertEqual(canvas[self.px(0.765, 0.69)], parts.L_HAND)                   # pose wrist/index capsule
        self.assertEqual(canvas[self.px(0.245, 0.70)], parts.R_HAND)                   # 21-point hand hull
        self.assertEqual(canvas[self.px(0.56, 0.70)], parts.L_THIGH)
        self.assertEqual(canvas[self.px(0.44, 0.875)], parts.R_SHIN)
        self.assertEqual(canvas[self.px(0.58, 0.975)], parts.L_FOOT)
        self.assertEqual(canvas[self.px(0.5, 0.19)], parts.HEAD)
        self.assertEqual(canvas[self.px(0.5, 0.31)], parts.NECK)
        self.assertEqual(canvas[2, 2], 0)                                              # far corner: unassigned

    def test_invisible_landmarks_are_skipped(self):
        body = stick_figure()
        for name in ("left_hip", "left_knee", "left_ankle", "left_heel", "left_foot_index"):
            body[B[name], 3] = 0.1
        canvas, _ = parts.rasterize_body(H, W, body, None, None, None)
        self.assertFalse((canvas == parts.L_THIGH).any())
        self.assertFalse((canvas == parts.L_SHIN).any())
        self.assertTrue((canvas == parts.R_THIGH).any())
        self.assertTrue((canvas == parts.HEAD).any())                                  # head from the pose points

    def test_nothing_without_landmarks_and_head_from_face_only(self):
        canvas, unit = parts.rasterize_body(H, W, None, None, None, None)
        self.assertIsNone(unit)
        self.assertFalse(canvas.any())
        canvas, unit = parts.rasterize_body(H, W, None, None, None, face478())
        self.assertIsNotNone(unit)
        self.assertEqual(canvas[self.px(0.5, 0.19)], parts.HEAD)
        self.assertFalse((canvas == parts.TORSO).any())

    def test_torso_extends_down_without_hips(self):
        body = stick_figure()
        for name in ("left_hip", "right_hip"):
            body[B[name], 3] = 0.0
        canvas, _ = parts.rasterize_body(H, W, body, None, None, None)
        self.assertEqual(canvas[self.px(0.5, 0.55)], parts.TORSO)


class NearestFillTests(unittest.TestCase):
    def test_nearest_seed_wins_within_the_limit(self):
        index = np.zeros((40, 60), np.uint8)
        index[5:10, 5:10] = parts.TORSO
        index[30:35, 50:55] = parts.L_HAND
        person = np.ones((40, 60), bool)
        person[0, :] = False
        out = parts.nearest_fill(index, person, limit_px=100)
        self.assertEqual(out[12, 12], parts.TORSO)
        self.assertEqual(out[28, 48], parts.L_HAND)
        self.assertEqual(out[7, 7], parts.TORSO)                                       # seeds unchanged
        self.assertEqual(out[0, 30], 0)                                                # outside the person
        self.assertFalse((out[1:] == 0).any())

    def test_far_pixels_become_body_other(self):
        index = np.zeros((40, 60), np.uint8)
        index[5:10, 5:10] = parts.TORSO
        out = parts.nearest_fill(index, np.ones((40, 60), bool), limit_px=6)
        self.assertEqual(out[12, 12], parts.TORSO)
        self.assertEqual(out[35, 55], parts.BODY_OTHER)

    def test_no_seeds_everything_body_other(self):
        person = np.zeros((10, 10), bool)
        person[2:8, 2:8] = True
        out = parts.nearest_fill(np.zeros((10, 10), np.uint8), person, limit_px=50)
        self.assertTrue((out[person] == parts.BODY_OTHER).all())
        self.assertFalse(out[~person].any())


# --------------------------------------------------------------------------- depth bands and face detail
class DepthTests(unittest.TestCase):
    def test_luma_and_bands(self):
        grad = np.linspace(0, 1, 90, dtype=np.float32)[None].repeat(30, 0)
        rgb = np.repeat((grad * 255).astype(np.uint8)[:, :, None], 3, axis=2)
        luma = parts.depth_luma(rgb)
        self.assertAlmostEqual(float(luma[0, -1]), 1.0, delta=0.01)
        t1, t2 = parts.band_thresholds([grad], [np.zeros((30, 90), bool)])
        self.assertAlmostEqual(t1, 1 / 3, delta=0.03)
        self.assertAlmostEqual(t2, 2 / 3, delta=0.03)
        bands = parts.depth_bands(grad, (t1, t2), blur_px=0)
        self.assertEqual(bands[15, 5], parts.BG_FAR)
        self.assertEqual(bands[15, 45], parts.BG_MID)
        self.assertEqual(bands[15, 85], parts.BG_NEAR)

    def test_flat_depth_still_gives_two_thresholds(self):
        flat = np.full((10, 10), 0.5, np.float32)
        t1, t2 = parts.band_thresholds([flat], [np.zeros((10, 10), bool)])
        self.assertLess(t1, t2)


class FaceParseTests(unittest.TestCase):
    def test_head_box_framing(self):
        face = face478()
        box = parts.head_box(face, None, W, H)
        x0, y0, x1, y1 = box
        fx0, fy0, fx1, fy1 = parts.face_box(face, W, H)
        fw, fh = fx1 - fx0, fy1 - fy0
        self.assertAlmostEqual(x0, fx0 - 0.6 * fw, delta=1.1)
        self.assertAlmostEqual(x1, fx1 + 0.6 * fw, delta=1.1)
        self.assertAlmostEqual(y0, max(0, fy0 - fh), delta=1.1)
        self.assertAlmostEqual(y1, fy1 + 0.25 * fh, delta=1.1)
        self.assertIsNone(parts.head_box(None, None, W, H))
        self.assertIsNotNone(parts.head_box(None, stick_figure(), W, H))

    def test_override_only_head_neck_torso(self):
        classes = np.zeros((512, 512), np.uint8)
        classes[:150] = 18                      # hat
        classes[150:400] = 1                    # skin
        classes[400:] = 16                      # cloth
        classes[200:220, :50] = 0               # background: never overrides
        index = np.full((100, 100), parts.HEAD, np.uint8)
        index[:, 80:] = parts.L_HAND
        person = np.ones((100, 100), bool)
        person[:, :5] = False
        box = (0, 0, 100, 100)
        parts.apply_faceparse(index, classes, box, person)
        self.assertEqual(index[10, 50], parts.HAT)
        self.assertEqual(index[50, 50], parts.FACE_SKIN)
        self.assertEqual(index[90, 50], parts.TORSO)
        self.assertEqual(index[41, 2], parts.HEAD)                                     # bisenet background
        self.assertEqual(index[50, 90], parts.L_HAND)                                  # hands never overridden
        self.assertEqual(index[50, 2], parts.HEAD)                                     # outside the person mask
        self.assertIs(parts.apply_faceparse(index, None, box, person), index)

    def test_face_parse_frames_with_a_fake_backend(self):
        frames = np.zeros((3, H, W, 3), np.uint8)
        boxes = [(10, 10, 60, 60), None, (20, 20, 80, 90)]
        seen = []

        def fake(crops):
            seen.append(crops.shape)
            return np.full((len(crops), 512, 512), 17, np.uint8)

        out, info = parts.face_parse_frames(frames, boxes, lambda f, m: None, backend=fake)
        self.assertEqual(seen, [(2, 512, 512, 3)])
        self.assertIsNone(out[1])
        self.assertEqual(out[0].shape, (512, 512))
        self.assertEqual(info["frames"], 2)


# --------------------------------------------------------------------------- gaze
class GazeTests(unittest.TestCase):
    def test_centre_right_and_ear(self):
        g = parts.frame_gaze(face478(0.0), W, H)
        self.assertEqual(g["direction"], "centre")
        self.assertAlmostEqual(g["vector"][0], 0.0, delta=0.05)
        self.assertAlmostEqual(g["left"]["ear"], 0.02 * H / (0.03 * W), delta=0.02)  # lid gap / eye width
        g = parts.frame_gaze(face478(0.6), W, H)
        self.assertEqual(g["direction"], "right")
        self.assertEqual(g["left"]["direction"], "right")
        self.assertGreater(g["vector"][0], 0.4)
        g = parts.frame_gaze(face478(-0.6), W, H)
        self.assertEqual(g["direction"], "left")
        self.assertIsNone(parts.frame_gaze(face478()[:468], W, H))                      # no irises
        self.assertEqual(parts.gaze_direction(0.0, 0.5), "down")
        self.assertEqual(parts.gaze_direction(0.0, -0.5), "up")

    def test_blink_is_a_dip_not_a_low_baseline(self):
        ears = [0.30] * 10 + [0.08, 0.07] + [0.30] * 10 + [0.14] * 12
        blinks, openness = parts.blink_flags(ears)
        self.assertEqual([i for i, b in enumerate(blinks) if b], [10, 11])
        self.assertTrue(all(b is False for b in blinks[22:]))                            # narrowed, not blinking
        self.assertAlmostEqual(openness[0], 1.0, delta=0.01)
        self.assertLess(openness[25], 0.6)
        blinks, openness = parts.blink_flags([None, 0.3, None])
        self.assertEqual(blinks, [None, False, None])

    def test_marker_drawn_only_in_the_eyes(self):
        g = parts.frame_gaze(face478(0.6), W, H)
        index = np.zeros((H, W), np.uint8)
        parts.draw_gaze(index, g, np.ones((H, W), bool))
        ys, xs = np.nonzero(index == parts.GAZE)
        self.assertGreater(len(xs), 0)
        self.assertTrue(all(0.44 * W <= x <= 0.57 * W for x in xs))
        self.assertTrue(all(0.15 * H <= y <= 0.21 * H for y in ys))
        g["blink"] = True
        index[:] = 0
        parts.draw_gaze(index, g, np.ones((H, W), bool))
        self.assertFalse(index.any())


class TemporalTests(unittest.TestCase):
    def test_neighbours_outvote(self):
        a = np.array([[1, 2, 3]], np.uint8)
        b = np.array([[9, 2, 9]], np.uint8)
        c = np.array([[1, 5, 3]], np.uint8)
        self.assertEqual(parts.temporal_majority(a, b, c).tolist(), [[1, 2, 3]])
        self.assertEqual(parts.temporal_majority(None, b, c).tolist(), [[9, 2, 9]])


# --------------------------------------------------------------------------- things
class ThingsTests(unittest.TestCase):
    def test_parse_things(self):
        self.assertEqual(things.parse_things(" Microphone, laptop;laptop\n cap ,, "), ["microphone", "laptop", "cap"])
        self.assertEqual(things.parse_things(""), [])
        self.assertEqual(things.parse_things(None), [])
        with self.assertRaises(things.ThingsError):
            things.parse_things(",".join(f"t{i}" for i in range(things.MAX_THINGS + 1)))

    def test_sampling_and_interpolation(self):
        self.assertEqual(things.sample_indices(64, 2.0), [0, 8, 16, 24, 32, 40, 48, 56, 63])
        self.assertEqual(things.sample_indices(3, 2.0), [0, 2])
        self.assertEqual(things.sample_indices(0), [])
        sampled = np.zeros((2, 1, 2, 2), np.float32)
        sampled[1] = 1.0
        out = things.interpolate(sampled, [0, 4], 6)
        self.assertEqual(out.shape, (6, 1, 2, 2))
        self.assertEqual(int(out[2, 0, 0, 0]), 128)
        self.assertEqual(int(out[5, 0, 0, 0]), 255)                                    # held after the last sample

    def test_zoom_box_and_merge(self):
        masks = np.zeros((2, H, W), bool)
        masks[:, 60:100, 100:140] = True
        box = things.zoom_box(masks, W, H)
        self.assertEqual(box, (76, 36, W, H))                                          # grown 60 %, clipped
        big = np.zeros((1, H, W), bool)
        big[:, 10:110, 10:150] = True
        self.assertIsNone(things.zoom_box(big, W, H))
        self.assertIsNone(things.zoom_box(np.zeros((1, H, W), bool), W, H))
        full = np.zeros((1, 8, 8), np.float32)
        crop = np.full((1, 4, 4), 0.9, np.float32)
        merged = things.merge_zoom(full, crop, (80, 60, 160, 120), W, H)
        self.assertAlmostEqual(float(merged[0, 6, 6]), 0.9, delta=0.01)
        self.assertEqual(float(merged[0, 1, 1]), 0.0)

    def test_regions_hysteresis_and_min_area(self):
        p = np.zeros((2, 100, 100), np.float32)
        p[0, 10:40, 10:40] = 0.45                  # big blob, no strong pixel: dropped
        p[0, 60:90, 60:90] = 0.45
        p[0, 70, 70] = 0.8                         # one strong pixel: the blob stays
        p[1, 50, 50] = 0.95                        # strong but a single pixel: too small
        regions = things.things_regions(p, threshold=0.4, hysteresis=0.1, min_area=0.0005)
        self.assertFalse(regions[0, 20, 20])
        self.assertTrue(regions[0, 65, 65])
        self.assertFalse(regions[1].any())

    def test_detect_things_with_a_fake_backend(self):
        frames = np.zeros((10, H, W, 3), np.uint8)

        class Fake:
            size, name, device = 16, "fake", "cpu"
            calls = []

            def segment(self, batch, prompts):
                self.calls.append((len(batch), tuple(prompts), batch.shape[1:3]))
                out = np.zeros((len(batch), len(prompts), 16, 16), np.float32)
                out[:, 0, :4, :4] = 0.9
                return out

        found = things.detect_things(frames, ["cup", "ball"], lambda f, m: None, backend=Fake(), fps=2.0,
                                     template="a photo of a {}")
        self.assertEqual(found["prompts"], ["a photo of a cup", "a photo of a ball"])
        self.assertEqual(found["sampled"], [0, 8, 9])
        self.assertEqual(found["probs"].shape, (10, 2, 16, 16))
        self.assertEqual(int(found["probs"][5, 0, 0, 0]), 230)
        self.assertEqual(int(found["probs"][5, 1].max()), 0)
        self.assertIsNone(found["zoom"])
        self.assertEqual(found["max_prob"].shape, (10, 2))
        empty = things.detect_things(frames, [], lambda f, m: None, backend=Fake())
        self.assertEqual(empty["probs"].shape[1], 0)


# --------------------------------------------------------------------------- the whole step on a fake job
class FakeThings:
    """CLIPSeg stand-in: 'cup' is a blob in the top-left corner (outside the person), 'ball' is nowhere."""
    size, name, device, repo, revision = 32, "fake-clipseg", "cpu", "fake/clipseg", "0000"

    def segment(self, batch, prompts):
        out = np.zeros((len(batch), len(prompts), 32, 32), np.float32)
        out[:, 0, 1:6, 1:6] = 0.9
        return out


def fake_face_parse(crops):
    out = np.full((len(crops), 512, 512), 1, np.uint8)     # skin
    out[:, :120] = 18                                        # hat on top
    out[:, 440:] = 16                                        # cloth at the bottom
    return out


def make_job(home: Path, frames: int = 8) -> Job:
    job = Job.create("/tmp/clip.mp4", 0.0, frames / 16.0, "depth")      # a depth job prepared with --motion
    rng = np.random.default_rng(1)
    mask = person_mask()
    grad = np.linspace(0, 255, W, dtype=np.float32)[None].repeat(H, 0).astype(np.uint8)
    with VideoWriter(job.dir / "work.mp4", W, H, 16, crf=14) as wv, \
            VideoWriter(job.dir / "mask.mp4", W, H, 16, crf=12) as mv, \
            VideoWriter(job.dir / "depth.mp4", W, H, 16, crf=14) as dv:
        for i in range(frames):
            frame = np.full((H, W, 3), (40 + 10 * i, 90, 160), np.uint8)
            frame[mask] = (200, 150, 120)
            frame += rng.integers(0, 6, (H, W, 3), dtype=np.uint8)
            wv.write(frame)
            mv.write((mask.astype(np.uint8) * 255))
            depth = np.repeat(grad[:, :, None], 3, axis=2).copy()
            depth[mask] = 230
            dv.write(depth)
    body = [stick_figure() for _ in range(frames)]
    hand = {"label": "right", "score": 0.9, "image": right_hand21().tolist(), "world": None}
    motion = build_motion(W, H, 16, body, body, {"left": [None] * frames, "right": [hand] * frames},
                          # the eyes turn right half-way (motion.json smooths the track, so no alternation)
                          face={"frames": [face478(0.6 if i >= frames // 2 else 0.0) for i in range(frames)],
                                "points": 478,
                                "model": "face_landmarker.task"}, source={"job": job.id})
    write_motion(motion, job.dir / "motion.json")
    for name in ("source", "control"):
        (job.dir / f"{name}.mp4").write_bytes((job.dir / "work.mp4").read_bytes())
    for name in ("source", "work", "depth", "mask", "control", "motion"):
        job.set_artifact(name, job.path(name), save=False)
    job["has_audio"] = False
    job["frames"] = frames
    job["status"] = "prepared"
    job.save()
    return job


class RunPartsTests(unittest.TestCase):
    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def run_cli(self, argv, backends=None):
        stream = io.StringIO()
        code = __import__("zfx.parts_cli", fromlist=["main"]).main(argv, backends=backends, stream=stream)
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        return code, lines

    def test_outputs_schema_and_cli(self):
        job = make_job(self.home)
        approve(job)
        backends = parts.Backends(things=FakeThings(), face_parse=fake_face_parse)
        code, lines = self.run_cli(["--job", str(job.dir), "--things", "cup, ball", "--gaze"], backends)
        self.assertEqual(code, 0, lines[-1])
        self.assertTrue(lines[-1].get("done"))
        self.assertTrue(all("stage" in line and "overall" in line for line in lines[:-1]))
        stages = [line["stage"] for line in lines[:-1]]
        for stage in ("load", "landmarks", "faceparse", "things", "parts", "render", "preview"):
            self.assertIn(stage, stages)
        result = lines[-1]["result"]
        for name in ("parts.mp4", "parts.npz", "parts.json", "control-parts.mp4", "preview-parts.jpg"):
            self.assertTrue((job.dir / name).is_file(), name)
        self.assertFalse((job.dir / "preview.jpg").exists())                            # never written here
        self.assertEqual(count_frames(job.dir / "parts.mp4"), 8)
        self.assertEqual(count_frames(job.dir / "control-parts.mp4"), 8)

        data = json.loads((job.dir / "parts.json").read_text())
        self.assertEqual(data["format"], "zeus-parts")
        self.assertEqual(data["version"], 1)
        self.assertEqual((data["frames"] if isinstance(data["frames"], int) else len(data["frames"])), 8)
        self.assertEqual([e["id"] for e in data["legend"]][:29], list(range(29)))
        self.assertEqual(data["legend"][29]["name"], "thing:cup")
        self.assertEqual(data["things"]["prompt"], "cup, ball")
        self.assertEqual(data["things"]["names"], ["cup", "ball"])
        self.assertEqual(data["things"]["found"]["cup"]["frames"], 8)
        self.assertEqual(data["things"]["found"]["ball"]["frames"], 0)
        self.assertTrue(any("ball" in wmsg for wmsg in data["warnings"]))
        frame0 = data["frames"][0]
        for key in ("i", "t", "body", "face", "face_detail", "person", "pixels", "things", "gaze", "blink"):
            self.assertIn(key, frame0)
        self.assertIn("torso", frame0["pixels"])
        self.assertIn("hat", frame0["pixels"])
        self.assertIn("face-skin", frame0["pixels"])
        self.assertIn("thing:cup", frame0["pixels"])
        self.assertNotIn("thing:ball", frame0["pixels"])
        self.assertEqual(frame0["gaze"]["direction"], "centre")
        self.assertEqual(data["frames"][7]["gaze"]["direction"], "right")
        self.assertIn("gaze", frame0["pixels"])
        self.assertEqual(data["gaze"]["frames"], 8)
        self.assertEqual(data["stats"]["face_detail_frames"], 8)
        self.assertEqual(data["sources"]["face_parser"], "fake")
        self.assertEqual(data["things"]["model"], "fake/clipseg")
        self.assertEqual(sorted(data["timings"]), sorted(["load", "landmarks", "faceparse", "things", "parts",
                                                           "render", "preview", "total"]))

        z = np.load(job.dir / "parts.npz")
        self.assertEqual(z["index"].shape, (8, H, W))
        self.assertEqual(z["index"].dtype, np.uint8)
        self.assertEqual(list(z["things"]), ["cup", "ball"])
        index = z["index"][0]
        mask = person_mask()
        self.assertFalse(np.isin(index[~mask], [parts.TORSO, parts.HEAD, parts.FACE_SKIN]).any())
        self.assertTrue(np.isin(index[~mask], [parts.BG_FAR, parts.BG_MID, parts.BG_NEAR, 40]).all())
        self.assertEqual(index[int(0.47 * H), int(0.5 * W)], parts.TORSO)
        self.assertEqual(index[10, 10], 40)                                            # the cup, outside the person
        self.assertTrue((index[mask] != 0).all())

        self.assertEqual(result["frames"], 8)
        self.assertIn("torso", result["classes"])
        self.assertEqual(result["things"]["cup"]["rate"], 1.0)
        reloaded = Job.load(job.dir)
        for name in ("parts", "parts_index", "parts_json", "control_parts", "preview_parts"):
            self.assertIsNotNone(reloaded.artifact(name), name)
        self.assertEqual(reloaded["parts_info"]["frames"], 8)
        verify(reloaded)                                                               # approval untouched

    def test_no_things_flag_and_over_person(self):
        job = make_job(self.home, frames=4)
        backends = parts.Backends(things=FakeThings(), face_parse=fake_face_parse)
        code, lines = self.run_cli(["--job", str(job.dir), "--things", "cup", "--no-things", "--no-preview"], backends)
        self.assertEqual(code, 0, lines[-1])
        data = json.loads((job.dir / "parts.json").read_text())
        self.assertEqual(data["things"]["names"], [])
        self.assertIsNone(data["gaze"])
        self.assertFalse((job.dir / "preview-parts.jpg").exists())
        self.assertNotIn("gaze", data["frames"][0])
        code, lines = self.run_cli(["--job", str(job.dir), "--things", "cup", "--things-over-person",
                                    "--no-face-detail", "--no-preview"], backends)
        self.assertEqual(code, 0, lines[-1])
        data = json.loads((job.dir / "parts.json").read_text())
        self.assertTrue(data["things"]["over_person"])
        self.assertEqual(data["stats"]["face_detail_frames"], 0)
        self.assertNotIn("hat", data["frames"][0]["pixels"])

    def test_bad_arguments_give_one_error_line(self):
        code, lines = self.run_cli(["--job", str(self.home / "Jobs" / "nope")])
        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("error", lines[0])
        self.assertIn("stage", lines[0])
        job = make_job(self.home, frames=2)
        code, lines = self.run_cli(["--job", str(job.dir), "--things-threshold", "1.5"], parts.Backends())
        self.assertEqual(code, 1)
        self.assertIn("threshold", lines[-1]["error"])
        code, lines = self.run_cli(["--job", str(job.dir), "--things", ",".join(f"t{i}" for i in range(30))],
                                   parts.Backends(things=FakeThings()))
        self.assertEqual(code, 1)
        self.assertIn("At most", lines[-1]["error"])

    def test_missing_depth_is_a_plain_error(self):
        job = make_job(self.home, frames=2)
        (job.dir / "depth.mp4").unlink()
        code, lines = self.run_cli(["--job", str(job.dir)], parts.Backends(face_parse=fake_face_parse))
        self.assertEqual(code, 1)
        self.assertIn("depth.mp4", lines[-1]["error"])

    def test_face_detail_unavailable_is_a_warning(self):
        job = make_job(self.home, frames=2)
        code, lines = self.run_cli(["--job", str(job.dir), "--no-preview"], parts.Backends())
        self.assertEqual(code, 0, lines[-1])
        data = json.loads((job.dir / "parts.json").read_text())
        self.assertEqual(data["stats"]["face_detail_frames"], 0)
        self.assertTrue(any(w.startswith("Face detail skipped") for w in data["warnings"]))
        self.assertIn("head", data["frames"][0]["pixels"])


if __name__ == "__main__":
    unittest.main()
