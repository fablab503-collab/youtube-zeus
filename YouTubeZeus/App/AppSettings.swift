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

    // Tools (empty = find automatically)
    var ytdlpPath: String { didSet { save("ytdlpPath", ytdlpPath) } }
    var ffmpegPath: String { didSet { save("ffmpegPath", ffmpegPath) } }
    var whisperPath: String { didSet { save("whisperPath", whisperPath) } }

    init() {
        let d = UserDefaults.standard
        d.register(defaults: [
            "preferredLanguages": "en, fr, it, es, ro, de",
            "useWhisperFallback": true,
            "whisperModel": WhisperModel.turbo.rawValue,
            "maxParallel": 2,
            "autoSummarize": true,
            "summaryLanguage": "auto",
            "summaryEngine": "auto",
            "secondBrainEnabled": true,
            "secondBrainFolder": "/Volumes/Volume1/SecondBrain/Sources/YouTube",
            "pollMinutes": 30,
            "skipShorts": false,
            "notifyWhenEaten": true,
            "keepInMenuBar": true,
            "skillEngine": "codex",
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
            "githubFolder": "/Volumes/Volume1/SecondBrain/Sources/GitHub",
            "ytdlpPath": "",
            "ffmpegPath": "",
            "whisperPath": "",
        ])
        preferredLanguages = d.string(forKey: "preferredLanguages") ?? ""
        useWhisperFallback = d.bool(forKey: "useWhisperFallback")
        whisperModel = d.string(forKey: "whisperModel") ?? WhisperModel.turbo.rawValue
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
        skillEngine = d.string(forKey: "skillEngine") ?? "codex"
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
