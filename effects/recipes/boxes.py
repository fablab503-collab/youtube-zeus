import sys, json, cv2, numpy as np, mediapipe as mp
from mediapipe.tasks import python as mpt
from mediapipe.tasks.python import vision
work, landmarker, out = sys.argv[1:4]
lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
    base_options=mpt.BaseOptions(model_asset_path=landmarker), running_mode=vision.RunningMode.VIDEO, num_faces=1))
cap = cv2.VideoCapture(work); fps = cap.get(cv2.CAP_PROP_FPS) or 16; w, h = int(cap.get(3)), int(cap.get(4)); boxes = []; i = 0
while True:
    ok, f = cap.read()
    if not ok: break
    ox, oy = int(w * 0.5), int(h * 0.3); crop = np.ascontiguousarray(f[oy:, ox:]); cw, ch = crop.shape[1], crop.shape[0]
    r = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)), int(i * 1000 / fps))
    if r.face_landmarks:
        pts = np.array([(ox + p.x * cw, oy + p.y * ch) for p in r.face_landmarks[0]]); x0, y0 = pts.min(0); x1, y1 = pts.max(0)
        boxes.append([float(x0), float(y0), float(x1), float(y1)])
    else: boxes.append(None)
    i += 1
json.dump({"w": w, "h": h, "fps": fps, "boxes": boxes}, open(out, "w")); print(i, "frames,", sum(b is not None for b in boxes), "faces")
