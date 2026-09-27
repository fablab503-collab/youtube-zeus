# Changelog

Every feature is shipped with `./scripts/ship.sh "<message>"` (build, commit, push to GitHub, vault snapshot).
Newest first.

## 2.4.1 — 2026-09-27

- **Summaries with the local AI**: new engine (Settings › AI › "Local AI on this Mac"), and the last fallback in
  "auto". Long videos are read in parts (notes and chapters per part, then one summary).
- **Codex usage limit**: when the ChatGPT plan's Codex limit is reached, Zeus pauses Codex until the reset time
  it gives (so Daniel's own Codex work is not blocked any longer than needed), continues with the local AI, and
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

- **Code on GitHub**: private repository `fablab503-collab/youtube-zeus`; `scripts/ship.sh` builds, regenerates
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
