"""Generate a portrait of a person who does not exist with the local Wan2.1-VACE (text only, no reference)."""
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import numpy as np
from zfx.engines import vace
out = Path(sys.argv[2])
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
vace.configure_mps_memory()
import torch
from PIL import Image
path = vace.check_model(); device = vace.pick_device(); dtype = vace.pick_dtype(device, path)
prompt = ("Studio portrait photograph of a woman in her thirties with short dark brown hair and freckles, "
          "clean face, looking straight at the camera, neutral relaxed expression, soft even light, plain light grey "
          "background, sharp focus, realistic skin, high detail, head and shoulders")
t0 = time.time()
pe, ne, _ = vace.get_prompt_embeds(path, prompt, vace.NEGATIVE_PROMPT, "cpu", lambda *a, **k: None)
pipe = vace.load_pipeline(path, device, dtype)
pe, ne = pe.to(device=device, dtype=dtype), ne.to(device=device, dtype=dtype)
h, w, n = 640, 368, 5
grey = [Image.fromarray(np.full((h, w, 3), 128, np.uint8)) for _ in range(n)]
white = [Image.fromarray(np.full((h, w), 255, np.uint8)) for _ in range(n)]
g = torch.Generator(device="cpu").manual_seed(int(sys.argv[3]) if len(sys.argv) > 3 else 7)
with vace.mps_runtime(pipe, device):
    res = pipe(video=grey, mask=white, reference_images=None, prompt_embeds=pe, negative_prompt_embeds=ne,
               height=h, width=w, num_frames=n, num_inference_steps=25, guidance_scale=vace.GUIDANCE,
               generator=g, output_type="np", max_sequence_length=vace.MAX_SEQUENCE_LENGTH).frames[0]
img = (np.clip(np.asarray(res, np.float32), 0, 1) * 255 + 0.5).astype(np.uint8)
Image.fromarray(img[n // 2]).save(out, quality=95)
print(f"saved {out} in {time.time() - t0:.0f} s, range {img.min()}-{img.max()}")
