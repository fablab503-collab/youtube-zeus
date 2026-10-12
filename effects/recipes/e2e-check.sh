#!/bin/bash
# Zeus Effects end-to-end check, 2026-10-11. Writes results.md next to this script.
S="$(cd "$(dirname "$0")" && pwd)"
Z="/Volumes/4TB Tahoe/Users/danielmadac/Developer/YouTubeZeus"
H="$HOME/Library/Application Support/YouTube Zeus/Effects"
PY="$H/.venv/bin/python"; E="$Z/effects/zeus_effects.py"
URL="https://www.youtube.com/watch?v=d6spCMcfkew"
LOCAL="$H/Jobs/20261010-222549-d6spCMcfkew-6191/source.mp4"
R="$S/results.md"; echo "# Zeus Effects end-to-end $(date '+%F %H:%M')" > "$R"
cd "$Z/effects"
step() { # name, expect(ok|fail), command...
  local name="$1" expect="$2"; shift 2
  local t0=$(date +%s); "$@" > "$S/$name.jsonl" 2> "$S/$name.err"; local code=$?; local t=$(( $(date +%s) - t0 ))
  local last=$(tail -1 "$S/$name.jsonl" | cut -c1-300)
  local verdict="PASS"; { [ "$expect" = ok ] && [ $code -ne 0 ]; } && verdict="FAIL"; { [ "$expect" = fail ] && [ $code -eq 0 ]; } && verdict="FAIL"
  local peak=$(grep "peak memory footprint" "$S/$name.err" | awk '{printf "%.1f GB", $1/1e9}')
  echo "- **$name** $verdict (exit $code, ${t}s, peak $peak): \`$last\`" >> "$R"
}
job_of() { /opt/homebrew/bin/python3 -c "import json,sys; print(json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]).get('job',''))" "$S/$1.jsonl" 2>/dev/null; }
T="/usr/bin/time -l"
step prep_youtube_depth ok $T "$PY" "$E" prepare --input "$URL" --start 605 --duration 2 --mode depth
step prep_youtube_mesh ok $T "$PY" "$E" prepare --input "$URL" --start 600 --duration 3 --mode mesh
step prep_local_depthmesh ok $T "$PY" "$E" prepare --input "$LOCAL" --duration 3 --mode depth_mesh
J1=$(job_of prep_youtube_depth); J2=$(job_of prep_youtube_mesh); J3=$(job_of prep_local_depthmesh)
echo "- jobs: depth=$J1 · mesh=$J2 · depth_mesh=$J3" >> "$R"
ffmpeg -loglevel error -y -ss 1.5 -i "$LOCAL" -frames:v 1 "$S/ref.jpg"
step generate_before_approve fail "$PY" "$E" generate --job "$J2" --engine faceswap --ref "$S/ref.jpg" --consent
for j in "$J1" "$J2" "$J3"; do "$PY" "$E" approve --job "$j" > /dev/null 2>&1; done
step generate_without_consent fail "$PY" "$E" generate --job "$J2" --engine faceswap --ref "$S/ref.jpg"
cp "$J3/control.mp4" "$S/control.bak"; printf 'x' >> "$J3/control.mp4"
step generate_after_tamper fail "$PY" "$E" generate --job "$J3" --engine faceswap --ref "$S/ref.jpg" --consent
cp "$S/control.bak" "$J3/control.mp4"
step faceswap_mesh_job ok $T "$PY" "$E" generate --job "$J2" --engine faceswap --ref "$S/ref.jpg" --consent
grep -ioE "coreml|CPUExecutionProvider|cpu only|retry" "$J2/facefusion.log" 2>/dev/null | sort | uniq -c | sed 's/^/  - facefusion: /' >> "$R"
for j in "$J2"; do
  a1=$(ffmpeg -loglevel error -i "$j/audio.m4a" -map 0:a -c copy -f data - | md5); a2=$(ffmpeg -loglevel error -i "$j/final.mp4" -map 0:a -c copy -f data - | md5)
  [ "$a1" = "$a2" ] && echo "  - original audio in final.mp4: identical bytes" >> "$R" || echo "  - original audio in final.mp4: DIFFERENT ($a1 vs $a2)" >> "$R"
  echo "  - metadata: $(ffprobe -v error -show_entries format_tags=comment -of csv=p=0 "$j/final.mp4")" >> "$R"
  echo "  - final: $(ffprobe -v error -show_entries stream=codec_type,width,height,r_frame_rate -of csv=p=0 "$j/final.mp4" | tr '\n' ' ')" >> "$R"
done
step vace_depth_job ok $T "$PY" "$E" generate --job "$J1" --engine vace --ref "$S/ref.jpg" --consent
[ -f "$J1/final.mp4" ] && echo "  - vace final: $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$J1/final.mp4") s" >> "$R"
APP="$Z/build/DerivedData/Build/Products/Release/YouTube Zeus.app/Contents/MacOS/YouTube Zeus"
step app_cli_doctor ok "$APP" --cli effects doctor --json
step app_cli_list ok "$APP" --cli effects list --json
echo "- done $(date '+%H:%M')" >> "$R"
