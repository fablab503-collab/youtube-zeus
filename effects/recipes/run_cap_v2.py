"""Cap removal test: VACE inpaints the cap region (BiSeNet 'hat') as hair, then the hi-res composite puts it back."""
import os, sys, json, time
from pathlib import Path
sys.path.insert(0, "/Volumes/4TB Tahoe/Users/danielmadac/Developer/YouTubeZeus/effects")
os.environ["ZEUS_VACE_CONTROL"] = "as-is"
from zfx.engines import vace
from zfx import generate
vace.NEGATIVE_PROMPT = vace.NEGATIVE_PROMPT + ", hat, cap, baseball cap, beanie, headband, fabric, cloth on the head, plaid, checkered pattern, blue patch"
job = Path.home() / "Library/Application Support/YouTube Zeus/Effects/Jobs/20261011-cap-removal-daniel-v2"
ref = "/private/tmp/claude-501/-Users-danielmadac-Documents/0e6e1e71-09ce-4138-a32a-da0d50512b35/scratchpad/facepack-test/Daniel.c101face/frames/y+00_p+00.jpg"
prompt = ("short tousled dark brown hair, no hat, no cap, natural hairline and fringe, real hair texture, "
          "same soft studio light, photorealistic")
log = lambda stage, p, m="": print(f"{time.strftime('%H:%M:%S')} {stage} {p if p is None else round(p, 3)} {m}", flush=True)
t0 = time.time()
info = vace.run(job, [ref], job / "result-raw.mp4", log, prompt=prompt, frames=33, steps=20, size="360p", seed=20261011,
                control=job / "control.mp4", mask=job / "mask.mp4")
print("vace done", round(time.time() - t0), "s")
crops = job / "result-crops.json"
out = generate.composite_final(job, crops, job / "source.mp4", job / "mask.mp4", job / "audio.m4a", job / "final.mp4",
                               "vace", "fast", None, log) if crops.is_file() else \
      generate.mux_final(job / "result-raw.mp4", job / "source.mp4", job / "audio.m4a", job / "final.mp4", "vace", mask=job / "mask.mp4")
print(json.dumps({k: v for k, v in out.items() if isinstance(v, (int, float, str, bool))})[:600])
print("total", round(time.time() - t0), "s")
