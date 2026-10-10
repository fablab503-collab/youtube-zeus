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
| Lives in | `$ZEUS_EFFECTS_HOME/facefusion/` (+ own `.venv`, Python 3.12) | `$ZEUS_EFFECTS_HOME/models/Wan2.1-VACE-1.3B-diffusers/` (18 GB) |
| Runs in | FaceFusion's venv (subprocess) | main `.venv` (torch MPS + diffusers) |
| Input | `source.mp4` (original fps/size) | `control.mp4` + `mask.mp4` (16 fps) + reference photos |
| Overrides | `ZEUS_FACEFUSION_DIR`, `ZEUS_FACESWAP_MODEL` | `ZEUS_VACE_DIR`, `ZEUS_VACE_DEVICE`, `ZEUS_VACE_DTYPE`, `ZEUS_VACE_TEXT_DEVICE`, `ZEUS_VACE_ATTN_MB` |

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
  Components: UMT5-XXL text encoder (11.4 GB fp32 on disk), WanVACE transformer (7.1 GB fp32),
  Wan VAE (0.5 GB), UniPC scheduler.
- Memory plan (16 GB): text encoder alone in bf16 → prompt + negative embeddings → freed;
  then transformer (bf16) + VAE (fp32, tiling + slicing) with `prompt_embeds` /
  `negative_prompt_embeds`; attention computed in query chunks above `ZEUS_VACE_ATTN_MB` (1024).
- Pipeline call (names from installed diffusers 0.41.0 `pipeline_wan_vace.py`): `video`, `mask`,
  `reference_images`, `prompt_embeds`, `negative_prompt_embeds`, `height`, `width`, `num_frames`,
  `num_inference_steps`, `guidance_scale=5.0`, `generator`, `output_type="np"`,
  `callback_on_step_end`, `callback_on_step_end_tensor_inputs=["latents"]`, `max_sequence_length=512`.
  Scheduler `UniPCMultistepScheduler(flow_shift=3.0)` (model card: 3.0 for 480P).
- Sizes: `480p` = 832x480 (480x832 portrait), `360p` = 640x368. Frames per window 4k+1
  (`--frames`, default 33); longer clips run in windows overlapping by 8 frames, each later window
  continues from the previous window's generated frames, overlaps are cross-faded. Steps default 20.
- Output `result-raw.mp4` at 16 fps; `final.mp4` is retimed/scaled back to the source.

## Not used (on purpose)

No Enhancor, Seedance, Replicate or any hosted API; no vocal split / pitch shift (the original
audio is always copied back untouched).
