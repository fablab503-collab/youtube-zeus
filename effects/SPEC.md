# Zeus Effects — engine contract (v1)

Free, local-only version of the "Genjutsu" character-replacement workflow
(idea: https://github.com/sirioberati/Genjustsu-Open-Source-Workflow, MIT, by Sirio Berati).
Keep *how* people move (depth, masks, face mesh, whole-body pose), replace *who* they are with the
user's reference photo, put the original soundtrack back.

**Hard rules**
- Everything runs on this Mac (Apple Silicon, M2 Pro, 16 GB). No paid API, no Replicate,
  no Enhancor/Seedance, no cloud calls except downloading open model weights and yt-dlp.
- No API keys needed. Never ask for or store keys.
- Model weights and venvs live outside the repo, under the runtime folder
  `~/Library/Application Support/YouTube Zeus/Effects/` (env `ZEUS_EFFECTS_HOME` overrides).
- No pitch tricks to dodge filters (there is no IP filter locally). `generate` always gives the final video the
  **complete original audio** of the trimmed source, copied untouched. Only the separate, opt-in `voice` step
  (2026-10-11) changes the audio: it splits voice and music, converts the voice to a consented reference with the same
  words and timing, and writes `final-voice.mp4` next to `final.mp4`, which stays as it was.
- Every generation is gated by an explicit review approval bound to SHA-256 hashes of the
  prepared artifacts, and by a consent flag ("the reference photos show me or people who agreed").
- Outputs carry metadata `comment=AI-generated with YouTube Zeus Effects (<engine>)`.

## Layout

Repo (`effects/`, MIT, ships with Zeus):
```
effects/SPEC.md            this file
effects/setup.sh           creates runtime venv(s), installs deps, downloads small weights
effects/requirements.txt   main engine deps (pinned)
effects/zeus_effects.py    CLI entry point (argparse), subcommands below
effects/zfx/               package: io.py (download/trim/normalize/audio), depth.py,
                           masks.py (Apple Vision via pyobjc), composite.py, mesh.py,
                           pose.py (body + hands motion capture), bvh.py (BVH skeleton),
                           review.py (hash gate), jobs.py, progress.py
effects/zfx/engines/       vace.py (Wan2.1-VACE-1.3B, diffusers, MPS), faceswap.py (FaceFusion)
effects/tests/             unittest, runnable without weights (synthetic clips)
```
Runtime (`$ZEUS_EFFECTS_HOME`, default `~/Library/Application Support/YouTube Zeus/Effects/`):
```
.venv/            main engine, Python 3.12 via `uv`
facefusion/       FaceFusion checkout + its own .venv (optional, `setup.sh --faceswap`)
models/           Video-Depth-Anything-Small, face_landmarker.task, pose_landmarker_heavy.task, hand_landmarker.task,
                  Wan2.1-VACE-1.3B-diffusers (optional, `setup.sh --vace`)
Jobs/<job-id>/    one folder per job
effects.log
```

## CLI (run with `$ZEUS_EFFECTS_HOME/.venv/bin/python effects/zeus_effects.py …`)

Progress goes to **stdout as JSON lines**, one object per line:
`{"stage":"depth","progress":0.42,"message":"frame 40/96"}`.
The last line is either `{"done":true,"job":"<dir>","result":{…}}` or
`{"error":"<plain-English message>","stage":"…"}` (exit code 1). Human logs go to stderr and `effects.log`.

| Command | What it does |
|---|---|
| `doctor [--json]` | Python, torch+MPS, ffmpeg, yt-dlp, pyobjc Vision, weights present per engine (incl. the pose and hand landmarkers), free RAM/disk. Never downloads. |
| `prepare --input <file or YouTube URL> [--start S] [--duration D≤10] --mode depth\|depth_mesh\|mesh\|pose\|depth_pose [--job DIR] [--cookies FILE] [--motion]` | Download/trim, normalize, depth (not in `pose`), masks, face mesh, motion capture (pose modes, or `--motion`), composite. Creates the job. |
| `motion --job DIR [--json]` | Summary of the job's motion capture for agents: frames with a body, each hand and the face, average visibility, limbs tracked, the BVH (joints, channels, fps) and file paths. |
| `approve --job DIR` | Records the review: SHA-256 of every prepared artifact into `job.json.review`. |
| `generate --job DIR --engine vace\|faceswap --ref IMG [--ref IMG2] [--ref-pack DIR.c101face] [--prompt TEXT] [--consent] [--frames N] [--steps N] [--size 480p\|360p] [--seed N] [--upscale fast\|best\|lanczos]` | Refuses without `--consent`, without approval, or if any approved artifact hash changed. Writes `result-raw.mp4` then `final.mp4` (original audio). |
| `status --job DIR [--json]` | Prints `job.json`. |
| `list [--json]` | Jobs under `Jobs/`, newest first. |

## Job folder

```
job.json            {id, created, input, start, duration, mode, fps, size, status, artifacts{name: path}, review{approved_at, hashes}, generations[...]}
source.mp4          trimmed original, original fps and audio (never modified after creation)
audio.m4a           original audio, stream-copied when possible
work.mp4            16 fps, long side ≤ 832, even dims (what models see)
depth.mp4           colored depth (Video-Depth-Anything-Small, inferno-like colormap), same frames as work.mp4
mask.mp4            people mask, white = person (Apple Vision VNGeneratePersonSegmentationRequest, accurate), dilated 8 px, temporally smoothed
control.mp4         depth people over the original background (mode depth / depth_mesh), + mesh overlay (depth_mesh), or original + mesh (mesh),
                    the pose skeleton on black (pose), grey depth inside the mask (hard edge) + the skeleton inside the mask (depth_pose)
face-landmarks.json MediaPipe Face Landmarker, 478 points per frame (mesh and pose modes)
motion.json         whole-body motion capture (pose modes / --motion), see "Motion capture" below
motion.bvh          BVH body skeleton solved from motion.json (pose modes / --motion)
pose.mp4            OpenPose-style skeleton on black, same frames/size as work.mp4 (pose modes / --motion)
preview.jpg         contact sheet: 4 frames × (work | depth | mask | [pose] | control) for review (no depth column in mode pose)
result-raw.mp4      engine output (no audio)
result-crops/ + result-crops.json   vace: per window the generated crop, the original crop and the mask at the
                    generation size (wNN/{gen,base,mask}/NNNN.png) + boxes; color/ = Log LUTs when the source is Log
final.mp4           result-raw + original audio, retimed to source fps; metadata comment as above
```

## Modes

| Mode | control.mp4 | VACE control at generation (engines/vace.py) |
|---|---|---|
| `depth` | coloured depth inside the people mask (feathered) over the original | grey depth inside the mask, original outside |
| `depth_mesh` | the same + white face mesh | the same, mesh in grey |
| `mesh` | the original + white face mesh | as is |
| `pose` | OpenPose-style skeleton (body, hands, 68 face dots) on black; no depth computed | as is (the background comes back from the source in the final composite) |
| `depth_pose` | grey depth inside the mask (hard edge, no halo) over the original + the skeleton inside the mask | as is (already grey) |

`pose` and `depth_pose` always save the motion capture (and run the face mesh, the face is part of it);
`--motion` adds it to the other modes. Approval then also hashes `motion.json`, `pose.mp4`, `motion.bvh` and
`face-landmarks.json`.

## Motion capture (`zfx/pose.py`, `zfx/bvh.py`)

Every frame of work.mp4 goes through MediaPipe Pose Landmarker **heavy** (33 body points, VIDEO mode) and Hand
Landmarker (21 points per hand, 2 hands), official `.task` files from Google's model storage (Apache-2.0;
`setup.sh` downloads them, and a missing one is fetched on first use; `ZEUS_EFFECTS_NO_DOWNLOAD=1` forbids that).
Like the face mesh, they first run on one fixed crop around the main person of mask.mp4 (small presenters are too
small for MediaPipe's detectors in the full frame) and fall back to the full frame when that finds the body in fewer
frames. Hands go to the person's own left/right by the nearest body wrist (a hand farther than 1.2 × the shoulder
width is someone else's); without a body, MediaPipe's handedness is used swapped (it assumes a mirrored selfie).
Smoothing: One-Euro filter (image: min_cutoff 1.2 Hz, beta 4; world: 1.2 Hz, beta 1.5; d_cutoff 1 Hz) on x, y, z,
reset after every frame without a detection; raw values are kept next to the smoothed ones.

`motion.json` (format `"zeus-motion"`, version 1; the schema is `zfx.pose.MOTION_SCHEMA`):
```
format, version, generator, created, source{job, input, start, duration, video, source_fps}
fps (16), frames, width, height, time[s per frame], coordinates{image, world, frame_index}
models{pose, hands, face}, search{pose: person_crop|full_frame, crop, hands}, smoothing{...}
names{body[33], hand[21], face}, edges{body, hand}, limb_groups{part: [body names]}
body{image, world, image_raw, world_raw}       per frame null or 33 × [x, y, z, visibility]
hands{left|right: {image, world, image_raw, world_raw: per frame null or 21 × [x, y, z], score, frames_found}}
face null | {points 478, model, file, image, image_raw: per frame null or 478 × [x, y, z], frames_found, openpose68}
stats{frames, body_frames, left_hand_frames, right_hand_frames, face_frames, visibility{mean, per_landmark, limbs}}
```
image = normalized to the frame (x right, y down, z MediaPipe relative depth in the x scale); world = metres,
camera-aligned (x right, y down, z away from the camera), body origin between the hips.

`motion.bvh`: Hips (6 channels) › Spine › Spine1 › Neck › Head, Left/RightShoulder › Arm › ForeArm › Hand,
Left/RightUpLeg › Leg › Foot (19 joints, 60 channels, ZXY, degrees), Y up, centimetres, facing +Z, frame time
1/16 s. Rest = T-pose with the person's measured bone lengths. Rotations: hips and chest from the hip/shoulder lines,
spine = half-way, head from ears + nose, hands from wrist/knuckles, every other bone = the shortest swing of its rest
direction onto the measured one in the parent's frame (twist inherited from the shoulder/hip lines, never flips).
Hidden joints: each bone is blended (slerp) from a neutral pose (arms down along the body, legs straight, head
level) to its solved rotation by the best visibility of the landmarks that place it (0 at <= 0.2, full at >= 0.5),
so partly seen limbs follow MediaPipe's estimate and limbs never in the picture relax, without pops; frames without
a body repeat the nearest solved one; root = rest hip height + the image movement of the hips (or shoulders) in cm, no depth. Fingers and the
face are in motion.json only. Use: Blender's BVH importer directly; Unreal Engine 5.5 has no BVH importer: import
in Blender, export FBX (armature + animation), import into UE as an animation on a new skeleton, then retarget to
the Third Person template's Manny with an IK Rig per skeleton (chains spine/neck/head/arms/legs) and an IK Retargeter.

## Engines

- **vace** (full character replacement, slow): `Wan-AI/Wan2.1-VACE-1.3B-diffusers` (Apache-2.0) via
  diffusers `WanVACEPipeline` on MPS. Inputs: `video` = control.mp4 frames, `mask` = mask.mp4 (1 = regenerate),
  `reference_images` = user photos, prompt = user prompt + "replace the person with the person in the reference
  image, keep the exact motion, lips and timing; depth colours are guidance only and must not appear".
  Frame count 4k+1 at 16 fps; long clips in overlapping windows. Fits 16 GB unified memory (measured 2026-10-11,
  M2 Pro: 5.0 GB peak, ~16 min per 2 s window at 360p / 33 frames / 20 steps): UMT5 prompt encoding on the CPU in a
  separate process (cached), a one-time fp16 `transformer-half/` copy streamed to the GPU block by block, fp16 VAE
  with tiling and a GPU sync per conv, chunked attention, scheduler step on the CPU. Inside the mask the control is
  grey depth; the reference photo is cut out onto white. Details and all measured runs: `ENGINES.md`.
- **vace at the source resolution** (`zfx/cropbox.py`, `zfx/upscale.py`, `zfx/hires.py`, `zfx/logcolor.py`): each
  window is generated on a crop around the person (union of the window's main-person mask boxes + 12 % margin, grown to the
  generation aspect, inside the frame; the whole frame when the person fills most of it; `ZEUS_VACE_CROP=0` turns it
  off), from source.mp4 at full resolution on work.mp4's 16 fps clock. The crop is upscaled (`--upscale fast` =
  Real-ESRGAN realesr-animevideov3, `best` = realesrgan-x4plus, `lanczos`; Real-ESRGAN ncnn-vulkan, BSD-3, installed by
  `setup-engines.sh --upscaler`, Lanczos when missing), colour-matched (Lab mean/std from a ring just outside the mask),
  given the source's grain (measured in flat areas), and merged inside the mask with a distance feather of ~1.5 % of
  the box's short side into the original frames at their own size, fps and colour tags: every pixel outside the mask
  is the original, nothing is time-stretched, the audio is stream-copied. Camera Log (Cinema 101's `c101 probe` on the
  job's input file and source.mp4, else the colour tags; `ZEUS_FX_LOG_PROFILE` forces a profile or `none`): the model
  sees a Rec.709 picture through `c101 bake`'s LUT (ffmpeg lut3d) and the patch goes back to Log through the LUT's
  numerical inverse. job.json's generation records `color` (detection, LUTs, inverse round trip) and `mux`
  (boxes, upscaler, colour shifts, grain, feather).
- **faceswap** (face only, fast): FaceFusion 3.x headless (`headless-run`, processors `face_swapper face_enhancer`,
  execution provider `coreml`, fallback `cpu`) on `source.mp4`, then original audio. No depth/mask needed;
  still requires approve + consent.

## Face packs (`--ref-pack`)

`generate --ref-pack <Name>.c101face` takes the references from a face pack made by Cinema 101's "Scan my face"
(TrueDepth camera on the iPhone; layout and manifest in `FACE-PACK.md`), alone or with `--ref` photos.
`zfx/facepack.py` refuses a pack whose manifest is not `format` "c101face" `version` 1, whose `consent` is not
exactly `true`, or that names any path leaving the pack folder (absolute, `..`, or a link out); missing frames are
skipped and unknown fields ignored. It picks up to 5 frames in this order: the sharpest frontal neutral frame
(|yaw| < 8°, |pitch| < 10°), the sharpest near +20° and −20° yaw, near +40° and −40°, then a smile, skipping frames
under 35 % of the pack's sharpest; ties go to the frame closest to the angle, then the file name. vace uses only the
first 2 (each reference is an extra latent frame in every window). The picks are copied to `refs/pack-N.jpg` (before
any `--ref` photos) and the generation record gets `face_pack` (pack path, name, person, manifest SHA-256, and each
frame with why it was chosen). `facepack --pack <dir> [--max-refs N] [--json]` shows the same choice without
generating. `--consent` is still required: the pack's own consent does not replace it.

## Zeus app integration (Swift)

- Sidebar item **Effects**: pick an eaten video (uses its YouTube URL) or a local file, choose start/length
  (≤10 s), mode, Prepare → review (preview.jpg + playable depth/mask/control) → Approve → reference photo(s),
  consent checkbox, engine (Face swap · fast / Full replace · slow) → Generate → play `final.mp4`,
  Reveal in Finder, Save to Movies/YouTube Zeus Effects.
- `Services/EffectsEngine.swift` runs the CLI as a subprocess, parses JSON lines, exposes progress.
- Reachable by agents: `zeus effects doctor|prepare|approve|generate|status|list` (pass-through),
  `youtubezeus://effects?video=<id>` link, documented in `AgentGuide.swift`.
- Free first: no cloud engine option at all in this tab.
