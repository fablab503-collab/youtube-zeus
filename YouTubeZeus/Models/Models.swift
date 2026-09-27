import Foundation
import SwiftData

// MARK: - Plain data (safe to use from any context)

nonisolated enum EatStatus: String, Codable, CaseIterable, Sendable {
    case discovered     // found by a watched channel, not eaten yet
    case queued
    case fetching
    case transcribing
    case summarizing
    case waiting        // live / premiere / captions not ready: retried later
    case done
    case failed

    var label: String {
        switch self {
        case .discovered: "New"
        case .queued: "Queued"
        case .fetching: "Eating"
        case .transcribing: "Listening"
        case .summarizing: "Summarizing"
        case .waiting: "Waiting"
        case .done: "Eaten"
        case .failed: "Failed"
        }
    }

    var symbol: String {
        switch self {
        case .discovered: "sparkle"
        case .queued: "clock"
        case .fetching: "bolt.fill"
        case .transcribing: "waveform"
        case .summarizing: "apple.intelligence"
        case .waiting: "hourglass"
        case .done: "checkmark.circle.fill"
        case .failed: "exclamationmark.triangle.fill"
        }
    }

    var isBusy: Bool { self == .fetching || self == .transcribing || self == .summarizing }
}

nonisolated enum TranscriptSource: String, Codable, Sendable {
    case none = ""
    case captions
    case autoCaptions = "auto-captions"
    case whisper

    var label: String {
        switch self {
        case .none: "—"
        case .captions: "Captions"
        case .autoCaptions: "Auto-captions"
        case .whisper: "Whisper"
        }
    }

    var symbol: String {
        switch self {
        case .none: "questionmark"
        case .captions: "captions.bubble.fill"
        case .autoCaptions: "text.bubble.fill"
        case .whisper: "waveform"
        }
    }
}

nonisolated struct TranscriptSegment: Codable, Hashable, Sendable {
    var start: Double
    var end: Double
    var text: String
}

nonisolated struct TranscriptParagraph: Identifiable, Hashable, Sendable {
    let id: Int
    let start: Double
    let end: Double
    let text: String

    var tag: String { String(format: "p%03d", id + 1) }
}

nonisolated struct VideoChapter: Codable, Hashable, Sendable {
    var start: Double
    var title: String
}

nonisolated struct VideoDigest: Codable, Hashable, Sendable {
    var summary: String
    var keyPoints: [String]
    var topics: [String]
    var chapters: [VideoChapter]
    var language: String
    var engine: String
    var generatedAt: Date
}

// MARK: - SwiftData models

@Model
final class Video {
    @Attribute(.unique) var videoID: String
    var title: String
    var channelTitle: String
    var channelID: String
    var publishedAt: Date?
    var duration: Double
    var thumbnailURLString: String?
    var videoDescription: String
    var statusRaw: String
    var statusDetail: String
    var sourceRaw: String
    var language: String
    var addedAt: Date
    var eatenAt: Date?
    var transcriptText: String
    @Attribute(.externalStorage) var segmentsData: Data?
    @Attribute(.externalStorage) var chaptersData: Data?
    @Attribute(.externalStorage) var digestData: Data?
    var digestError: String?
    var secondBrainPath: String?
    var exportPending: Bool
    var fromChannelWatch: Bool
    var isShort: Bool
    var attempts: Int
    @Relationship(deleteRule: .cascade, inverse: \SkillDraft.video) var skills: [SkillDraft] = []

    init(videoID: String, title: String = "", channelTitle: String = "", channelID: String = "",
         publishedAt: Date? = nil, status: EatStatus = .queued, fromChannelWatch: Bool = false) {
        self.videoID = videoID
        self.title = title
        self.channelTitle = channelTitle
        self.channelID = channelID
        self.publishedAt = publishedAt
        self.duration = 0
        self.thumbnailURLString = "https://i.ytimg.com/vi/\(videoID)/hqdefault.jpg"
        self.videoDescription = ""
        self.statusRaw = status.rawValue
        self.statusDetail = ""
        self.sourceRaw = ""
        self.language = ""
        self.addedAt = .now
        self.transcriptText = ""
        self.exportPending = false
        self.fromChannelWatch = fromChannelWatch
        self.isShort = false
        self.attempts = 0
    }

    var status: EatStatus {
        get { EatStatus(rawValue: statusRaw) ?? .queued }
        set { statusRaw = newValue.rawValue }
    }

    var source: TranscriptSource {
        get { TranscriptSource(rawValue: sourceRaw) ?? .none }
        set { sourceRaw = newValue.rawValue }
    }

    var displayTitle: String { title.isEmpty ? "Video \(videoID)" : title }

    var url: URL { URL(string: "https://www.youtube.com/watch?v=\(videoID)")! }

    func url(at seconds: Double) -> URL {
        URL(string: "https://www.youtube.com/watch?v=\(videoID)&t=\(Int(seconds))s")!
    }

    var thumbnailURL: URL? { thumbnailURLString.flatMap(URL.init(string:)) }

    var segments: [TranscriptSegment] {
        get {
            guard let segmentsData else { return [] }
            return (try? JSONDecoder().decode([TranscriptSegment].self, from: segmentsData)) ?? []
        }
        set { segmentsData = try? JSONEncoder().encode(newValue) }
    }

    var chapters: [VideoChapter] {
        get {
            guard let chaptersData else { return [] }
            return (try? JSONDecoder().decode([VideoChapter].self, from: chaptersData)) ?? []
        }
        set { chaptersData = newValue.isEmpty ? nil : try? JSONEncoder().encode(newValue) }
    }

    var digest: VideoDigest? {
        get {
            guard let digestData else { return nil }
            return try? JSONDecoder.iso.decode(VideoDigest.self, from: digestData)
        }
        set { digestData = newValue.flatMap { try? JSONEncoder.iso.encode($0) } }
    }

    var paragraphs: [TranscriptParagraph] { Paragrapher.paragraphs(from: segments) }

    var wordCount: Int { transcriptText.split(whereSeparator: \.isWhitespace).count }
}

@Model
final class Channel {
    @Attribute(.unique) var channelID: String
    var title: String
    var handle: String
    var avatarURLString: String?
    var autoEat: Bool
    var addedAt: Date
    var lastCheckedAt: Date?
    var lastError: String?
    var knownVideoIDs: [String]

    init(channelID: String, title: String, handle: String = "", avatarURLString: String? = nil, autoEat: Bool = true) {
        self.channelID = channelID
        self.title = title
        self.handle = handle
        self.avatarURLString = avatarURLString
        self.autoEat = autoEat
        self.addedAt = .now
        self.knownVideoIDs = []
    }

    var url: URL {
        if !handle.isEmpty, handle.hasPrefix("@") {
            return URL(string: "https://www.youtube.com/\(handle)")!
        }
        return URL(string: "https://www.youtube.com/channel/\(channelID)")!
    }

    var avatarURL: URL? { avatarURLString.flatMap(URL.init(string:)) }
}

nonisolated enum SkillStatus: String, Codable, Sendable {
    case draft, published, rejected

    var label: String {
        switch self {
        case .draft: "To review"
        case .published: "Published"
        case .rejected: "Rejected"
        }
    }
}

@Model
final class SkillDraft {
    @Attribute(.unique) var id: UUID
    var name: String
    var title: String
    var statusRaw: String
    @Attribute(.externalStorage) var capabilityData: Data
    @Attribute(.externalStorage) var evidenceData: Data
    var skillMDOverride: String?
    var droppedEvidence: Int
    var version: String
    var model: String
    var createdAt: Date
    var publishedAt: Date?
    var publishedPath: String?
    var video: Video?

    init(name: String, title: String, capabilityData: Data, evidenceData: Data, droppedEvidence: Int, model: String, video: Video) {
        self.id = UUID()
        self.name = name
        self.title = title
        self.statusRaw = SkillStatus.draft.rawValue
        self.capabilityData = capabilityData
        self.evidenceData = evidenceData
        self.droppedEvidence = droppedEvidence
        self.version = "1.0.0"
        self.model = model
        self.createdAt = .now
        self.video = video
    }

    var status: SkillStatus {
        get { SkillStatus(rawValue: statusRaw) ?? .draft }
        set { statusRaw = newValue.rawValue }
    }

    var capability: Capability? { try? JSONDecoder().decode(Capability.self, from: capabilityData) }
    var evidence: [EvidenceItem] { (try? JSONDecoder().decode([EvidenceItem].self, from: evidenceData)) ?? [] }
}

// MARK: - Helpers

nonisolated extension JSONDecoder {
    static var iso: JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }
}

nonisolated extension JSONEncoder {
    static var iso: JSONEncoder {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        return e
    }
}

nonisolated enum Paragrapher {
    /// Groups caption lines into readable paragraphs of roughly 20-45 seconds,
    /// preferring to break at the end of a sentence.
    static func paragraphs(from segments: [TranscriptSegment]) -> [TranscriptParagraph] {
        var result: [TranscriptParagraph] = []
        var buffer: [String] = []
        var start: Double = 0
        var end: Double = 0
        var chars = 0

        func flush() {
            let text = buffer.joined(separator: " ")
                .replacingOccurrences(of: "  ", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                result.append(TranscriptParagraph(id: result.count, start: start, end: end, text: text))
            }
            buffer.removeAll()
            chars = 0
        }

        for segment in segments {
            let text = segment.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            if buffer.isEmpty { start = segment.start }
            buffer.append(text)
            end = max(end, segment.end)
            chars += text.count
            let length = end - start
            let sentenceEnd = text.last.map { ".!?…。！？".contains($0) } ?? false
            if (length >= 20 && sentenceEnd) || length >= 45 || chars >= 700 {
                flush()
            }
        }
        flush()
        return result
    }
}

nonisolated extension Double {
    var timestamp: String {
        let total = Int(self.rounded(.down))
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%02d:%02d", m, s)
    }
}
