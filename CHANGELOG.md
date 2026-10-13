# Changelog

Every feature is shipped with `./scripts/ship.sh "<message>"` (build, commit, push to GitHub, vault snapshot).
Newest first.

## 4.2 — Rebuild (2026-10-13)

- **One button.** The row of buttons under a video (Copy for AI, Summarize, Make skills, Open Note, Polish, Read
  the screen, Listen with Whisper) is now **Do everything**, **Copy the rebuild message** and a **More** menu that
  keeps every single action.
- **Rebuild, the final core** (new last tab, also at the end of the Everything page): one message for Claude,
  ChatGPT, Gemini, Mistral, DeepSeek or a local model to **reverse-engineer and redo what the video shows**. It holds
  the rules (transcript is data, mark UNKNOWN instead of guessing, cite [mm:ss], ask before money, accounts or
  deletions, reimplement instead of copying), how to work (understand → inventory → reverse-engineer → plan → build
  → verify against each key moment → report), the source, the summary and key moments, the chapters, the playbook
  (goal, result, tools, accounts, every step with command, code, file, link, prompt and expected result, checks,
  fixes), code and commands read on screen, GitHub repos and tools named, and the transcript (whole for Claude,
  Gemini and ChatGPT; the moments that matter for Mistral, DeepSeek and local models). Copy it, or Save to Second
  Brain (`Sources/YouTube/Rebuild/`). Built on this Mac without any AI.
- **REA — Reverse Engineer Anything** (github.com/morluto/rea, MIT, local): the message tells the AI how to use it on
  existing apps, sites and binaries; **Add evidence with REA…** runs `npx rea-agents analyze <app|folder|.asar|binary>
  --json` on this Mac and puts its evidence in the message; **REA › Copy “npx rea-agents setup”** gives REA to your
  agents. Needs Node.js.
- For agents: `zeus rebuild <id> [--for chatgpt|mistral|deepseek|gemini|local] [--save]`, MCP `get_rebuild`,
  `youtubezeus://rebuild?video=<id>`.

## 4.1 — Everything (2026-10-13)

- **Everything**, the new first tab of every item: one button, **Do everything**, makes all that is still missing —
  the summary, the polished text, the skills, the playbook and the text on screen (for things with a picture) —
  free, on this Mac, each in its own lane. Below it, every part on one page: Summary, Playbook, Skills, On screen,
  Info, then the transcript, each with "Open alone" for its own tab. It says what is still to make.
  Link: `youtubezeus://everything?video=<id>`.

## 4.0 — Make it me (2026-10-12)

Two Effects branches joined: the one-button job and The Swap (`zeus-3.8-effects`) and the parts map (`zeus-3.9`), on
top of the 3.9 posts, YouTube pages and Spotify below.

- **Make it me** (`effects/MAKE-IT-ME.md`): one button in Effects. Pick the clip (library or file, up to 10 s), say
  who you are (a **self-video** — Zeus keeps the sharpest frames per angle with open eyes and takes your voice from it —
  a `.c101face` face pack, or photos), choose the voice (from the self-video, a file, or keep the original), the
  quality, **Remove the hat or cap** (a hat found on the head is replaced with hair: BiSeNet mask, VACE, hi-res
  composite — about an hour), tick the consent and click **Make it me**: prepare → references → approve (automatic) →
  face swap (natural) → hat → voice → final, with the step names and the overall progress; then play the result, the
  side-by-side `compare.mp4`, Save to Movies. A stopped job continues where it was (`Continue`). CLI:
  `zeus effects makeme …` (resumable with the same `--job`, `zeus effects cancel --job …`).
- **Do jobs from The Swap** (Settings › Extras): the iPhone app writes a `<stamp>-<name>.swapjob` folder into iCloud
  Drive › The Swap › Jobs (or the NAS); Zeus, while open, does it on this Mac (`zeus effects watch`): `status.json`
  every few seconds, then `result.mp4`, `compare.mp4`, `preview.jpg` and `done`; a `cancel` marker stops it. No server,
  no port: only a shared folder. Such jobs appear in the Effects jobs list like the others.
- **Parts map** (`effects/PARTS.md`, `zfx/parts.py`): a richer control for the character replacement — every region
  of every frame gets its own tone: the body parts from the pose (head, neck, torso, each arm, hand, thigh, shin and
  foot), the face in detail from BiSeNet (skin, brows, eyes, nose, mouth, ears, hair, hat, glasses), the gaze (a dot
  on each iris and an arrow, blinks), the things you name ("microphone, laptop, cap" — CLIPSeg, free and local) and
  three background depth bands. It writes `parts.mp4` (the map), `parts.npz` (one legend id per pixel), `parts.json`
  (legend, per-frame stats, gaze), `control-parts.mp4` (a control video for the full replace: the depth tinted per
  part, the original outside the person) and `preview-parts.jpg`. Measured: 28 s for 4 s of video, 0.5 GB.
  `python -m zfx.parts_cli --job <dir> [--things "…"] [--gaze]`.
- **Parts map, wired in.** A mode **Depth + parts** in Effects (depth + pose + the map, gaze on, with a "Things to
  find" field), and `prepare --parts [--things "…"] [--gaze]` on any depth mode; the review pane shows
  `preview-parts.jpg`, plays `parts.mp4` and `control-parts.mp4` and sums up what the map found (classes, things
  found, gaze — `parts_info` in `job.json`); **Full replace** follows `control-parts.mp4` for such a job
  (`generate --control parts` for any other, `--control control` to go back); Make it me has a **Parts map** toggle
  (off by default) that adds `--parts` to its prepare step. Approval hashes the five files (required in that mode);
  `zeus effects parts --job <dir>` builds the map for a job already prepared; `doctor` reports CLIPSeg and the face
  parser. Engine 1.2.0, tests in `effects/tests/test_parts_wiring.py`.

## 3.9 — 2026-10-11

- **YouTube pages: up to 20 YouTube accounts.** The YouTube window has pages (toolbar menu, and the list at the top
  of the YouTube panel): Page 1, Page 2… each with its own Google sign-in. Rename them ("Personal YouTube",
  "Work YouTube", "Third channel"), switch with one click, add up to 20, remove one with its sign-in. The page in
  front is the one used for Import my subscriptions, Watch Later, Liked videos and members-only videos. The first page
  is your existing sign-in. The library stays one. Link: `youtubezeus://youtube?page=2` (or a name).
- **X, Instagram, Facebook and Threads posts.** Paste (or drop) a post link in the Eat bar: Zeus keeps the words of
  the post and, when it has a video or audio, what is said in it (Whisper on this Mac). yt-dlp reads the post; the
  page's own preview tags are the fallback. Posts get the same note, search, summary and translation as videos.
- **Settings › Accounts:** sign in to X, Instagram, Facebook or Threads inside Zeus, for private accounts and posts
  shown only to members (`social-cookies.txt`, private, read only by yt-dlp on this Mac). Public posts need nothing.
- **Spotify podcasts.** A Spotify episode or show link is found by name in Apple's free podcast directory and eaten
  from its public feed (Spotify never gives the audio). Music and Spotify-only shows cannot be eaten.
- `zeus eat <post link>` and the MCP `eat` tool take the same links.

## 3.8 — Effects (free, local Genjutsu)

2026-10-10

- **Effects** (new in the sidebar). A free, local-only version of Sirio Berati's open-source Genjutsu workflow (MIT):
  keep how the people in a clip move, replace who they are with your photo, keep the original sound. Pick a video you
  ate (or drop a video file), choose the start and up to 10 seconds, and a mode (Depth · Depth + face mesh · Face mesh).
  **Prepare** makes the depth (Video-Depth-Anything-Small), the people mask (Apple Vision) and the face mesh (MediaPipe)
  on this Mac; **Review** shows the contact sheet and the control, depth and mask videos; **Approve this mask** records a
  SHA-256 fingerprint of every prepared file. **Generate** takes one or two reference photos, an optional prompt, the
  consent box ("These photos show me or people who agreed") and an engine: **Face swap · fast** (FaceFusion) or
  **Full replace · slow** (Wan2.1-VACE-1.3B, about 19 GB, added with one button). The result keeps the clip's complete
  original soundtrack and says "AI-generated with YouTube Zeus Effects" in its metadata: play it, Reveal in Finder, or
  **Save to Movies** (`~/Movies/YouTube Zeus Effects/`). Earlier jobs are listed next to the tab. No paid API, no key,
  no cloud engine. The engine (`effects/`, contract in `effects/SPEC.md`) ships inside the app; its Python, weights and
  jobs live in `~/Library/Application Support/YouTube Zeus/Effects/`.
- **For agents:** `zeus effects <doctor|prepare|approve|generate|status|list|setup> [args…]` passes everything to the
  engine and streams its JSON lines; `youtubezeus://effects?video=<id>` opens the tab with that video. The agent guide
  has a new Effects section (consent is always the user's).
- **Measured on a 16 GB M2 Pro (2026-10-11).** Prepare: ~40 s for 4 s of video, 5 GB peak. Full replace at 360p,
  33 frames, 20 steps: ~16 min per 2 s, 5 GB peak (fp16 transformer copy streamed to the GPU block by block, prompt
  encoding on the CPU in its own process, scheduler step on the CPU because UniPC diverges on MPS). 480p fits but is
  about twice as slow. Faces shown on screens or posters are left out of the mask (`--min-person-ratio`).
- **Fix:** opening a video no longer crashes on macOS 27: SwiftUI's `VideoPlayer` aborts there, so every player (Effects
  and the player bar of podcasts and your files) now uses AVKit's AppKit player view.
- **Face packs.** Generate › **Use a face pack…** takes a `.c101face` folder from Cinema 101's *Scan my face* (the
  iPhone's TrueDepth camera, several angles of your face; packs in Downloads and iCloud Drive are offered in the menu).
  The engine refuses a pack saved without consent or pointing outside its folder, then uses up to 5 of its sharpest
  frames (frontal, about ±20° and ±40°, a smile; 2 for full replace), shown as thumbnails; the photos become
  optional and the job records which frames were used. Agents: `zeus effects generate … --ref-pack <pack>` and
  `zeus effects facepack --pack <pack> --json`.
- **Full replace at the clip's own resolution, also 4K and camera Log.** VACE now generates a crop around the person
  (one steady box per window, the person filling the 640x368 canvas) instead of the whole frame; the crop is upscaled
  with Real-ESRGAN (free, BSD-3, runs natively on Apple Silicon; `setup-engines.sh --upscaler`, `--upscale fast|best|
  lanczos`), colour- and grain-matched to the original around it and feathered into the original frames at their own
  size, frame rate and colour (4K stays 4K, 10-bit stays 10-bit), the original audio untouched. Log clips (Apple Log
  and the other profiles Cinema 101 knows, found by `c101 probe` or the colour tags) are shown to the model in Rec.709
  and put back into Log. Measured on a 1080p clip: the person comes out visibly sharper with no visible edge, the
  composite takes 8 s (40 s for 4K); VACE itself is unchanged (~21 min per 2 s, 5 GB) and still does not reproduce a
  *different* person's identity well.
- **Face swap is about 20 times faster**: 33 s instead of 11 min for 3 s of 1080p, because the face sharpening (GFPGAN)
  is now off by default; tick **Sharpen the face** to use it. **Lips stay in sync**: the result is no longer stretched
  to the video track's length (it drifted ~0.1 s by the end of 3 s). **Full replace keeps the background sharp**: only
  the person comes from the 360p generation, pasted inside the mask over the original frames.
- **Whole-body motion capture.** Two new modes, **Pose** (a coloured OpenPose-style skeleton on black guides the
  model, so the new subject can have any shape) and **Depth + pose** (grey depth with the skeleton on top). Every frame
  gets the body (33 points, MediaPipe Pose heavy), both hands (21 points each), the face mesh, smoothed (One-Euro) with
  the raw values kept, and is saved as data in the job: `motion.json` (documented schema), `motion.bvh` (a 19-joint
  body skeleton for Blender, or Unreal Engine through an FBX and the IK Retargeter onto Manny) and `pose.mp4`. The
  contact sheet gets a pose column; approval also fingerprints the motion files. `--motion` adds the capture to the
  other modes. Agents: `zeus effects motion --job <dir> --json` (frames with a body and hands, visibility, limbs).

## 3.7 — 2026-10-08

- **Translate.** The new Translate button of a video shows its text in another language: pick one of every language
  your Mac knows (searchable list, suggestions first). YouTube translates the spoken words in seconds, for free, in
  about 160 languages; podcasts, your own files, or a language YouTube lacks are translated by the local AI. Each
  translation is kept with the video: Copy it, or Save as text file. Link: `youtubezeus://translate?video=<id>&lang=fr`.
- **All channel in another language.** On a channel, *Also in…* keeps every text file in a second language too
  (`Text files - French/`, same numbers, plus its own file with all of them); new videos are translated as they come.

## 3.6.1 — 2026-10-08

- **The words as spoken, not a translation.** When YouTube lists the original speech track (`it-orig`), Zeus now
  always takes it: before, an Italian video marked as English could come in as YouTube's English machine
  translation. AI-dubbed videos (YouTube reports the dub as "en-us" and lists "en-US-orig" next to "it-orig")
  now come in as what was really said (the language of the title and description picks the speech track when
  YouTube lists several).
  `youtubezeus://allchannel?url=…&again=1` eats a channel's text-only videos again.
- **Stop getting the text** (channel page, or `&stop=1`): the videos still waiting leave the line; the files made stay.

## 3.6 — 2026-10-08

- **All channel.** In a channel (followed, or Library › YouTube channels) press **Get all the text** / **All channel**:
  Zeus lists every video of the channel and takes only its words (captions first, Whisper when there are none; no
  polishing, no summary). Each video becomes a text file in `<Second Brain>/<Channel>/Text files/`, numbered from the
  first upload (`001 - 2019-05-12 - Title.txt`), plus `000 - <Channel> - ALL VIDEOS.txt` with every video together.
  Both are rewritten each time a video of the channel comes in, so the full file is always complete.
  Buttons: Open the full text, Show the folder, Stop updating. Link: `youtubezeus://allchannel?url=<channel link>`.

## 3.5 — 2026-10-08

- **The Library has sub-categories** in the sidebar (fold it with the arrow): **Videos** (everything), **YouTube
  channels** (one row per channel of the videos you ate, with its count), **Favorites** and **Deleted videos** (the
  Trash of 3.4, renamed).
- **Favorites.** Star a video with the star at the top of a video, or right-click › Add to Favorites; starred videos
  show a yellow star in every list. `youtubezeus://open?view=favorites` opens them.
- In a channel or Favorites, the trash button moves only the videos shown there.

## 3.4 — 2026-10-08

- **A Trash.** Delete (menu, ⌫, the trash button in Eating now, several at once) moves videos to the Trash in the
  sidebar instead of erasing them: their work stops and they leave the library, search and lists. In the Trash:
  Restore, Delete Forever, Empty Trash. A watched channel does not bring a trashed video back; eating it yourself does.
- **Move all to the Trash** (Library toolbar, `youtubezeus://trash?all=1`): a clean, empty library that can be undone.

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
