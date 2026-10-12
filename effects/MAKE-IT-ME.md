# "Make it me" — one job, no assistant in the loop (contract v1, 2026-10-11)

Daniel: "Make the process local so I can do it in the app, not using Claude as intermediary."
Today the v2 result (natural face swap → hat removal → his voice) was stitched by hand from scripts. This contract turns it
into **one button** in YouTube Zeus (Mac) and in The Swap (iPhone, through the Mac). Everything stays free and local.

## The job
Inputs (all optional except the clip and the consent):
- `clip`: a video file or a YouTube link + start/duration (≤ 10 s in v1).
- `me`: how the person looks — a `.c101face` face pack (Cinema 101 / The Swap "Scan my face"), **or a self-video**
  (the app extracts the best frames: sharp, eyes open, front/±20°/±40°/smile; see "Film yourself"), or photos.
- `voice`: a voice sample (wav/m4a, 10–60 s) — or the speech found in the self-video.
- options: `quality` natural|fast, `hat` remove|keep, `voice` yes|no, `consent` true (required).

Steps, in order, each with progress 0–1 and a plain-English message (JSON lines, same style as `zeus_effects.py`):
1. `prepare` — download/trim, depth, masks, face mesh (needed by the hat step).
2. `refs` — pick the reference frames (from pack / self-video / photos) and the voice sample (cleaned: Demucs vocals,
   silences trimmed, normalized).
3. `approve` — automatic in this flow (the user consented and chose the clip; the preview is still saved for the record).
4. `faceswap` — FaceFusion, quality natural by default.
5. `hat` — only when a hat/cap is found: BiSeNet class 18 on a head crop → mask (EMA, dilate 17 px) → VACE inpaint with
   control = grey in the mask, refs = the frontal frame, prompt from the pack's hair description if any, negative
   "hat, cap, beanie, fabric, plaid, checkered pattern, blue patch" → hi-res composite. Skipped with a message when no hat.
6. `voice` — Chatterbox VC + EQ/loudness match + remix (skipped when no voice sample).
7. `final` — `final.mp4` (+ `final-voice.mp4` when voice ran), AI comment, side-by-side preview `compare.mp4`.

CLI: `zeus_effects.py makeme --input <clip|url> [--start S --duration D] (--ref-pack DIR | --self-video FILE | --ref IMG…)
[--voice FILE] [--quality natural|fast] [--hat remove|keep] [--no-voice] --consent [--job DIR]`.
Resumable: re-running with the same job dir skips finished steps (hashes). `cancel` kills the job's process group.

## Mac assist by shared folder (no network service — same pattern as Cinema 101's render farm)
A job is a **folder**, written by the phone and watched by the Mac; nothing listens on a port.
- Place: iCloud Drive › `The Swap` › `Jobs` (works anywhere), or `/Volumes/Volume1/The Swap Jobs` on the NAS at home
  (both apps let the user pick; iCloud is the default). Zeus watches the folders it is given (Settings › Extras ›
  "Do jobs from The Swap", on by default once the engine is set up; a LaunchAgent only if Daniel turns it on).
- Job folder `<yyyyMMdd-HHmmss>-<name>.swapjob/`: `clip.mp4` (or `clip.txt` with a YouTube link + start/duration),
  `me.c101face/` or `photos/`, `voice.m4a` (optional), `job.json` (options from "The job" + `consent: true`,
  `device`, `app`, `version`), and the phone writes `request` (empty marker) last, so the Mac never reads a half-copied job.
- Zeus: on `request` → writes `status.json` every few seconds (`step`, `progress`, `message`, `started`, `updated`,
  `error`), then `result.mp4` (final or final-voice), `compare.mp4`, `preview.jpg`, and `done` (marker) last;
  `cancel` marker from the phone stops the job (`cancelled` marker back). Jobs older than 14 days are cleaned with
  Daniel's OK (setting).
- The phone: shows the Mac's step names and progress from `status.json`, downloads `result.mp4` and `compare.mp4` when
  `done` appears, saves to Photos with the AI comment. If no Mac picks the job up within 2 minutes: "No Mac is working
  right now — face swap only on the phone?".

## "Film yourself" (reference capture, both apps)
A guided 30–60 s self-video: chest up, soft light, no hat: turn left/right/up/down, then talk 20 s, smile. The app extracts
a face pack (sharpest frame per angle bin, eyes open, same manifest as Cinema 101's scan, `expression` smile/neutral)
and the voice sample (speech only, cleaned). Output: `<Name>.c101face` + `voice.m4a` next to it. On the iPhone this can
also come from the TrueDepth scan (richer). Consent line before saving.

## Zeus app
Sidebar › Effects › **Make it me** card: clip (library video or file) · Me (face pack / self-video / photos) · Voice
(file or "from the self-video") · options · consent · one button · progress with the step names · result player +
side by side · Save to Movies. Jobs list shows them like other jobs.

## The Swap (iPhone)
New tab **Make it me**: pick a clip (Photos/Files) · Me (Scan my face / Film yourself / photos) · Voice · options ·
consent · button. Face swap (quality natural) runs **on the phone**; when `hat` or `voice` is on, the phone writes a
`.swapjob` folder to the shared place, shows the Mac's progress from `status.json`, then saves the result to Photos.
If no Mac picks it up: offer face swap only, on the phone.
