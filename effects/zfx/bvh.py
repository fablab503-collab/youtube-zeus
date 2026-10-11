"""motion.bvh: a standard BVH body skeleton solved from the world landmarks of motion.json.

Skeleton (Mixamo-style names, which Blender, MotionBuilder and Unreal's IK Retargeter auto-map well):

  Hips (root: Xposition Yposition Zposition Zrotation Xrotation Yrotation)
    Spine > Spine1 > Neck > Head > End Site
                   > LeftShoulder > LeftArm > LeftForeArm > LeftHand > End Site
                   > RightShoulder > RightArm > RightForeArm > RightHand > End Site
    LeftUpLeg > LeftLeg > LeftFoot > End Site
    RightUpLeg > RightLeg > RightFoot > End Site

19 joints, 60 channels per frame, every other joint "Zrotation Xrotation Yrotation" (BVH order: the matrix is
Rz * Rx * Ry), degrees. Y up, centimetres, the character faces +Z, its left is +X. Frame Time = 1 / work fps.

Rest pose: a T-pose (arms along +/-X, palms down, legs straight down, feet pointing forward and 30 degrees down)
whose bone lengths are the person's own, measured from the world landmarks (median over frames where both ends
are visible; anything never seen is a default adult proportion scaled to what was measured).

Rotation solve (simple and stable, per frame, from the smoothed world landmarks):
  MediaPipe world (metres, x right, y down, z away from the camera) -> BVH axes: (x, -y, -z), a proper rotation.
  Hips    full frame from the hip line (-> +X) and the hips -> shoulders direction (-> +Y).
  Spine1  full frame from the hips -> shoulders direction (-> +Y) and the shoulder line (-> +X).
  Spine   half-way (slerp) between Hips and Spine1.
  Neck    swing of +Y onto mid-shoulders -> mid-ears, in the chest frame.
  Head    full frame from the ear line (-> +X) and mid-ears -> nose (-> +Z).
  Shoulders (clavicles) follow the chest (identity local rotation: there is no landmark to tell a shrug).
  Arm, ForeArm, UpLeg, Leg, Foot: "bone direction alignment": the shortest rotation (swing) that turns the rest
          bone direction onto the measured one, expressed in the parent's frame, so the twist is inherited from
          the stable shoulder/hip lines and never flips.
  Hand    full frame from wrist -> knuckles (mid index/pinky, -> +/-X) and pinky -> index (-> +Z).
Hidden joints: MediaPipe guesses every joint, also the hidden ones. Each bone's local rotation is blended (slerp)
from a neutral pose (arms down along the body, legs vertical, everything else as in the rest pose) to
the solved rotation by the visibility of the landmarks that place it (the best of e.g. elbow and wrist for the arm):
weight 0 at <= 0.2, 1 at >= 0.5, linear between. A partly seen limb follows MediaPipe's estimate, a limb never in
the picture relaxes instead of following a guess, and nothing pops as visibility changes. Frames without a body
repeat the nearest solved frame. Root position: the rest hip height plus the image movement of the hips (or of the shoulders
when the hips are mostly hidden) converted to centimetres with the person's measured size; no depth (Z = 0).

Fingers and the face are not in the BVH: they are in motion.json (21 points per hand, 478-point face mesh).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .pose import B

VIS_SOLVE = 0.5          # landmarks used to measure bone lengths and to pick the root anchor
VIS_LOW, VIS_HIGH = 0.2, 0.5   # blend weight of a solved bone: 0 at VIS_LOW, 1 at VIS_HIGH (see solve_frame)
ANKLE_HEIGHT = 0.08                          # metres, ankle joint above the floor in the rest pose
FOOT_REST = np.array([0.0, -0.5, math.sqrt(3) / 2.0])   # ankle -> toes in the rest pose: forward, 30 deg down
CHANNELS_ROOT = ("Xposition", "Yposition", "Zposition", "Zrotation", "Xrotation", "Yrotation")
CHANNELS_JOINT = ("Zrotation", "Xrotation", "Yrotation")

# (joint, parent); order = the BVH hierarchy order (depth first) = the channel order of every motion line.
SKELETON = (
    ("Hips", None),
    ("Spine", "Hips"), ("Spine1", "Spine"), ("Neck", "Spine1"), ("Head", "Neck"),
    ("LeftShoulder", "Spine1"), ("LeftArm", "LeftShoulder"), ("LeftForeArm", "LeftArm"), ("LeftHand", "LeftForeArm"),
    ("RightShoulder", "Spine1"), ("RightArm", "RightShoulder"), ("RightForeArm", "RightArm"),
    ("RightHand", "RightForeArm"),
    ("LeftUpLeg", "Hips"), ("LeftLeg", "LeftUpLeg"), ("LeftFoot", "LeftLeg"),
    ("RightUpLeg", "Hips"), ("RightLeg", "RightUpLeg"), ("RightFoot", "RightLeg"),
)
JOINTS = tuple(j for j, _ in SKELETON)
PARENT = dict(SKELETON)
END_SITES = ("Head", "LeftHand", "RightHand", "LeftFoot", "RightFoot")

# Default adult proportions in metres (MediaPipe landmark distances, not anatomical joint centres).
DEFAULT_LENGTHS = {"torso": 0.50, "neck": 0.17, "head": 0.15, "shoulder_width": 0.36, "hip_width": 0.22,
                   "upper_arm": 0.28, "forearm": 0.25, "hand": 0.09, "thigh": 0.43, "shin": 0.41, "foot": 0.17}

X_AXIS, Y_AXIS, Z_AXIS = np.eye(3)
I3 = np.eye(3)


class BVHError(RuntimeError):
    pass


# --------------------------------------------------------------------------- small rotation helpers
def unit(v) -> Optional[np.ndarray]:
    v = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else None


def frame_rotation(primary, secondary, rest_primary, rest_secondary) -> Optional[np.ndarray]:
    """Rotation taking the rest frame (rest_primary, rest_secondary) onto the measured one (primary exactly,
    secondary orthogonalised against it). None when degenerate."""
    e1 = unit(primary)
    if e1 is None:
        return None
    s = np.asarray(secondary, dtype=np.float64)
    e2 = unit(s - float(np.dot(s, e1)) * e1)
    if e2 is None:
        return None
    measured = np.column_stack([e1, e2, np.cross(e1, e2)])
    r1 = unit(rest_primary)
    r2 = unit(np.asarray(rest_secondary, float) - float(np.dot(rest_secondary, r1)) * r1)
    rest = np.column_stack([r1, r2, np.cross(r1, r2)])
    return measured @ rest.T


def swing(a, b) -> np.ndarray:
    """Shortest-arc rotation matrix turning direction a onto direction b."""
    a, b = unit(a), unit(b)
    if a is None or b is None:
        return I3.copy()
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if c < -1.0 + 1e-9:   # opposite: half turn about any axis perpendicular to a
        axis = unit(np.cross(a, X_AXIS if abs(a[0]) < 0.9 else Y_AXIS))
        return 2.0 * np.outer(axis, axis) - I3
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return I3 + vx + vx @ vx * (1.0 / (1.0 + c))


def mat_to_quat(m: np.ndarray) -> np.ndarray:
    """(w, x, y, z), w >= 0."""
    t = np.trace(m)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = np.array([0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s])
    else:
        i = int(np.argmax(np.diag(m)))
        if i == 0:
            s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
            q = np.array([(m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s])
        elif i == 1:
            s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
            q = np.array([(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s])
        else:
            s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
            q = np.array([(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s])
    q = q / np.linalg.norm(q)
    return -q if q[0] < 0 else q


def quat_to_mat(q) -> np.ndarray:
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def rot_fraction(m: np.ndarray, t: float) -> np.ndarray:
    """m ** t (the same axis, t times the angle)."""
    q = mat_to_quat(m)
    angle = 2.0 * math.atan2(float(np.linalg.norm(q[1:])), float(q[0]))
    axis = unit(q[1:])
    if axis is None or angle < 1e-9:
        return I3.copy()
    half = 0.5 * t * angle
    return quat_to_mat(np.concatenate([[math.cos(half)], math.sin(half) * axis]))


def axis_rotation(axis: str, degrees: float) -> np.ndarray:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    if axis == "X":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "Y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def euler_zxy(m: np.ndarray) -> tuple[float, float, float]:
    """R = Rz(a) Rx(b) Ry(c) -> (a, b, c) in degrees."""
    sb = float(np.clip(m[2, 1], -1.0, 1.0))
    b = math.asin(sb)
    if abs(sb) < 0.999999:
        a = math.atan2(-m[0, 1], m[1, 1])
        c = math.atan2(-m[2, 0], m[2, 2])
    else:   # gimbal lock: put everything on Z
        a = math.atan2(m[1, 0], m[0, 0])
        c = 0.0
    return math.degrees(a), math.degrees(b), math.degrees(c)


def zxy_matrix(z: float, x: float, y: float) -> np.ndarray:
    return axis_rotation("Z", z) @ axis_rotation("X", x) @ axis_rotation("Y", y)


# --------------------------------------------------------------------------- landmarks -> skeleton
def to_bvh_axes(points) -> np.ndarray:
    """MediaPipe world (x right, y down, z away from the camera) -> BVH (x, y up, z towards the camera)."""
    p = np.asarray(points, dtype=np.float64)[..., :3]
    return p * np.array([1.0, -1.0, -1.0])


def _mid(p, a: str, b: str) -> np.ndarray:
    return (p[B[a]] + p[B[b]]) / 2.0


def measure_lengths(world_frames: Sequence) -> dict:
    """Bone lengths (metres) of this person: median over frames where both ends are visible."""
    samples: dict[str, list] = {k: [] for k in DEFAULT_LENGTHS}

    def add(key, frame, pts, a, b, need):
        if all(frame[B[n], 3] >= VIS_SOLVE for n in need):
            samples[key].append(float(np.linalg.norm(a - b)))

    for frame in world_frames:
        if frame is None:
            continue
        f = np.asarray(frame, dtype=np.float64)
        p = to_bvh_axes(f)
        mid_hip, mid_sh, mid_ear = _mid(p, "left_hip", "right_hip"), _mid(p, "left_shoulder", "right_shoulder"), \
            _mid(p, "left_ear", "right_ear")
        add("torso", f, p, mid_sh, mid_hip, ("left_shoulder", "right_shoulder", "left_hip", "right_hip"))
        add("neck", f, p, mid_ear, mid_sh, ("left_shoulder", "right_shoulder", "left_ear", "right_ear"))
        add("head", f, p, p[B["left_ear"]], p[B["right_ear"]], ("left_ear", "right_ear"))
        add("shoulder_width", f, p, p[B["left_shoulder"]], p[B["right_shoulder"]], ("left_shoulder", "right_shoulder"))
        add("hip_width", f, p, p[B["left_hip"]], p[B["right_hip"]], ("left_hip", "right_hip"))
        for s in ("left", "right"):
            add("upper_arm", f, p, p[B[f"{s}_shoulder"]], p[B[f"{s}_elbow"]], (f"{s}_shoulder", f"{s}_elbow"))
            add("forearm", f, p, p[B[f"{s}_elbow"]], p[B[f"{s}_wrist"]], (f"{s}_elbow", f"{s}_wrist"))
            add("hand", f, p, p[B[f"{s}_wrist"]], _mid(p, f"{s}_index", f"{s}_pinky"),
                (f"{s}_wrist", f"{s}_index", f"{s}_pinky"))
            add("thigh", f, p, p[B[f"{s}_hip"]], p[B[f"{s}_knee"]], (f"{s}_hip", f"{s}_knee"))
            add("shin", f, p, p[B[f"{s}_knee"]], p[B[f"{s}_ankle"]], (f"{s}_knee", f"{s}_ankle"))
            add("foot", f, p, p[B[f"{s}_ankle"]], p[B[f"{s}_foot_index"]], (f"{s}_ankle", f"{s}_foot_index"))
    measured = {k: float(np.median(v)) for k, v in samples.items() if v}
    ratios = [measured[k] / DEFAULT_LENGTHS[k] for k in measured if measured[k] > 1e-3]
    scale = float(np.median(ratios)) if ratios else 1.0
    lengths = {k: measured.get(k, DEFAULT_LENGTHS[k] * scale) for k in DEFAULT_LENGTHS}
    return {"lengths": lengths, "measured": sorted(measured), "scale_of_defaults": round(scale, 3)}


def rest_offsets(lengths: dict) -> tuple[dict, dict]:
    """Joint offsets and End Site offsets of the T-pose, in centimetres."""
    L = {k: 100.0 * v for k, v in lengths.items()}
    t = L["torso"]
    off = {
        "Hips": np.zeros(3),
        "Spine": np.array([0.0, 0.30 * t, 0.0]),
        "Spine1": np.array([0.0, 0.35 * t, 0.0]),
        "Neck": np.array([0.0, 0.35 * t, 0.0]),
        "Head": np.array([0.0, L["neck"], 0.0]),
        "LeftShoulder": np.array([0.0, 0.35 * t, 0.0]),
        "LeftArm": np.array([L["shoulder_width"] / 2.0, 0.0, 0.0]),
        "LeftForeArm": np.array([L["upper_arm"], 0.0, 0.0]),
        "LeftHand": np.array([L["forearm"], 0.0, 0.0]),
        "RightShoulder": np.array([0.0, 0.35 * t, 0.0]),
        "RightArm": np.array([-L["shoulder_width"] / 2.0, 0.0, 0.0]),
        "RightForeArm": np.array([-L["upper_arm"], 0.0, 0.0]),
        "RightHand": np.array([-L["forearm"], 0.0, 0.0]),
        "LeftUpLeg": np.array([L["hip_width"] / 2.0, 0.0, 0.0]),
        "LeftLeg": np.array([0.0, -L["thigh"], 0.0]),
        "LeftFoot": np.array([0.0, -L["shin"], 0.0]),
        "RightUpLeg": np.array([-L["hip_width"] / 2.0, 0.0, 0.0]),
        "RightLeg": np.array([0.0, -L["thigh"], 0.0]),
        "RightFoot": np.array([0.0, -L["shin"], 0.0]),
    }
    ends = {
        "Head": np.array([0.0, L["head"], 0.0]),
        "LeftHand": np.array([L["hand"], 0.0, 0.0]),
        "RightHand": np.array([-L["hand"], 0.0, 0.0]),
        "LeftFoot": FOOT_REST * L["foot"],
        "RightFoot": FOOT_REST * L["foot"],
    }
    return off, ends


def heading(rot: np.ndarray) -> np.ndarray:
    """Only the turn about the vertical axis of a rotation (where its +Z faces on the floor)."""
    fwd = rot[:, 2]
    if math.hypot(fwd[0], fwd[2]) < 1e-6:
        return I3.copy()
    return axis_rotation("Y", math.degrees(math.atan2(fwd[0], fwd[2])))


def neutral_local(joint: str, parent_global: Optional[np.ndarray] = None) -> np.ndarray:
    """The neutral pose hidden bones relax to: arms down along the body (10 degrees out), upper legs straight down
    (vertical, only turned with the hips), everything else as in the rest pose."""
    if joint == "LeftArm":
        return swing(X_AXIS, np.array([0.17, -1.0, 0.0]))
    if joint == "RightArm":
        return swing(-X_AXIS, np.array([-0.17, -1.0, 0.0]))
    if joint in ("LeftUpLeg", "RightUpLeg") and parent_global is not None:
        return parent_global.T @ heading(parent_global)
    return I3.copy()


def blend_weight(visibility: float) -> float:
    return float(np.clip((visibility - VIS_LOW) / (VIS_HIGH - VIS_LOW), 0.0, 1.0))


def slerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    if t >= 1.0:
        return b
    if t <= 0.0:
        return a
    return a @ rot_fraction(a.T @ b, t)


# Landmarks whose best visibility weights each solved bone (hips and chest are always solved).
EVIDENCE = {
    "Neck": ("left_ear", "right_ear"),
    "Head": ("left_ear", "right_ear", "nose"),
    **{f"{S}{bone}": tuple(f"{s}_{n}" for n in names)
       for S, s in (("Left", "left"), ("Right", "right"))
       for bone, names in (("Arm", ("elbow", "wrist")), ("ForeArm", ("elbow", "wrist")),
                           ("Hand", ("wrist", "index", "pinky")), ("UpLeg", ("knee", "ankle")),
                           ("Leg", ("knee", "ankle")), ("Foot", ("ankle", "foot_index", "heel")))},
}


def solve_frame(world_frame, prev_local: Optional[dict] = None) -> dict:
    """One frame of world landmarks (33 x [x, y, z, visibility]) -> {joint: local 3x3 rotation}.
    prev_local: the previous frame's result, used only where a frame is degenerate (zero-length bone)."""
    f = np.asarray(world_frame, dtype=np.float64)
    p = to_bvh_axes(f)
    vis = f[:, 3] if f.shape[1] > 3 else np.ones(len(f))
    prev_local = prev_local or {}
    G: dict = {}
    L: dict = {}

    def put(joint: str, global_rot: Optional[np.ndarray]) -> None:
        parent = PARENT[joint]
        pg = G[parent] if parent else I3
        if global_rot is None:
            local = prev_local.get(joint, neutral_local(joint, pg))
        else:
            local = pg.T @ global_rot
            if joint in EVIDENCE:
                w = blend_weight(max(vis[B[n]] for n in EVIDENCE[joint]))
                local = slerp(neutral_local(joint, pg), local, w)
        L[joint] = local
        G[joint] = pg @ local

    def swing_from_parent(joint: str, rest_dir, a, b) -> np.ndarray:
        pg = G[PARENT[joint]]
        return pg @ swing(rest_dir, pg.T @ (b - a))

    mid_hip = _mid(p, "left_hip", "right_hip")
    mid_sh = _mid(p, "left_shoulder", "right_shoulder")
    mid_ear = _mid(p, "left_ear", "right_ear")

    put("Hips", frame_rotation(p[B["left_hip"]] - p[B["right_hip"]], mid_sh - mid_hip, X_AXIS, Y_AXIS))
    chest = frame_rotation(mid_sh - mid_hip, p[B["left_shoulder"]] - p[B["right_shoulder"]], Y_AXIS, X_AXIS)
    put("Spine", None if chest is None else G["Hips"] @ rot_fraction(G["Hips"].T @ chest, 0.5))
    put("Spine1", chest)
    put("Neck", swing_from_parent("Neck", Y_AXIS, mid_sh, mid_ear))
    put("Head", frame_rotation(p[B["left_ear"]] - p[B["right_ear"]], p[B["nose"]] - mid_ear, X_AXIS, Z_AXIS))

    for side, sign in (("Left", 1.0), ("Right", -1.0)):
        s = side.lower()
        rest = sign * X_AXIS
        L[f"{side}Shoulder"] = I3
        G[f"{side}Shoulder"] = G["Spine1"]
        sh, el, wr = p[B[f"{s}_shoulder"]], p[B[f"{s}_elbow"]], p[B[f"{s}_wrist"]]
        put(f"{side}Arm", swing_from_parent(f"{side}Arm", rest, sh, el))
        put(f"{side}ForeArm", swing_from_parent(f"{side}ForeArm", rest, el, wr))
        knuckles = _mid(p, f"{s}_index", f"{s}_pinky")
        put(f"{side}Hand", frame_rotation(knuckles - wr, p[B[f"{s}_index"]] - p[B[f"{s}_pinky"]], rest, Z_AXIS))
        hip, knee, ankle, toe = (p[B[f"{s}_hip"]], p[B[f"{s}_knee"]], p[B[f"{s}_ankle"]],
                                 p[B[f"{s}_foot_index"]])
        put(f"{side}UpLeg", swing_from_parent(f"{side}UpLeg", -Y_AXIS, hip, knee))
        put(f"{side}Leg", swing_from_parent(f"{side}Leg", -Y_AXIS, knee, ankle))
        put(f"{side}Foot", swing_from_parent(f"{side}Foot", FOOT_REST, ankle, toe))
    return L


def root_track(motion: dict, lengths: dict) -> tuple[list, dict]:
    """Per frame the root position in metres (BVH axes) or None without a body, and how it was made."""
    img = motion["body"]["image"]
    world = motion["body"]["world"]
    w, h = float(motion["width"]), float(motion["height"])
    hip_height = lengths["thigh"] + lengths["shin"] + ANKLE_HEIGHT
    found = [i for i, f in enumerate(img) if f is not None and i < len(world) and world[i] is not None]
    if not found:
        return [None] * len(img), {"anchor": None}
    hip_vis = float(np.median([min(img[i][B["left_hip"]][3], img[i][B["right_hip"]][3]) for i in found]))
    anchor = "hips" if hip_vis >= VIS_SOLVE else "shoulders"
    pair = ("left_hip", "right_hip") if anchor == "hips" else ("left_shoulder", "right_shoulder")

    def px(frame, name):
        return np.array([frame[B[name]][0] * w, frame[B[name]][1] * h])

    ratios = []
    anchors = {}
    for i in found:
        fi, fw = img[i], np.asarray(world[i], dtype=np.float64)
        anchors[i] = (px(fi, pair[0]) + px(fi, pair[1])) / 2.0
        if anchor == "hips":
            img_len = np.linalg.norm((px(fi, "left_shoulder") + px(fi, "right_shoulder")) / 2.0 - anchors[i])
            world_len = np.linalg.norm(_mid(fw[:, :3], "left_shoulder", "right_shoulder")
                                       - _mid(fw[:, :3], "left_hip", "right_hip"))
        else:
            img_len = np.linalg.norm(px(fi, "left_shoulder") - px(fi, "right_shoulder"))
            world_len = np.linalg.norm(fw[B["left_shoulder"], :3] - fw[B["right_shoulder"], :3])
        if img_len > 4.0:
            ratios.append(world_len / img_len)
    metres_per_px = float(np.median(ratios)) if ratios else 0.0
    first = anchors[found[0]]
    out: list = [None] * len(img)
    for i in found:
        d = (anchors[i] - first) * metres_per_px
        out[i] = np.array([d[0], hip_height - d[1], 0.0])     # image right = +X, image down = -Y
    return out, {"anchor": anchor, "metres_per_pixel": round(metres_per_px, 6), "hip_height_m": round(hip_height, 4),
                 "depth": "not tracked (Z = 0)"}


def build_bvh(motion: dict, track: str = "world") -> dict:
    """motion dict -> {"text": BVH text, "frames", "channels", "joints", "held_frames", "lengths", "root"}."""
    world = motion["body"][track]
    n = len(world)
    if not any(f is not None for f in world):
        raise BVHError("No body was found in any frame: nothing to write to motion.bvh.")
    measured = measure_lengths(world)
    lengths = measured["lengths"]
    offsets, ends = rest_offsets(lengths)
    roots, root_info = root_track(motion, lengths)

    rows: list = [None] * n
    prev_local: dict = {}
    for i, frame in enumerate(world):
        if frame is None or roots[i] is None:
            continue
        local = solve_frame(frame, prev_local)
        prev_local = local
        row = list(roots[i] * 100.0)
        for joint in JOINTS:
            row.extend(euler_zxy(local[joint]))
        rows[i] = row
    held = 0
    first = next(r for r in rows if r is not None)
    last = first
    for i in range(n):          # frames without a body repeat the nearest earlier (or the first) solved frame
        if rows[i] is None:
            rows[i] = list(last)
            held += 1
        else:
            last = rows[i]
    data = np.array(rows, dtype=np.float64)
    rot_cols = [c for c in range(data.shape[1]) if c >= 3]
    data[:, rot_cols] = np.degrees(np.unwrap(np.radians(data[:, rot_cols]), axis=0))
    fps = float(motion.get("fps") or 16)
    text = format_bvh(offsets, ends, data, 1.0 / fps)
    return {"text": text, "frames": n, "channels": data.shape[1], "joints": len(JOINTS), "held_frames": held,
            "frame_time": 1.0 / fps, "lengths_m": {k: round(v, 4) for k, v in lengths.items()},
            "measured": measured["measured"], "scale_of_defaults": measured["scale_of_defaults"], "root": root_info}


def format_bvh(offsets: dict, ends: dict, data: np.ndarray, frame_time: float) -> str:
    children: dict = {j: [] for j in JOINTS}
    for j, parent in SKELETON:
        if parent:
            children[parent].append(j)
    lines = ["HIERARCHY"]

    def fmt(v) -> str:
        return " ".join(f"{float(x):.6f}" for x in v)

    def emit(joint: str, depth: int) -> None:
        pad = "\t" * depth
        parent = PARENT[joint]
        lines.append(f"{pad}{'ROOT' if parent is None else 'JOINT'} {joint}")
        lines.append(f"{pad}{{")
        lines.append(f"{pad}\tOFFSET {fmt(offsets[joint])}")
        channels = CHANNELS_ROOT if parent is None else CHANNELS_JOINT
        lines.append(f"{pad}\tCHANNELS {len(channels)} {' '.join(channels)}")
        for child in children[joint]:
            emit(child, depth + 1)
        if joint in END_SITES:
            lines.append(f"{pad}\tEnd Site")
            lines.append(f"{pad}\t{{")
            lines.append(f"{pad}\t\tOFFSET {fmt(ends[joint])}")
            lines.append(f"{pad}\t}}")
        lines.append(f"{pad}}}")

    emit("Hips", 0)
    lines.append("MOTION")
    lines.append(f"Frames: {len(data)}")
    lines.append(f"Frame Time: {frame_time:.6f}")
    for row in data:
        lines.append(" ".join(f"{float(v):.4f}" for v in row))
    return "\n".join(lines) + "\n"


def write_bvh(motion: dict, path: Path) -> dict:
    from .jobs import atomic_write_text
    info = build_bvh(motion)
    atomic_write_text(path, info.pop("text"))
    info["file"] = Path(path).name
    return info


# --------------------------------------------------------------------------- reading (tests, checks)
def parse_bvh(text: str) -> dict:
    """Minimal BVH reader: {"joints": [{name, parent, offset, channels, end_site}], "frames", "frame_time",
    "motion": (frames, channels) array}. Raises BVHError on a malformed file."""
    tokens = text.split()
    pos = 0

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise BVHError("unexpected end of file")
        pos += 1
        return tokens[pos - 1]

    def expect(word: str) -> None:
        got = take()
        if got != word:
            raise BVHError(f"expected {word!r}, got {got!r}")

    joints: list = []
    expect("HIERARCHY")

    def joint(parent: Optional[str], kind: str) -> None:
        name = take()
        expect("{")
        expect("OFFSET")
        offset = [float(take()) for _ in range(3)]
        expect("CHANNELS")
        count = int(take())
        channels = [take() for _ in range(count)]
        entry = {"name": name, "parent": parent, "offset": offset, "channels": channels, "end_site": None,
                 "kind": kind}
        joints.append(entry)
        while True:
            word = take()
            if word == "JOINT":
                joint(name, "JOINT")
            elif word == "End":
                expect("Site")
                expect("{")
                expect("OFFSET")
                entry["end_site"] = [float(take()) for _ in range(3)]
                expect("}")
            elif word == "}":
                return
            else:
                raise BVHError(f"unexpected {word!r} in joint {name}")

    expect("ROOT")
    joint(None, "ROOT")
    expect("MOTION")
    expect("Frames:")
    frames = int(take())
    expect("Frame")
    expect("Time:")
    frame_time = float(take())
    values = tokens[pos:]
    n_channels = sum(len(j["channels"]) for j in joints)
    if len(values) != frames * n_channels:
        raise BVHError(f"motion has {len(values)} values, expected {frames} x {n_channels}")
    motion = np.array([float(v) for v in values], dtype=np.float64).reshape(frames, n_channels)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    start = next(k for k, ln in enumerate(lines) if ln.startswith("Frame Time:")) + 1
    if len(lines) - start != frames or any(len(ln.split()) != n_channels for ln in lines[start:]):
        raise BVHError("every motion line must hold one frame of all channels")
    return {"joints": joints, "frames": frames, "frame_time": frame_time, "channels": n_channels, "motion": motion}


def forward_kinematics(parsed: dict, frame: int) -> dict:
    """Global (position cm, rotation 3x3) of every joint, plus "<joint>/end" for End Sites."""
    row = parsed["motion"][frame]
    out: dict = {}
    col = 0
    for j in parsed["joints"]:
        values = dict(zip(j["channels"], row[col:col + len(j["channels"])]))
        col += len(j["channels"])
        rot = I3.copy()
        for ch in j["channels"]:
            if ch.endswith("rotation"):
                rot = rot @ axis_rotation(ch[0].upper(), values[ch])
        local_pos = np.array(j["offset"], dtype=np.float64)
        if j["parent"] is None:
            local_pos = local_pos + np.array([values.get("Xposition", 0.0), values.get("Yposition", 0.0),
                                              values.get("Zposition", 0.0)])
            gpos, grot = local_pos, rot
        else:
            ppos, prot = out[j["parent"]]
            gpos, grot = ppos + prot @ local_pos, prot @ rot
        out[j["name"]] = (gpos, grot)
        if j["end_site"] is not None:
            out[j["name"] + "/end"] = (gpos + grot @ np.array(j["end_site"]), grot)
    return out


def describe_bvh(path: Path) -> dict:
    parsed = parse_bvh(Path(path).read_text(encoding="utf-8"))
    return {"file": Path(path).name, "joints": [j["name"] for j in parsed["joints"]], "frames": parsed["frames"],
            "frame_time": parsed["frame_time"], "channels": parsed["channels"],
            "fps": round(1.0 / parsed["frame_time"], 3) if parsed["frame_time"] else None}
