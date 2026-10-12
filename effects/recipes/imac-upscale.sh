#!/bin/bash
# Upscale a clip to 4K (3840 wide, height kept proportional) on the iMac with Real-ESRGAN (ncnn-vulkan, Radeon Pro 580).
# usage: upscale.sh <in.mp4> <best|fast> <out.mp4>     audio is copied untouched from <in.mp4>
set -euo pipefail
export PATH=/usr/local/bin:$PATH
IN="$1"; Q="$2"; OUT="$3"
RE="$HOME/iMac Worker/tools/realesrgan"
FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$IN")
W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$IN"); H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$IN")
TH=$(( (3840 * H / W) / 2 * 2 ))
WORK=$(mktemp -d "${TMPDIR:-/tmp}/upscale.XXXX"); trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/in" "$WORK/out"
ffmpeg -nostdin -loglevel error -i "$IN" -fps_mode passthrough "$WORK/in/%06d.png"
N=$(ls "$WORK/in" | wc -l | tr -d ' '); echo "$N frames at $FPS, ${W}x${H} -> 3840x${TH}, quality $Q"
if [ "$Q" = best ]; then MODEL=realesrgan-x4plus; S=4; else MODEL=realesr-animevideov3; S=2; fi
t=$(date +%s)
"$RE/realesrgan-ncnn-vulkan" -i "$WORK/in" -o "$WORK/out" -n "$MODEL" -s "$S" -m "$RE/models" -f png > "$WORK/re.log" 2>&1
echo "upscaled $N frames in $(( $(date +%s) - t )) s with $MODEL"
ENC="-c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p"
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264 || ENC="-c:v hevc_videotoolbox -b:v 40M -tag:v hvc1"
ffmpeg -nostdin -loglevel error -y -framerate "$FPS" -i "$WORK/out/%06d.png" -i "$IN" -map 0:v -map 1:a? \
  -vf "scale=3840:${TH}:flags=lanczos" $ENC -c:a copy -metadata comment="AI-generated with YouTube Zeus Effects, upscaled to 4K on the iMac (Real-ESRGAN $MODEL)" -movflags +faststart "$OUT"
ls -la "$OUT"
