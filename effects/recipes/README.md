# Recipes — one-off scripts kept as utilities (2026-10-11/12)

| Script | What it does | Run |
|---|---|---|
| `imac-upscale.sh in.mp4 fast\|best out.mp4` | 1080p → 4K with Real-ESRGAN ncnn-vulkan on the iMac (fast ≈ 1.3 s/frame, best ≈ 37 s/frame); audio copied | from a folder holding the script + in.mp4: `imac run --bg "bash imac-upscale.sh in.mp4 best out.mp4"` (iMac Worker) |
| `portrait.py <effects dir> out.jpg [seed]` | a portrait of a person who does not exist (Wan2.1-VACE text-only, 5 frames, 25 steps, ~5 min, 3.6 GB) — a consent-free test face | `"$PY" -I recipes/portrait.py "$PWD" out.jpg 7` from `effects/` |
| `e2e-check.sh` | end-to-end check of the engine (3 prepare modes, the 3 refusals, face swap, VACE, app CLI) → `results.md` next to the script | `bash recipes/e2e-check.sh` (~40 min) |
| `faceswap-quality-variants.sh` | the A/B/C/D face-swap quality comparison that led to "natural" (pixel boost 512, real teeth) | needs `seg.mp4` + `refs.txt` next to it |
| `boxes.py`, `capmask.py`, `run_cap_v2.py`, `v2.sh` | the hat-removal recipe that became `zfx/engines/hat.py` (kept for reference) | see `v2.sh` |
