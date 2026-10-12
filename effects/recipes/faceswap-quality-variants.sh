#!/bin/bash
S="$(cd "$(dirname "$0")" && pwd)"; FF="$HOME/Library/Application Support/YouTube Zeus/Effects/facefusion"; cd "$FF"
REFS=($(cat "$S/refs.txt"))
run() { local name="$1"; shift; local t=$(date +%s)
  .venv/bin/python facefusion.py headless-run --source-paths "${REFS[@]}" --target-path "$S/seg.mp4" --output-path "$S/$name.mp4" \
    --execution-providers coreml cpu --face-selector-mode one --temp-path "$S/tmp-$name" --jobs-path "$S/jobs-$name" --log-level error "$@" > "$S/$name.log" 2>&1
  echo "$name exit $? in $(( $(date +%s)-t ))s" >> "$S/times.txt"; rm -rf "$S/tmp-$name" "$S/jobs-$name"; }
run B --processors face_swapper --face-swapper-pixel-boost 512x512 --face-mask-types box occlusion
run C --processors face_swapper --face-swapper-pixel-boost 512x512 --face-mask-types box occlusion region --face-mask-regions skin left-eyebrow right-eyebrow left-eye right-eye nose upper-lip lower-lip
run D --processors face_swapper --face-swapper-pixel-boost 512x512 --face-mask-types box occlusion region --face-mask-regions skin left-eyebrow right-eyebrow nose upper-lip lower-lip
echo done >> "$S/times.txt"
