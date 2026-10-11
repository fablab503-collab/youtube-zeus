# Face pack v1 — "Scan my face" (Cinema 101) → face swap reference (YouTube Zeus Effects)

Daniel's request (2026-10-11): use the iPhone's TrueDepth camera (the camera behind Face ID — never Face ID's own
data, which no app can read) to capture his face as fully as possible, so face swaps of him look right from every
angle. Everything stays on his devices: no upload, no account, no cloud.

## Capture (Cinema 101, iPhone/iPad with TrueDepth)

- Entry: Settings › Profile › **Scan my face** (also `c101://facescan`). Unavailable without TrueDepth: say so plainly.
- ARKit `ARFaceTrackingConfiguration` (front camera), highest-resolution supported video format; high-resolution stills
  with `ARSession.captureHighResolutionFrame` when the format supports it, else `capturedImage`.
- Guided, ~10–15 s: keep the face in the oval; turn slowly left, right, up, down; then neutral, smile, mouth open.
  A ring fills per pose bin (yaw −45…+45 in 15° steps × pitch −20/0/+20, plus three expressions). Done when the
  front bins and at least ±30° yaw are filled. Cancel any time; nothing is kept unless saved.
- For every filled bin keep the **sharpest** frame (Laplacian variance on the face crop, eyes open, no motion blur).
- Before saving: a one-line consent: "This scan is of me (or of someone who agreed). It stays on my devices."

## Pack layout

A folder with the extension `.c101face` (a document package on iOS), saved to Files › Cinema 101 › Face Packs,
shareable by AirDrop / iCloud Drive / the existing NAS & WebDAV sending.

```
<Name> <yyyy-MM-dd HH-mm>.c101face/
  manifest.json
  frames/<bin>.jpg              upright RGB stills, sRGB, JPEG q 0.95, the highest resolution available
  depth/<bin>.png               optional: 16-bit grey, millimetres, TrueDepth depth aligned to the frame
  mesh.obj                      neutral ARFaceGeometry: 1,220 vertices (metres, face-anchor space), triangles, UVs
  blendshapes.json              optional: the whole scan, {fps, names[52], samples:[{t, values[52], transform[16]}]}
```

`manifest.json`:
```json
{
  "format": "c101face", "version": 1,
  "created": "2026-10-11T16:00:00+02:00",
  "person": "Daniel Madac",
  "consent": true,
  "device": "iPhone19,2", "app": "Cinema 101 <version>",
  "frames": [
    {"file": "frames/y+00_p+00.jpg", "bin": "y+00_p+00", "yaw": 1.8, "pitch": -0.6, "roll": 0.4,
     "expression": "neutral", "sharpness": 412.5, "width": 1920, "height": 1440, "time": 2.31,
     "face_box": [0.31, 0.22, 0.38, 0.51], "depth": "depth/y+00_p+00.png"}
  ],
  "mesh": "mesh.obj",
  "blendshapes": "blendshapes.json"
}
```
Angles in degrees, camera's point of view: yaw + = the person turns to their left, pitch + = chin up.
`face_box` is normalized [x, y, w, h], origin top-left, in the upright image. Unknown fields must be ignored.

## Use (YouTube Zeus Effects)

- `zeus_effects.py generate … --ref-pack <dir.c101face>` (may be combined with `--ref`). The engine reads the manifest
  and picks up to 5 references: the sharpest frontal neutral frame, then the sharpest at about ±20° and ±40° yaw, then a
  smile; frames below a sharpness floor are skipped. Refuses a pack whose `consent` is not true.
- Swift: Generate card › **Use a face pack…** (folder picker accepting `.c101face`), and packs found in
  `~/Downloads` and the iCloud Drive Cinema 101 folder are offered.
- Later (v2): per-shot pose matching (pick the reference whose yaw matches the target face), and the mesh/depth for
  full replace and for "act it yourself" (blendshapes driving the result).
