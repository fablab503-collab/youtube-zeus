# Zeus Effects — engine contract (v1)

Free, local-only version of the "Genjutsu" character-replacement workflow
(idea: https://github.com/sirioberati/Genjustsu-Open-Source-Workflow, MIT, by Sirio Berati).
Keep *how* people move (depth, masks, face mesh), replace *who* they are with the user's
reference photo, put the original soundtrack back.

**Hard rules**
- Everything runs on this Mac (Apple Silicon, M2 Pro, 16 GB). No paid API, no Replicate,
  no Enhancor/Seedance, no cloud calls except downloading open model weights and yt-dlp.
- No API keys needed. Never ask for or store keys.
- Model weights and venvs live outside the repo, under the runtime folder
  `~/Library/Application Support/YouTube Zeus/Effects/` (env `ZEUS_EFFECTS_HOME` overrides).
- No vocal split / pitch shift: there is no IP filter locally. The final video always gets the
  **complete original audio** of the trimmed source, copied untouched.
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
                           review.py (hash gate), jobs.py, progress.py
effects/zfx/engines/       vace.py (Wan2.1-VACE-1.3B, diffusers, MPS), faceswap.py (FaceFusion)
effects/tests/             unittest, runnable without weights (synthetic clips)
```
Runtime (`$ZEUS_EFFECTS_HOME`, default `~/Library/Application Support/YouTube Zeus/Effects/`):
```
.venv/            main engine, Python 3.12 via `uv`
facefusion/       FaceFusion checkout + its own .venv (optional, `setup.sh --faceswap`)
models/           Video-Depth-Anything-Small, Wan2.1-VACE-1.3B-diffusers (optional, `setup.sh --vace`)
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
| `doctor [--json]` | Python, torch+MPS, ffmpeg, yt-dlp, pyobjc Vision, weights present per engine, free RAM/disk. Never downloads. |
| `prepare --input <file or YouTube URL> [--start S] [--duration D≤10] --mode depth\|depth_mesh\|mesh [--job DIR] [--cookies FILE]` | Download/trim, normalize, depth, masks, composite, mesh. Creates the job. |
| `approve --job DIR` | Records the review: SHA-256 of every prepared artifact into `job.json.review`. |
| `generate --job DIR --engine vace\|faceswap --ref IMG [--ref IMG2] [--prompt TEXT] [--consent] [--frames N] [--steps N] [--size 480p\|360p] [--seed N]` | Refuses without `--consent`, without approval, or if any approved artifact hash changed. Writes `result-raw.mp4` then `final.mp4` (original audio). |
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
control.mp4         depth people over the original background (mode depth / depth_mesh), + mesh overlay (depth_mesh), or original + mesh (mesh)
face-landmarks.json MediaPipe Face Landmarker, 478 points per frame (mesh modes)
preview.jpg         contact sheet: 4 frames × (work | depth | mask | control) for review
result-raw.mp4      engine output (no audio)
final.mp4           result-raw + original audio, retimed to source fps; metadata comment as above
```

## Engines

- **vace** (full character replacement, slow): `Wan-AI/Wan2.1-VACE-1.3B-diffusers` (Apache-2.0) via
  diffusers `WanVACEPipeline` on MPS. Inputs: `video` = control.mp4 frames, `mask` = mask.mp4 (1 = regenerate),
  `reference_images` = user photos, prompt = user prompt + "replace the person with the person in the reference
  image, keep the exact motion, lips and timing; depth colours are guidance only and must not appear".
  Frame count 4k+1 at 16 fps; long clips in overlapping windows. Must fit 16 GB unified memory: encode the
  prompt with the UMT5 text encoder first and free it before loading the transformer; bf16/fp16; VAE tiling/slicing.
- **faceswap** (face only, fast): FaceFusion 3.x headless (`headless-run`, processors `face_swapper face_enhancer`,
  execution provider `coreml`, fallback `cpu`) on `source.mp4`, then original audio. No depth/mask needed;
  still requires approve + consent.

## Zeus app integration (Swift)

- Sidebar item **Effects**: pick an eaten video (uses its YouTube URL) or a local file, choose start/length
  (≤10 s), mode, Prepare → review (preview.jpg + playable depth/mask/control) → Approve → reference photo(s),
  consent checkbox, engine (Face swap · fast / Full replace · slow) → Generate → play `final.mp4`,
  Reveal in Finder, Save to Movies/YouTube Zeus Effects.
- `Services/EffectsEngine.swift` runs the CLI as a subprocess, parses JSON lines, exposes progress.
- Reachable by agents: `zeus effects doctor|prepare|approve|generate|status|list` (pass-through),
  `youtubezeus://effects?video=<id>` link, documented in `AgentGuide.swift`.
- Free first: no cloud engine option at all in this tab.
