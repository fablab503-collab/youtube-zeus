#!/bin/bash
# Zeus Effects — the voice step (zeus_effects.py voice): FREE, LOCAL speech-to-speech voice conversion.
#
#   setup-voice.sh            create/repair the voice runtime and download the weights (idempotent)
#   setup-voice.sh --check    report what is installed, change nothing
#
# What it installs (all MIT, no API keys, no paid service, nothing runs in the cloud):
#   voice/.venv/                     Python 3.12 (uv): chatterbox-tts 0.1.7 (Resemble AI) + demucs 4.1.0 (Meta)
#                                    with torch 2.6.0 / torchaudio 2.6.0 as chatterbox-tts pins them (~1 GB)
#   models/chatterbox-vc/s3gen.safetensors   ResembleAI/chatterbox (official HF repo, MIT), 1.06 GB:
#                                    S3 tokenizer + CAMPPlus speaker encoder + S3Gen flow/vocoder (the VC model)
#   models/htdemucs/                 adefossez/HTDemucs (official HF repo of Demucs' author, MIT), 84 MB
# under the runtime folder ${ZEUS_EFFECTS_HOME:-~/Library/Application Support/YouTube Zeus/Effects}/
# (paths contain spaces; always quoted). Overrides: ZEUS_VOICE_PYTHON, ZEUS_CHATTERBOX_DIR, ZEUS_DEMUCS_DIR.
# Uninstall: delete voice/, models/chatterbox-vc/ and models/htdemucs/ there.
set -euo pipefail

RUNTIME_HOME="${ZEUS_EFFECTS_HOME:-$HOME/Library/Application Support/YouTube Zeus/Effects}"
VOICE_DIR="$RUNTIME_HOME/voice"
VENV="$VOICE_DIR/.venv"
PY="${ZEUS_VOICE_PYTHON:-$VENV/bin/python}"
CB_DIR="${ZEUS_CHATTERBOX_DIR:-$RUNTIME_HOME/models/chatterbox-vc}"
DM_DIR="${ZEUS_DEMUCS_DIR:-$RUNTIME_HOME/models/htdemucs}"
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

# Pinned 2026-10-11. setuptools < 81: resemble-perth (Chatterbox's watermark) still imports pkg_resources.
PACKAGES=("chatterbox-tts==0.1.7" "demucs==4.1.0" "setuptools==80.10.2")
CB_REPO="ResembleAI/chatterbox"
CB_REV="5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18"
CB_FILE="s3gen.safetensors"
CB_SIZE=1056484620
CB_SHA256="2b78103c654207393955e4900aac14a12de8ef25f4b09424f1ef91941f161d4e"   # = the HF LFS oid
DM_REPO="adefossez/HTDemucs"
DM_REV="cbc8a9b1a87023b7fd74e7b3412e6321c0eab003"
DM_FILE="955717e8.safetensors"
DM_SIZE=84025440
DM_SHA256="d9fa14133cfcc034a6758923bb3a8ca9f8dfd0b582134643bbf83f72c17576dd"

say() { printf '[zeus-voice setup] %s\n' "$*"; }
die() { printf '[zeus-voice setup] ERROR: %s\n' "$*" >&2; exit 1; }
size_of() { stat -f%z "$1" 2>/dev/null || echo 0; }

CHECK=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $arg (use --help)" ;;
    esac
done

report() {
    local ok=1
    if [ -x "$PY" ] && "$PY" -W ignore -c "import chatterbox.vc, demucs.apply, perth" >/dev/null 2>&1; then
        say "ok: voice venv $VENV ($(du -sh "$VENV" 2>/dev/null | cut -f1))"
    else
        say "missing: voice venv $VENV"; ok=0
    fi
    if [ "$(size_of "$CB_DIR/$CB_FILE")" = "$CB_SIZE" ]; then
        say "ok: Chatterbox VC $CB_DIR/$CB_FILE ($CB_SIZE bytes)"
    else
        say "missing: Chatterbox VC weights $CB_DIR/$CB_FILE"; ok=0
    fi
    if [ "$(size_of "$DM_DIR/$DM_FILE")" = "$DM_SIZE" ] && [ -f "$DM_DIR/htdemucs.yaml" ]; then
        say "ok: Demucs htdemucs $DM_DIR ($DM_SIZE bytes)"
    else
        say "missing: Demucs weights $DM_DIR"; ok=0
    fi
    [ "$ok" = 1 ] && say "voice step ready" || say "voice step NOT ready: run bash \"$0\""
    return 0
}

if [ "$CHECK" = 1 ]; then report; exit 0; fi

command -v uv >/dev/null 2>&1 || die "uv is not installed (brew install uv)"
command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is not installed (brew install ffmpeg)"
mkdir -p "$VOICE_DIR" "$CB_DIR" "$DM_DIR"

# 1. venv + packages
if [ ! -x "$VENV/bin/python" ]; then
    say "creating $VENV (Python 3.12)"
    uv venv --python 3.12 "$VENV"
fi
if ! "$VENV/bin/python" -W ignore -c "import chatterbox.vc, demucs.apply, perth; import importlib.metadata as m; \
assert m.version('chatterbox-tts') == '0.1.7' and m.version('demucs') == '4.1.0'" >/dev/null 2>&1; then
    say "installing ${PACKAGES[*]}"
    uv pip install --python "$VENV/bin/python" "${PACKAGES[@]}"
fi
"$VENV/bin/python" -W ignore -c "import chatterbox.vc, demucs.apply, perth" || die "the voice venv does not import"

# 2. weights (official Hugging Face repos, pinned revisions, resumable), checked by size and SHA-256
fetch() {   # repo rev dir file size sha256 [extra files...]
    local repo="$1" rev="$2" dir="$3" file="$4" size="$5" sha="$6"; shift 6
    if [ "$(size_of "$dir/$file")" != "$size" ]; then
        say "downloading $repo $file (+ $*) into $dir"
        "$VENV/bin/hf" download "$repo" "$file" "$@" --revision "$rev" --local-dir "$dir" >/dev/null
    fi
    [ "$(size_of "$dir/$file")" = "$size" ] || die "$dir/$file has the wrong size"
    if [ ! -f "$dir/.$file.sha256-ok" ]; then
        local got; got="$(shasum -a 256 "$dir/$file" | cut -d' ' -f1)"
        [ "$got" = "$sha" ] || die "$dir/$file SHA-256 $got != $sha"
        touch "$dir/.$file.sha256-ok"
    fi
    say "ok: $dir/$file"
}
fetch "$CB_REPO" "$CB_REV" "$CB_DIR" "$CB_FILE" "$CB_SIZE" "$CB_SHA256"
fetch "$DM_REPO" "$DM_REV" "$DM_DIR" "$DM_FILE" "$DM_SIZE" "$DM_SHA256" htdemucs.yaml 955717e8.json

report
