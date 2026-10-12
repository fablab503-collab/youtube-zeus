"""Cap mask per frame: BiSeNet face parser (CelebAMask-HQ classes: 17 hair, 18 hat) on a head crop found by MediaPipe."""
import sys, json, numpy as np, cv2, onnxruntime as ort
work, out_mask, out_ctrl, parser, boxes_json = sys.argv[1:6]
BOX = json.load(open(boxes_json))["boxes"]
sess = ort.InferenceSession(parser, providers=["CoreMLExecutionProvider", "CPUExecutionProvider"])
inp = sess.get_inputs()[0].name
cap = cv2.VideoCapture(work); fps = cap.get(cv2.CAP_PROP_FPS) or 16
w, h = int(cap.get(3)), int(cap.get(4))
fourcc = cv2.VideoWriter_fourcc(*"mp4v")
wm = cv2.VideoWriter(out_mask, fourcc, fps, (w, h), False); wc = cv2.VideoWriter(out_ctrl, fourcc, fps, (w, h))
ema, i, stats = None, 0, []
while True:
    ok, f = cap.read()
    if not ok: break
    m = np.zeros((h, w), np.float32)
    if i < len(BOX) and BOX[i]:
        x0, y0, x1, y1 = BOX[i]; fw, fh = x1 - x0, y1 - y0
        bx0, bx1 = int(max(0, x0 - 0.6 * fw)), int(min(w, x1 + 0.6 * fw))
        by0, by1 = int(max(0, y0 - 1.0 * fh)), int(min(h, y1 + 0.2 * fh))
        crop = f[by0:by1, bx0:bx1]
        x = cv2.resize(crop, (512, 512))[:, :, ::-1].astype(np.float32) / 255.0
        x = (x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
        y = sess.run(None, {inp: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]
        cls = y.argmax(0)
        hat = cv2.resize((cls == 18).astype(np.float32), (bx1 - bx0, by1 - by0), interpolation=cv2.INTER_LINEAR)
        m[by0:by1, bx0:bx1] = hat
    ema = m if ema is None else 0.6 * m + 0.4 * ema
    b = (ema > 0.4).astype(np.uint8) * 255
    b = cv2.dilate(b, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)))
    stats.append(float((b > 0).mean()))
    wm.write(b)
    ctrl = f.copy(); ctrl[b > 0] = 127   # inpainting control: grey where the cap was
    wc.write(ctrl); i += 1
wm.release(); wc.release()
print(json.dumps({"frames": i, "cap_frames": sum(s > 0.001 for s in stats), "mean_cover": round(float(np.mean(stats)), 4)}))
