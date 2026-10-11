#!/usr/bin/env bash
# Zeus Effects: set up the free, local-only runtime on this Mac.
#
#   bash effects/setup.sh             main engine: Python 3.12 venv (uv), deps, small weights
#   bash effects/setup.sh --vace      (Wan2.1-VACE engine)   handled by engines/setup
#   bash effects/setup.sh --faceswap  (FaceFusion engine)    handled by engines/setup
#
# Runtime folder: $ZEUS_EFFECTS_HOME, default "~/Library/Application Support/YouTube Zeus/Effects".
# Idempotent: run it again any time, finished steps are skipped. Never asks for API keys.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${ZEUS_EFFECTS_HOME:-"$HOME/Library/Application Support/YouTube Zeus/Effects"}"
VENV="$HOME_DIR/.venv"
PY="$VENV/bin/python"
MODELS="$HOME_DIR/models"
VENDOR="$HOME_DIR/vendor"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

VDA_REPO_URL="https://github.com/DepthAnything/Video-Depth-Anything"
VDA_DIR="$VENDOR/Video-Depth-Anything"
VDA_WEIGHTS_DIR="$MODELS/Video-Depth-Anything-Small"
VDA_WEIGHTS="$VDA_WEIGHTS_DIR/video_depth_anything_vits.pth"
VDA_WEIGHTS_URL="https://huggingface.co/depth-anything/Video-Depth-Anything-Small/resolve/main/video_depth_anything_vits.pth"
FACE_TASK="$MODELS/face_landmarker.task"
FACE_TASK_URL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
# Whole-body motion capture (pose modes): official MediaPipe models (Apache-2.0) from Google's model storage
POSE_TASK="$MODELS/pose_landmarker_heavy.task"
POSE_TASK_URL="https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task"
HAND_TASK="$MODELS/hand_landmarker.task"
HAND_TASK_URL="https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task"

say() { printf '[zeus-effects setup] %s\n' "$*"; }
die() { printf '[zeus-effects setup] ERROR: %s\n' "$*" >&2; exit 1; }

WANT_MAIN=1
for arg in "$@"; do
    case "$arg" in
        --vace|--faceswap)
            say "$arg: handled by engines/setup"
            if [ -f "$SCRIPT_DIR/setup-engines.sh" ]; then
                say "  run: bash \"$SCRIPT_DIR/setup-engines.sh\" $arg"
            fi
            WANT_MAIN=0
            ;;
        --main) WANT_MAIN=2 ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown option: $arg (use --help)" ;;
    esac
done
# --vace/--faceswap alone only print the hint; add --main to also (re)check the main engine.
if [ "$WANT_MAIN" -eq 0 ]; then exit 0; fi

# Download $1 -> $2 with resume, atomically (via .part), only when missing or too small ($3 bytes).
fetch() {
    local url="$1" dest="$2" min_bytes="$3" size=0
    if [ -f "$dest" ]; then size=$(stat -f%z "$dest" 2>/dev/null || echo 0); fi
    if [ "$size" -ge "$min_bytes" ]; then
        say "ok: $(basename "$dest") already present ($size bytes)"
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    say "downloading $(basename "$dest") from $url"
    curl -L --fail --retry 3 --retry-delay 2 -C - -o "$dest.part" "$url" \
        || curl -L --fail --retry 3 --retry-delay 2 -o "$dest.part" "$url" \
        || die "download failed: $url"
    size=$(stat -f%z "$dest.part" 2>/dev/null || echo 0)
    [ "$size" -ge "$min_bytes" ] || die "downloaded file too small ($size bytes): $dest.part"
    mv -f "$dest.part" "$dest"
    say "ok: $(basename "$dest") ($size bytes)"
}

# 1. Tools
UV="$(command -v uv || true)"
[ -n "$UV" ] || die "uv is not installed. Install it with: brew install uv"
for tool in ffmpeg ffprobe yt-dlp git curl; do
    command -v "$tool" >/dev/null 2>&1 || say "warning: $tool not found (brew install ${tool/ffprobe/ffmpeg})"
done

mkdir -p "$HOME_DIR" "$MODELS" "$VENDOR" "$HOME_DIR/Jobs"
say "runtime folder: $HOME_DIR"

# 2. Python 3.12 venv + pinned deps
if [ ! -x "$PY" ]; then
    say "creating venv (Python 3.12) with uv"
    "$UV" venv --python 3.12 "$VENV"
else
    say "ok: venv exists ($("$PY" --version 2>&1))"
fi
say "installing requirements (uv skips what is already installed)"
"$UV" pip install --python "$PY" -r "$SCRIPT_DIR/requirements.txt"

# 3. Video-Depth-Anything code (Apache-2.0), vendored outside the repo
if [ -d "$VDA_DIR/.git" ] && [ -f "$VDA_DIR/video_depth_anything/video_depth.py" ]; then
    say "ok: Video-Depth-Anything code present ($(git -C "$VDA_DIR" rev-parse --short HEAD 2>/dev/null || echo '?'))"
else
    rm -rf "$VDA_DIR"
    say "cloning Video-Depth-Anything"
    git clone --depth 1 "$VDA_REPO_URL" "$VDA_DIR"
fi

# 4. Small weights
fetch "$VDA_WEIGHTS_URL" "$VDA_WEIGHTS" 50000000      # ~110 MB, Video-Depth-Anything-Small (vits)
fetch "$FACE_TASK_URL" "$FACE_TASK" 1000000           # ~3.6 MB, MediaPipe Face Landmarker
fetch "$POSE_TASK_URL" "$POSE_TASK" 10000000          # ~30 MB, MediaPipe Pose Landmarker (heavy)
fetch "$HAND_TASK_URL" "$HAND_TASK" 2000000           # ~7.8 MB, MediaPipe Hand Landmarker

# 5. Report
say "checking the install"
"$PY" "$SCRIPT_DIR/zeus_effects.py" doctor || true
say "done. Engines: bash \"$SCRIPT_DIR/setup.sh\" --vace | --faceswap (handled by engines/setup)"
