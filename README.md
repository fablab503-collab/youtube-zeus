# YouTube Zeus 3.0 — the YouTube eater, native on macOS 27

Open source (MIT). A native macOS app that turns YouTube videos, playlists and whole channels, podcasts and your own
recordings into organised, searchable Markdown notes in your Second Brain (an Obsidian-style vault), and hands that
knowledge to any AI agent (command line, MCP server, links, skills).

**Free and open by default:** every step runs on the Mac with free tools — yt-dlp, ffmpeg, MLX Whisper or whisper.cpp, Apple's Vision and NaturalLanguage, SQLite, Ollama with an open model (Qwen3, Apache 2.0) and Apple Intelligence. No paid AI is needed. Codex / OpenAI are optional and off (Settings › Skills & cloud AI).

Paste a YouTube or podcast link, or drop a recording on the window. Zeus eats it and keeps its text.

## New in 3.0

- **Faster listening**: MLX Whisper (large-v3-turbo) when `uv` is installed, whisper.cpp otherwise — a 20-minute talk in
  40 s instead of 67 s on an M2 Pro, with fewer word errors, and no repetition loops (measured; see CHANGELOG).
- **Instant full-text search** of titles, summaries, transcripts and on-screen text (SQLite FTS5): "phrases", word*,
  OR, NOT, a NEAR b; every hit opens the transcript at its moment. `zeus search`.
- **Answers that point to the second**: every key point and summary sentence links to the moment it comes from; Ask
  your brain's sources are checked against their quotes.
- **Podcasts by RSS** (or an Apple Podcasts link): published transcripts when they exist, Whisper otherwise.
- **Your own files**: .mp4, .mov, .mp3, .m4a, .wav… transcribed, summarized and filed, all on the Mac.
- **Text on screen**: slide titles, code and terminal commands read by Apple's Vision, with their moments.
- **Notes for people, tools and companies** named in the videos (local AI), with backlinks and moments.
- **Weekly digest** every Sunday: what was eaten, best ideas, open questions, new repositories.
- **MCP server** (`zeus mcp`) for Claude, Claude Code, Codex, Cursor, LM Studio, Gemini CLI — one click to connect.
- **Share from iPhone and iPad**: an "Eat with Zeus" shortcut sends links to the Mac through iCloud Drive.

- **Captions first**: the channel's own captions in the video's language, then YouTube's original auto-captions (yt-dlp, json3).
- **Whisper when there are none**: downloads the audio and listens on this Mac with whisper.cpp (`whisper-cli`, model downloaded once into `~/Library/Application Support/YouTube Zeus/Models`). "Listen with Whisper" also re-does videos that only had auto-captions.
- **Watches channels**: follow a channel by pasting its link, or import Google Takeout `subscriptions.csv`. Public RSS feeds are checked every 30 minutes (no Google account); new uploads are eaten automatically. Live streams, premieres and uploads whose captions are not ready yet wait and are retried.
- **On-device summaries**: Apple Intelligence (Foundation Models) writes a summary, key points, topics and chapters. Long videos are read in parts (map → reduce). Free and private.
- **Second Brain**: each transcript becomes a Markdown note with vault front matter in `<notes folder>/<Channel>/<date> - <title>.md` (default `~/SecondBrain/Sources/YouTube`, Settings › Eating). A folder on a NAS works too: when it is not mounted, notes wait and are written when it comes back.
- **YouTube sign-in**: a YouTube window inside Zeus (sidebar › YouTube). Sign in once; the sign-in is written to a private cookies file for yt-dlp (members-only, age-restricted, your lists). Or use cookies from Safari/Chrome/Firefox/Brave/Edge. Browse and press "Eat this video / playlist / channel". Import your subscriptions, eat Watch Later and Liked videos.
- **Collections**: a playlist, a whole channel, Watch Later or Liked videos is eaten as one collection, in order, with its own index note (`Collections/<name>.md`). Every channel folder gets `_Index - <Channel>.md`; `YouTube index.md` lists channels, collections, topics and recent videos. Notes also carry tags, views, likes, the description and the top comments.
- **Local AI polishing**: Ollama + `qwen3:4b-instruct` (2.5 GB, fits in 8 GB of RAM) fixes punctuation, capitals and misheard words of auto-captions and Whisper text, paragraph by paragraph, never translating or shortening. Original / Polished toggle.
- **AI hand-off**: "Copy for AI" knowledge packs (video or collection) for Claude, ChatGPT, Gemini, Grok, GLM…; the `zeus` command for terminals and agents; the `youtube-zeus` Agent Skill installed for Claude Code, Codex, Gemini CLI and ~/.agents (Settings › AI hand-off). A copy of the skill and universal instructions lives in the Second Brain (`Sources/YouTube/_For AI/`).
- **Ask your brain**: ask a question in any language; Zeus ranks the eaten videos and paragraphs on the Mac, and the local AI answers with numbered sources (video + timestamp + quote). **Topics** in the sidebar group videos by their summary tags; new summaries reuse existing tags so the library stays tidy.
- **GitHub links**: every github.com/<owner>/<repo> link in a video's description (or in the channel's own comment, or spelled out in the transcript) is checked through the GitHub API: exists, renamed, archived, fork, last push, stars, license, latest release, published security advisories, owner account age, and whether the repository links back to the video or its site. The video note gets a "## GitHub" section; each repository gets a note in `Sources/GitHub/<owner>-<repo>.md` (README excerpt, "Seen in" every video; Zeus only rewrites the block between `%% zeus:github:facts start/end %%`, your notes are kept) and `_Index - GitHub from YouTube.md` lists them all. Sidebar › GitHub. Results are cached 3 days; add a GitHub token (Settings › Eating, or `gh auth login`) for more than ~15 repositories an hour.
- **Two-way Second Brain link**: "Open in Obsidian" on every page opens the exact note (video, channel, collection, repository, indexes; menu Brain ⇧⌘B); every note links back with `youtubezeus://open?…`, so a click in the vault brings the same page up in Zeus.
- **Guide for AI agents**: `Sources/YouTube/_For AI/YOUTUBE-ZEUS-GUIDE.md` (rewritten at launch), `zeus guide`, and [`docs/AGENT-GUIDE.md`](docs/AGENT-GUIDE.md) — how Zeus works and every way to connect. Developers: [`AGENTS.md`](AGENTS.md).
- **Whole channels and Claude packs**: import every playlist of a channel as collections (`@channel/playlists`, "Import playlists", `zeus playlists --import`), then turn any collection into a **Claude pack**: a skill installed in `~/.claude/skills` (index + one file per video), a zip for claude.ai, a one-message digest and the full transcripts in parts in `Sources/YouTube/Claude packs/` (`zeus pack <playlist>`). Polishing and summaries run in two parallel lanes.
- **Skills** (the 0.1 feature): the local AI (free; or, if you opt in, Codex / the OpenAI API) proposes up to three evidence-backed skills. Every quote is checked against the transcript, the bundle is validated (structure, prohibited content, evidence), and nothing is published without "Approve and publish". Output: `SKILL.md`, `references/evidence.md`, `references/changes.md`, `evals/evals.json` in `~/.codex/skills/<name>/` (older versions go to `Skill History`). The API key lives in the Keychain.

## Install and build

Needs macOS 27, Xcode 27 and a few free tools:

```bash
brew install xcodegen yt-dlp ffmpeg whisper-cpp deno   # required
brew install uv                                       # optional: MLX Whisper, the fastest listening on Apple silicon
brew install --cask ollama                            # the free local AI (polishing, summaries, names, digests, Ask)
brew install gh && gh auth login                      # optional: more GitHub checks per hour
```

In `project.yml`, set `DEVELOPMENT_TEAM` to your own Apple team (or sign locally with "Sign to Run Locally"), and
optionally change the bundle identifier. Then:

```bash
./scripts/build.sh              # Release build, installs /Applications/YouTube Zeus.app
./scripts/build.sh --no-install # build only
```

The app is not sandboxed (it runs yt-dlp, ffmpeg and whisper-cli) and uses the hardened runtime. The first time you polish or summarize, Zeus downloads the local model (`qwen3:4b-instruct`, 2.5 GB) through Ollama. Keep Ollama's model folder out of iCloud-synced folders (Ollama › Settings › Model location), or iCloud may move the model away.

## zeus command

```bash
zeus eat "<link>" --save            # knowledge pack to stdout + note in the Second Brain + app library
zeus eat "<playlist|channel>" --limit 20 --save
zeus eat "<link>" --polish --summary --json
zeus get <link|id>                  # saved note, no network
zeus search "<words>" [--json]      # full text: "phrases", word*, OR, NOT, a NEAR b
zeus eat ~/Movies/lecture.mp4       # your own recording, eaten by the app
zeus podcast "<feed or Apple Podcasts link>" --follow --latest 3
zeus screen "<link|id>"             # text on screen: slide titles, code, commands
zeus entities [name] / zeus entity "<name>"   # people, tools and companies
zeus digest [--last] [--save]       # the weekly digest
zeus mcp                            # MCP server (stdio) for Claude, Codex, Cursor, LM Studio…
zeus list "<playlist|channel>" --limit 50
zeus github "<link>" [--save] [--json]   # GitHub repos linked in a video, checked through the GitHub API
zeus repos [--json]                 # every repo linked in the library
zeus open "<link|id|youtubezeus://…>"    # show it in the app
zeus guide                          # the full guide for AI agents
zeus playlists "<channel>" --import # every playlist of a channel as collections
zeus pack "<playlist>"              # Claude pack: skill + digest + transcript parts
```

## Links (`youtubezeus://`)

```
youtubezeus://eat?url=<link>                 youtubezeus://open?video=<id>[&tab=Info][&t=<seconds>]
youtubezeus://eat?file=<path>                youtubezeus://podcast?feed=<feed>[&latest=N]
youtubezeus://search?q=<words>               youtubezeus://screen?video=<id>
youtubezeus://open?entity=<name>             youtubezeus://digest?week=<2026-W39>
youtubezeus://open?collection=<list id>      youtubezeus://open?channel=<channel id>
youtubezeus://open?repo=<owner>/<repo>       youtubezeus://open?view=library|github|ask|skills|eating|youtube
youtubezeus://ask?q=<question>
```

The `zeus` command is installed by Settings › AI hand-off as a tiny script in /opt/homebrew/bin (or ~/.local/bin) that runs the app binary with `--cli`.

## Contributing and shipping

Source: https://github.com/fablab503-collab/youtube-zeus. Read [`AGENTS.md`](AGENTS.md) first (conventions: free engines first, every feature reachable by agents). Every feature: build, add a `CHANGELOG.md` entry, then `./scripts/ship.sh "<message>"` (build, regenerate the agent guide, commit, push; with `ZEUS_VAULT_COPY` set, also refresh a source snapshot in your vault).

## Launch arguments (scripting and tests)

```bash
open -a "YouTube Zeus" --args -eat "https://youtu.be/…"        # eat a link at launch
open -a "YouTube Zeus" --args -eatWhisper "https://youtu.be/…" # force Whisper
open -a "YouTube Zeus" --args -selectVideo <videoID>
open -a "YouTube Zeus" --args -ask "question"                 # also: -browse <url>, -showTopic <name>, -polish <id>
open -a "YouTube Zeus" --args -checkGitHub <videoID|all>      # check GitHub links again (-showGitHub opens the list)
open "youtubezeus://eat?url=<percent-encoded link>"           # hand a link to the running app
```

The after-eating queue (polish → summary → note → indexes) is rebuilt at every launch and every channel check, so nothing is lost when the app quits.

`~/Library/Application Support/YouTube Zeus/diagnostics.json` shows tools, Apple Intelligence and Second Brain status. The library is a SwiftData store in the same folder (`Library.store`).

## Layout

- `YouTubeZeus/App` — app entry, settings, app model
- `YouTubeZeus/Models` — SwiftData models (Video, Channel, SkillDraft) and transcript types
- `YouTubeZeus/Services` — process runner, yt-dlp, caption parsers, Whisper, channel feeds, eat pipeline, Second Brain export, Apple Intelligence summaries
- `YouTubeZeus/Skills` — OpenAI client, analysis contract, renderer, validator, compiler/publisher
- `YouTubeZeus/Views` — SwiftUI (Liquid Glass) views
- `YouTubeZeus/AppIcon.icon` — Icon Composer icon

Version 0.1 (Aug 2026) was a Python/FastAPI web app; 2.x is a full native rewrite.

## Privacy

Everything runs on your Mac. Nothing is sent anywhere except the requests the tools need (YouTube for videos and
captions, podcast feeds and their audio, Apple's podcast lookup for Apple Podcasts links, GitHub's public API for
repository checks, Ollama's registry and Hugging Face to download the models once). Links shared from the iPhone travel
through your own iCloud Drive. Optional cloud
engines (Codex, OpenAI API) are off unless you turn them on. Keys live in the Keychain; the YouTube sign-in in a
private cookies file in Application Support.

## Licence and responsible use

MIT — see [`LICENSE`](LICENSE). © 2026 Daniel Madac · FabLab 503.

Downloading YouTube content can conflict with YouTube's Terms of Service. Use YouTube Zeus for personal study of
videos you are allowed to access, respect creators, and do not republish their transcripts.
