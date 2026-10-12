"""BiSeNet face parser over a batch of head crops, run as a subprocess in FaceFusion's venv.

zfx.parts starts this file with FaceFusion's own Python ("$ZEUS_EFFECTS_HOME/facefusion/.venv/bin/python",
which has onnxruntime with the CoreML provider) because the main Effects venv has no onnxruntime. It imports
nothing from zfx: only numpy and onnxruntime.

  python -I faceparse_worker.py MODEL.onnx IN.npy OUT.npy [--cpu]

  MODEL.onnx  FaceFusion's bisenet_resnet_34.onnx (CelebAMask-HQ, 19 classes, input 1x3x512x512 RGB, ImageNet
              mean/std, like facefusion/face_masker.py create_region_mask)
  IN.npy      (N, 512, 512, 3) uint8 RGB head crops (already resized to 512 x 512)
  OUT.npy     (N, 512, 512) uint8 class per pixel: 0 background, 1 skin, 2 l_brow, 3 r_brow, 4 l_eye, 5 r_eye,
              6 eye_glasses, 7 l_ear, 8 r_ear, 9 ear_ring, 10 nose, 11 mouth, 12 u_lip, 13 l_lip, 14 neck,
              15 necklace, 16 cloth, 17 hair, 18 hat

stdout: JSON lines {"i": done, "n": total} and finally {"done": true, "provider": "...", "frames": N};
onnxruntime's own messages go to stderr.
"""

import json
import sys
import time

import numpy as np

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
SIZE = 512


def make_session(model_path: str, cpu_only: bool):
    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    options = ort.SessionOptions()
    options.log_severity_level = 3
    providers = ["CPUExecutionProvider"] if cpu_only else ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    try:
        return ort.InferenceSession(model_path, options, providers=providers)
    except Exception as exc:  # CoreML refused the graph: the CPU does it
        if cpu_only:
            raise
        print(json.dumps({"warning": f"CoreML provider failed ({exc}); using the CPU"}), flush=True)
        return ort.InferenceSession(model_path, options, providers=["CPUExecutionProvider"])


def main(argv) -> int:
    if len(argv) < 3:
        print(json.dumps({"error": "usage: faceparse_worker.py MODEL.onnx IN.npy OUT.npy [--cpu]"}), flush=True)
        return 2
    model_path, src, dst = argv[:3]
    cpu_only = "--cpu" in argv[3:]
    t0 = time.monotonic()
    session = make_session(model_path, cpu_only)
    input_name = session.get_inputs()[0].name
    crops = np.load(src, mmap_mode="r")
    n = int(crops.shape[0])
    out = np.zeros((n, SIZE, SIZE), np.uint8)
    for i in range(n):
        crop = np.asarray(crops[i], dtype=np.float32) / 255.0
        if crop.shape[:2] != (SIZE, SIZE):
            raise ValueError(f"crop {i} is {crop.shape[1]}x{crop.shape[0]}, expected {SIZE}x{SIZE}")
        x = ((crop - MEAN) / STD).transpose(2, 0, 1)[None].astype(np.float32)
        logits = session.run(None, {input_name: x})[0][0]        # (19, 512, 512)
        out[i] = logits.argmax(0).astype(np.uint8)
        if i % 4 == 3 or i + 1 == n:
            print(json.dumps({"i": i + 1, "n": n}), flush=True)
    np.save(dst, out)
    print(json.dumps({"done": True, "provider": session.get_providers()[0], "frames": n,
                      "seconds": round(time.monotonic() - t0, 2)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
