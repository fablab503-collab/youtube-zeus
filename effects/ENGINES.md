# Zeus Effects — generation engines (free, local)

Two engines turn an approved job into `result-raw.mp4`; `zfx/generate.py` then writes `final.mp4`
(video retimed to the source fps/duration and scaled to the source size, plus `audio.m4a`
stream-copied untouched, metadata `comment=AI-generated with YouTube Zeus Effects (<engine>)`)
and appends a record to `job.json.generations`. Both refuse to run without `--consent`
("Tick: the photos show me or people who agreed") and without a valid review approval.

Nothing here needs an account, an API key or a paid service. Weights are downloaded once from
GitHub / Hugging Face by `effects/setup-engines.sh` (VACE) or by FaceFusion on its first run.

| | `faceswap` (fast, face only) | `vace` (slow, full person) |
|---|---|---|
| Code | `zfx/engines/faceswap.py` | `zfx/engines/vace.py` |
| Install | `effects/setup-engines.sh --faceswap` | `effects/setup-engines.sh --vace` |
| Lives in | `$ZEUS_EFFECTS_HOME/facefusion/` (+ own `.venv`, Python 3.12) | `$ZEUS_EFFECTS_HOME/models/Wan2.1-VACE-1.3B-diffusers/` (18 GB + 4.3 GB `transformer-half/`) |
| Runs in | FaceFusion's venv (subprocess) | main `.venv` (torch MPS + diffusers) |
| Input | `source.mp4` (original fps/size) | `control.mp4` + `mask.mp4` (16 fps) + reference photos |
| Overrides | `ZEUS_FACEFUSION_DIR`, `ZEUS_FACESWAP_MODEL` | `ZEUS_VACE_*` (see below) |

## faceswap — FaceFusion 3.x headless

- Checkout: `git clone --depth 1 https://github.com/facefusion/facefusion` (tested: 3.9.1, commit `72470819`),
  venv by `uv venv --seed --python 3.12`, then FaceFusion's own installer `python install.py default --skip-conda`
  (on macOS the only onnxruntime choice is `default` = `onnxruntime==1.30.0`, which ships the
  `CoreMLExecutionProvider`).
- Command (flags from `facefusion/program.py`):
  `python facefusion.py headless-run --source-paths <ref…> --target-path source.mp4 --output-path <out>
  --processors face_swapper face_enhancer --execution-providers coreml cpu --face-selector-mode one
  --temp-path <job>/.facefusion-tmp --jobs-path <job>/.facefusion-jobs --log-level info`,
  run with the FaceFusion folder as working directory. If CoreML fails it retries with `cpu` only.
- Progress: FaceFusion's tqdm bars (`downloading`, `analysing`, `processing`, `merging`) are parsed
  into `{"stage":"generate",...}` lines. Full log: `<job>/facefusion.log`.
- FaceFusion runs a built-in content check (NSFW models) on every target and refuses explicit content.

### Model licenses (from FaceFusion's own model metadata, downloaded to `facefusion/.assets/models`, ~1.7 GB)

| Model | Role | Vendor | License |
|---|---|---|---|
| hyperswap_1a_256 | face swapper (default) | FaceFusion | **ResearchRAIL** |
| arcface_w600k_r50 | face recognizer (always used) | InsightFace | **Non-Commercial** |
| gfpgan_1.4 | face enhancer | TencentARC | Apache-2.0 |
| yoloface_8n | face detector | derronqi | GPL-3.0 |
| 2dfan4 | 68-point landmarks | breadbread1984 | MIT |
| fan_68_5 | landmark helper | FaceFusion | OpenRAIL-M |
| xseg_1 | occlusion mask | DeepFaceLab | GPL-3.0 |
| bisenet_resnet_34 | face parser | yakhyo | MIT |
| fairface | face classifier | dchen236 | CC-BY-4.0 |
| kim_vocal_2 | voice extractor (pre-checked, unused here) | KimberleyJensen | **Non-Commercial** |
| nsfw_1 / nsfw_2 / nsfw_3 | content check | EraX / Marqo / Freepik | Apache-2.0 / Apache-2.0 / MIT |
| FaceFusion code | — | Henry Ruhs | OpenRAIL-AS |

Because the recognizer (InsightFace) and the default swapper are non-commercial / research
licenses, **treat face-swap output as personal / research use, not for monetised uploads.**
`ZEUS_FACESWAP_MODEL=ghost_2_256` switches the swapper to an Apache-2.0 model, but the InsightFace
recognizer is still required by FaceFusion.

## vace — Wan2.1-VACE-1.3B (diffusers, MPS)

- Weights: `Wan-AI/Wan2.1-VACE-1.3B-diffusers`, **Apache-2.0** (model card: "We claim no rights over
  your generated contents"; usage must not violate law, harm people or spread misinformation).
  Download: `uvx --from huggingface_hub hf download Wan-AI/Wan2.1-VACE-1.3B-diffusers --local-dir …`
  (resumable; `setup-engines.sh --vace` also checks every safetensors shard against its header size).
  Components on disk: UMT5-XXL text encoder (5.7 B params, **11.4 GB bf16**), WanVACE transformer
  (2.15 B params: 5.7 GB fp32 base blocks + 1.5 GB bf16 VACE blocks), Wan VAE (0.5 GB fp32), UniPC scheduler.
- `setup-engines.sh --vace` then writes **`transformer-half/`** once (4.3 GB fp16, ~10 s, streamed tensor by
  tensor; `rope`, `time_embedder`, `scale_shift_table`, `norm1-3` stay fp32 as diffusers keeps them).
  `ZEUS_VACE_HALF=bf16` writes bf16 instead. Without it the engine still runs from the original shards.
- Pipeline call (names from installed diffusers 0.41.0 `pipeline_wan_vace.py`): `video`, `mask`,
  `reference_images`, `prompt_embeds`, `negative_prompt_embeds`, `height`, `width`, `num_frames`,
  `num_inference_steps`, `guidance_scale=5.0`, `generator`, `output_type="np"`,
  `callback_on_step_end`, `callback_on_step_end_tensor_inputs=["latents"]`, `max_sequence_length=512`.
  Scheduler `UniPCMultistepScheduler(flow_shift=3.0)` (model card: 3.0 for 480P).
- Sizes: `480p` = 832x480 (480x832 portrait), `360p` = 640x368. Frames per window 4k+1
  (`--frames`); longer clips run in windows overlapping by 8 frames, each later window continues from the
  previous window's generated frames, overlaps are cross-faded. Output `result-raw.mp4` at 16 fps;
  `final.mp4` is retimed/scaled back to the source.

### Running it in 16 GB (M2 Pro, measured 2026-10-11)

What made the first runs (2026-10-10) die at 12–22 GB, and what `zfx/engines/vace.py` does now:

| Problem (measured) | Fix in vace.py |
|---|---|
| Not the text encoder: it was freed (footprint fell to 1.8 GB before the transformer loaded). It does cost 11.4 GB while it runs. | Embeddings cached on disk by text; a miss runs UMT5 **on the CPU in its own process** (`python -m zfx.engines.vace encode`): mmapped bf16 weights, **0.66 GB peak, 21 s**, same embeddings as MPS (cosine 0.9996). |
| Transformer load read + cast 7.1 GB of fp32/bf16 shards. | `transformer-half/` is mmapped (0.2 s, 0.3 GB footprint, nothing cast). |
| **The OOM itself was the VAE**: MPS runs asynchronously, Python queues hundreds of convs and every queued conv keeps its buffers + MPSGraph temporaries ("other allocations" 14–17 GiB of the 20 GiB limit) — VAE alone OOMed on 9 frames 640x368. | GPU sync after every VAE conv; 3-D convs run as exact sums of 2-D convs (MPS conv3d is ~4x slower); 256 px tiles; **VAE fp16** (same reconstruction, MAE 1.28 vs 1.33 /255). 9 frames encode+decode: 15 + 20 s, 3.8 GB. |
| PyTorch's MPS SDPA: 171 ms at 5520 tokens, **103 s** at 15600 tokens, and 4 GB of driver memory that `empty_cache()` never returns. diffusers passes `enable_gqa=False`, which also sent our first chunked version back to it. | Attention as chunked matmul → softmax → matmul (512 MB of scores per chunk): 62 ms / 0.5 s, max error 2e-4 vs fp32. |
| Every weight on the GPU (4.3 GB fp16 + temporaries, 7.4 GB footprint): next to the YouTube Zeus app (whisper + Ollama qwen3:4b = ~8.6 GB wired) the Mac swapped ~50 GB in 200 s; one 2760-token forward took 55 s. | **Blocks streamed**: each of the 45 blocks is read with one sequential `pread` (~3 GB/s) and copied to the GPU only while it runs; everything else stays mmapped. Same output (equal to 4 digits), forward 10 s at 2760 tokens, 14 s at 5520, 70–90 s at 15600; footprint 2.8–4.2 GB. |
| PyTorch's MPS allocator caches up to 1.4x / refuses at 1.7x the recommended working set (~16.6 / 20 GB). | `PYTORCH_MPS_LOW_WATERMARK_RATIO=0.5`, `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.9` (~10.7 GB hard cap) unless set. |
| **Every output was burned, posterised and blurred** — also with stock diffusers settings (bf16, original weights, native SDPA/conv3d, fp32 VAE). Cause: `UniPCMultistepScheduler.step` with latents on MPS does not converge (ideal flow model: 0.13 mean error on MPS vs 6e-5 on CPU, even at order 1). | Scheduler step on the CPU (latents ~1 MB). Text-to-video check: clean man in a grey hoodie; VACE background now pixel-sharp. |
| With the inferno-coloured depth inside the mask and the raw reference photo, the new person came out smeared with orange/blue depth colours (33 frames, 20 steps). | Inside the mask the control becomes **grey depth** (inferno's lightness rises with depth, so its luminance keeps the order; modes depth / depth_mesh; `ZEUS_VACE_CONTROL=as-is` keeps the colours) and each **reference photo is cut out onto white** with Apple Vision person segmentation, as VACE expects (`ZEUS_VACE_REF_CUTOUT=0` keeps the photo). Outside the mask the original `work.mp4` frame is used, because control.mp4 feathers the depth ~6 px past the mask and VACE kept that halo as a yellow glow. Same clip and seed: a clean, plausible person following the motion. |
| fp16 vs bf16 | fp16 matmul 5.4 vs 2.0 TFLOPS on the M2 Pro; one forward agrees with a CPU fp32 reference to 0.16 % (cosine 0.999996), largest activation 365 (fp16 limit 65504). Default fp16. |

### Measured runs (job-vace-2s: presenter clip, depth mode, reference = a frame of the same man)

All with the YouTube Zeus app open (whisper transcribing, Ollama qwen3:4b loaded: 8.5–10 GB wired before
VACE started), so times include some swapping; peak = `/usr/bin/time -l` "peak memory footprint".

| Run | Frames | Size | Steps | Control / reference | Peak footprint | Wall time (per step) | Swap during run* | Output |
|---|---|---|---|---|---|---|---|---|
| before the scheduler fix | 17 | 640x368 | 8 | colour depth / photo | 4.8 GB | 467 s (43 s) | heavy (71 GB out) | burned, blurred, grid — noise |
| a | 17 | 640x368 | 8 | colour depth / photo | **3.8 GB** | **250 s** (24 s; VAE encode+step 1: 56 s, decode 25 s) | light (3.7 GB) | background pixel-sharp; person = blue dotted silhouette (not a person) |
| b (CLI, new prompt) | 33 | 640x368 | 20 | colour depth / photo | 4.9 GB | 965 s (41 s; prompt encode 22 s on CPU) | moderate (10 GB) | a man with cap + hoodie following the raised hand, but face smeared and depth colours leaking |
| c | 33 | 640x368 | 20 | grey depth / manual cut-out | 4.9 GB | 946 s (42 s) | moderate | **clean, plausible person** (cap, beard, hoodie, talking), follows the motion |
| d (CLI, defaults) | 33 | 640x368 | 20 | grey depth / auto cut-out (Vision) | 5.0 GB | 947 s (41 s; VAE 101 + 48 s) | moderate (12 GB) | plausible person following the motion (raised hand), softer face, some smear on the hand |
| e (CLI) | 33 | 832x480 | 20 | colour depth / photo | 6.4 GB | 1937 s (87 s; VAE 192 + 90 s) | moderate-heavy (28 GB, 9–10 % free) | background sharp; person = pale faceless silhouette with yellow streaks |
| f (CLI) | 33 | 832x480 | 20 | grey depth / auto cut-out | 6.5 GB | 1939 s (86 s; VAE 192 + 91 s) | moderate-heavy (24 GB) | follows the motion, but blurry face, pink/yellow cast and a glow from the depth halo — worse than 360p |
| **g (CLI, defaults = final code)** | 33 | 640x368 | 20 | grey depth, original frame outside the mask / auto cut-out | **5.0 GB** | **944 s** (42 s; VAE 101 + 47 s) | moderate (10 GB) | **plausible person** (cap, beard, hoodie, mic, talking) following the motion; background untouched; only the fast-moving raised hand smears |

\* system-wide swap-outs while the run lasted (the app's whisper + Ollama were running too); none of the runs
came near the 10.7 GB MPS cap. For scale: the same 5-frame clip took 1150 s with stock diffusers settings
(bf16, native SDPA/conv3d, all weights on the GPU) and 160 s with these settings, visually the same output
(both before the scheduler fix).
Outputs: `Effects/Tests/engines-test/vace-half/` (sheets `*-sheet.png`, `*-frame*.png`).

### Settings for a 16 GB Mac

- **`--size 360p` (640x368), `--frames 33`, `--steps 20`** — the engine defaults now (`DEFAULT_SIZE = "360p"`;
  the CLI `--size` default is the engine's). ~16 min per 33-frame window (2 s of video) with the app busy,
  5 GB peak. A 10 s clip is 6 windows ≈ 1.5–2 h.
- 8 steps is too few (the person does not form); 17-frame windows save little time per second of video.
- **480p fits but is not worth it here**: 6.4–6.5 GB peak and ~32 min per 2 s window (a 10 s clip ≈ 3+ h),
  and on this small-in-frame presenter it looked worse than 360p.
- It needs `transformer-half/` (`setup-engines.sh --vace`) to stay at these numbers; with the app's Ollama model
  unloaded (≈ 3.8 GB more free) it swaps less and runs faster.
- Quality (run g): a believable person at 640x368 that follows the motion; fast hands smear and fine detail is
  1.3 B-model soft. The grey-depth control, the original background outside the mask and the cut-out
  reference mattered far more than size or steps (480p with the same settings was worse, run f).

Overrides: `ZEUS_VACE_DIR`, `ZEUS_VACE_DEVICE`, `ZEUS_VACE_DTYPE` (fp16/bf16/fp32), `ZEUS_VACE_VAE_DTYPE`,
`ZEUS_VACE_TEXT_DEVICE` (default cpu), `ZEUS_VACE_STREAM=0` (keep all weights on the GPU, needs ~8 GB free),
`ZEUS_VACE_ATTN_MB` (512), `ZEUS_VACE_VAE_TILE` (256), `ZEUS_VACE_SYNC_EVERY` (1), `ZEUS_VACE_HALF`
(setup: fp16/bf16), `PYTORCH_MPS_HIGH_WATERMARK_RATIO` / `PYTORCH_MPS_LOW_WATERMARK_RATIO`.

## Not used (on purpose)

No Enhancor, Seedance, Replicate or any hosted API; no vocal split / pitch shift (the original
audio is always copied back untouched).

## End-to-end check, 2026-10-11 (M2 Pro 16 GB, Zeus + Ollama running, ~1.6 GB free at start)

| Step | Result |
|---|---|
| doctor | all ok (Python 3.12.13, torch 2.11 MPS, weights, both engines) |
| prepare, YouTube, depth, 2 s | 30 s, 4.9 GB peak |
| prepare, YouTube, mesh, 3 s | 40 s, 5.3 GB (mesh mode still computes depth + mask it does not need) |
| prepare, local file, depth_mesh, 3 s | 35 s, 5.3 GB |
| generate before approve / without --consent / after a prepared file changed | all refused with a clear message |
| faceswap with face_enhancer, 3 s 1080p30 | 697 s; CoreML requested; audio byte-identical; AI comment set |
| faceswap without enhancer (now the default) | **33 s**, visually the same at normal size; `--enhance` keeps the old path |
| vace, 2 s at 360p / 33 frames / 20 steps | 1180 s, 5.0 GB peak; a plausible person, but hands do not follow the control and the last frames can glitch (colour noise) |
| `YouTube Zeus --cli effects doctor/list` | ok |

Fixed the same day:
- **Lip drift**: `mux_final` stretched the raw video to the source's video-stream duration (2.97 s) instead of the file's
  (3.02 s): final frame k drifted to source frame k+3 by the end of 3 s (~0.1 s). Now never time-stretched; faceswap output
  starts at the source's video start (33 ms on YouTube files). Measured after the fix: 0 frames off along the whole clip.
- **Soft background with vace**: the whole frame came from the 360p generation. Now the generated pixels are composited only
  inside the feathered mask over the original full-resolution frames.

Still open: vace motion fidelity (hands) and end-of-window glitches; mesh mode doing depth work it does not use; the identity
swap with a *different* person is untested (all tests used the presenter's own face).
