# Zeus Effects — parts map (`zfx/parts.py`, 2026-10-12)

A richer control map for the character replacement: every region of every frame gets its own tone, so a swap or
redraw model can map each part of the person (and the things the user names) instead of one silhouette.
Free, local, Mac only (measured on an M2 Pro 16 GB). Everything lives in new files: `zfx/parts.py` (the map),
`zfx/things.py` (named things, CLIPSeg), `zfx/faceparse_worker.py` (BiSeNet in FaceFusion's venv),
`zfx/parts_cli.py` (command), `tests/test_parts.py`.

```
"$ZEUS_EFFECTS_HOME/.venv/bin/python" -m zfx.parts_cli --job DIR [--things "microphone, laptop, cap"] [--gaze]
      [--no-things] [--things-over-person] [--things-fps 2] [--things-threshold 0.4] [--things-template "{}"]
      [--things-no-zoom] [--no-face-detail] [--no-preview] [--device mps|cpu] [-v]
```
Run from `effects/`. The job must be prepared in a depth mode (`depth`, `depth_mesh`, `depth_pose`): it needs
`work.mp4` (16 fps), `mask.mp4` and `depth.mp4`. `motion.json` / `face-landmarks.json` are used when present;
otherwise the pose and the face mesh are computed with `zfx.pose` / `zfx.mesh` and saved. The approved artifacts
are never modified (new files only, recorded in `job.json.artifacts` as `parts`, `parts_index`, `parts_json`,
`control_parts`, `preview_parts`, with a `parts_info` summary), so `generate` keeps working on an approved job.

## Outputs (job folder)

| File | What |
|---|---|
| `parts.mp4` | the index map rendered with the fixed palette: one hue per class, lightness by depth (near = bright, far = dark), edges feathered 2 px |
| `parts.npz` | `index` (N, H, W) uint8 = legend id per pixel, plus `ids`, `names`, `things`, `fps`, `size`, `format` |
| `parts.json` | legend (id → name → colour), the things prompt and settings, per-frame stats (pixels per class, person coverage, things, gaze, blink, openness), clip stats, timings, warnings (format `zeus-parts`, version 1) |
| `control-parts.mp4` | the control video for VACE: the original frame outside the person and the things, inside them the depth luminance tinted by the part hue ("depth with tones"), the person's outline hard |
| `preview-parts.jpg` | contact sheet: 4 frames × (work, depth, mask, parts, control-parts) + the legend of the classes present (`preview.jpg` is left alone) |

## Legend (fixed ids; left/right = the person's own, like MediaPipe)

| id | name | group | hue° | sat | colour at value 1 | source |
|---|---|---|---|---|---|---|
| 0 | background-far | background | 230 | 0.55 | #7395ff | depth.mp4 (tertiles of the background depth) |
| 1 | background-mid | background | 215 | 0.40 | #99b8ff | depth.mp4 |
| 2 | background-near | background | 200 | 0.25 | #bfe9ff | depth.mp4 |
| 3 | body-other | body | 0 | 0.00 | #ffffff (grey) | person pixels farther than 1.5 shoulder widths from every part, or no pose |
| 4 | head | head | 25 | 0.50 | #ffb580 | pose/face ellipse, where BiSeNet gives nothing |
| 5 | face-skin | face | 30 | 0.28 | #ffe1b8 | BiSeNet 1 |
| 6 | brows | face | 35 | 0.95 | #ff9b0d | BiSeNet 2, 3 |
| 7 | eyes | face | 58 | 0.95 | #fff70d | BiSeNet 4, 5 |
| 8 | nose | face | 12 | 0.55 | #ff8f73 | BiSeNet 10 |
| 9 | mouth | face | 350 | 0.90 | #ff1a40 | BiSeNet 11, 12, 13 (mouth + lips) |
| 10 | ears | head | 18 | 0.70 | #ff874d | BiSeNet 7, 8, 9 (ear ring) |
| 11 | hair | head | 275 | 0.70 | #ab4dff | BiSeNet 17 |
| 12 | hat | head | 175 | 0.90 | #1affea | BiSeNet 18 |
| 13 | glasses | face | 305 | 0.85 | #ff26ff | BiSeNet 6 |
| 14 | neck | body | 38 | 0.50 | #ffd180 | pose quad chin → shoulders, BiSeNet 14, 15 (necklace) |
| 15 | torso | body | 0 | 0.85 | #ff2626 | pose quad shoulders → hips, BiSeNet 16 (cloth) |
| 16 | left-upper-arm | arms | 130 | 0.90 | #1aff40 | pose capsule shoulder → elbow |
| 17 | right-upper-arm | arms | 70 | 0.90 | #d4ff1a | |
| 18 | left-forearm | arms | 145 | 0.70 | #4dffa6 | pose capsule elbow → wrist |
| 19 | right-forearm | arms | 85 | 0.75 | #a6ff40 | |
| 20 | left-hand | arms | 160 | 0.95 | #0dffaf | hull + bones of the 21 hand points, else wrist → fingers capsule |
| 21 | right-hand | arms | 100 | 0.95 | #5fff0d | |
| 22 | left-thigh | legs | 320 | 0.85 | #ff26b6 | pose capsule hip → knee |
| 23 | right-thigh | legs | 255 | 0.85 | #6626ff | |
| 24 | left-shin | legs | 330 | 0.65 | #ff59ad | pose capsule knee → ankle |
| 25 | right-shin | legs | 265 | 0.65 | #9c59ff | |
| 26 | left-foot | legs | 340 | 0.95 | #ff0d5e | pose capsules ankle → toes, ankle → heel |
| 27 | right-foot | legs | 240 | 0.95 | #0d0dff | |
| 28 | gaze | face | 190 | 1.00 | #00d5ff (always full value; white in the control) | iris dot + arrow (`--gaze`) |
| 40+k | thing:<name> | things | (300 + 137.5 k) mod 360 | 1.00 | magenta, yellow-green, blue, red… | CLIPSeg, k = position in the prompt, at most 24 |

`zfx.parts.legend(things)` returns the same table as dicts (`id, name, group, hue, saturation, color, hex`);
`parts.json["legend"]` carries it, `parts.npz` the ids and names. The ids never change.

## How the map is built (per frame)

1. **Body parts** from the 33 MediaPipe pose points (visibility ≥ 0.5): the torso quad (shoulders → hips, or
   1.7 shoulder widths down when the hips are out of frame), capsules for thighs, shins, feet, upper arms,
   forearms, the neck quad from the chin line (face mesh point 152, else below the mouth) to the shoulders, the
   head ellipse around the face mesh (else around the eyes/ears), the hands from the 21 hand points (convex hull +
   fat bones) or from the pose's wrist/index/pinky. Draw order: torso, legs, neck, head, arms, hands (a hand in
   front of the face keeps its tone). All of it is intersected with `mask.mp4`; person pixels no part reached take
   the nearest part (Euclidean distance transform) within 1.5 shoulder widths, the rest stays `body-other`.
2. **Head detail**: the face box (mesh) grown 0.6× sideways, 1.0× up and 0.25× down → 512 × 512 crop → BiSeNet
   (FaceFusion's `bisenet_resnet_34.onnx`, CelebAMask-HQ, run once for the whole clip in FaceFusion's venv on
   CoreML). Its classes override only `head`, `neck`, `torso` and `body-other` pixels inside the person mask.
   Without FaceFusion the step is skipped with a warning (coarse head only).
3. **Gaze** (`--gaze`): per eye the iris centre (mesh 468 / 473) relative to the eye box (corners 33–133 /
   362–263, lids 160·158·153·144 / 385·387·373·380): `dx, dy` in fractions of the half width / height,
   `direction` = centre / left / right / up / down (picture terms: "right" = the picture's right, i.e. the
   person's left), the mean vector of both eyes, EAR (eye aspect ratio), `blink` = EAR under 0.7 × the median EAR
   of the ±8 frames around (and under 0.24), `openness` = EAR / the clip's 90th percentile. The marker (a dot on
   each iris, a short arrow when not centred) is drawn after the temporal smoothing.
4. **Things** (`--things "microphone, laptop, cap"`): CLIPSeg (`CIDAS/clipseg-rd64-refined`, Apache-2.0,
   revision `999e0328…`, ~600 MB downloaded once into `models/clipseg-rd64-refined/`, torch MPS) on frames
   sampled at 2 fps, on the whole frame and — when the person is small — on a crop around the person (zoom pass,
   merged by maximum); the 352 × 352 probability maps are interpolated linearly to every frame; per thing the
   blobs ≥ threshold (0.4) that contain a pixel ≥ threshold + 0.1 and cover ≥ 0.02 % of the frame are kept; the
   most likely thing wins a pixel; things stay outside the person mask unless `--things-over-person`.
5. **Background**: three depth bands from the luminance of `depth.mp4`, thresholds = tertiles of the background
   depth over 16 sampled frames (recorded in `parts.json["depth_bands"]`), blurred 3 px before banding.
6. **Temporal smoothing**: a majority vote over 3 frames (where the previous and next frames agree they outvote
   the current one). Rendered edges are feathered 2 px (`parts.mp4`: every edge; `control-parts.mp4`: only edges
   inside the person/things, the outline stays hard because VACE keeps a feathered halo as a glow).

## Using `control-parts.mp4` with VACE

- It has the same frames, size and fps as `control.mp4`, so it can replace it as the control video of the
  `vace` engine (`engines/vace.py` with `ZEUS_VACE_CONTROL=as-is`, or the `--control` of `vace.run`), with
  `mask.mp4` as the regenerate mask and the reference photos as usual. Outside the person it is the original
  frame, like the depth modes; inside, the grey depth VACE was trained on is kept as the luminance and the hue
  marks the part — a soft, consistent layout cue (head / hair / hat / face / neck / torso / each arm, hand, leg).
- Prompts: name the parts you want kept or changed with the same words as the legend and say what the tones
  are, for example "replace the person with the person in the reference image; keep the exact motion, the cap,
  the hands and the lips; the colour tones are a layout guide only and must not appear in the picture". Named
  things keep their own tint, so "keep the laptop and the microphone exactly" refers to visible regions.
- For a swap model that maps regions, use `parts.npz` directly (uint8 index per pixel per frame, legend ids)
  and `parts.json` for the per-frame gaze/blink and the things present.
- It is an experiment: VACE was not trained on tinted depth. Compare with the plain `control.mp4` on a short
  window before a long run; if the hues bleed into the result, lower `TINT_SATURATION` in `zfx/parts.py` (0.55).

## Measured (job `20261011-161416-d6spCMcfkew-6d49`, 64 frames 832 × 468, presenter in a corner, 6.7 % of the frame)

`--things "microphone, laptop, cap" --gaze`: 27.6 s real on the second run (47.5 s the first time: the CoreML
model compiles once and CLIPSeg loads), peak RSS 484 MB (`/usr/bin/time -l`; the first run 615 MB), with
another agent's VACE job running in parallel. Timings: load 0.8 s, face parser 7.8 s (64 crops, CoreML),
CLIPSeg 14 s (9 sampled frames × 2 passes × 3 prompts), map 1.4 s, render 1.7 s. Body in 64/64 frames, face
and face detail in 64/64. Classes: torso 3.4 % of the frame, face-skin 0.9, head 0.7, hat 0.43, neck 0.31,
right-hand 0.62 (frames with the raised hand), brows/eyes/nose/mouth/ears/hair small. Things: laptop found in
64/64 frames (the strip at the bottom right), microphone 9/64 outside the person (30/64 anywhere), cap 64/64
outside the person — which is wrong: the dark round microphone next to the head scores higher as "cap" (0.7–0.8)
than as "microphone" (0.5–0.6), so the blob is tinted "cap", while the real cap on the head is BiSeNet's `hat`
tone. Gaze: centre in 64/64 frames (the presenter looks at the camera; eyes 11 px wide), 3 blink frames, mean
openness 0.63 (eyes narrowed in the first half while smiling).

## Limits

- CLIPSeg is an open-vocabulary guess at 352 × 352, not a detector: it confuses look-alikes (a dark round
  microphone and a cap), fires at ~0.4–0.6 on slide graphics, and misses thin things. Check `things.found`
  (`rate`, `rate_anywhere`, `mean_max_prob`) in `parts.json`; raise `--things-threshold`, name things
  precisely ("microphone on a boom arm"), or name only what you need. A thing on the person needs
  `--things-over-person` (it then overrides the body tones).
- Vertical gaze is weak on small faces (the lids follow the iris; eye height here 1–2 px); horizontal gaze and
  blinks are usable. Blinks shorter than a frame (62 ms at 16 fps) are missed; `openness` shows narrowed eyes.
- The body parts are polygons and capsules in 2-D: a limb crossing in front of the torso is right, a limb behind
  the torso is not; self-occlusion is not modelled. Hidden landmarks (visibility < 0.5) draw nothing, the nearest
  part takes over (a seated presenter: the hoodie below the shoulders becomes `torso`).
- BiSeNet is trained on aligned celebrity portraits: side views, hands on the face and very small faces give
  rough labels; the collar of a hoodie often becomes `neck`. It only ever overrides head/neck/torso/body-other.
- Everything is at `work.mp4` resolution (long side 832); `parts.npz` is the only lossless map, the videos are
  H.264 (never read the classes back from `parts.mp4`).
- The temporal majority vote delays a one-frame event by nothing but removes one-frame flickers, including a
  real one-frame appearance.

## Wiring into `prepare` and the app (to do by the main session)

- `zeus_effects.py`: a `--parts` flag (and `--things TEXT`, `--gaze`, `--things-over-person`) on `prepare`, or a
  mode `depth_parts`; after the composite step call `zfx.parts.run_parts(job, things=parse_things(args.things),
  gaze=True, report=say)`; add `"parts"`, `"parts_index"`, `"parts_json"`, `"control_parts"`, `"preview_parts"`
  to `zfx.jobs.ARTIFACT_FILES` and to `zfx.review.OPTIONAL_REVIEWED` so the approval hashes them; a `parts`
  subcommand that runs `zfx.parts_cli.main` for existing jobs.
- `engines/vace.py`: when the job has `control_parts` and the mode (or a `--control parts` option) asks for it,
  use `control-parts.mp4` as the control ("as-is", it is already grey depth inside the mask).
- Swift (`EffectsEngine.swift`, `EffectsView.swift`): a mode "Depth + parts" (`depth_parts`) in `EffectsMode`
  with an explanation, a "Things to find" text field passed as `--things`, the review pane showing
  `preview-parts.jpg` and playing `parts.mp4` / `control-parts.mp4`, and the `parts_info` of `job.json`
  (classes, things found, gaze) in the job summary.
