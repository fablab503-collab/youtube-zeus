"""Whole-body motion capture (zfx.pose, zfx.bvh): One-Euro filter, motion.json, BVH, pose drawing, pose modes.

No network and no MediaPipe weights: every test works on synthetic landmarks.
Run:  "$ZEUS_EFFECTS_HOME/.venv/bin/python" -m unittest discover -s effects/tests -p 'test_pose.py' -v
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

import synth
from zfx import bvh, pose
from zfx.composite import control_frame, gray_depth_frame
from zfx.jobs import Job
from zfx.pose import B
from zfx.review import ReviewError, approve, required_artifacts, verify

os.environ["ZEUS_EFFECTS_NO_DOWNLOAD"] = "1"   # never fetch models from a test


# --------------------------------------------------------------------------- synthetic people
def tpose_world() -> np.ndarray:
    """33 x [x, y, z, visibility] MediaPipe world landmarks (metres, x right = the person's left when facing the
    camera, y down, z away from the camera) of a T-pose: arms out, palms down, feet forward."""
    p = np.zeros((33, 4))
    p[:, 3] = 1.0

    def put(name, x, y, z):
        p[B[name], :3] = (x, y, z)

    for side, sg in (("left", 1.0), ("right", -1.0)):
        put(f"{side}_hip", 0.10 * sg, 0.0, 0.0)
        put(f"{side}_shoulder", 0.18 * sg, -0.50, 0.0)
        put(f"{side}_elbow", 0.46 * sg, -0.50, 0.0)
        put(f"{side}_wrist", 0.71 * sg, -0.50, 0.0)
        put(f"{side}_index", 0.80 * sg, -0.50, -0.02)     # forward (towards the camera) of the pinky
        put(f"{side}_pinky", 0.80 * sg, -0.50, 0.02)
        put(f"{side}_thumb", 0.75 * sg, -0.50, -0.05)
        put(f"{side}_knee", 0.10 * sg, 0.43, 0.0)
        put(f"{side}_ankle", 0.10 * sg, 0.84, 0.0)
        put(f"{side}_foot_index", 0.10 * sg, 0.84 + 0.5 * 0.17, -math.sqrt(3) / 2 * 0.17)
        put(f"{side}_heel", 0.10 * sg, 0.90, 0.04)
        put(f"{side}_ear", 0.07 * sg, -0.67, 0.0)
        put(f"{side}_eye", 0.03 * sg, -0.69, -0.08)
        put(f"{side}_eye_inner", 0.015 * sg, -0.69, -0.085)
        put(f"{side}_eye_outer", 0.045 * sg, -0.69, -0.07)
        put(f"mouth_{side}", 0.03 * sg, -0.62, -0.09)
    put("nose", 0.0, -0.67, -0.10)
    return p


def world_to_image(world: np.ndarray, cx=0.5, cy=0.55, scale=0.45) -> np.ndarray:
    """A simple orthographic camera: world metres -> normalized image points (+ visibility)."""
    img = np.zeros_like(world)
    img[:, 0] = cx + world[:, 0] * scale * 0.6
    img[:, 1] = cy + world[:, 1] * scale
    img[:, 2] = world[:, 2] * 0.3
    img[:, 3] = world[:, 3]
    return img


def rot_y(deg):
    return bvh.axis_rotation("Y", deg)


def posed_world() -> np.ndarray:
    """T-pose with the left arm lowered and bent, the right leg lifted, the head turned, the body turned 25 deg."""
    p = tpose_world()
    pts = p[:, :3].copy()
    # left arm: upper arm 50 deg down, forearm bent forward
    sh = pts[B["left_shoulder"]]
    upper = np.array([math.cos(math.radians(50)), math.sin(math.radians(50)), 0.0]) * 0.28
    pts[B["left_elbow"]] = sh + upper
    pts[B["left_wrist"]] = pts[B["left_elbow"]] + np.array([0.05, 0.05, -0.23])
    pts[B["left_index"]] = pts[B["left_wrist"]] + np.array([0.02, 0.02, -0.09])
    pts[B["left_pinky"]] = pts[B["left_wrist"]] + np.array([0.04, 0.03, -0.08])
    # right leg: knee forward and up
    hip = pts[B["right_hip"]]
    pts[B["right_knee"]] = hip + np.array([0.0, 0.25, -0.35])
    pts[B["right_ankle"]] = pts[B["right_knee"]] + np.array([0.0, 0.40, 0.05])
    pts[B["right_foot_index"]] = pts[B["right_ankle"]] + np.array([0.0, 0.05, -0.16])
    # head turned left
    c = (pts[B["left_ear"]] + pts[B["right_ear"]]) / 2
    for name in ("left_ear", "right_ear", "nose"):
        pts[B[name]] = c + rot_y(30) @ (pts[B[name]] - c)
    # whole body turned 25 deg about the vertical axis
    p[:, :3] = (rot_y(25) @ pts.T).T
    return p


def synthetic_motion(frames: int = 6, width: int = 320, height: int = 240, gaps=(2,)) -> dict:
    world, img = [], []
    base = tpose_world()
    for i in range(frames):
        if i in gaps:
            world.append(None)
            img.append(None)
            continue
        w = base.copy()
        w[B["left_wrist"], 1] += 0.01 * i          # a little movement
        world.append(w.tolist())
        img.append(world_to_image(w).tolist())
    hands = {"left": [], "right": []}
    for i in range(frames):
        for side in ("left", "right"):
            if img[i] is None or (side == "right" and i % 2):
                hands[side].append(None)
                continue
            wr = np.array(img[i][B[f"{side}_wrist"]][:3])
            pts = np.array([wr + [0.004 * (k % 5) * (1 if side == "left" else -1), 0.003 * (k // 5), 0.0]
                            for k in range(21)])
            hands[side].append({"label": "right" if side == "left" else "left", "score": 0.9,
                                "image": pts.tolist(), "world": (pts * 0.1).tolist()})
    face = {"model": "face_landmarker.task", "points": 478,
            "frames": [None if img[i] is None else
                       [[0.5 + 0.01 * math.cos(k), 0.3 + 0.01 * math.sin(k), 0.0] for k in range(478)]
                       for i in range(frames)]}
    return pose.build_motion(width, height, 16, img, world, hands, face,
                             source={"job": "test", "input": "synthetic", "video": "work.mp4"},
                             search={"pose": "full_frame", "crop": None, "hands": "full_frame"},
                             models={"pose": "pose_landmarker_heavy.task", "hands": "hand_landmarker.task",
                                     "face": "face_landmarker.task"})


# --------------------------------------------------------------------------- One-Euro
class OneEuroTests(unittest.TestCase):
    def test_constant_signal_is_unchanged(self):
        f = pose.OneEuroFilter(16, 1.0, 0.5, 1.0)
        for _ in range(20):
            out = f(np.array([0.3, -2.0]))
        np.testing.assert_allclose(out, [0.3, -2.0], atol=1e-12)

    def test_first_value_passes_and_step_is_smoothed(self):
        f = pose.OneEuroFilter(16, 1.0, 0.0, 1.0)
        self.assertEqual(float(f(np.array(0.0))), 0.0)
        y = float(f(np.array(1.0)))
        tau = 1.0 / (2 * math.pi * 1.0)
        alpha = 1.0 / (1.0 + tau * 16)            # beta 0: a plain exponential filter at min_cutoff
        self.assertAlmostEqual(y, alpha, places=12)
        self.assertTrue(0.0 < y < 1.0)

    def test_reduces_jitter_and_beta_cuts_lag(self):
        rng = np.random.default_rng(0)
        noisy = 0.5 + rng.normal(0, 0.01, 160)           # a still joint with detector jitter
        f = pose.OneEuroFilter(16, **pose.SMOOTHING["image"])
        out = np.array([float(f(np.array(v))) for v in noisy])
        self.assertLess(out[16:].std(), 0.6 * noisy[16:].std())
        self.assertAlmostEqual(float(out[16:].mean()), 0.5, delta=0.005)
        # a fast ramp: a higher beta follows it more closely (less lag)
        ramp = np.arange(32) * 0.5
        lag = []
        for beta in (0.0, 5.0):
            f = pose.OneEuroFilter(16, 1.0, beta, 1.0)
            lag.append(abs(float([f(np.array(v)) for v in ramp][-1]) - ramp[-1]))
        self.assertLess(lag[1], lag[0])

    def test_vectorised_and_reset(self):
        f = pose.OneEuroFilter(16, 1.0, 1.0, 1.0)
        a = f(np.zeros((33, 3)))
        self.assertEqual(a.shape, (33, 3))
        f(np.ones((33, 3)))
        f.reset()
        np.testing.assert_array_equal(f(np.full((33, 3), 5.0)), np.full((33, 3), 5.0))

    def test_smooth_track_gaps_and_visibility(self):
        frames = [np.array([[0.0, 0.0, 0.0, 0.9]]), np.array([[1.0, 1.0, 1.0, 0.2]]), None,
                  np.array([[3.0, 3.0, 3.0, 0.7]])]
        out = pose.smooth_track(frames, 16, pose.SMOOTHING["image"])
        self.assertIsNone(out[2])
        self.assertTrue(0.0 < out[1][0, 0] < 1.0)                 # smoothed
        np.testing.assert_array_equal(out[3][0, :3], [3.0, 3.0, 3.0])   # restarts after the gap
        self.assertEqual([o[0, 3] for o in out if o is not None], [0.9, 0.2, 0.7])   # visibility stays raw


# --------------------------------------------------------------------------- BVH
class BVHTests(unittest.TestCase):
    def test_tpose_gives_near_zero_rotations(self):
        local = bvh.solve_frame(tpose_world())
        for joint in bvh.JOINTS:
            angles = bvh.euler_zxy(local[joint])
            self.assertLess(max(abs(a) for a in angles), 0.1, (joint, angles))

    def test_euler_zxy_round_trip(self):
        rng = np.random.default_rng(1)
        for _ in range(50):
            z, x, y = rng.uniform(-170, 170), rng.uniform(-80, 80), rng.uniform(-170, 170)
            m = bvh.zxy_matrix(z, x, y)
            np.testing.assert_allclose(bvh.zxy_matrix(*bvh.euler_zxy(m)), m, atol=1e-9)

    def test_writer_structure(self):
        motion = synthetic_motion(frames=7, gaps=(0, 3))
        info = bvh.build_bvh(motion)
        parsed = bvh.parse_bvh(info["text"])
        names = [j["name"] for j in parsed["joints"]]
        self.assertEqual(names, list(bvh.JOINTS))
        self.assertEqual(names[0], "Hips")
        self.assertEqual(parsed["joints"][0]["kind"], "ROOT")
        self.assertEqual(parsed["joints"][0]["channels"], list(bvh.CHANNELS_ROOT))
        self.assertTrue(all(j["channels"] == list(bvh.CHANNELS_JOINT) for j in parsed["joints"][1:]))
        self.assertEqual(parsed["channels"], 6 + 3 * (len(bvh.JOINTS) - 1))
        self.assertEqual(parsed["channels"], 60)
        self.assertEqual(parsed["frames"], 7)
        self.assertEqual(parsed["motion"].shape, (7, 60))
        values = info["text"].split("Frame Time:")[1].split()[1:]
        self.assertEqual(len(values), parsed["frames"] * parsed["channels"])
        self.assertAlmostEqual(parsed["frame_time"], 1 / 16, places=6)
        ends = {j["name"] for j in parsed["joints"] if j["end_site"] is not None}
        self.assertEqual(ends, set(bvh.END_SITES))
        parents = {j["name"]: j["parent"] for j in parsed["joints"]}
        self.assertEqual(parents["LeftForeArm"], "LeftArm")
        self.assertEqual(parents["Head"], "Neck")
        self.assertEqual(parents["RightUpLeg"], "Hips")
        self.assertEqual(info["held_frames"], 2)
        np.testing.assert_array_equal(parsed["motion"][0], parsed["motion"][1])   # leading gap repeats frame 1
        np.testing.assert_array_equal(parsed["motion"][3], parsed["motion"][2])   # a gap repeats the last frame
        self.assertTrue(info["text"].startswith("HIERARCHY\nROOT Hips\n{\n\tOFFSET 0.000000 0.000000 0.000000"))

    def test_rest_pose_matches_tpose_and_units(self):
        motion = synthetic_motion(frames=3, gaps=())
        parsed = bvh.parse_bvh(bvh.build_bvh(motion)["text"])
        fk = bvh.forward_kinematics(parsed, 0)
        hips, lhand, rhand = fk["Hips"][0], fk["LeftHand"][0], fk["RightHand"][0]
        self.assertAlmostEqual(hips[1], 100 * (0.43 + 0.41 + bvh.ANKLE_HEIGHT), delta=0.5)   # cm, Y up
        self.assertGreater(lhand[0], 60.0)          # left hand on +X
        self.assertLess(rhand[0], -60.0)
        self.assertAlmostEqual(lhand[1], fk["LeftArm"][0][1], delta=0.5)   # arms horizontal
        self.assertGreater(fk["Head/end"][0][1], fk["Head"][0][1])        # head up
        self.assertGreater(fk["LeftFoot/end"][0][2], fk["LeftFoot"][0][2])   # toes forward (+Z)

    def test_solved_bones_follow_the_landmarks(self):
        world = posed_world()
        motion = {"fps": 16, "width": 640, "height": 480,
                  "body": {"world": [world.tolist()], "image": [world_to_image(world).tolist()]}}
        parsed = bvh.parse_bvh(bvh.build_bvh(motion)["text"])
        fk = bvh.forward_kinematics(parsed, 0)
        p = bvh.to_bvh_axes(world)

        def direction(a, b):
            v = np.asarray(b) - np.asarray(a)
            return v / np.linalg.norm(v)

        checks = [("LeftArm", "LeftForeArm", "left_shoulder", "left_elbow"),
                  ("LeftForeArm", "LeftHand", "left_elbow", "left_wrist"),
                  ("RightArm", "RightForeArm", "right_shoulder", "right_elbow"),
                  ("RightUpLeg", "RightLeg", "right_hip", "right_knee"),
                  ("RightLeg", "RightFoot", "right_knee", "right_ankle"),
                  ("LeftUpLeg", "LeftLeg", "left_hip", "left_knee"),
                  ("RightFoot", "RightFoot/end", "right_ankle", "right_foot_index")]
        for j0, j1, l0, l1 in checks:
            got = direction(fk[j0][0], fk[j1][0])
            want = direction(p[B[l0]], p[B[l1]])
            self.assertGreater(float(np.dot(got, want)), 0.9999, (j0, j1, got, want))
        # the head frame: ear line on the head's X axis, the nose forward on its Z axis
        head_rot = fk["Head"][1]
        ear = direction(p[B["right_ear"]], p[B["left_ear"]])
        self.assertGreater(float(np.dot(head_rot[:, 0], ear)), 0.9999)
        # the hips: hip line on X
        hip_line = direction(p[B["right_hip"]], p[B["left_hip"]])
        self.assertGreater(float(np.dot(fk["Hips"][1][:, 0], hip_line)), 0.9999)

    def test_hidden_legs_stay_in_rest_pose(self):
        world = posed_world()
        for name in ("left_knee", "right_knee", "left_ankle", "right_ankle", "left_foot_index",
                     "right_foot_index", "left_heel", "right_heel"):
            world[B[name], 3] = 0.05
        local = bvh.solve_frame(world)
        for joint in ("LeftLeg", "RightLeg", "LeftFoot", "RightFoot"):
            np.testing.assert_allclose(local[joint], np.eye(3), atol=1e-12)
        self.assertGreater(abs(bvh.euler_zxy(local["LeftArm"])[0]), 10)   # the visible arm is still solved
        # the hidden legs hang vertically below the (turned, tilted) hips
        world[B["left_hip"], 1] -= 0.05                                   # tilt the pelvis
        motion = {"fps": 16, "width": 640, "height": 480,
                  "body": {"world": [world.tolist()], "image": [world_to_image(world).tolist()]}}
        fk = bvh.forward_kinematics(bvh.parse_bvh(bvh.build_bvh(motion)["text"]), 0)
        for side in ("Left", "Right"):
            leg = fk[f"{side}Foot"][0] - fk[f"{side}UpLeg"][0]
            self.assertGreater(-leg[1] / np.linalg.norm(leg), 0.9999, side)

    def test_hidden_arm_relaxes_and_partly_seen_arm_blends(self):
        world = tpose_world()
        for name in ("right_elbow", "right_wrist", "right_index", "right_pinky", "right_thumb"):
            world[B[name], 3] = 0.1
        local = bvh.solve_frame(world)
        np.testing.assert_allclose(local["RightArm"], bvh.neutral_local("RightArm"), atol=1e-12)
        parsed = bvh.parse_bvh(bvh.build_bvh({"fps": 16, "width": 640, "height": 480, "body": {
            "world": [world.tolist()], "image": [world_to_image(world).tolist()]}})["text"])
        fk = bvh.forward_kinematics(parsed, 0)
        self.assertLess(fk["RightHand"][0][1], fk["RightArm"][0][1] - 40)    # hangs down along the body
        self.assertGreater(fk["LeftHand"][0][0], 60.0)                       # the seen arm keeps its T-pose
        # half-way visibility: half-way between the neutral pose and the solved (T-pose) rotation
        world[B["right_elbow"], 3] = (bvh.VIS_LOW + bvh.VIS_HIGH) / 2
        half = bvh.solve_frame(world)["RightArm"]
        neutral_angle = math.degrees(math.acos((np.trace(bvh.neutral_local("RightArm")) - 1) / 2))
        half_angle = math.degrees(math.acos((np.trace(half) - 1) / 2))
        self.assertAlmostEqual(half_angle, neutral_angle / 2, places=6)

    def test_no_body_is_an_error(self):
        motion = synthetic_motion(frames=3, gaps=(0, 1, 2))
        with self.assertRaises(bvh.BVHError):
            bvh.build_bvh(motion)

    def test_parser_rejects_wrong_value_count(self):
        text = bvh.build_bvh(synthetic_motion(frames=3, gaps=()))["text"]
        with self.assertRaises(bvh.BVHError):
            bvh.parse_bvh(text.rstrip("\n") + " 1.0\n")


# --------------------------------------------------------------------------- motion.json
class MotionJSONTests(unittest.TestCase):
    def test_schema_round_trip(self):
        motion = synthetic_motion(frames=6, gaps=(2,))
        with tempfile.TemporaryDirectory() as tmp:
            path = pose.write_motion(motion, Path(tmp) / "motion.json")
            text = path.read_text()
            loaded = pose.load_motion(path)
        self.assertEqual(json.loads(json.dumps(motion)), loaded)
        self.assertEqual(loaded["format"], "zeus-motion")
        self.assertEqual(loaded["version"], 1)
        for key in ("fps", "frames", "width", "height", "time", "names", "smoothing", "body", "hands", "face",
                    "stats", "source", "coordinates", "edges", "limb_groups", "models", "search"):
            self.assertIn(key, loaded)
        n = loaded["frames"]
        self.assertEqual(n, 6)
        self.assertEqual(len(loaded["time"]), n)
        self.assertEqual(loaded["time"][1], 0.0625)
        self.assertEqual(len(loaded["names"]["body"]), 33)
        self.assertEqual(len(loaded["names"]["hand"]), 21)
        for track in ("image", "world", "image_raw", "world_raw"):
            frames = loaded["body"][track]
            self.assertEqual(len(frames), n)
            self.assertIsNone(frames[2])
            self.assertEqual(np.asarray(frames[0]).shape, (33, 4))
        for side in ("left", "right"):
            self.assertEqual(len(loaded["hands"][side]["image"]), n)
            self.assertEqual(np.asarray(loaded["hands"][side]["image"][0]).shape, (21, 3))
        self.assertIsNone(loaded["hands"]["right"]["image"][1])
        self.assertEqual(np.asarray(loaded["face"]["image"][0]).shape, (478, 3))
        self.assertEqual(len(loaded["face"]["openpose68"]), 68)
        self.assertTrue(loaded["smoothing"]["raw_kept"])
        self.assertEqual(loaded["smoothing"]["method"], "one_euro")
        self.assertEqual(loaded["stats"]["body_frames"], 5)
        self.assertEqual(loaded["stats"]["left_hand_frames"], 5)
        self.assertEqual(loaded["stats"]["face_frames"], 5)
        frame_lines = [ln for ln in text.splitlines() if ln.lstrip().startswith("[[")]
        self.assertGreaterEqual(len(frame_lines), 5 * 4)     # one frame per line (body tracks alone: 4 x 5)
        self.assertTrue(all(json.loads(ln.strip().rstrip(",")) for ln in frame_lines))
        raw, smooth = loaded["body"]["image_raw"][1], loaded["body"]["image"][1]
        self.assertNotEqual(raw[B["left_wrist"]], smooth[B["left_wrist"]])   # the moving wrist got smoothed

    def test_load_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text(json.dumps({"format": "something", "version": 1}))
            with self.assertRaises(pose.PoseError):
                pose.load_motion(path)

    def test_summary(self):
        s = pose.summarize(synthetic_motion(frames=6, gaps=(2,)))
        self.assertEqual((s["frames"], s["body_frames"], s["face_frames"]), (6, 5, 5))
        self.assertAlmostEqual(s["body_rate"], 5 / 6, places=3)
        self.assertIn("left_leg", s["limbs_tracked"])
        self.assertEqual(s["average_visibility"], 1.0)

    def test_assign_hands_by_body_wrists(self):
        body = world_to_image(tpose_world()).tolist()
        lw, rw = body[B["left_wrist"]], body[B["right_wrist"]]
        near_l = {"label": "left", "score": 0.9, "image": [[lw[0] + 0.01, lw[1], 0]] * 21, "world": None}
        near_r = {"label": "left", "score": 0.8, "image": [[rw[0] - 0.01, rw[1], 0]] * 21, "world": None}
        far = {"label": "right", "score": 0.9, "image": [[0.99, 0.01, 0]] * 21, "world": None}
        out = pose.assign_hands([[near_r, near_l], [far], [near_l]], [body, body, None], 320, 240)
        self.assertIs(out["left"][0], near_l)
        self.assertIs(out["right"][0], near_r)
        self.assertIsNone(out["left"][1])            # someone else's hand, far from both wrists
        self.assertIsNone(out["right"][1])
        self.assertIs(out["right"][2], near_l)       # no body: MediaPipe's "left" (mirrored) = the right hand


# --------------------------------------------------------------------------- drawing, pose.mp4, control
class DrawingTests(unittest.TestCase):
    def setUp(self):
        self.h, self.w = 240, 320
        self.body = world_to_image(tpose_world())

    def px(self, name):
        p = self.body[B[name]]
        return int(round(p[1] * self.h)), int(round(p[0] * self.w))

    def test_skeleton_on_black(self):
        img = pose.draw_pose(self.h, self.w, self.body)
        self.assertEqual(img.shape, (self.h, self.w, 3))
        self.assertEqual(img.dtype, np.uint8)
        self.assertEqual(img[0, 0].tolist(), [0, 0, 0])
        self.assertGreater(int(img.any(axis=2).sum()), 300)
        # joints carry the OpenPose colours: right shoulder (index 2) orange-ish, left wrist (7) green-ish
        y, x = self.px("right_shoulder")
        self.assertEqual(tuple(img[y, x]), pose.OPENPOSE_COLORS[2])
        y, x = self.px("left_wrist")
        self.assertEqual(tuple(img[y, x]), pose.OPENPOSE_COLORS[7])
        # the middle of the left upper arm is the limb colour at 60 %
        ys, xs = zip(self.px("left_shoulder"), self.px("left_elbow"))
        mid = img[sum(ys) // 2, sum(xs) // 2]
        np.testing.assert_allclose(mid, (np.array(pose.OPENPOSE_COLORS[4]) * 0.6).astype(int), atol=2)

    def test_invisible_points_are_not_drawn(self):
        body = self.body.copy()
        for name in ("left_knee", "right_knee", "left_ankle", "right_ankle", "left_foot_index", "right_foot_index",
                     "left_heel", "right_heel"):
            body[B[name], 3] = 0.1
        img = pose.draw_pose(self.h, self.w, body)
        y, x = self.px("left_knee")
        self.assertEqual(img[y - 3:y + 4, x - 3:x + 4].sum(), 0)
        y, x = self.px("left_ankle")
        self.assertEqual(img[y - 3:y + 4, x - 3:x + 4].sum(), 0)

    def test_hands_and_face(self):
        motion = synthetic_motion(frames=2, gaps=())
        parts = pose.frame_parts(motion, 0)
        img = pose.draw_pose(self.h, self.w, **parts)
        hand = np.asarray(parts["left_hand"])
        y, x = int(round(hand[20][1] * self.h)), int(round(hand[20][0] * self.w))
        self.assertEqual(img[y, x].tolist(), [0, 0, 255])                      # blue hand dots
        face = np.asarray(parts["face"])
        y, x = int(round(face[pose.FACE68[8]][1] * self.h)), int(round(face[pose.FACE68[8]][0] * self.w))
        self.assertEqual(img[y, x].tolist(), [255, 255, 255])                   # white face dots
        self.assertEqual(pose.draw_pose(self.h, self.w).sum(), 0)              # nothing detected: black

    def test_pose_video(self):
        motion = synthetic_motion(frames=5, gaps=(3,))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pose.mp4"
            stats = pose.write_pose_video(motion, out, lambda f, m: None)
            from zfx.io import probe, read_frames
            info = probe(out)
            frames = read_frames(out)
        self.assertEqual(stats, {"frames": 5, "frames_with_body": 4})
        self.assertEqual((info["width"], info["height"]), (320, 240))
        self.assertEqual(len(frames), 5)
        self.assertLess(frames[3].max(), 20)        # no body: black
        self.assertGreater(frames[0].max(), 200)

    def test_control_modes(self):
        h, w = self.h, self.w
        work = np.zeros((h, w, 3), np.uint8)
        work[:, :, 1] = 200
        depth = np.zeros((h, w, 3), np.uint8)
        depth[:, :, 0] = 250
        mask = np.zeros((h, w), np.uint8)
        mask[40:200, 100:220] = 255
        skel = pose.draw_pose(h, w, self.body)
        self.assertIs(control_frame("pose", work, None, mask, pose=skel), skel)
        grey = gray_depth_frame(work, depth, mask)
        self.assertEqual(grey[120, 160].tolist(), [75, 75, 75])        # luminance of (250, 0, 0)
        self.assertEqual(grey[5, 5].tolist(), [0, 200, 0])             # original outside
        self.assertEqual(grey[120, 99].tolist(), [0, 200, 0])          # hard edge, no halo
        ctrl = control_frame("depth_pose", work, depth, mask, pose=skel)
        drawn_inside = skel.any(axis=2) & (mask > 127)
        np.testing.assert_array_equal(ctrl[drawn_inside], skel[drawn_inside])
        outside = skel.any(axis=2) & (mask <= 127)
        self.assertTrue(outside.any())
        np.testing.assert_array_equal(ctrl[outside], work[outside])  # the skeleton stays on the person


# --------------------------------------------------------------------------- pose modes: review and CLI
def fake_pose_job(mode="depth_pose") -> Job:
    job = Job.create("/tmp/in.mp4", 1.0, 2.0, mode)
    files = {"source": b"src", "work": b"work", "mask": b"mask", "control": b"control", "preview": b"jpg",
             "landmarks": b'{"frames": []}', "pose": b"posevideo", "bvh": b"HIERARCHY"}
    if mode != "pose":
        files["depth"] = b"depth"
    for name, data in files.items():
        path = job.path(name)
        path.write_bytes(data)
        job.set_artifact(name, path, save=False)
    pose.write_motion(synthetic_motion(frames=4, gaps=(1,)), job.path("motion"))
    job.set_artifact("motion", job.path("motion"), save=False)
    job["has_audio"] = False
    job["status"] = "prepared"
    job.save()
    return job


class PoseModeReviewTests(unittest.TestCase):
    def setUp(self):
        self.home_ctx = synth.TempHome()
        self.home = self.home_ctx.__enter__()

    def tearDown(self):
        self.home_ctx.__exit__(None, None, None)

    def test_required_artifacts(self):
        job = fake_pose_job("pose")
        names = required_artifacts(job)
        self.assertIn("motion", names)
        self.assertIn("pose", names)
        self.assertIn("landmarks", names)
        self.assertNotIn("depth", names)
        self.assertIn("depth", required_artifacts(fake_pose_job("depth_pose")))

    def test_approve_hashes_motion_and_pose_video(self):
        job = fake_pose_job()
        review = approve(job)
        for name in ("motion", "pose", "bvh", "landmarks", "depth"):
            self.assertIn(name, review["hashes"])
        verify(Job.load(job.dir))
        job.path("motion").write_text(job.path("motion").read_text().replace('"fps": 16', '"fps": 15'))
        with self.assertRaises(ReviewError) as ctx:
            verify(Job.load(job.dir))
        self.assertIn("motion.json changed", str(ctx.exception))

    def test_missing_motion_cannot_be_approved(self):
        job = fake_pose_job()
        job.path("motion").unlink()
        with self.assertRaises(ReviewError):
            approve(job)

    def test_motion_cli(self):
        job = fake_pose_job()
        proc = subprocess.run([sys.executable, str(synth.CLI), "motion", "--job", str(job.dir), "--json"],
                              capture_output=True, text=True, timeout=120, env=dict(os.environ))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        last = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(last["done"])
        result = last["result"]
        self.assertEqual((result["frames"], result["body_frames"]), (4, 3))
        self.assertEqual(result["files"]["motion"], str(job.dir / "motion.json"))
        self.assertIn("left_arm", result["limbs_tracked"])
        text = subprocess.run([sys.executable, str(synth.CLI), "motion", "--job", str(job.dir)],
                              capture_output=True, text=True, timeout=120, env=dict(os.environ))
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("body        3/4 (75 %)", text.stdout)

    def test_motion_cli_without_motion(self):
        job = Job.create("/tmp/in.mp4", 1.0, 2.0, "depth")
        proc = subprocess.run([sys.executable, str(synth.CLI), "motion", "--job", str(job.dir), "--json"],
                              capture_output=True, text=True, timeout=120, env=dict(os.environ))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no motion capture", json.loads(proc.stdout.strip().splitlines()[-1])["error"])


class ModelFileTests(unittest.TestCase):
    def test_no_download_when_forbidden(self):
        with synth.TempHome() as home:
            with self.assertRaises(pose.PoseError) as ctx:
                pose.ensure_model(home / "models" / "pose_landmarker_heavy.task", "https://invalid.example/x.task")
            self.assertIn("setup.sh", str(ctx.exception))

    def test_present_model_is_used(self):
        with synth.TempHome() as home:
            path = home / "models" / "hand_landmarker.task"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\0" * 2048)
            self.assertEqual(pose.ensure_model(path, "https://invalid.example/x.task", min_bytes=1024), path)


class PersonCropTests(unittest.TestCase):
    def test_crop_around_the_person(self):
        from zfx.io import write_video
        frames = []
        for i in range(4):
            m = np.zeros((240, 320, 3), np.uint8)
            m[150:230, 250 + i:300 + i] = 255
            frames.append(m)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_video(frames, Path(tmp) / "mask.mp4")
            crop = pose.person_crop(path, 320, 240)
        self.assertIsNotNone(crop)
        x, y, w, h = crop
        self.assertLessEqual(x, 250)
        self.assertLessEqual(y, 150)
        self.assertGreaterEqual(x + w, 303)
        self.assertGreaterEqual(y + h, 230)
        self.assertLess(w * h, 0.7 * 320 * 240)


if __name__ == "__main__":
    unittest.main()
