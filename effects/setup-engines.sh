#!/bin/bash
# Zeus Effects — optional FREE LOCAL generation engines.
#
#   setup-engines.sh --vace        verify / resume-download Wan2.1-VACE-1.3B-diffusers (Apache-2.0, ~18 GB),
#                                  then write transformer-half/ (fp16 copy, 4.3 GB, one-time) so it fits 16 GB
#   setup-engines.sh --faceswap    clone FaceFusion 3.x + its own Python 3.12 venv (onnxruntime with CoreML)
#   setup-engines.sh --upscaler    Real-ESRGAN ncnn-vulkan 0.2.5.0 (BSD-3, official macOS build, 52 MB) for the
#                                  sharp full-resolution composite of vace (falls back to lanczos without it)
#   setup-engines.sh --check       report what is installed, change nothing
#
# Idempotent: re-running only does the missing parts. No API keys, no paid services.
# Everything goes under the runtime folder (paths contain spaces; always quoted):
#   ${ZEUS_EFFECTS_HOME:-~/Library/Application Support/YouTube Zeus/Effects}/
#     models/Wan2.1-VACE-1.3B-diffusers/    (override: ZEUS_VACE_DIR)
#       transformer-half/   (fp16 copy made here from the download; ZEUS_VACE_HALF=bf16 for bf16)
#     facefusion/  + facefusion/.venv/       (override: ZEUS_FACEFUSION_DIR)
#     tools/realesrgan/  realesrgan-ncnn-vulkan + models/  (override: ZEUS_REALESRGAN_DIR)
set -euo pipefail

RUNTIME_HOME="${ZEUS_EFFECTS_HOME:-$HOME/Library/Application Support/YouTube Zeus/Effects}"
VACE_REPO="Wan-AI/Wan2.1-VACE-1.3B-diffusers"
VACE_DIR="${ZEUS_VACE_DIR:-$RUNTIME_HOME/models/Wan2.1-VACE-1.3B-diffusers}"
FF_DIR="${ZEUS_FACEFUSION_DIR:-$RUNTIME_HOME/facefusion}"
FF_URL="https://github.com/facefusion/facefusion"
FF_PY="$FF_DIR/.venv/bin/python"
PY="$RUNTIME_HOME/.venv/bin/python"
ESR_DIR="${ZEUS_REALESRGAN_DIR:-$RUNTIME_HOME/tools/realesrgan}"
ESR_URL="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip"
ESR_SIZE=51817124   # bytes, from the GitHub release API (2026-10-11)
ESR_SHA256="e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a"   # measured 2026-10-11 (universal x86_64+arm64)
EFFECTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

log() { printf '[setup-engines] %s\n' "$*" >&2; }
die() { printf '[setup-engines] ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found. $2"; }

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ---------------------------------------------------------------- VACE
vace_complete() {
  # 0 = complete. Checks model_index.json and that every safetensors shard listed in the
  # index files exists with the exact size its header announces (catches truncated files).
  [ -f "$VACE_DIR/model_index.json" ] || return 1
  local py
  py="$(command -v python3)"
  "$py" - "$VACE_DIR" <<'PY'
import json, os, struct, sys
root = sys.argv[1]
def ok(path):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            header = json.loads(fh.read(n))
        end = max(v["data_offsets"][1] for k, v in header.items() if k != "__metadata__")
        return size == 8 + n + end
    except Exception:
        return False
need = ["vae/diffusion_pytorch_model.safetensors"]
for sub in ("transformer", "text_encoder"):
    idx = [f for f in os.listdir(os.path.join(root, sub)) if f.endswith(".index.json")] if os.path.isdir(os.path.join(root, sub)) else []
    if not idx:
        sys.exit(1)
    shards = set(json.load(open(os.path.join(root, sub, idx[0])))["weight_map"].values())
    need += [f"{sub}/{s}" for s in shards]
for rel in ["tokenizer/tokenizer.json", "scheduler/scheduler_config.json", "transformer/config.json",
            "text_encoder/config.json", "vae/config.json"]:
    if not os.path.isfile(os.path.join(root, rel)):
        sys.exit(1)
bad = [r for r in need if not ok(os.path.join(root, r))]
sys.exit(1 if bad else 0)
PY
}

vace_half_ready() {
  # 0 = transformer-half/ complete (zeus-half.json + the safetensors file as long as its header says)
  [ -x "$PY" ] || return 1
  ( cd "$EFFECTS_DIR" && ZEUS_VACE_DIR="$VACE_DIR" "$PY" -c '
import sys
from zfx.engines import vace
sys.exit(0 if vace.half_ready("transformer") else 1)' ) >/dev/null 2>&1
}

vace_half() {
  # transformer-half/ (fp16 by default, 4.3 GB), streamed tensor by tensor from the downloaded fp32/bf16
  # shards (a few hundred MB of RAM). Loading it instead of the 7.1 GB of shards keeps the load peak low
  # on a 16 GB Mac (see effects/ENGINES.md).
  [ -x "$PY" ] || die "Runtime Python missing ($PY). Run effects/setup.sh first."
  if vace_half_ready && [ "${FORCE:-0}" != "1" ]; then
    log "VACE transformer-half/ present."
    return 0
  fi
  log "Writing VACE transformer-half/ (one-time, 4.3 GB on disk, under a minute)…"
  ( cd "$EFFECTS_DIR" && ZEUS_VACE_DIR="$VACE_DIR" "$PY" -m zfx.engines.vace half --model "$VACE_DIR" ) \
    || die "Could not write transformer-half/ in $VACE_DIR."
  log "VACE transformer-half/ ready."
}

setup_vace() {
  if vace_complete; then
    log "VACE weights complete: $VACE_DIR"
    vace_half
    return 0
  fi
  need uvx "Install uv: https://docs.astral.sh/uv/ (brew install uv)."
  mkdir -p "$VACE_DIR"
  log "Downloading $VACE_REPO into $VACE_DIR (resumable, ~18 GB, Apache-2.0)…"
  # hf download resumes partial files kept in <local-dir>/.cache/huggingface/download
  uvx --from huggingface_hub hf download "$VACE_REPO" --local-dir "$VACE_DIR"
  vace_complete || die "VACE download finished but files are incomplete. Run setup-engines.sh --vace again."
  log "VACE weights ready."
  vace_half
}

# ---------------------------------------------------------------- Real-ESRGAN (upscaler)
esr_ready() {
  [ -x "$ESR_DIR/realesrgan-ncnn-vulkan" ] && [ -f "$ESR_DIR/models/realesr-animevideov3-x2.param" ] \
    && [ -f "$ESR_DIR/models/realesrgan-x4plus.param" ]
}

esr_selftest() {
  # 0 = the binary runs on this Mac and upscales a tiny frame (x2, realesr-animevideov3, Vulkan via MoltenVK)
  local t
  t="$(mktemp -d)"
  ffmpeg -v error -f lavfi -i "testsrc2=size=64x48" -frames:v 1 "$t/in.png" || { rm -rf "$t"; return 1; }
  if "$ESR_DIR/realesrgan-ncnn-vulkan" -i "$t/in.png" -o "$t/out.png" -n realesr-animevideov3 -s 2 \
       -m "$ESR_DIR/models" -f png >"$t/log" 2>&1 && [ -s "$t/out.png" ]; then
    rm -rf "$t"; return 0
  fi
  log "Real-ESRGAN self-test failed: $(tail -3 "$t/log" | tr '\n' ' ')"
  rm -rf "$t"; return 1
}

setup_upscaler() {
  need curl "curl is part of macOS."
  need ffmpeg "Install ffmpeg: brew install ffmpeg"
  if ! esr_ready || [ "${FORCE:-0}" = "1" ]; then
    local dl="$ESR_DIR.download"
    rm -rf "$dl"; mkdir -p "$dl"
    log "Downloading Real-ESRGAN ncnn-vulkan (official macOS build, BSD-3, 52 MB) from github.com/xinntao…"
    curl -fL --retry 3 -o "$dl/realesrgan.zip" "$ESR_URL" || die "Real-ESRGAN download failed."
    local size
    size="$(stat -f %z "$dl/realesrgan.zip")"
    [ "$size" = "$ESR_SIZE" ] || die "Real-ESRGAN zip has $size bytes, expected $ESR_SIZE."
    local digest
    digest="$(shasum -a 256 "$dl/realesrgan.zip" | cut -d' ' -f1)"
    [ "$digest" = "$ESR_SHA256" ] || die "Real-ESRGAN zip sha256 $digest, expected $ESR_SHA256."
    /usr/bin/ditto -x -k "$dl/realesrgan.zip" "$dl/x" || die "Could not unzip Real-ESRGAN."
    local bin models
    bin="$(find "$dl/x" -type f -name realesrgan-ncnn-vulkan | head -1)"
    models="$(find "$dl/x" -type d -name models | head -1)"
    [ -n "$bin" ] && [ -n "$models" ] || die "Unexpected Real-ESRGAN zip layout (no binary or models/)."
    rm -rf "$ESR_DIR"; mkdir -p "$ESR_DIR"
    mv "$bin" "$ESR_DIR/realesrgan-ncnn-vulkan"
    mv "$models" "$ESR_DIR/models"
    chmod +x "$ESR_DIR/realesrgan-ncnn-vulkan"
    find "$(dirname "$bin")" -maxdepth 1 -iname 'README*' -exec cp {} "$ESR_DIR/" \; 2>/dev/null || true
    rm -rf "$dl"
  fi
  esr_ready || die "Real-ESRGAN files incomplete in $ESR_DIR."
  log "Real-ESRGAN binary: $(lipo -archs "$ESR_DIR/realesrgan-ncnn-vulkan" 2>/dev/null || file -b "$ESR_DIR/realesrgan-ncnn-vulkan")"
  if esr_selftest; then
    log "Real-ESRGAN ready: $ESR_DIR"
  else
    log "WARNING: Real-ESRGAN does not run on this Mac; vace composites fall back to lanczos upscaling."
  fi
}

# ---------------------------------------------------------------- FaceFusion
ff_ready() {
  [ -x "$FF_PY" ] && [ -f "$FF_DIR/facefusion.py" ] && \
    "$FF_PY" -c 'import onnxruntime, cv2, numpy, scipy, tqdm' >/dev/null 2>&1
}

setup_faceswap() {
  need git "Install the Xcode command line tools: xcode-select --install"
  need uv "Install uv: https://docs.astral.sh/uv/ (brew install uv)."
  need ffmpeg "Install ffmpeg: brew install ffmpeg"
  mkdir -p "$RUNTIME_HOME"
  if [ ! -f "$FF_DIR/facefusion.py" ]; then
    log "Cloning FaceFusion into $FF_DIR…"
    git clone --depth 1 "$FF_URL" "$FF_DIR"
  else
    log "FaceFusion checkout present: $FF_DIR ($(git -C "$FF_DIR" log -1 --format=%h 2>/dev/null || echo '?'))"
  fi
  if [ ! -x "$FF_PY" ]; then
    log "Creating FaceFusion venv (Python 3.12 via uv)…"
    # --seed installs pip: FaceFusion's install.py shells out to `pip`.
    uv venv --seed --python 3.12 "$FF_DIR/.venv"
  fi
  if ff_ready && [ "${FORCE:-0}" != "1" ]; then
    log "FaceFusion dependencies already installed."
  else
    log "Running FaceFusion installer (onnxruntime 'default' = the macOS wheel with CoreML)…"
    # install.py: `python install.py <onnxruntime> --skip-conda`; on macOS the only choice is 'default'
    # (plain `onnxruntime`, which ships the CoreMLExecutionProvider on Apple Silicon).
    ( cd "$FF_DIR" && \
      VIRTUAL_ENV="$FF_DIR/.venv" PATH="$FF_DIR/.venv/bin:$PATH" \
      "$FF_PY" install.py default --skip-conda )
  fi
  ff_ready || die "FaceFusion install incomplete (imports fail in $FF_DIR/.venv)."
  local providers
  providers="$("$FF_PY" -c 'import onnxruntime; print(",".join(onnxruntime.get_available_providers()))')"
  log "onnxruntime providers: $providers"
  case "$providers" in
    *CoreMLExecutionProvider*) ;;
    *) log "WARNING: CoreML provider missing; FaceFusion will run on CPU (slower)." ;;
  esac
  log "FaceFusion ready. Its models (inswapper/hyperswap, GFPGAN, YOLO face, …) download on first run"
  log "into $FF_DIR/.assets/models (see effects/ENGINES.md for their licenses)."
}

check() {
  if vace_complete; then
    if vace_half_ready; then echo "vace: ready ($VACE_DIR, transformer-half/ present)"
    else echo "vace: weights ready, transformer-half/ missing -> setup-engines.sh --vace"; fi
  else echo "vace: missing or incomplete ($VACE_DIR) -> setup-engines.sh --vace"; fi
  if ff_ready; then echo "faceswap: ready ($FF_DIR)"; else echo "faceswap: not installed ($FF_DIR) -> setup-engines.sh --faceswap"; fi
  if esr_ready; then echo "upscaler: Real-ESRGAN ($ESR_DIR)"; else echo "upscaler: lanczos only -> setup-engines.sh --upscaler"; fi
}

[ $# -eq 0 ] && usage 1
DO_VACE=0; DO_FF=0; DO_ESR=0; DO_CHECK=0
for a in "$@"; do
  case "$a" in
    --vace) DO_VACE=1 ;;
    --faceswap) DO_FF=1 ;;
    --upscaler) DO_ESR=1 ;;
    --check) DO_CHECK=1 ;;
    --force) export FORCE=1 ;;
    -h|--help) usage 0 ;;
    *) log "unknown option: $a"; usage 1 ;;
  esac
done
[ "$DO_FF" = 1 ] && setup_faceswap
[ "$DO_VACE" = 1 ] && setup_vace
[ "$DO_ESR" = 1 ] && setup_upscaler
[ "$DO_CHECK" = 1 ] && check
exit 0
