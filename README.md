# YouTube Zeus 2.0 — the YouTube eater, native on macOS 27

Paste a YouTube link (video, playlist or channel). Zeus eats the video and keeps its text.

- **Captions first**: the channel's own captions in the video's language, then YouTube's original auto-captions (yt-dlp, json3).
- **Whisper when there are none**: downloads the audio and listens on this Mac with whisper.cpp (`whisper-cli`, model downloaded once into `~/Library/Application Support/YouTube Zeus/Models`). "Listen with Whisper" also re-does videos that only had auto-captions.
- **Watches channels**: follow a channel by pasting its link, or import Google Takeout `subscriptions.csv`. Public RSS feeds are checked every 30 minutes (no Google account); new uploads are eaten automatically. Live streams, premieres and uploads whose captions are not ready yet wait and are retried.
- **On-device summaries**: Apple Intelligence (Foundation Models) writes a summary, key points, topics and chapters. Long videos are read in parts (map → reduce). Free and private.
- **Second Brain**: each transcript becomes a Markdown note with vault front matter in `/Volumes/Volume1/SecondBrain/Sources/YouTube/<Channel>/<date> - <title>.md`. When the NAS is not mounted, notes wait and are written when it comes back.
- **Codex skills** (the 0.1 feature): OpenAI (Responses API, strict JSON schema, `store=false`) proposes up to three evidence-backed skills. Every quote is checked against the transcript, the bundle is validated (structure, prohibited content, evidence), and nothing is published without "Approve and publish". Output: `SKILL.md`, `references/evidence.md`, `references/changes.md`, `evals/evals.json` in `~/.codex/skills/<name>/` (older versions go to `Skill History`). The API key lives in the Keychain.

## Build

Needs Xcode 27, XcodeGen and Homebrew tools: `brew install xcodegen yt-dlp ffmpeg whisper-cpp deno`.

```bash
./scripts/build.sh              # Release build, installs /Applications/YouTube Zeus.app
./scripts/build.sh --no-install # build only
```

Signed with the Developer ID Application certificate of team B7P7FR67VK (hardened runtime, no sandbox, because it runs yt-dlp, ffmpeg and whisper-cli).

## Launch arguments (scripting and tests)

```bash
open -a "YouTube Zeus" --args -eat "https://youtu.be/…"        # eat a link at launch
open -a "YouTube Zeus" --args -eatWhisper "https://youtu.be/…" # force Whisper
open -a "YouTube Zeus" --args -selectVideo <videoID>
```

`~/Library/Application Support/YouTube Zeus/diagnostics.json` shows tools, Apple Intelligence and Second Brain status. The library is a SwiftData store in the same folder (`Library.store`).

## Layout

- `YouTubeZeus/App` — app entry, settings, app model
- `YouTubeZeus/Models` — SwiftData models (Video, Channel, SkillDraft) and transcript types
- `YouTubeZeus/Services` — process runner, yt-dlp, caption parsers, Whisper, channel feeds, eat pipeline, Second Brain export, Apple Intelligence summaries
- `YouTubeZeus/Skills` — OpenAI client, analysis contract, renderer, validator, compiler/publisher
- `YouTubeZeus/Views` — SwiftUI (Liquid Glass) views
- `YouTubeZeus/AppIcon.icon` — Icon Composer icon

Version 0.1 (Python/FastAPI web app, Aug 2026) is kept as a git bundle on the NAS:
`SecondBrain/NAS/Snapshots/MacBook-Daniel-Migration-2026-08-31/Repositories/YouTube-Zeus.bundle`.

Downloading YouTube content can conflict with YouTube's Terms of Service; this app is for personal use on your own Mac.
