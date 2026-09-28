import AppKit
import SwiftData
import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    var body: some View {
        TabView {
            Tab("Eating", systemImage: "fork.knife") { EatingSettings() }
            Tab("Channels", systemImage: "dot.radiowaves.left.and.right") { ChannelSettings() }
            Tab("AI", systemImage: "apple.intelligence") { AISettings() }
            Tab("Local AI", systemImage: "wand.and.stars") { LocalAISettings() }
            Tab("AI hand-off", systemImage: "square.and.arrow.up.on.square") { HandOffSettings() }
            Tab("Skills & cloud AI", systemImage: "sparkles.rectangle.stack") { CodexSettings() }
            Tab("Tools", systemImage: "wrench.and.screwdriver") { ToolSettings() }
        }
        .frame(width: 660, height: 560)
        .tint(.zeus)
    }
}

private struct EatingSettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app
    @State private var modelReady = false
    @State private var downloading: Double?
    @State private var githubToken = ""
    @State private var hasGitHubToken = KeychainStore.read("github")?.isEmpty == false

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Captions") {
                TextField("Preferred languages", text: $settings.preferredLanguages)
                Text("Zeus takes the captions in the video's own language first. These languages (comma separated codes) are used when a video has several caption tracks and none in its own language.")
                    .font(.caption).foregroundStyle(.secondary)
                Stepper("Eat \(settings.maxParallel) video\(settings.maxParallel == 1 ? "" : "s") at a time", value: $settings.maxParallel, in: 1...4)
            }
            Section("When there are no captions") {
                Toggle("Listen with Whisper on this Mac", isOn: $settings.useWhisperFallback)
                Picker("Whisper model", selection: $settings.whisperModel) {
                    ForEach(WhisperModel.allCases) { Text($0.label).tag($0.rawValue) }
                }
                .disabled(!settings.useWhisperFallback)
                HStack {
                    if let downloading {
                        ProgressView(value: downloading)
                        Text(downloading.formatted(.percent.precision(.fractionLength(0)))).monospacedDigit()
                    } else if modelReady {
                        Label("Model downloaded", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    } else {
                        Text("The model downloads the first time it is needed.").foregroundStyle(.secondary)
                        Spacer()
                        Button("Download now") { download() }
                    }
                }
                .font(.callout)
            }
            Section("Second Brain") {
                Toggle("Save each transcript as a note", isOn: $settings.secondBrainEnabled)
                HStack {
                    Text(settings.secondBrainFolder).font(.callout.monospaced()).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Choose…") { chooseFolder() }
                }
                Label(app.exporter.folderReachable ? "Folder reachable" : "Not reachable now — notes are saved when the volume is connected.",
                      systemImage: app.exporter.folderReachable ? "checkmark.circle.fill" : "externaldrive.badge.exclamationmark")
                    .foregroundStyle(app.exporter.folderReachable ? .green : .orange)
                    .font(.callout)
            }
            Section("GitHub links") {
                Toggle("Find and check GitHub repositories linked in videos", isOn: $settings.githubEnabled)
                HStack {
                    Text(settings.githubURL.path).font(.callout.monospaced()).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Choose…") { chooseGitHubFolder() }
                }
                HStack {
                    SecureField(hasGitHubToken ? "Token saved in the Keychain" : "GitHub token (optional)", text: $githubToken)
                    Button("Save") {
                        KeychainStore.save(githubToken.trimmingCharacters(in: .whitespacesAndNewlines), account: "github")
                        githubToken = ""
                        hasGitHubToken = KeychainStore.read("github")?.isEmpty == false
                        app.show(hasGitHubToken ? "GitHub token saved in the Keychain (used after the next launch)." : "GitHub token removed.")
                    }
                }
                Text("Each repository is checked through the GitHub API (exists, last activity, license, security advisories, links back to the video) and gets a note in this folder; its facts block is rewritten, your own notes are kept. Without a token GitHub allows about 15 repositories an hour; a token with no scopes (or `gh auth login`) raises that.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .task(id: settings.whisperModel) { refreshModel() }
    }

    private func refreshModel() {
        let model = WhisperModel(rawValue: settings.whisperModel) ?? .turbo
        modelReady = WhisperTranscriber(whisperPath: "", ffmpegPath: "", model: model).hasModel
    }

    private func download() {
        let model = WhisperModel(rawValue: settings.whisperModel) ?? .turbo
        downloading = 0
        Task {
            do {
                try await WhisperTranscriber(whisperPath: "", ffmpegPath: "", model: model).ensureModel { value in
                    Task { @MainActor in downloading = value }
                }
                app.show("Whisper model ready.")
            } catch {
                app.show(error.localizedDescription, error: true)
            }
            downloading = nil
            refreshModel()
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = settings.secondBrainURL.deletingLastPathComponent()
        if panel.runModal() == .OK, let url = panel.url { settings.secondBrainFolder = url.path }
    }

    private func chooseGitHubFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = settings.githubURL.deletingLastPathComponent()
        if panel.runModal() == .OK, let url = panel.url { settings.githubFolder = url.path }
    }
}

private struct ChannelSettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app
    @Query(sort: \Channel.title) private var channels: [Channel]
    @State private var importing = false
    @State private var loginItem = false

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Watching") {
                Picker("Check channels every", selection: $settings.pollMinutes) {
                    ForEach([15, 30, 60, 120, 360], id: \.self) { minutes in
                        Text(minutes < 60 ? "\(minutes) minutes" : "\(minutes / 60) hour\(minutes == 60 ? "" : "s")").tag(minutes)
                    }
                }
                Toggle("Skip Shorts", isOn: $settings.skipShorts)
                Toggle("Notify me when a new upload is eaten", isOn: $settings.notifyWhenEaten)
                Toggle("Keep Zeus in the menu bar", isOn: $settings.keepInMenuBar)
                Toggle("Open at login", isOn: Binding(get: { loginItem }, set: {
                    app.launchAtLogin = $0
                    loginItem = app.launchAtLogin
                }))
                Text("Zeus watches while it is open (even with the window closed). Channels are read from their public feeds: no Google account needed.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Import") {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Import your YouTube subscriptions")
                        Text("From Google Takeout › YouTube › subscriptions › subscriptions.csv").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(importing ? "Importing…" : "Choose file…") { importCSV() }.disabled(importing)
                }
                Link("Open Google Takeout", destination: URL(string: "https://takeout.google.com/settings/takeout/custom/youtube")!)
            }
            Section("Following \(channels.count) channel\(channels.count == 1 ? "" : "s")") {
                ForEach(channels) { channel in
                    HStack {
                        Avatar(url: channel.avatarURL, title: channel.title, size: 22)
                        Text(channel.title)
                        Spacer()
                        Toggle("Auto-eat", isOn: Binding(get: { channel.autoEat }, set: {
                            channel.autoEat = $0
                            try? app.context.save()
                        }))
                        .toggleStyle(.switch).controlSize(.mini).labelsHidden()
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onAppear { loginItem = app.launchAtLogin }
    }

    private func importCSV() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.commaSeparatedText, .plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        importing = true
        Task {
            do {
                let count = try await app.watcher.importSubscriptions(from: url)
                app.show("Now following \(count) more channel\(count == 1 ? "" : "s").")
            } catch {
                app.show(error.localizedDescription, error: true)
            }
            importing = false
        }
    }
}

private struct AISettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app

    private let languages: [(String, String)] = [
        ("auto", "Same as the video"), ("fr", "French"), ("en", "English"), ("it", "Italian"),
        ("es", "Spanish"), ("de", "German"), ("pt", "Portuguese"),
    ]

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Free by default") {
                Label("Zeus uses only free AI: Apple Intelligence on this Mac and the open-source local AI (Ollama, \(settings.polishModel)). Nothing leaves the Mac. Codex / OpenAI are optional and off.",
                      systemImage: "lock.shield")
                    .font(.callout)
            }
            Section("Apple Intelligence") {
                Label(app.summarizer.availabilityMessage,
                      systemImage: app.summarizer.isAvailable ? "checkmark.circle.fill" : "exclamationmark.circle")
                    .foregroundStyle(app.summarizer.isAvailable ? .green : .orange)
                Picker("Summaries with", selection: $settings.summaryEngine) {
                    Text("Apple Intelligence when ready, else the local AI (free)").tag("auto")
                    Text("Local AI only (free, open source)").tag("local")
                    Text("Apple Intelligence only").tag("apple")
                    Text("Codex (ChatGPT plan — optional, uses your limit)").tag("codex")
                }
                if app.engine.codexPaused, let until = app.engine.codexPausedUntil {
                    Label("Codex reached your ChatGPT usage limit: Zeus uses it again after \(until.formatted(date: .omitted, time: .shortened)) and summarizes with the local AI meanwhile.",
                          systemImage: "hourglass")
                        .font(.caption).foregroundStyle(.orange)
                }
                Toggle("Summarize every eaten video", isOn: $settings.autoSummarize)
                Picker("Summary language", selection: $settings.summaryLanguage) {
                    ForEach(languages, id: \.0) { Text($0.1).tag($0.0) }
                }
                Text("Apple Intelligence and the local AI are free and private; long videos are read in parts. Apple Intelligence turns on in System Settings › Apple Intelligence & Siri; until its model is ready, the local AI writes the summaries. Codex is only used if you pick it here and allow it in Settings › Skills & cloud AI; it sends the transcript to OpenAI and uses your ChatGPT plan's limit.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

private struct CodexSettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app
    @State private var key = ""
    @State private var hasKey = false

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Engines") {
                Picker("Make skills with", selection: $settings.skillEngine) {
                    Text("Local AI on this Mac (free, open source)").tag("local")
                    Text("Codex CLI — your ChatGPT plan (optional)").tag("codex")
                    Text("OpenAI API key (optional, paid)").tag("openai")
                }
                .pickerStyle(.radioGroup)
                Picker("Ask your brain with", selection: $settings.askEngine) {
                    Text("Local AI on this Mac (free)").tag("local")
                    Text("Codex (optional)").tag("codex")
                }
                Text("The local AI (\(settings.polishModel) with Ollama) is free and never sends anything out. It reads about 10k tokens at once, so skills from very long videos come from their first part.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Cloud AI (optional, off by default)") {
                Toggle("Allow sending transcripts to OpenAI (Codex or the API)", isOn: $settings.openAIConsent)
                Text("Only needed if you pick Codex or the OpenAI API above or in Settings › AI. Codex uses your ChatGPT plan's limit — the same one as your own Codex work.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if settings.skillEngine == "codex" {
                Section("Codex CLI") {
                    Label(app.compiler.codexPath ?? "Not found — install with: npm i -g @openai/codex",
                          systemImage: app.compiler.codexPath == nil ? "xmark.circle.fill" : "checkmark.circle.fill")
                        .foregroundStyle(app.compiler.codexPath == nil ? .red : .green)
                    TextField("Model (empty = Codex default)", text: $settings.codexModel)
                    Text("Runs `codex exec` read-only in an empty folder. Uses your ChatGPT plan limits, no API billing.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            } else if settings.skillEngine == "openai" {
                Section("OpenAI API") {
                    HStack {
                        SecureField(hasKey ? "Key saved in the Keychain" : "sk-…", text: $key)
                        Button("Save") {
                            KeychainStore.save(key.trimmingCharacters(in: .whitespacesAndNewlines), account: "openai")
                            key = ""
                            hasKey = app.compiler.hasKey
                            app.show(hasKey ? "Key saved in the Keychain." : "Key removed.")
                        }
                        if hasKey {
                            Button("Remove", role: .destructive) {
                                KeychainStore.save("", account: "openai")
                                hasKey = false
                            }
                        }
                    }
                    TextField("Model", text: $settings.openAIModel)
                    Text("Requests are sent with store=false. API billing is separate from ChatGPT and Codex subscriptions.")
                        .font(.caption).foregroundStyle(.secondary)
                    Stepper("Daily limit: \(settings.dailyTokenLimit.formatted()) tokens", value: $settings.dailyTokenLimit, in: 50_000...2_000_000, step: 50_000)
                    Text("Used today: \(settings.tokensUsedToday().formatted()) tokens").font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Publishing") {
                HStack {
                    Text(settings.publishFolder).font(.callout.monospaced())
                    Spacer()
                    Menu("Change") {
                        Button("Codex (~/.codex/skills)") { settings.publishFolder = "~/.codex/skills" }
                        Button("Claude Code (~/.claude/skills)") { settings.publishFolder = "~/.claude/skills" }
                        Button("Other folder…") {
                            let panel = NSOpenPanel()
                            panel.canChooseDirectories = true
                            panel.canChooseFiles = false
                            panel.canCreateDirectories = true
                            if panel.runModal() == .OK, let url = panel.url { settings.publishFolder = url.path }
                        }
                    }
                    .fixedSize()
                }
                Picker("Skill language", selection: $settings.skillLanguage) {
                    ForEach(["English", "French", "Italian", "Spanish"], id: \.self) { Text($0).tag($0) }
                }
                Text("Nothing is published without your approval. Previous versions are kept in ~/Library/Application Support/YouTube Zeus/Skill History.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear { hasKey = app.compiler.hasKey }
    }
}

private struct ToolSettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app
    @State private var versions: [String: String] = [:]
    @State private var updating = false

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Command-line tools") {
                toolRow("yt-dlp", path: $settings.ytdlpPath)
                toolRow("ffmpeg", path: $settings.ffmpegPath)
                toolRow("whisper-cli", path: $settings.whisperPath)
                Text("Leave a path empty to find the tool automatically (Homebrew).").font(.caption).foregroundStyle(.secondary)
            }
            Section("yt-dlp") {
                HStack {
                    Text("YouTube changes often. If eating starts failing, update yt-dlp.")
                    Spacer()
                    Button(updating ? "Updating…" : "Update yt-dlp") { update() }.disabled(updating)
                }
            }
            Section("Data") {
                Button("Show the library folder") { NSWorkspace.shared.open(AppFolders.support) }
            }
        }
        .formStyle(.grouped)
        .task { await loadVersions() }
    }

    private func toolRow(_ name: String, path: Binding<String>) -> some View {
        let found = ToolLocator.find(name, override: path.wrappedValue)
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Image(systemName: found == nil ? "xmark.circle.fill" : "checkmark.circle.fill")
                    .foregroundStyle(found == nil ? .red : .green)
                Text(name).font(.body.monospaced())
                Spacer()
                Text(versions[name] ?? (found ?? "not found")).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            TextField("Automatic", text: path).font(.caption.monospaced())
        }
    }

    private func loadVersions() async {
        if let path = ToolLocator.find("yt-dlp", override: settings.ytdlpPath) {
            versions["yt-dlp"] = await YTDLP(executable: path).version()
        }
        if let path = ToolLocator.find("ffmpeg", override: settings.ffmpegPath),
           let output = try? await ProcessRunner.run(path, ["-version"]) {
            versions["ffmpeg"] = output.stdoutString.split(separator: " ").dropFirst(2).first.map(String.init)
        }
        if let path = ToolLocator.find("whisper-cli", override: settings.whisperPath) {
            versions["whisper-cli"] = path
        }
    }

    private func update() {
        guard let brew = ToolLocator.find("brew", override: "") else {
            app.show("Homebrew was not found.", error: true)
            return
        }
        updating = true
        Task {
            do {
                _ = try await ProcessRunner.check(brew, ["upgrade", "yt-dlp"])
                app.show("yt-dlp is up to date.")
            } catch {
                app.show(error.localizedDescription, error: true)
            }
            updating = false
            await loadVersions()
        }
    }
}

private struct LocalAISettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app
    @State private var models: [String] = []
    @State private var running = false
    @State private var working = false

    private let suggestions = [
        ("qwen3:4b-instruct", "Qwen3 4B Instruct — best balance, multilingual (2.5 GB)"),
        ("gemma3:4b", "Gemma 3 4B — good in European languages (3.3 GB)"),
        ("qwen3:8b", "Qwen3 8B — best quality, needs 8 GB free (5.2 GB)"),
        ("llama3.2:3b", "Llama 3.2 3B — fast, English first (2 GB)"),
    ]

    var body: some View {
        @Bindable var settings = settings
        Form {
            Section("Polish transcripts on this Mac") {
                Toggle("Polish auto-captions and Whisper text automatically", isOn: $settings.polishEnabled)
                Toggle("Also polish captions written by the channel", isOn: $settings.polishCaptionsToo)
                Text("A small AI running in Ollama fixes punctuation, capitals and misheard words. It never translates or shortens. The original stays one click away. Nothing leaves your Mac.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Model") {
                Picker("Model", selection: $settings.polishModel) {
                    ForEach(suggestions, id: \.0) { Text($0.1).tag($0.0) }
                    ForEach(models.filter { m in !suggestions.contains(where: { $0.0 == m }) }, id: \.self) { Text($0).tag($0) }
                }
                HStack {
                    Label(running ? "Ollama is running" : (app.polisher.isInstalled ? "Ollama is installed (starts when needed)" : "Ollama is not installed"),
                          systemImage: running ? "checkmark.circle.fill" : "circle.dashed")
                        .foregroundStyle(running ? .green : .secondary)
                    Spacer()
                    if let progress = app.polisher.downloadProgress {
                        ProgressView(value: progress).frame(width: 120)
                        Text(progress.formatted(.percent.precision(.fractionLength(0)))).monospacedDigit()
                    } else if models.contains(settings.polishModel) || models.contains(settings.polishModel + ":latest") {
                        Label("Downloaded", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    } else {
                        Button(working ? "Starting…" : "Download model") {
                            working = true
                            Task {
                                do { try await app.polisher.prepare(); app.show("Local AI ready.") }
                                catch { app.show(error.localizedDescription, error: true) }
                                working = false
                                await reload()
                            }
                        }
                        .disabled(working || !app.polisher.isInstalled)
                    }
                }
                .font(.callout)
                if !app.polisher.isInstalled {
                    Link("Get Ollama", destination: URL(string: "https://ollama.com/download")!)
                }
            }
        }
        .formStyle(.grouped)
        .task { await reload() }
    }

    private func reload() async {
        running = await app.polisher.client.isRunning()
        models = running ? await app.polisher.client.installedModels() : []
    }
}

private struct HandOffSettings: View {
    @Environment(AppSettings.self) private var settings
    @Environment(AppModel.self) private var app
    @State private var command: URL?
    @State private var refresh = 0

    var body: some View {
        Form {
            Section("zeus command (for terminals and AI agents)") {
                HStack {
                    Label(command?.path ?? "Not installed", systemImage: command == nil ? "terminal" : "checkmark.circle.fill")
                        .foregroundStyle(command == nil ? Color.secondary : Color.green)
                        .font(.callout.monospaced())
                    Spacer()
                    Button(command == nil ? "Install" : "Reinstall") {
                        do {
                            command = try HandOff.installCommand()
                            app.show("zeus is ready: try “zeus help” in Terminal.")
                        } catch {
                            app.show("Could not install zeus: \(error.localizedDescription)", error: true)
                        }
                    }
                }
                Text("zeus eat <link> --save · zeus get <link> · zeus search <words> · zeus ask <question> · zeus repos · zeus open <link> · zeus guide. Claude Code, Codex, Gemini CLI or any agent with a terminal can eat videos and read your YouTube brain.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Skill for AI agents") {
                ForEach(HandOff.skillTargets, id: \.name) { target in
                    let installed = HandOff.isInstalled(in: target.folder)
                    HStack {
                        Image(systemName: installed ? "checkmark.circle.fill" : "circle").foregroundStyle(installed ? .green : .secondary)
                        VStack(alignment: .leading) {
                            Text(target.name)
                            Text(target.folder.path.replacingOccurrences(of: FileManager.default.homeDirectoryForCurrentUser.path, with: "~") + "/youtube-zeus")
                                .font(.caption.monospaced()).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(installed ? "Update" : "Install") {
                            do {
                                try HandOff.install(in: target.folder)
                                refresh += 1
                                app.show("youtube-zeus skill installed for \(target.name).")
                            } catch {
                                app.show(error.localizedDescription, error: true)
                            }
                        }
                    }
                }
                .id(refresh)
                Text("A copy of the skill and of the instructions is also kept in the Second Brain (Sources/YouTube/_For AI).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Guide for AI agents") {
                HStack {
                    Text("How Zeus works and every way to connect: commands, youtubezeus:// links, notes, GitHub checks. Rewritten in the Second Brain at each launch.")
                        .font(.callout)
                    Spacer()
                    Button(BrainLinks.openLabel) {
                        AgentGuide.writeToBrain(settings: settings)
                        app.openInBrain(app.agentGuideURL)
                    }
                    Button("Copy") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(AgentGuide.markdown(settings: settings, forVault: false), forType: .string)
                        app.show("Guide copied — paste it into any AI.")
                    }
                }
                Text("Links from notes and other apps: youtubezeus://open?video=<id> · ?collection= · ?channel= · ?repo=<owner>/<repo> · ?view=github · youtubezeus://ask?q=… · youtubezeus://eat?url=…")
                    .font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                if !BrainLinks.obsidianInstalled {
                    Label("Obsidian is not installed on this Mac: notes open in the default Markdown app. Install Obsidian and open the Second Brain folder as a vault to jump straight to each page.",
                          systemImage: "info.circle")
                        .font(.caption).foregroundStyle(.orange)
                }
            }
            Section("Chat AIs without a terminal (Grok, GLM, Gemini, ChatGPT…)") {
                HStack {
                    Text("Paste these instructions once, then use “Copy for AI” on any video or collection.")
                    Spacer()
                    Button("Copy instructions") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(AIPack.universalInstructions, forType: .string)
                        app.show("Instructions copied.")
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onAppear { command = HandOff.installedCommand }
    }
}
