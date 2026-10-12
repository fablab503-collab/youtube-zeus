import Foundation
import Observation

/// User settings, stored in UserDefaults.
@Observable
final class AppSettings {
    private let defaults = UserDefaults.standard

    // Eating
    var preferredLanguages: String { didSet { save("preferredLanguages", preferredLanguages) } }
    var useWhisperFallback: Bool { didSet { save("useWhisperFallback", useWhisperFallback) } }
    var whisperModel: String { didSet { save("whisperModel", whisperModel) } }
    var speechEngine: String { didSet { save("speechEngine", speechEngine) } }
    var maxParallel: Int { didSet { save("maxParallel", maxParallel) } }

    // Apple Intelligence
    var autoSummarize: Bool { didSet { save("autoSummarize", autoSummarize) } }
    var summaryLanguage: String { didSet { save("summaryLanguage", summaryLanguage) } }
    var summaryEngine: String { didSet { save("summaryEngine", summaryEngine) } }

    // Second Brain
    var secondBrainEnabled: Bool { didSet { save("secondBrainEnabled", secondBrainEnabled) } }
    var secondBrainFolder: String { didSet { save("secondBrainFolder", secondBrainFolder) } }

    // Channels
    var pollMinutes: Int { didSet { save("pollMinutes", pollMinutes) } }
    var skipShorts: Bool { didSet { save("skipShorts", skipShorts) } }
    var notifyWhenEaten: Bool { didSet { save("notifyWhenEaten", notifyWhenEaten) } }
    var keepInMenuBar: Bool { didSet { save("keepInMenuBar", keepInMenuBar) } }

    // Codex skills (OpenAI)
    var skillEngine: String { didSet { save("skillEngine", skillEngine) } }
    var askEngine: String { didSet { save("askEngine", askEngine) } }
    var codexModel: String { didSet { save("codexModel", codexModel) } }
    var openAIModel: String { didSet { save("openAIModel", openAIModel) } }
    var openAIConsent: Bool { didSet { save("openAIConsent", openAIConsent) } }
    var dailyTokenLimit: Int { didSet { save("dailyTokenLimit", dailyTokenLimit) } }
    var publishFolder: String { didSet { save("publishFolder", publishFolder) } }
    var skillLanguage: String { didSet { save("skillLanguage", skillLanguage) } }

    // YouTube account
    var cookieSource: String { didSet { save("cookieSource", cookieSource) } }
    var commentsCount: Int { didSet { save("commentsCount", commentsCount) } }

    // Local AI polishing (Ollama)
    var polishEnabled: Bool { didSet { save("polishEnabled", polishEnabled) } }
    var polishModel: String { didSet { save("polishModel", polishModel) } }
    var polishCaptionsToo: Bool { didSet { save("polishCaptionsToo", polishCaptionsToo) } }

    // Organising
    var writeIndexes: Bool { didSet { save("writeIndexes", writeIndexes) } }
    var githubEnabled: Bool { didSet { save("githubEnabled", githubEnabled) } }
    var githubFolder: String { didSet { save("githubFolder", githubFolder) } }

    // 3.0
    var readScreenOfFiles: Bool { didSet { save("readScreenOfFiles", readScreenOfFiles) } }
    var screenInterval: Int { didSet { save("screenInterval", screenInterval) } }
    var entitiesEnabled: Bool { didSet { save("entitiesEnabled", entitiesEnabled) } }
    var entityNoteThreshold: Int { didSet { save("entityNoteThreshold", entityNoteThreshold) } }
    var digestEnabled: Bool { didSet { save("digestEnabled", digestEnabled) } }
    var digestWeekday: Int { didSet { save("digestWeekday", digestWeekday) } }
    var digestHour: Int { didSet { save("digestHour", digestHour) } }
    var phoneInboxEnabled: Bool { didSet { save("phoneInboxEnabled", phoneInboxEnabled) } }

    // 3.8: Make it me — "Do jobs from The Swap" (the shared-folder watcher runs with the app)
    var swapJobsEnabled: Bool { didSet { save("swapJobsEnabled", swapJobsEnabled) } }
    /// Finished .swapjob folders older than this many days are removed (0 = never).
    var swapJobsCleanDays: Int { didSet { save("swapJobsCleanDays", swapJobsCleanDays) } }

    // 3.2: playbooks
    /// Look at the key frames with a vision model (else the transcript and the text on screen only).
    var playbookVision: Bool { didSet { save("playbookVision", playbookVision) } }
    /// A vision model key of `ModelCatalog.visionOptions`, an Ollama tag, or "" for the best one for this Mac.
    var visionModel: String { didSet { save("visionModel", visionModel) } }
    /// At most this many key frames are looked at.
    var playbookFrames: Int { didSet { save("playbookFrames", playbookFrames) } }
    /// Where agents work on a playbook (one folder per playbook).
    var playbookFolder: String { didSet { save("playbookFolder", playbookFolder) } }

    // Tools (empty = find automatically)
    var ytdlpPath: String { didSet { save("ytdlpPath", ytdlpPath) } }
    var ffmpegPath: String { didSet { save("ffmpegPath", ffmpegPath) } }
    var whisperPath: String { didSet { save("whisperPath", whisperPath) } }

    init() {
        let d = UserDefaults.standard
        // Before 2.5.1 the default folders pointed to the author's NAS. An install that relied on those defaults
        // keeps them (written once, explicitly) now that the defaults are generic.
        if d.object(forKey: "legacyFoldersKept") == nil {
            let persistent = d.persistentDomain(forName: Bundle.main.bundleIdentifier ?? "") ?? [:]
            let legacy = "/Volumes/Volume1/SecondBrain/Sources/YouTube"
            let existingInstall = persistent["noteFormat"] != nil || persistent["freeEngines2_5"] != nil
            if existingInstall, persistent["secondBrainFolder"] == nil {
                d.set(legacy, forKey: "secondBrainFolder")
                if persistent["githubFolder"] == nil { d.set("/Volumes/Volume1/SecondBrain/Sources/GitHub", forKey: "githubFolder") }
            }
            d.set(true, forKey: "legacyFoldersKept")
        }
        d.register(defaults: [
            "preferredLanguages": "en, fr, it, es, ro, de",
            "useWhisperFallback": true,
            "whisperModel": WhisperModel.turbo.rawValue,
            "speechEngine": SpeechEngine.auto.rawValue,
            "maxParallel": 2,
            "autoSummarize": true,
            "summaryLanguage": "auto",
            "summaryEngine": "auto",
            "secondBrainEnabled": true,
            "secondBrainFolder": "~/SecondBrain/Sources/YouTube",
            "pollMinutes": 30,
            "skipShorts": false,
            "notifyWhenEaten": true,
            "keepInMenuBar": true,
            "skillEngine": "local",
            "askEngine": "local",
            "codexModel": "",
            "openAIModel": "gpt-5-mini",
            "openAIConsent": false,
            "dailyTokenLimit": 250_000,
            "publishFolder": "~/.codex/skills",
            "skillLanguage": "English",
            "cookieSource": "zeus",
            "commentsCount": 30,
            "polishEnabled": true,
            "polishModel": "qwen3:4b-instruct",
            "polishCaptionsToo": false,
            "writeIndexes": true,
            "githubEnabled": true,
            "githubFolder": "",
            "readScreenOfFiles": true,
            "screenInterval": 2,
            "entitiesEnabled": true,
            "entityNoteThreshold": 2,
            "digestEnabled": true,
            "digestWeekday": 1,
            "digestHour": 19,
            "phoneInboxEnabled": true,
            "swapJobsEnabled": true,
            "swapJobsCleanDays": 0,
            "playbookVision": true,
            "visionModel": "",
            "playbookFrames": 60,
            "playbookFolder": "~/Zeus Playbooks",
            "ytdlpPath": "",
            "ffmpegPath": "",
            "whisperPath": "",
        ])
        preferredLanguages = d.string(forKey: "preferredLanguages") ?? ""
        useWhisperFallback = d.bool(forKey: "useWhisperFallback")
        whisperModel = d.string(forKey: "whisperModel") ?? WhisperModel.turbo.rawValue
        speechEngine = d.string(forKey: "speechEngine") ?? SpeechEngine.auto.rawValue
        maxParallel = max(1, d.integer(forKey: "maxParallel"))
        autoSummarize = d.bool(forKey: "autoSummarize")
        summaryLanguage = d.string(forKey: "summaryLanguage") ?? "auto"
        summaryEngine = d.string(forKey: "summaryEngine") ?? "auto"
        secondBrainEnabled = d.bool(forKey: "secondBrainEnabled")
        secondBrainFolder = d.string(forKey: "secondBrainFolder") ?? ""
        pollMinutes = max(5, d.integer(forKey: "pollMinutes"))
        skipShorts = d.bool(forKey: "skipShorts")
        notifyWhenEaten = d.bool(forKey: "notifyWhenEaten")
        keepInMenuBar = d.bool(forKey: "keepInMenuBar")
        // Zeus is free by default (Apple Intelligence and the local AI): settings saved by older versions that
        // pointed to Codex / OpenAI are switched back once.
        if !d.bool(forKey: "freeEngines2_5") {
            d.set(false, forKey: "openAIConsent")
            d.set("auto", forKey: "summaryEngine")
            d.set("local", forKey: "skillEngine")
            d.set("local", forKey: "askEngine")
            d.set(true, forKey: "freeEngines2_5")
        }
        skillEngine = d.string(forKey: "skillEngine") ?? "local"
        askEngine = d.string(forKey: "askEngine") ?? "local"
        codexModel = d.string(forKey: "codexModel") ?? ""
        openAIModel = d.string(forKey: "openAIModel") ?? "gpt-5-mini"
        openAIConsent = d.bool(forKey: "openAIConsent")
        dailyTokenLimit = d.integer(forKey: "dailyTokenLimit")
        publishFolder = d.string(forKey: "publishFolder") ?? "~/.codex/skills"
        skillLanguage = d.string(forKey: "skillLanguage") ?? "English"
        cookieSource = d.string(forKey: "cookieSource") ?? "zeus"
        commentsCount = d.integer(forKey: "commentsCount")
        polishEnabled = d.bool(forKey: "polishEnabled")
        polishModel = d.string(forKey: "polishModel") ?? "qwen3:4b-instruct"
        polishCaptionsToo = d.bool(forKey: "polishCaptionsToo")
        writeIndexes = d.bool(forKey: "writeIndexes")
        githubEnabled = d.bool(forKey: "githubEnabled")
        githubFolder = d.string(forKey: "githubFolder") ?? ""
        readScreenOfFiles = d.bool(forKey: "readScreenOfFiles")
        screenInterval = max(1, d.integer(forKey: "screenInterval"))
        entitiesEnabled = d.bool(forKey: "entitiesEnabled")
        entityNoteThreshold = max(1, d.integer(forKey: "entityNoteThreshold"))
        digestEnabled = d.bool(forKey: "digestEnabled")
        digestWeekday = min(7, max(1, d.integer(forKey: "digestWeekday")))
        digestHour = min(23, max(0, d.integer(forKey: "digestHour")))
        phoneInboxEnabled = d.bool(forKey: "phoneInboxEnabled")
        swapJobsEnabled = d.bool(forKey: "swapJobsEnabled")
        swapJobsCleanDays = max(0, d.integer(forKey: "swapJobsCleanDays"))
        playbookVision = d.bool(forKey: "playbookVision")
        visionModel = d.string(forKey: "visionModel") ?? ""
        playbookFrames = min(150, max(10, d.integer(forKey: "playbookFrames")))
        playbookFolder = d.string(forKey: "playbookFolder") ?? "~/Zeus Playbooks"
        ytdlpPath = d.string(forKey: "ytdlpPath") ?? ""
        ffmpegPath = d.string(forKey: "ffmpegPath") ?? ""
        whisperPath = d.string(forKey: "whisperPath") ?? ""
    }

    private func save(_ key: String, _ value: Any) { defaults.set(value, forKey: key) }

    /// yt-dlp arguments that carry the YouTube sign-in.
    var cookieArguments: [String] {
        switch cookieSource {
        case "none", "": return []
        case "zeus":
            let file = YouTubeAccount.cookieFile
            return FileManager.default.fileExists(atPath: file.path) ? ["--cookies", file.path] : []
        default: return ["--cookies-from-browser", cookieSource]
        }
    }

    /// Posts (3.8): the sign-ins made in Zeus's web window for X, Instagram, Facebook and Threads, or the browser
    /// chosen as the cookie source.
    var socialCookieArguments: [String] {
        switch cookieSource {
        case "none", "", "zeus":
            let file = YouTubeAccount.socialCookieFile
            return FileManager.default.fileExists(atPath: file.path) ? ["--cookies", file.path] : []
        default: return ["--cookies-from-browser", cookieSource]
        }
    }

    func makeSocialYTDLP() -> YTDLP? {
        guard let path = ToolLocator.find("yt-dlp", override: ytdlpPath) else { return nil }
        return YTDLP(executable: path, cookieArguments: socialCookieArguments, comments: 0)
    }

    /// A configured yt-dlp, or nil when it is not installed.
    func makeYTDLP(withComments: Bool = false) -> YTDLP? {
        guard let path = ToolLocator.find("yt-dlp", override: ytdlpPath) else { return nil }
        return YTDLP(executable: path, cookieArguments: cookieArguments, comments: withComments ? commentsCount : 0)
    }

    var preferredLanguageList: [String] {
        preferredLanguages
            .split(whereSeparator: { $0 == "," || $0 == " " })
            .map { $0.lowercased() }
            .filter { !$0.isEmpty }
    }

    var publishFolderURL: URL {
        URL(fileURLWithPath: (publishFolder as NSString).expandingTildeInPath, isDirectory: true)
    }

    var secondBrainURL: URL {
        URL(fileURLWithPath: (secondBrainFolder as NSString).expandingTildeInPath, isDirectory: true)
    }

    /// Notes about people, tools and companies: Sources/People, Sources/Tools, Sources/Companies.
    func entityFolder(_ kind: EntityKind) -> URL {
        secondBrainURL.deletingLastPathComponent().appendingPathComponent(kind.plural, isDirectory: true)
    }

    /// Weekly digest notes: Sources/YouTube/Digests.
    var digestFolder: URL { secondBrainURL.appendingPathComponent("Digests", isDirectory: true) }

    /// Playbook notes: Sources/YouTube/Playbooks (frames in Playbooks/attachments).
    var playbookNotesFolder: URL { secondBrainURL.appendingPathComponent("Playbooks", isDirectory: true) }

    /// Where agents work on playbooks (~/Zeus Playbooks by default).
    var playbookWorkURL: URL {
        URL(fileURLWithPath: (playbookFolder as NSString).expandingTildeInPath, isDirectory: true)
    }

    /// Where notes about GitHub repositories found in videos live (next to Sources/YouTube by default).
    var githubURL: URL {
        githubFolder.isEmpty
            ? secondBrainURL.deletingLastPathComponent().appendingPathComponent("GitHub", isDirectory: true)
            : URL(fileURLWithPath: (githubFolder as NSString).expandingTildeInPath, isDirectory: true)
    }

    // Daily OpenAI token accounting
    func tokensUsedToday() -> Int {
        let usage = defaults.dictionary(forKey: "tokenUsage") as? [String: Int] ?? [:]
        return usage[Self.dayKey()] ?? 0
    }

    func addTokens(_ count: Int) {
        var usage = defaults.dictionary(forKey: "tokenUsage") as? [String: Int] ?? [:]
        let key = Self.dayKey()
        usage = usage.filter { $0.key >= Self.dayKey(daysAgo: 30) }
        usage[key, default: 0] += count
        defaults.set(usage, forKey: "tokenUsage")
    }

    private static func dayKey(daysAgo: Int = 0) -> String {
        let date = Calendar.current.date(byAdding: .day, value: -daysAgo, to: .now) ?? .now
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}

nonisolated enum WhisperModel: String, CaseIterable, Identifiable, Sendable {
    case base = "base"
    case small = "small"
    case turbo = "large-v3-turbo-q5_0"
    case turboFull = "large-v3-turbo"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .base: "Base — fastest (142 MB)"
        case .small: "Small — good (466 MB)"
        case .turbo: "Large v3 Turbo — best balance (574 MB)"
        case .turboFull: "Large v3 Turbo full — best quality (1.6 GB)"
        }
    }

    var fileName: String { "ggml-\(rawValue).bin" }

    var downloadURL: URL {
        URL(string: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/\(fileName)")!
    }
}
