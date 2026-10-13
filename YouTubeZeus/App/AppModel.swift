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
    case ask
    case github
    case channel(String)
    case collection(String)
    case topic(String)
    case podcast(String)
    case entities
    case digests
    /// Videos moved out of the library (3.4): "Deleted videos" under Library.
    case trash
    /// Library sub-categories (3.5): starred videos, and the videos of one YouTube channel (by its name).
    case favorites
    case libraryChannel(String)
    /// Effects (3.8): a free, local Genjutsu (keep the movement, replace the person, keep the sound).
    case effects
}

/// "Show this item at that moment": the detail view opens the transcript there and highlights the paragraph.
struct JumpRequest: Equatable {
    let id = UUID()
    let videoID: String
    let seconds: Double
}

struct Toast: Identifiable, Equatable {
    let id = UUID()
    let text: String
    let isError: Bool
}

struct PlaylistOffer: Identifiable {
    var id: String { listID }
    let videoID: String
    let listID: String
}

struct PodcastOffer: Identifiable {
    let id = UUID()
    let channelID: String
    let title: String
    let feed: URL
    let episodes: [PodcastEpisode]
    let language: String?
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
    let indexer: SearchIndexer
    let setup: SetupAssistant
    let player = MediaPlayer()
    let account = YouTubeAccount()
    /// The Effects tab's engine runs (3.8).
    let effects = EffectsEngine()
    /// "Do jobs from The Swap": the shared-folder watcher (3.8, effects/MAKE-IT-ME.md).
    let swapWatcher = SwapWatcher()
    private var browserStorage: BrowserModel?
    /// YouTube pages: several YouTube accounts, one in front (3.9).
    let youtubePages = YouTubePages()

    /// Created the first time the YouTube window is shown.
    var browser: BrowserModel {
        if let browserStorage, browserStorage.pageID == youtubePages.activeID { return browserStorage }
        let created = BrowserModel(pageID: youtubePages.activeID)
        browserStorage = created
        return created
    }

    /// A new YouTube window for the page now in front (3.9), when the window was already open.
    func resetBrowser() {
        if browserStorage != nil { browserStorage = BrowserModel(pageID: youtubePages.activeID) }
    }

    /// The YouTube browser when it was opened (nil before: it is not loaded just to show suggestions).
    var openedBrowser: BrowserModel? { browserStorage }
    /// The small YouTube window opened from the Eat bar (3.3).
    var showMiniBrowser = false
    /// A link the mini browser puts in the Eat bar.
    var eatBarFill: String?

    var selection: SidebarItem? = .library
    var selectedVideoID: String?
    var selectedSkillID: UUID?
    var toast: Toast?
    var channelOffer: ChannelOffer?
    var podcastOffer: PodcastOffer?
    var playlistOffer: PlaylistOffer?
    var importingPlaylists: String?
    /// Channels whose videos are kept as text files (3.6, "All channel").
    var textChannelNames: Set<String> = Set(UserDefaults.standard.stringArray(forKey: AppModel.textChannelsKey) ?? [])
    @ObservationIgnored var textRebuilds: [String: Task<Void, Never>] = [:]
    /// Translations being made ("<video id>|<language>") and the language each channel is also kept in (3.7).
    var translating: Set<String> = []
    /// Items REA is reading a target for (4.2).
    var reaRunning: Set<String> = []
    /// Repo Scout in Sidebar › GitHub (4.4): its reports, today's trending without it, and the running scan.
    var scoutReports: [RepoScout.Report] = []
    var scoutTrending: [RepoScout.Entry] = []
    var scoutLog = ""
    var scoutPick: RepoScout.Pick?
    var scoutTask: Task<Void, Never>?
    var channelLanguages: [String: String] = (UserDefaults.standard.dictionary(forKey: AppModel.channelLanguagesKey) as? [String: String]) ?? [:]
    @ObservationIgnored var channelTranslateTasks: [String: Task<Void, Never>] = [:]
    var packResults: [String: PackResult] = [:]
    var buildingPacks: Set<String> = []
    var isResolvingLink = false
    var focusEatBar = 0
    var initialTab: String?
    var jump: JumpRequest?
    /// The library's search field (also set by youtubezeus://search?q=…).
    var librarySearch = ""
    var selectedEntity: String?
    var selectedDigest: String?
    /// 3.0 background work
    @ObservationIgnored var entityRebuildTask: Task<Void, Never>?
    @ObservationIgnored var v3Loop: Task<Void, Never>?
    var entityVersion = 0
    var digestVersion = 0
    var phoneLinksEaten = 0
    /// The setup page (first launch, Settings › Tools, youtubezeus://setup, zeus setup).
    var showSetup = false
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
        self.indexer = SearchIndexer(context: context)
        self.setup = SetupAssistant(settings: settings)
    }

    func start() {
        guard !started else { return }
        started = true
        engine.onProcessed = { [weak self] id in self?.videoProcessed(id) }
        engine.onChanged = { [weak self] id in self?.itemChanged(id) }
        account.onChecked = { [weak self] signedIn in self?.youtubePages.setSignedIn(signedIn) }
        engine.resumeInterrupted()
        PlaybookFiles.clearMaking()
        watcher.start()
        startSwapWatcherIfEnabled()
        Task { await openSetupIfNeeded() }
        Task {
            await account.refresh()
            HandOff.writeToBrain(root: settings.secondBrainURL)
            AgentGuide.writeToBrain(settings: settings)
            groundOlderSummaries()
            rewriteNotesIfFormatChanged()
            exporter.scheduleIndexes(in: context)
            await indexer.catchUp()
            await refreshClaudePacks()
            startV3()
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
        if let id = domain["checkGitHub"] as? String {
            if id == "all" {
                for video in (try? context.fetch(FetchDescriptor<Video>())) ?? [] where video.status.hasText {
                    engine.scheduleGitHub(video.videoID, force: true)
                }
            } else if let video = engine.video(id) {
                engine.recheckGitHub(video)
            }
        }
        if (domain["showGitHub"] as? String) != nil {
            selection = .github
        }
        if let link = domain["eatList"] as? String {
            Task { await eatList(url: link) }
        }
        if (domain["showBrowser"] as? String) != nil {
            selection = .browser
        }
        if (domain["showEffects"] as? String) != nil {
            openEffects(video: domain["effectsVideo"] as? String)
            if let job = domain["effectsJob"] as? String { effects.selectedJobID = job }
        }
        if let link = domain["browse"] as? String, let url = URL(string: link) {
            selection = .browser
            browser.open(url)
        }
        if let question = domain["ask"] as? String {
            selection = .ask
            Task { await ask(question) }
        }
        if let topic = domain["showTopic"] as? String {
            selection = .topic(topic)
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
        if let link = YouTubeLink.firstLink(in: text) ?? (YouTubeLink.parse(text) != nil ? text : nil) { return link }
        // A post or a Spotify episode (3.8).
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return SocialSite.of(trimmed) != nil ? trimmed : nil
    }

    func eatClipboard() {
        guard let link = clipboardLink else {
            show("There is no YouTube, X, Instagram, Facebook, Threads or Spotify link on the clipboard.", error: true)
            return
        }
        Task { await eat(link) }
    }

    func eat(_ raw: String) async {
        // Posts (X, Instagram, Facebook, Threads, Telegram, TikTok, LinkedIn, Reddit…), Spotify episodes and any other
        // page yt-dlp knows (3.8, 4.3). Podcast feeds keep their own path.
        if YouTubeLink.parse(raw) == nil, !PodcastFeed.looksLikePodcast(raw), let site = SocialSite.of(raw) {
            await eatSocial(raw, site: site)
            return
        }
        guard let link = YouTubeLink.parse(raw) else {
            let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            if PodcastFeed.looksLikePodcast(text) {
                await followPodcast(text)
            } else if text.hasPrefix("/") || text.hasPrefix("~") || text.hasPrefix("file://") {
                let url = text.hasPrefix("file://") ? URL(string: text) : URL(fileURLWithPath: (text as NSString).expandingTildeInPath)
                if let url { await eatFiles([url]) }
            } else if text.lowercased().hasPrefix("http") {
                // Maybe a feed without a telltale address: try it as a podcast.
                await followPodcast(text)
            } else {
                show("That doesn't look like a YouTube link, a podcast feed or a file.", error: true)
            }
            return
        }
        // A channel's Playlists tab: import every playlist as a collection.
        if case .channel(let url) = link, raw.lowercased().contains("/playlists") {
            await importPlaylists(from: url)
            return
        }
        // A video opened from a playlist: eat the video, and offer the whole playlist.
        if case .video(let id) = link,
           let list = URLComponents(string: raw.hasPrefix("http") ? raw : "https://" + raw)?.queryItems?.first(where: { $0.name == "list" })?.value,
           list.hasPrefix("PL") || list.hasPrefix("OL") {
            playlistOffer = PlaylistOffer(videoID: id, listID: list)
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

    /// Imports every playlist of a channel as a collection (nothing is eaten until you ask).
    func importPlaylists(from channelURL: URL, eatAll: Bool = false) async {
        guard let ytdlp = settings.makeYTDLP() else {
            show(EatError.missing("yt-dlp").localizedDescription, error: true)
            return
        }
        isResolvingLink = true
        defer {
            isResolvingLink = false
            importingPlaylists = nil
        }
        do {
            importingPlaylists = "Reading the playlists…"
            let found = try await ytdlp.playlists(ofChannel: channelURL.absoluteString)
            guard !found.lists.isEmpty else {
                show("This channel shows no playlists.", error: true)
                return
            }
            var imported = 0
            var videos = 0
            for (index, playlist) in found.lists.enumerated() {
                importingPlaylists = "Playlist \(index + 1) of \(found.lists.count): \(playlist.title)"
                let url = "https://www.youtube.com/playlist?list=\(playlist.id)"
                guard let listing = try? await ytdlp.flatList(url: url), !listing.entries.isEmpty else { continue }
                upsertList(listID: playlist.id, title: playlist.title, kind: .playlist, url: url,
                           channel: found.channel.isEmpty ? listing.title : found.channel, videoIDs: listing.entries.map(\.id))
                if eatAll {
                    engine.enqueue(listing.entries.map { (id: $0.id, title: $0.title, channel: $0.channel, channelID: $0.channelID) })
                }
                imported += 1
                videos += listing.entries.count
            }
            exporter.scheduleIndexes(in: context)
            AppLog.write("PLAYLISTS imported \(imported) of \(found.channel) (\(videos) videos)")
            show("Imported \(imported) playlists of \(found.channel) (\(videos) videos) as collections."
                 + (eatAll ? " Eating them all." : " Open one and press “Eat the missing” to eat it."))
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    @discardableResult
    func upsertList(listID: String, title: String, kind: VideoListKind, url: String, channel: String, videoIDs: [String]) -> VideoList {
        var descriptor = FetchDescriptor<VideoList>(predicate: #Predicate { $0.listID == listID })
        descriptor.fetchLimit = 1
        if let existing = try? context.fetch(descriptor).first {
            existing.title = title
            existing.videoIDs = videoIDs
            if existing.channelTitle.isEmpty { existing.channelTitle = channel }
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
    /// `youtubezeus://…` links: from the zeus command, from notes in the Second Brain, from other apps and agents.
    /// The setup page opens by itself when Zeus cannot eat videos yet (no yt-dlp), and once on a Mac where tools
    /// are missing. Afterwards it is in Settings › Tools, `youtubezeus://setup` and `zeus setup`.
    func openSetupIfNeeded() async {
        await setup.quickCheck()
        let firstTime = !UserDefaults.standard.bool(forKey: "setupSeen")
        UserDefaults.standard.set(true, forKey: "setupSeen")
        if setup.essentialsMissing || (firstTime && !setup.everythingReady) {
            AppLog.write("SETUP page opened: " + (setup.essentialsMissing ? "yt-dlp is missing" : "first launch with tools missing"))
            showSetup = true
        }
    }

    func handle(_ url: URL) {
        guard let target = BrainLinks.parse(url) else { return }
        AppLog.write("LINK \(url.absoluteString)")
        switch target {
        case .eat(let link):
            Task { await eat(link) }
            return
        case .video(let id, let tab, let seconds):
            if engine.video(id) == nil, MediaKind.of(id: id) == .youtube {
                Task { await eat("https://www.youtube.com/watch?v=\(id)") }
            }
            if selection != .library, selection != .github { selection = .library }
            initialTab = seconds == nil ? tab : "Transcript"
            selectedVideoID = id
            if let seconds { jump = JumpRequest(videoID: id, seconds: seconds) }
        case .eatFile(let path):
            Task { await eatFiles([URL(fileURLWithPath: (path as NSString).expandingTildeInPath)]) }
        case .podcast(let feed, let latest):
            Task { await followPodcast(feed, latest: latest) }
        case .search(let query):
            selection = .library
            librarySearch = query
        case .entity(let name):
            selection = .entities
            selectedEntity = name
        case .screen(let id):
            if let video = engine.video(id) {
                selection = .library
                selectedVideoID = id
                readScreen(video)
            }
        case .digest(let week):
            selection = .digests
            Task { await openDigest(week: week) }
        case .setup:
            showSetup = true
        case .trash(let all):
            if all { trashLibrary() }
            selection = .trash
        case .rebuild(let id, let readerName, let save):
            guard let video = engine.video(id), video.status.hasText else { show("No eaten video \(id) in the library.", error: true); return }
            selectedVideoID = id
            initialTab = DetailTab.rebuild.rawValue
            let reader = readerName.flatMap { name in RebuildBrief.Reader.allCases.first { $0.rawValue.lowercased().hasPrefix(name.lowercased()) } } ?? .claude
            if save { saveRebuild(video, open: false) }
            copyRebuild(video, for: reader)
        case .scout(let scan, let repo, let jury):
            UserDefaults.standard.set("today", forKey: "githubPage")
            selection = .github
            if let repo { scanScout(jury: jury, repos: [repo.replacingOccurrences(of: "https://github.com/", with: "")]) }
            else if let scan { scanScout(since: scan, jury: jury) }
        case .everything(let id):
            guard let video = engine.video(id) else { show("No video \(id) in the library.", error: true); return }
            selectedVideoID = id
            initialTab = DetailTab.everything.rawValue
            doEverything(video)
        case .youtubePage(let text):
            selection = .browser
            if let page = youtubePages.find(text) {
                switchYouTubePage(page.id)
            } else if youtubePages.canAdd, Int(text) == nil {
                addYouTubePage(named: text)
            } else {
                show("No YouTube page “\(text)”.", error: true)
            }
        case .translate(let id, let language):
            guard let video = engine.video(id) else { show("No video \(id) in the library.", error: true); return }
            Task {
                if await translate(video, to: language) != nil {
                    show("“\(video.displayTitle)” is now also in \(TranslationLanguages.name(language)).")
                }
            }
        case .allChannel(let link, let again, let stop, let language):
            if let language {
                let descriptor = FetchDescriptor<Video>()
                let names = Set(((try? context.fetch(descriptor)) ?? []).map(\.channelTitle)).intersection(textChannelNames)
                if let match = names.first(where: { $0 == link }) ?? (names.count == 1 ? names.first : nil) {
                    setChannelTextLanguage(match, ["none", "off", "original"].contains(language.lowercased()) ? nil : language)
                    show("The text files of \(match) are now also in \(TranslationLanguages.name(language)).")
                } else {
                    show("Which channel? Use its name: allchannel?channel=<name>&lang=fr", error: true)
                }
                return
            }
            if stop {
                // By name, or by the channel of a link: the channel's name as the videos carry it.
                let descriptor = FetchDescriptor<Video>()
                let names = Set(((try? context.fetch(descriptor)) ?? []).filter(\.textOnly).map(\.channelTitle))
                let match = names.first { $0 == link } ?? (names.count == 1 ? names.first : nil)
                if let match { stopChannelText(match) } else { show("Which channel? Use its name: allchannel?channel=<name>&stop=1", error: true) }
                return
            }
            let text = link.hasPrefix("http") ? link : (link.hasPrefix("@") ? "https://www.youtube.com/" + link : link)
            if let url = URL(string: text), url.host?.contains("youtube.com") == true {
                Task { await eatAllChannelText(name: "", url: url, again: again) }
            } else if let url = channelURL(forTitle: link) {
                Task { await eatAllChannelText(name: link, url: url, again: again) }
            } else {
                show("“\(link)” is not a YouTube channel link.", error: true)
            }
        case .eating(let paused):
            if paused { engine.pauseEating() } else { engine.resumeEating() }
        case .effects(let id, let seconds):
            openEffects(video: id, start: seconds)
        case .playbook(let id, let make, let force, let run):
            if selection != .library, selection != .github { selection = .library }
            selectedVideoID = id
            initialTab = DetailTab.playbook.rawValue
            if let video = engine.video(id) {
                if let run, let agent = PlaybookRunner.Agent(rawValue: run.lowercased()), video.playbook != nil, !make {
                    runPlaybook(video, with: agent)
                } else if make || (video.playbook == nil && video.playbookError == nil) {
                    // Missing: written now; after a failed try, only on request (the tab shows why).
                    makePlaybook(video, force: force)
                }
            }
        case .collection(let id):
            selection = .collection(id)
        case .channel(let id):
            if watcher.channel(id) != nil {
                selection = .channel(id)
            } else {
                // Not followed: show the channel's latest eaten video in the library.
                var descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.channelID == id },
                                                        sortBy: [SortDescriptor(\.addedAt, order: .reverse)])
                descriptor.fetchLimit = 1
                selection = .library
                selectedVideoID = (try? context.fetch(descriptor))?.first?.videoID
            }
        case .repo(let fullName):
            selection = .github
            let key = fullName.lowercased()
            let videos = (try? context.fetch(FetchDescriptor<Video>(sortBy: [SortDescriptor(\.addedAt, order: .reverse)]))) ?? []
            let match = videos.first { video in video.repos.contains { repo in repo.id == key } }
            selectedVideoID = match?.videoID ?? selectedVideoID
            initialTab = "Info"
        case .topic(let topic):
            selection = .topic(topic)
        case .view(let view):
            switch view.lowercased() {
            case "github": selection = .github
            case "ask": selection = .ask
            case "skills": selection = .skills
            case "eating", "queue": selection = .eating
            case "youtube", "browser": selection = .browser
            case "entities", "people", "tools", "companies": selection = .entities
            case "digests", "digest": selection = .digests
            case "favorites", "favourites", "starred": selection = .favorites
            case "deleted", "trash": selection = .trash
            case "effects": selection = .effects
            default: selection = .library
            }
        case .ask(let question):
            selection = .ask
            Task { await ask(question) }
        case .pack(let id):
            if let list = list(id) {
                selection = .collection(id)
                Task { await makeClaudePack(list) }
            }
        case .playlists(let channel):
            if let url = URL(string: channel.hasPrefix("http") ? channel : "https://www.youtube.com/" + channel) {
                Task { await importPlaylists(from: url) }
            }
            return
        }
        NSApp.activate()
    }

    /// Notes written by an older version get the current format once (links back to Zeus, GitHub section,
    /// 3.0: moments after key points and summary sentences, on-screen text, people/tools/companies).
    static let noteFormat = 4

    /// Summaries made before 3.0 get their moments once (no AI: the key points are matched to the transcript).
    func groundOlderSummaries() {
        let videos = ((try? context.fetch(FetchDescriptor<Video>())) ?? []).filter { $0.digestData != nil }
        var count = 0
        for video in videos {
            guard let digest = video.digest, digest.keyPointTimes == nil else { continue }
            video.digest = Grounder.ground(digest, paragraphs: video.displayParagraphs)
            count += 1
        }
        guard count > 0 else { return }
        try? context.save()
        AppLog.write("CITATIONS placed the key points and summary sentences of \(count) older summaries")
    }

    /// Something searchable changed (eaten, polished, summarized, screen read, names found).
    func itemChanged(_ id: String) {
        indexer.schedule(id)
        entityChanged(id)
        channelTextItemChanged(id)
    }

    /// Opens an item in the library, at a moment when given.
    func open(video id: String, at seconds: Double? = nil) {
        if selection != .library, selection != .github, selection != .ask { selection = .library }
        if selection == .ask { selection = .library }
        selectedVideoID = id
        if let seconds {
            initialTab = "Transcript"
            jump = JumpRequest(videoID: id, seconds: seconds)
        }
    }

    func rewriteNotesIfFormatChanged() {
        let defaults = UserDefaults.standard
        guard defaults.integer(forKey: "noteFormat") < Self.noteFormat, exporter.folderReachable else { return }
        let videos = ((try? context.fetch(FetchDescriptor<Video>())) ?? []).filter { $0.status.hasText && $0.secondBrainPath != nil }
        for video in videos { exporter.export(video) }
        try? context.save()
        defaults.set(Self.noteFormat, forKey: "noteFormat")
        AppLog.write("NOTES rewritten in format \(Self.noteFormat): \(videos.count)")
    }

    // MARK: Open in the Second Brain (exact page)

    func noteURL(for video: Video) -> URL? {
        video.secondBrainPath.map { URL(fileURLWithPath: $0) }
    }

    func channelIndexURL(_ channelTitle: String) -> URL {
        let folder = SecondBrainExporter.channelFolderName(channelTitle)
        return settings.secondBrainURL.appendingPathComponent(folder).appendingPathComponent("_Index - \(folder).md")
    }

    var youtubeIndexURL: URL { settings.secondBrainURL.appendingPathComponent("YouTube index.md") }
    var githubIndexURL: URL { settings.githubURL.appendingPathComponent("_Index - GitHub from YouTube.md") }
    var agentGuideURL: URL { settings.secondBrainURL.appendingPathComponent("_For AI").appendingPathComponent(AgentGuide.fileName) }

    /// Opens a note of the Second Brain at its exact page (Obsidian when installed).
    func openInBrain(_ file: URL?) {
        guard let file, BrainLinks.open(file) else {
            show(exporter.folderReachable ? "That note does not exist yet — it is written after eating." : "The Second Brain is not reachable (is its drive connected?).", error: true)
            return
        }
    }

    // MARK: Ask your YouTube brain

    /// The local AI by default (free, on this Mac); Codex only when chosen and allowed.
    var askEngine: AskBrain.Engine? {
        if settings.askEngine == "codex" {
            guard settings.openAIConsent, !engine.codexPaused, let codex = CodexLocator.path else { return nil }
            return .codex(path: codex, model: settings.codexModel)
        }
        return polisher.isInstalled ? .local(model: settings.polishModel) : nil
    }

    var askEngineLabel: String {
        settings.askEngine == "codex" ? "Codex" : "the local AI (\(settings.polishModel))"
    }

    var askAnswer: BrainAnswer?
    var askPassages: [BrainPassage] = []
    var askQuestion = ""
    var isAsking = false
    var askError: String?

    func ask(_ question: String) async {
        let question = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty else { return }
        askQuestion = question
        askAnswer = nil
        askError = nil
        guard let engine = askEngine else {
            askError = settings.askEngine == "codex"
                ? "Ask is set to Codex but Codex is not allowed or not found (Settings › Cloud AI). Pick the local AI to keep it free."
                : "Ask uses the local AI: install Ollama (free, ollama.com) and polish one video once to download the model."
            return
        }
        isAsking = true
        defer { isAsking = false }
        var passages: [BrainPassage] = []
        if let index = indexer.index {
            let found = await Task.detached(priority: .userInitiated) { AskBrain.retrieve(question: question, index: index) }.value
            var snapshots: [String: VideoSnapshot] = [:]
            for id in Set(found.map(\.videoID)) { snapshots[id] = self.engine.video(id)?.snapshot }
            passages = AskBrain.withContext(found, videos: snapshots)
        }
        if passages.isEmpty {
            let done = [EatStatus.done, .summarizing, .polishing].map(\.rawValue)
            let videos = ((try? context.fetch(FetchDescriptor<Video>(predicate: #Predicate { done.contains($0.statusRaw) })))) ?? []
            passages = AskBrain.retrieve(question: question, videos: videos.map(\.snapshot))
        }
        askPassages = passages
        guard !passages.isEmpty else {
            askError = "Nothing in your library talks about that yet. Eat a few videos about it first."
            return
        }
        do {
            let answer = try await AskBrain.ask(question: question, passages: passages, engine: engine)
            askAnswer = AskBrain.verified(answer) { id in self.engine.video(id)?.displayParagraphs ?? [] }
            AppLog.write("ASK ok: \(question)")
        } catch {
            askError = error.localizedDescription
        }
    }

    // MARK: Claude packs (skill + digest + transcript parts)

    private static let packRegistryKey = "claudePacks"
    private var packRefreshTask: Task<Void, Never>?

    var packRegistry: [String: String] {
        get { UserDefaults.standard.dictionary(forKey: Self.packRegistryKey) as? [String: String] ?? [:] }
        set { UserDefaults.standard.set(newValue, forKey: Self.packRegistryKey) }
    }

    func hasClaudePack(_ list: VideoList) -> Bool { packRegistry[list.listID] != nil }

    func packVaultFolder(_ list: VideoList) -> URL {
        settings.secondBrainURL.appendingPathComponent("Claude packs", isDirectory: true)
            .appendingPathComponent(SecondBrainExporter.sanitize(ClaudePack.packName(title: list.title, channel: list.channelTitle), limit: 100))
    }

    func packZipURL(_ list: VideoList) -> URL {
        let skill = ClaudePack.skillName(title: list.title, channel: list.channelTitle)
        return ClaudePackPaths.masterFolder.appendingPathComponent(skill).appendingPathComponent(skill + ".zip")
    }

    func packLocalPrompts(_ list: VideoList) -> URL {
        let skill = ClaudePack.skillName(title: list.title, channel: list.channelTitle)
        return ClaudePackPaths.masterFolder.appendingPathComponent(skill).appendingPathComponent("prompts")
    }

    /// Builds (or refreshes) the Claude pack of a collection off the main thread.
    func makeClaudePack(_ list: VideoList, quiet: Bool = false) async {
        guard !buildingPacks.contains(list.listID) else { return }
        let input = ClaudePackPaths.input(for: list, settings: settings) { self.engine.video($0) }
        guard !input.videos.isEmpty else {
            if !quiet { show("Nothing eaten yet in this collection.", error: true) }
            return
        }
        buildingPacks.insert(list.listID)
        defer { buildingPacks.remove(list.listID) }
        do {
            let result = try await Task.detached { try ClaudePack.build(input) }.value
            packResults[list.listID] = result
            var registry = packRegistry
            // Without the Second Brain copy, the fingerprint stays "dirty" so the next refresh copies it.
            registry[list.listID] = ClaudePackPaths.fingerprint(input) + (result.vaultWritten ? "" : "|vault-pending")
            packRegistry = registry
            AppLog.write("PACK \(result.skillName): \(result.videos) videos, \(result.summarized) summarized, \(result.parts) parts, \(result.changedFiles) files changed")
            if !quiet {
                show("Claude pack ready: skill “\(result.skillName)” installed for Claude, \(result.videos) videos (digest ≈ \(result.digestTokens / 1000)k tokens)."
                     + (result.vaultWritten ? "" : " The Second Brain copy will follow when the NAS answers."))
            }
        } catch {
            show("Claude pack: \(error.localizedDescription)", error: true)
        }
    }

    /// Packs follow their collection: rebuilt (at most every 4 minutes) when videos are eaten, polished or summarized.
    func videoProcessed(_ id: String) {
        let registry = packRegistry
        guard !registry.isEmpty, packRefreshTask == nil else { return }
        let lists = registry.keys.compactMap { list($0) }
        guard lists.contains(where: { $0.videoIDs.contains(id) }) else { return }
        packRefreshTask = Task {
            try? await Task.sleep(for: .seconds(240))
            packRefreshTask = nil
            await refreshClaudePacks()
        }
    }

    func refreshClaudePacks() async {
        for (listID, old) in packRegistry {
            guard let list = list(listID) else { continue }
            let input = ClaudePackPaths.input(for: list, settings: settings) { self.engine.video($0) }
            if ClaudePackPaths.fingerprint(input) != old { await makeClaudePack(list, quiet: true) }
        }
    }

    func copyDigest(_ list: VideoList) {
        let file = packLocalPrompts(list).appendingPathComponent("digest.md")
        guard let text = try? String(contentsOf: file, encoding: .utf8) else {
            show("Make the Claude pack first.", error: true)
            return
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        show("Digest copied (≈ \(text.count / 4000)k tokens) — paste it into Claude.")
    }

    // MARK: AI packs

    func copyForAI(_ video: Video) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(AIPack.video(video.snapshot), forType: .string)
        show("Knowledge pack copied — paste it into Claude, ChatGPT, Gemini, Grok, GLM…")
    }

    /// The repository's note in the Second Brain, when it exists.
    func repoNoteURL(_ repo: RepoCheck) -> URL? {
        let file = settings.githubURL.appendingPathComponent("\(repo.noteName).md")
        return FileManager.default.fileExists(atPath: file.path) ? file : nil
    }

    func copyGitHubForAI(_ entries: [(RepoCheck, [VideoSnapshot])]) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(AIPack.github(entries), forType: .string)
        show("Copied \(entries.count) GitHub repositories for AI.")
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

    /// Delete forever (from the Trash). The notes already written in the Second Brain stay.
    func delete(_ video: Video) {
        engine.forget(video.videoID)
        if selectedVideoID == video.videoID { selectedVideoID = nil }
        indexer.remove(video.videoID)
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
            show("The Second Brain folder is not reachable (is its drive connected?). Zeus will save it when it is.", error: true)
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
