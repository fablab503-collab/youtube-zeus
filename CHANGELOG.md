# Changelog

Every feature is shipped with `./scripts/ship.sh "<message>"` (build, commit, push to GitHub, vault snapshot).
Newest first.

## 3.0 — 2026-09-28

The ten ideas starred on the 3.0 board. Everything stays free and on the Mac.

- **Faster listening, no more loops.** Measured on an M2 Pro with a 20-minute talk and its human captions: whisper.cpp
  as in 2.x 66.8 s / 7.9 % word errors; MLX Whisper large-v3-turbo without text context 39.6 s / 7.2 % (now the
  default when `uv` is installed, whisper.cpp as fallback); whisper.cpp without text context 53.4 s / 7.3 %; WhisperKit
  on the Neural Engine 101 s. With text context both greedy decoders fell into repetition loops (36–40 % errors), so
  Zeus never conditions on the previous text any more. Settings › Eating › Speech recognition.
- **Instant full-text search** (SQLite FTS5, `Search.sqlite`): titles, summaries, key points, transcripts, text on
  screen; "exact phrases", word*, OR, NOT, a NEAR b; every hit opens the transcript at its moment. The library's
  search field, `zeus search` (`--json`), Ask your brain (retrieval by BM25) and the MCP server use it.
- **Answers that point to the second.** Every key point and summary sentence is placed in the video (BM25 against
  the paragraphs, no AI; 95 % of the 1,020 key points of the library placed) and gets `[▶ mm:ss]`; Ask sources are
  corrected to the paragraph that holds their quote. `youtubezeus://open?video=…&t=…` opens the transcript there and
  highlights the paragraph. Notes are rewritten once (format 4).
- **Podcasts by RSS**: follow a feed or an Apple Podcasts link (paste it, drop it, `zeus podcast`,
  `youtubezeus://podcast?feed=…`); new episodes are eaten like videos, with the transcript published with the episode
  (Podcasting 2.0 JSON, WebVTT, SRT) when there is one, else Whisper. One folder and index note per show.
- **Your own files**: drop an .mp4, .mov, .mp3, .m4a, .wav… on the window (or "Choose files", `zeus eat <path>`,
  `youtubezeus://eat?file=…`): transcript, summary, note in `My files/`, all on the Mac. A small player plays podcasts and
  files from any moment.
- **Text on screen**: "Read the screen" (or `zeus screen`): frames every 2 s (ffmpeg), unchanged frames skipped, Apple's
  Vision reads the rest; slide titles, terminal commands and code blocks with their moments, in the note
  (`## On screen`), the "On screen" tab and search. Automatic for your own videos.
- **Notes for people, tools and companies**: after each summary the local AI (helped by NaturalLanguage) lists the
  names an item mentions, with the moments they are said; names in 2+ items get a note in `Sources/People`,
  `Sources/Tools`, `Sources/Companies` (facts block between markers, your notes kept) and an index. Sidebar › People &
  tools, `zeus entities`, `zeus entity`. Phrases, file names, generic words and names the AI itself doubts are left
  out, common misspellings are merged ("Cloud Code" → Claude Code, "VS Code" → Visual Studio Code), and notes are only
  rewritten when their content changes.
- **Weekly digest**: every Sunday at 19:00 (Settings › Extras) `Sources/YouTube/Digests/<year>-W<week>.md`: what was
  eaten, the best ideas and open questions (local AI, each pointing to its moment), new GitHub repositories, the
  week's names. A missed week is written at the next launch. Sidebar › Weekly digests, `zeus digest`.
- **Zeus as an MCP server**: `zeus mcp` (stdio JSON-RPC) with the tools search, get_transcript, get_note, ask,
  list_items, item_status, collections, claude_pack, entities, entity, github_repos, weekly_digest and eat; one-click
  connection for Claude, Claude Code, Codex, Cursor, LM Studio and Gemini CLI (Settings › Extras, configs backed up).
- **Share from iPhone and iPad**: Zeus makes an "Eat with Zeus" shortcut (signed with `shortcuts sign`) that appears in
  the share sheet; it saves the link in iCloud Drive › Shortcuts › YouTube Zeus › Inbox and the Mac eats it within a
  minute.

## 2.5.1 — 2026-09-28

- **Open source (MIT).** The repository is public: `LICENSE`, README (install, privacy, responsible use),
  generic default folders (`~/SecondBrain/Sources/YouTube`; existing installs keep theirs), no personal paths in
  the guide, the agent skill or the prompts, `ZEUS_VAULT_COPY` / `scripts/ship.local` for the vault snapshot.

## 2.5 — 2026-09-28

- **Free by default, no Codex needed.** Summaries: Apple Intelligence when its model is ready, else the local AI.
  "Ask your brain" and skill drafts: the local AI (`LocalLLM`, structured JSON through Ollama). Codex / OpenAI
  are optional cloud engines, off by default (Settings › Skills & cloud AI); an existing install is switched back
  to the free engines once. `zeus ask` and `zeus eat --summary` use the free engines too.
- Settings: "Free by default" note, new engine pickers (summaries, Ask, skills), cloud AI section.

## 2.4.1 — 2026-09-27

- **Summaries with the local AI**: new engine (Settings › AI › "Local AI on this Mac"), and the last fallback in
  "auto". Long videos are read in parts (notes and chapters per part, then one summary).
- **Codex usage limit**: when the ChatGPT plan's Codex limit is reached, Zeus pauses Codex until the reset time
  it gives (so the user's own Codex work is not blocked any longer than needed), continues with the local AI, and
  retries the summaries that had failed because of the limit (84 videos of the Nate Herk playlist).

## 2.4 — 2026-09-27

- **Whole channels with their playlists**: `…/@channel/playlists` links, "Import playlists" on a channel and in
  the follow sheet, `zeus playlists <channel> --import`, `youtubezeus://playlists?channel=…`: every playlist
  becomes a collection (grouped under its channel in the sidebar). Video links with `&list=PL…` offer the whole
  playlist. Playlist index notes carry the channel name ("Claude Code — Nate Herk").
- **Claude packs**: a collection becomes a Claude skill (`~/.claude/skills/<name>/`: index + one file per video),
  a zip for claude.ai, a one-message digest and the full transcripts in parts (`Sources/YouTube/Claude packs/`);
  rebuilt automatically while the collection is eaten and summarized. `zeus pack`, `youtubezeus://pack?…`.
- **Two lanes after eating**: local AI polishing and summaries run side by side.
- First use: Nate Herk's "Claude Code" playlist (110 videos).

## 2.3 — 2026-09-27

- **Code on GitHub**: repository `fablab503-collab/youtube-zeus`; `scripts/ship.sh` builds, regenerates
  the agent guide, commits, pushes and refreshes the source snapshot in the Second Brain. `AGENTS.md`,
  `CLAUDE.md`, this changelog.
- **Two-way Second Brain link**: "Open in Obsidian" (or "Open Note" without Obsidian) on videos, channels,
  collections, repositories and indexes opens the exact note (`obsidian://open?path=…`); menu Brain (⇧⌘B).
  Every note links back with `youtubezeus://open?video=…` / `collection=` / `channel=` / `repo=` / `view=`;
  video notes carry a `zeus:` property. New links: `youtubezeus://open?…`, `youtubezeus://ask?q=…`.
- **Guide for AI agents**: `Sources/YouTube/_For AI/YOUTUBE-ZEUS-GUIDE.md` (rewritten at launch), `zeus guide`,
  `docs/AGENT-GUIDE.md`; Settings › AI hand-off › Guide for AI agents.
- **GitHub checks everywhere**: "## GitHub" in channel and collection index notes, repositories in collection
  AI packs and in Ask your brain; `zeus repos`, `zeus open`, `zeus link`.

## 2.2 — 2026-09-27

- GitHub repositories linked in a video (description, channel's own comment, transcript) are checked through the
  GitHub API: exists/renamed, archived, fork, last push, stars, license, release, security advisories, owner age,
  link back to the video. "## GitHub" in video notes, `Sources/GitHub/<owner>-<repo>.md` notes (facts block
  between `%% zeus:github:facts %%` markers, user notes kept), `_Index - GitHub from YouTube.md`, sidebar ›
  GitHub, `zeus github`.

## 2.1 — 2026-09-27

- YouTube sign-in window, collections (playlists, whole channels, Watch Later, Liked) with index notes, local AI
  polishing (Ollama, qwen3:4b-instruct), `zeus` command, `youtube-zeus` Agent Skill, Copy for AI packs.
- Ask your brain (app and `zeus ask`), topics, after-eating queue rebuilt at every launch.

## 2.0 — 2026-09-27

- Native SwiftUI app for macOS 27 (Liquid Glass): captions or Whisper, channel watching, on-device summaries,
  Second Brain notes, Codex skill compiler.

## 0.1 — 2026-08

- Python/FastAPI web app built with Codex (history in the NAS bundle `YouTube-Zeus.bundle`, tip `8d63532`).
