#!/bin/bash
set -u
B=/private/tmp/claude-501/-Users-danielmadac-Documents/0e6e1e71-09ce-4138-a32a-da0d50512b35/scratchpad
S=$B/cap; H="$HOME/Library/Application Support/YouTube Zeus/Effects"; E="/Volumes/4TB Tahoe/Users/danielmadac/Developer/YouTubeZeus/effects"
J="$H/Jobs/20261011-150754-d6spCMcfkew-500d"; C="$H/Jobs/20261011-cap-removal-daniel-v2"; L=$S/v2.log; : > $L
echo "$(date +%T) 1 natural face swap" >> $L
cp "$J/final.mp4" "$J/final-before-natural.mp4"
cd "$E" && "$H/.venv/bin/python" zeus_effects.py generate --job "$J" --engine faceswap --ref-pack "$B/facepack-test/Daniel.c101face" --quality natural --consent > $S/v2-swap.jsonl 2>&1; echo "$(date +%T) swap exit $?" >> $L
echo "$(date +%T) 2 cap mask" >> $L
mkdir -p "$C"; cp "$J/final.mp4" "$C/source.mp4"; cp "$J/audio.m4a" "$C/audio.m4a"
ffmpeg -loglevel error -y -i "$C/source.mp4" -vf "fps=16,scale=832:-2" -an -c:v libx264 -crf 12 -pix_fmt yuv420p "$C/work.mp4"
"$H/.venv/bin/python" $S/boxes.py "$C/work.mp4" "$H/models/face_landmarker.task" $S/boxes-v2.json >> $L 2>&1
"$H/facefusion/.venv/bin/python" $S/capmask.py "$C/work.mp4" $S/m2.mp4 $S/c2.mp4 "$H/facefusion/.assets/models/bisenet_resnet_34.onnx" $S/boxes-v2.json >> $L 2>&1
ffmpeg -loglevel error -y -i $S/m2.mp4 -c:v libx264 -crf 12 -pix_fmt yuv420p "$C/mask.mp4"; ffmpeg -loglevel error -y -i $S/c2.mp4 -c:v libx264 -crf 12 -pix_fmt yuv420p "$C/control.mp4"
cp "$H/Jobs/20261011-cap-removal-daniel/job.json" "$C/job.json"; sed -i '' 's#20261011-cap-removal-daniel#20261011-cap-removal-daniel-v2#g' "$C/job.json"
echo "$(date +%T) 3 cap removal" >> $L
cd "$E" && "$H/.venv/bin/python" $S/run_cap_v2.py >> $L 2>&1; echo "$(date +%T) cap exit $?" >> $L
echo "$(date +%T) 4 voice" >> $L
ffmpeg -loglevel error -y -i "$C/final.mp4" -i "$J/audio-voice.m4a" -map 0:v -map 1:a -c copy -metadata comment="AI-generated with YouTube Zeus Effects (faceswap natural, vace hair); AI-generated voice (chatterbox)" -movflags +faststart "$C/final-voice.mp4"
echo "$(date +%T) done" >> $L
