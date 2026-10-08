# Changelog

Every feature is shipped with `./scripts/ship.sh "<message>"` (build, commit, push to GitHub, vault snapshot).
Newest first.

## 3.3.1 — 2026-10-08

- Fixed: deleting a video while it was being summarized, polished, eaten, read on screen or made into a playbook
  crashed the app (the lane wrote to the deleted video). Delete now takes the video out of every lane, and each lane
  checks the video is still there before saving.
- When Safari's cookies are chosen for yt-dlp, macOS refuses them unless YouTube Zeus has Full Disk Access; the
  YouTube page's "Cookies from" should stay on "Zeus's YouTube window" (signed in inside Zeus).

## 3.3 — 2026-10-06

- **A small YouTube browser in the Eat bar.** The red ▶ button next to the link field opens YouTube in a window
  over the app (the same signed-in browser as the YouTube page): sign in, search, open a video, then **Copy URL**,
  **Put in the Eat bar** or **Eat now**, with the video's thumbnail and title.
- **Eating now: Pause, Cancel, Delete, several at once.** A Pause / Resume button stops eating (what was running
  waits at the front of the line and starts again on Resume); each row has Cancel and Delete; ⌘-click or ⇧-click
  selects several rows to cancel, retry or delete them together. Agents: `youtubezeus://pause` and `…://resume`.
- **Bouclier inside**: the YouTube browser (the mini one and the YouTube page) has Bouclier's YouTube protection
  built in: its script that strips ads from the player and skips the ones that slip through, its hiding of ad
  banners and promoted videos, and blocked ad and tracking servers. A "Bouclier on / off" switch sits in both
  browsers.
- **The video you have open is offered above the Eat bar**: its thumbnail and title, with **Use** (puts its link in
  the field), copy, and hide.

## 3.2.1 — 2026-10-06

Playbooks that are closer to what the video really does, and more detailed (from the first real playbook, "I Built
Another Andrej Karpathy Using Claude", where 9 of 39 steps were the intro and some links were made up):

- **Prompts are steps of their own (💬).** When the presenter asks their AI agent something (Claude Code, Codex…), the
  step carries the prompt word for word, the agent following the playbook does that task itself, and it is no longer
  read as a command (`claudocode run "…"`, `claudie -p …` become prompts).
- **No more intro steps.** The teaser at the start (the finished result, "look at this") and steps that only look at
  something are dropped: the review names them (`not_actions`) and a rule removes "look at" steps before the first
  real action. Small "check this" steps at the same moment are merged into one step.
- **No made-up links.** A link is kept only when the video says or shows it (screen text, what is said, description,
  repositories); otherwise the step says what to look for. `file://` and relative links go.
- **Claude Code knowledge**: sub-agents, skills, hooks, `CLAUDE.md` and MCP servers are files the agent writes in the
  project (`.claude/…`, `.mcp.json`), never a web page.
- **More detail in each step**: the words said at that moment (🎙 quote), 1–3 sentences of detail, and the stages of the
  job (Set up, Collect, Build, Test…) shown first in "Plan at a glance" and as headers in the steps.
- **`TRANSCRIPT.md`** next to `PLAYBOOK.md` (Run with Claude Code / Codex, and the Claude Code skill): what is said,
  with moments and chapters, for the agent to read around a step.
- **Tools**: browsers, Terminal and text editors are no longer listed; web services (TwitterAPI.io…) move to "Only
  you"; known tools always use the right check and install (`claude --version`, `brew install --cask notion`,
  `uv tool install youtube-transcript-api`…); apps are found in Applications ("on this Mac").
- Frames described by the vision model no longer repeat the same fact ("Graph view; Graph view; …").
- Playbook format 2 (older playbooks still open; "Make it again" gives them the new parts).

## 3.2 — 2026-10-05

- **Playbooks: a tutorial as steps for an AI agent.** A new Playbook tab (and `zeus playbook`) turns a video into
  instructions that Claude Code, Codex, ChatGPT or Mistral can follow from zero to a working result, in three passes on
  this Mac: the summary; video to text (the frames where the screen changes, found with ffmpeg and a fingerprint of
  each frame, are described by a local vision model — Qwen-VL or Gemma 3 in Ollama, chosen for the Mac and downloaded
  the first time — helped by the text Apple's Vision reads on them and the words said at that moment); then the local
  AI writes the steps part by part and reviews them. Each step says who does it (🤖 the agent or 🙋 you), the exact
  command, link, file or code, what it downloads, what to expect, its moment and frame, and how careful to be (⚠️ asks
  first for `sudo`, scripts piped to a shell, deleting, spending money, publishing; rules catch what the model missed).
  GitHub repositories in the steps carry Zeus's check.
- **The agent shows you the plan first.** Every playbook starts with "For the agent": present a visual overview (the
  steps, what will be downloaded and installed, what only you can do), wait for an OK, install missing tools itself
  (Homebrew, releases, official instructions), do everything it can, stop only at your steps with exact directions,
  never ask for a password or key in the chat, check each step, end with a report.
- **Infographic in the app**: goal, counts (steps, by the agent, by you, downloads, asked first), tools already on this
  Mac, accounts only you can provide, and the steps as a timeline with the video's frame, badges, commands to copy and
  the moment to play.
- **Hand it over**: Run with Claude Code (or Codex) opens Terminal in a work folder (`~/Zeus Playbooks/<date> <title>/`
  with `PLAYBOOK.md` and `frames/`); Install Claude Code with Homebrew when it is missing; Copy for any AI; install as a
  Claude Code skill; the note `Sources/YouTube/Playbooks/Playbook - <date> - <title>.md` (frames in `attachments/`),
  linked from the video's note.
- **Only for tutorials, quickly.** A check with the local AI comes first (seconds): a talk, an opinion or news has
  nothing to follow and stops there ("Not a tutorial.", with the reason), before minutes of downloading and looking at
  frames; "Make it anyway" (`--force`, `force: true`) skips it. Frames of the presenter on camera (no text on them) say
  little: at most 8 of them are described; screens, slides and code always are.
- Agents: `zeus playbook <id> [--make] [--force] [--json] [--skill] [--run claude|codex]`,
  `youtubezeus://playbook?video=<id>` (`&make=1`, `&force=1`, `&run=claude`), MCP tools `get_playbook` and
  `make_playbook`, `playbook:` in `item_status`. While a playbook is made, `zeus playbook`, `get_playbook` and
  `item_status` say the step the app is on; after a failed try they say why instead of starting again.
- Settings › Local AI › Playbooks (vision model, frames to look at, the agents' folder); the setup page and
  `zeus setup` show the vision model. The local AI model list in Settings is the setup page's list.
- Fixed: a model answer with a `}` before its first `{` could crash the app while reading it (summaries, Ask, skills).
- Fixed: `zeus setup --json` printed sizes like 18.600000000000001.

## 3.1 — 2026-10-05

- **Setup assistant**: a new Mac no longer needs Terminal. The first time Zeus opens (and whenever yt-dlp is missing),
  a setup page checks everything and installs what is missing, with live progress for each step: Apple's command-line
  tools, Homebrew (its official installer; Apple asks for the password once, or "Use Terminal instead"), yt-dlp and
  Deno, ffmpeg, whisper.cpp, Ollama (installed, started as a service, and waited for) and Whisper's speech model.
  "Set up everything" runs it all in order; each step also has its own button and the details show every line.
- **The local AI model for this Mac**: Zeus reads the chip, memory, GPU cores and free disk, checks Ollama's library
  for each model (real download size) and preselects the strongest one that runs comfortably: from Qwen3 1.7B on
  small Macs to Qwen3 14B, Gemma 3 27B or Qwen3 30B (MoE) on big ones. Heavier models are marked, models that do not
  fit are disabled, and the chosen one becomes the model of every local AI feature.
- Also from Settings › Tools ("Setup assistant"), Settings › Local AI ("Choose the best model for this Mac"), a
  "Set up tools" button on a video that failed because a tool is missing, `youtubezeus://setup` and
  `zeus setup [--install] [--model <tag>] [--json]` for agents.

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
