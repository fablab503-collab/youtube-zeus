import AppKit
import Foundation
import Observation
import ServiceManagement
import SwiftData

enum SidebarItem: Hashable {
    case eating
    case library
    case skills
    case browser
    case channel(String)
    case collection(String)
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
    let polisher: Polisher
    let account = YouTubeAccount()
    private var browserStorage: BrowserModel?

    /// Created the first time the YouTube window is shown.
    var browser: BrowserModel {
        if let browserStorage { return browserStorage }
        let created = BrowserModel()
        browserStorage = created
        return created
    }

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
            return try ModelContainer(for: Video.self, Channel.self, SkillDraft.self, VideoList.self, configurations: configuration)
        } catch {
            // Keep the old store aside instead of losing it, then start fresh.
            let backup = url.deletingLastPathComponent().appendingPathComponent("Library-\(Int(Date.now.timeIntervalSince1970)).store")
            try? FileManager.default.moveItem(at: url, to: backup)
            return try! ModelContainer(for: Video.self, Channel.self, SkillDraft.self, VideoList.self, configurations: configuration)
        }
    }

    init(context: ModelContext) {
        let settings = AppSettings()
        let summarizer = Summarizer()
        let exporter = SecondBrainExporter(settings: settings)
        let polisher = Polisher(settings: settings)
        let engine = EatEngine(context: context, settings: settings, summarizer: summarizer, exporter: exporter, polisher: polisher)
        self.polisher = polisher
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
        Task {
            await account.refresh()
            HandOff.writeToBrain(root: settings.secondBrainURL)
            exporter.scheduleIndexes(in: context)
        }
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
        if let id = domain["polish"] as? String, let video = engine.video(id) {
            engine.polishNow(video)
        }
        if let link = domain["eatList"] as? String {
            Task { await eatList(url: link) }
        }
        if (domain["showBrowser"] as? String) != nil {
            selection = .browser
        }
        if let id = domain["showCollection"] as? String {
            selection = .collection(id)
        }
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
            let kind: VideoListKind = listID == "WL" ? .watchLater : (listID == "LL" ? .liked : .playlist)
            await eatList(url: "https://www.youtube.com/playlist?list=\(listID)", kind: kind)
        case .channel(let url):
            await follow(url)
        }
    }

    /// Eats a playlist (or Watch Later / Liked) as one organised collection.
    func eatList(url: String, kind: VideoListKind = .playlist, title: String? = nil) async {
        guard let ytdlp = settings.makeYTDLP() else {
            show(EatError.missing("yt-dlp").localizedDescription, error: true)
            return
        }
        isResolvingLink = true
        defer { isResolvingLink = false }
        do {
            let listing = try await ytdlp.flatList(url: url)
            guard !listing.entries.isEmpty else {
                show(kind == .playlist ? "This playlist is empty or private." : "Nothing found. Are you signed in to YouTube in Zeus?", error: true)
                return
            }
            let listID: String
            switch kind {
            case .watchLater: listID = "WL"
            case .liked: listID = "LL"
            case .channel: listID = "channel:" + listing.channelID
            case .playlist: listID = listing.listID.isEmpty ? url : listing.listID
            }
            let name = title ?? (listing.listTitle.isEmpty ? listing.title : listing.listTitle)
            let list = upsertList(listID: listID, title: name, kind: kind, url: url, channel: listing.title,
                                  videoIDs: listing.entries.map(\.id))
            engine.enqueue(listing.entries.map { (id: $0.id, title: $0.title, channel: $0.channel, channelID: $0.channelID) })
            show("Eating \(listing.entries.count) videos of “\(name)”. They are organised as a collection.")
            selection = .collection(list.listID)
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    /// Eats every video of a channel as one collection.
    func eatWholeChannel(_ channel: Channel) async {
        let base = channel.url.absoluteString
        await eatList(url: base.hasSuffix("/videos") ? base : base + "/videos", kind: .channel, title: channel.title)
    }

    @discardableResult
    func upsertList(listID: String, title: String, kind: VideoListKind, url: String, channel: String, videoIDs: [String]) -> VideoList {
        var descriptor = FetchDescriptor<VideoList>(predicate: #Predicate { $0.listID == listID })
        descriptor.fetchLimit = 1
        if let existing = try? context.fetch(descriptor).first {
            existing.title = title
            existing.videoIDs = videoIDs
            existing.updatedAt = .now
            try? context.save()
            return existing
        }
        let list = VideoList(listID: listID, title: title, kind: kind, url: url, channelTitle: channel, videoIDs: videoIDs)
        context.insert(list)
        try? context.save()
        return list
    }

    func list(_ id: String) -> VideoList? {
        var descriptor = FetchDescriptor<VideoList>(predicate: #Predicate { $0.listID == id })
        descriptor.fetchLimit = 1
        return try? context.fetch(descriptor).first
    }

    func refresh(_ list: VideoList) async {
        await eatList(url: list.urlString, kind: list.kind, title: list.title)
    }

    func deleteList(_ list: VideoList) {
        if selection == .collection(list.listID) { selection = .library }
        context.delete(list)
        try? context.save()
        exporter.scheduleIndexes(in: context)
    }

    // MARK: YouTube account

    func importAccountSubscriptions() async {
        await account.refresh()
        guard let ytdlp = settings.makeYTDLP() else { return }
        do {
            let channels = try await ytdlp.subscribedChannels()
            guard !channels.isEmpty else {
                show("No subscriptions found. Sign in to YouTube in Zeus first.", error: true)
                return
            }
            let added = await watcher.follow(entries: channels)
            show("Following \(added) new channel\(added == 1 ? "" : "s") from your subscriptions (\(channels.count) in total).")
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    /// youtubezeus://eat?url=…  (used by the zeus command and by scripts)
    func handle(_ url: URL) {
        guard url.scheme == "youtubezeus" else { return }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        if url.host == "eat", let link = items.first(where: { $0.name == "url" })?.value {
            Task { await eat(link) }
        }
    }

    // MARK: AI packs

    func copyForAI(_ video: Video) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(AIPack.video(video.snapshot), forType: .string)
        show("Knowledge pack copied — paste it into Claude, ChatGPT, Gemini, Grok, GLM…")
    }

    func packVideos(for list: VideoList) -> (videos: [VideoSnapshot], missing: Int) {
        var videos: [VideoSnapshot] = []
        var missing = 0
        for id in list.videoIDs {
            if let video = engine.video(id), video.status.hasText { videos.append(video.snapshot) } else { missing += 1 }
        }
        return (videos, missing)
    }

    func copyForAI(_ list: VideoList, transcripts: Bool) {
        let (videos, missing) = packVideos(for: list)
        let text = AIPack.collection(title: list.title, kind: list.kind.label, url: list.urlString, videos: videos,
                                     missing: missing, includeTranscripts: transcripts)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        show("Copied \(videos.count) videos (\((text.count / 1000).formatted())k characters) — paste into any AI.")
    }

    func exportPack(_ list: VideoList) {
        let (videos, missing) = packVideos(for: list)
        let text = AIPack.collection(title: list.title, kind: list.kind.label, url: list.urlString, videos: videos,
                                     missing: missing, includeTranscripts: true)
        let panel = NSSavePanel()
        panel.nameFieldStringValue = SecondBrainExporter.sanitize(list.title, limit: 90) + " - AI pack.md"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try text.write(to: url, atomically: true, encoding: .utf8)
            show("Saved \(url.lastPathComponent). Attach it to any AI chat.")
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
