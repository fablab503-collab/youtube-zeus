import AppKit
import Foundation
import Observation
import ServiceManagement
import SwiftData

enum SidebarItem: Hashable {
    case eating
    case library
    case skills
    case channel(String)
}

struct Toast: Identifiable, Equatable {
    let id = UUID()
    let text: String
    let isError: Bool
}

struct ChannelOffer: Identifiable {
    let id = UUID()
    let channelID: String
    let title: String
    let latest: [PlaylistEntry]
}

@Observable
final class AppModel {
    let settings: AppSettings
    let context: ModelContext
    let summarizer: Summarizer
    let exporter: SecondBrainExporter
    let engine: EatEngine
    let watcher: ChannelWatcher
    let compiler: SkillCompiler

    var selection: SidebarItem? = .library
    var selectedVideoID: String?
    var selectedSkillID: UUID?
    var toast: Toast?
    var channelOffer: ChannelOffer?
    var isResolvingLink = false
    var focusEatBar = 0
    var initialTab: String?
    private var started = false

    static func makeContainer() -> ModelContainer {
        let url = AppFolders.support.appendingPathComponent("Library.store")
        let configuration = ModelConfiguration(url: url)
        do {
            return try ModelContainer(for: Video.self, Channel.self, SkillDraft.self, configurations: configuration)
        } catch {
            // Keep the old store aside instead of losing it, then start fresh.
            let backup = url.deletingLastPathComponent().appendingPathComponent("Library-\(Int(Date.now.timeIntervalSince1970)).store")
            try? FileManager.default.moveItem(at: url, to: backup)
            return try! ModelContainer(for: Video.self, Channel.self, SkillDraft.self, configurations: configuration)
        }
    }

    init(context: ModelContext) {
        let settings = AppSettings()
        let summarizer = Summarizer()
        let exporter = SecondBrainExporter(settings: settings)
        let engine = EatEngine(context: context, settings: settings, summarizer: summarizer, exporter: exporter)
        self.settings = settings
        self.context = context
        self.summarizer = summarizer
        self.exporter = exporter
        self.engine = engine
        self.watcher = ChannelWatcher(context: context, settings: settings, engine: engine, exporter: exporter)
        self.compiler = SkillCompiler(settings: settings)
    }

    func start() {
        guard !started else { return }
        started = true
        engine.resumeInterrupted()
        watcher.start()
        if settings.notifyWhenEaten { Notifier.requestPermission() }
        // Launch arguments for scripting and tests: -eat <link>, -selectVideo <id>
        let arguments = UserDefaults(suiteName: nil)
        if let link = arguments?.volatileDomain(forName: UserDefaults.argumentDomain)["eat"] as? String, !link.isEmpty {
            Task { await eat(link) }
        }
        if let link = arguments?.volatileDomain(forName: UserDefaults.argumentDomain)["eatWhisper"] as? String,
           case .video(let id) = YouTubeLink.parse(link) {
            engine.eatWithWhisper(id)
        }
        if let id = arguments?.volatileDomain(forName: UserDefaults.argumentDomain)["reeat"] as? String,
           let video = engine.video(id) {
            engine.retry(video)
        }
        if let id = arguments?.volatileDomain(forName: UserDefaults.argumentDomain)["compileSkills"] as? String,
           let video = engine.video(id) {
            compileSkills(video)
        }
        if let id = arguments?.volatileDomain(forName: UserDefaults.argumentDomain)["summarize"] as? String,
           let video = engine.video(id) {
            summarize(video)
        }
        let domain = arguments?.volatileDomain(forName: UserDefaults.argumentDomain) ?? [:]
        initialTab = domain["detailTab"] as? String
        if let id = domain["unfollow"] as? String, let channel = watcher.channel(id) {
            watcher.unfollow(channel)
        }
        if (domain["showSkills"] as? String) != nil {
            selection = .skills
            let drafts = (try? context.fetch(FetchDescriptor<SkillDraft>(sortBy: [SortDescriptor(\.createdAt, order: .reverse)]))) ?? []
            selectedSkillID = drafts.first?.id
        }
        if let id = arguments?.volatileDomain(forName: UserDefaults.argumentDomain)["selectVideo"] as? String {
            selectedVideoID = id
        }
        writeDiagnostics()
        NotificationCenter.default.addObserver(forName: NSWorkspace.didMountNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.exporter.retryPending(in: self.context)
            }
        }
    }

    /// A small status file for troubleshooting (Application Support/YouTube Zeus/diagnostics.json).
    func writeDiagnostics() {
        var info: [String: Any] = [
            "date": ISO8601DateFormatter().string(from: .now),
            "appleIntelligence": summarizer.availabilityMessage,
            "appleIntelligenceReady": summarizer.isAvailable,
            "codex": CodexLocator.path ?? "missing",
            "summaryEngine": settings.summaryEngine,
            "secondBrainReachable": exporter.folderReachable,
            "secondBrainFolder": settings.secondBrainFolder,
        ]
        for tool in ["yt-dlp", "ffmpeg", "whisper-cli"] {
            info[tool] = ToolLocator.find(tool, override: "") ?? "missing"
        }
        if let data = try? JSONSerialization.data(withJSONObject: info, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: AppFolders.support.appendingPathComponent("diagnostics.json"))
        }
    }

    // MARK: Eating

    var clipboardLink: String? {
        guard let text = NSPasteboard.general.string(forType: .string) else { return nil }
        return YouTubeLink.firstLink(in: text) ?? (YouTubeLink.parse(text) != nil ? text : nil)
    }

    func eatClipboard() {
        guard let link = clipboardLink else {
            show("There is no YouTube link on the clipboard.", error: true)
            return
        }
        Task { await eat(link) }
    }

    func eat(_ raw: String) async {
        guard let link = YouTubeLink.parse(raw) else {
            show("That doesn't look like a YouTube link.", error: true)
            return
        }
        switch link {
        case .video(let id):
            let videos = engine.enqueue([(id: id, title: "", channel: "", channelID: "")])
            if let video = videos.first, video.status == .done {
                show("Already eaten — opening it.")
            }
            if selection == .skills { selection = .library }
            selectedVideoID = id
        case .playlist(let listID):
            await eatList(url: "https://www.youtube.com/playlist?list=\(listID)")
        case .channel(let url):
            await follow(url)
        }
    }

    func eatList(url: String) async {
        guard let ytdlp = ToolLocator.find("yt-dlp", override: settings.ytdlpPath) else {
            show(EatError.missing("yt-dlp").localizedDescription, error: true)
            return
        }
        isResolvingLink = true
        defer { isResolvingLink = false }
        do {
            let listing = try await YTDLP(executable: ytdlp).flatList(url: url)
            engine.enqueue(listing.entries.map { (id: $0.id, title: $0.title, channel: $0.channel, channelID: $0.channelID) })
            show("Eating \(listing.entries.count) videos from the playlist.")
            selection = .eating
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    func follow(_ url: URL) async {
        isResolvingLink = true
        defer { isResolvingLink = false }
        do {
            let (channel, latest) = try await watcher.follow(url: url)
            selection = .channel(channel.channelID)
            show("Following \(channel.title). New uploads will be eaten automatically.")
            if !latest.isEmpty {
                channelOffer = ChannelOffer(channelID: channel.channelID, title: channel.title, latest: latest)
            }
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    func eatLatest(_ count: Int, from offer: ChannelOffer) {
        let entries = offer.latest.prefix(count).map { (id: $0.id, title: $0.title, channel: offer.title, channelID: offer.channelID) }
        engine.enqueue(Array(entries))
        channelOffer = nil
        show("Eating the latest \(entries.count) videos of \(offer.title).")
    }

    // MARK: Library actions

    func delete(_ video: Video) {
        engine.cancel(video.videoID)
        if selectedVideoID == video.videoID { selectedVideoID = nil }
        context.delete(video)
        try? context.save()
    }

    func copyTranscript(_ video: Video) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(video.transcriptText, forType: .string)
        show("Transcript copied.")
    }

    func copyMarkdown(_ video: Video) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(SecondBrainExporter.markdown(for: video), forType: .string)
        show("Markdown note copied.")
    }

    func exportMarkdown(_ video: Video) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = SecondBrainExporter.fileName(for: video)
        panel.allowedContentTypes = [.init(filenameExtension: "md") ?? .plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try SecondBrainExporter.markdown(for: video).write(to: url, atomically: true, encoding: .utf8)
            show("Saved \(url.lastPathComponent).")
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    func saveToSecondBrain(_ video: Video) {
        if exporter.export(video) {
            try? context.save()
            show("Saved in the Second Brain.")
        } else if !exporter.folderReachable {
            try? context.save()
            show("The Second Brain folder is not reachable (is Volume1 connected?). Zeus will save it when it is.", error: true)
        } else {
            show("Could not save to the Second Brain. Check the folder in Settings.", error: true)
        }
    }

    func reveal(_ path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func summarize(_ video: Video) {
        guard engine.canSummarize else {
            show(engine.summaryUnavailableMessage, error: true)
            return
        }
        Task {
            await engine.summarize(video)
            if let error = video.digestError { show(error, error: true) }
        }
    }

    func compileSkills(_ video: Video) {
        Task {
            do {
                let drafts = try await compiler.compile(video, context: context)
                show("\(drafts.count) skill draft\(drafts.count == 1 ? "" : "s") ready to review.")
                selectedSkillID = drafts.first?.id
            } catch {
                show(error.localizedDescription, error: true)
            }
        }
    }

    // MARK: Misc

    func show(_ text: String, error: Bool = false) {
        AppLog.write((error ? "ERROR " : "") + text)
        let toast = Toast(text: text, isError: error)
        self.toast = toast
        Task {
            try? await Task.sleep(for: .seconds(error ? 6 : 3.5))
            if self.toast == toast { self.toast = nil }
        }
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                show("Could not change the login item: \(error.localizedDescription)", error: true)
            }
        }
    }
}
