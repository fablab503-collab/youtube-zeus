import Foundation
import SwiftData

/// Turns an eaten video into a Markdown note (vault front matter) and writes it to the Second Brain.
final class SecondBrainExporter {
    let settings: AppSettings

    init(settings: AppSettings) { self.settings = settings }

    /// The note, as saved in the Second Brain or exported by hand.
    static func markdown(for video: Video) -> String {
        let day = DateFormatter()
        day.dateFormat = "yyyy-MM-dd"
        let paragraphs = video.paragraphs
        var lines: [String] = [
            "---",
            "date: \(day.string(from: video.eatenAt ?? .now))",
            "type: source",
            "tags:",
            "  - source",
            "  - youtube",
        ]
        if let digest = video.digest {
            for topic in digest.topics.prefix(6) {
                let tag = topic.lowercased()
                    .replacingOccurrences(of: #"[^\p{L}\p{N}]+"#, with: "-", options: .regularExpression)
                    .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
                if !tag.isEmpty { lines.append("  - \(tag)") }
            }
        }
        lines += [
            "ai-first: true",
            "title: \(yamlString(video.displayTitle))",
            "channel: \(yamlString(video.channelTitle))",
            "url: \(video.url.absoluteString)",
        ]
        if let published = video.publishedAt { lines.append("published: \(day.string(from: published))") }
        if video.duration > 0 { lines.append("duration: \"\(video.duration.timestamp)\"") }
        if !video.language.isEmpty { lines.append("language: \(video.language)") }
        lines += [
            "transcript-source: \(video.source.rawValue.isEmpty ? "unknown" : video.source.rawValue)",
            "eaten-by: YouTube Zeus 2.0",
            "confidence: \(video.source == .captions ? "high" : "medium")",
            "---",
            "",
            "# \(video.displayTitle)",
            "",
            "## For future agent",
            "",
            "Transcript of the YouTube video [\(escapeLink(video.displayTitle))](\(video.url.absoluteString)) by \(video.channelTitle.isEmpty ? "an unknown channel" : video.channelTitle), eaten by YouTube Zeus on \(day.string(from: video.eatenAt ?? .now)). Text source: \(sourceSentence(video.source)). Timestamps link to the moment in the video.",
            "",
        ]

        if let digest = video.digest {
            lines += ["## Summary", "", digest.summary, ""]
            if !digest.keyPoints.isEmpty {
                lines += ["## Key points", ""] + digest.keyPoints.map { "- \($0)" } + [""]
            }
            if !digest.chapters.isEmpty {
                lines += ["## Chapters", ""]
                lines += digest.chapters.map { "- [\($0.start.timestamp)](\(video.url(at: $0.start).absoluteString)) \($0.title)" }
                lines.append("")
            }
            lines += [digest.engine == "Apple Intelligence" ? "_Summary by Apple Intelligence, on device._" : "_Summary by \(digest.engine)._", ""]
        } else if !video.chapters.isEmpty {
            lines += ["## Chapters", ""]
            lines += video.chapters.map { "- [\($0.start.timestamp)](\(video.url(at: $0.start).absoluteString)) \($0.title)" }
            lines.append("")
        }

        lines += ["## Transcript", ""]
        for paragraph in paragraphs {
            lines.append("**[\(paragraph.start.timestamp)](\(video.url(at: paragraph.start).absoluteString))** \(paragraph.text)")
            lines.append("")
        }
        return lines.joined(separator: "\n")
    }

    static func sourceSentence(_ source: TranscriptSource) -> String {
        switch source {
        case .captions: "captions written by the channel"
        case .autoCaptions: "YouTube automatic captions (may contain recognition errors, little punctuation)"
        case .whisper: "local Whisper speech recognition (may contain recognition errors)"
        case .none: "unknown"
        }
    }

    static func fileName(for video: Video) -> String {
        let day = DateFormatter()
        day.dateFormat = "yyyy-MM-dd"
        let date = day.string(from: video.publishedAt ?? video.addedAt)
        return "\(date) - \(sanitize(video.displayTitle, limit: 120)).md"
    }

    static func sanitize(_ text: String, limit: Int) -> String {
        let cleaned = text
            .replacingOccurrences(of: #"[/\\:*?"<>|#\^\[\]]"#, with: " ", options: .regularExpression)
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: " ."))
        return String(cleaned.prefix(limit)).trimmingCharacters(in: .whitespaces).isEmpty ? "Untitled" : String(cleaned.prefix(limit))
    }

    private static func yamlString(_ text: String) -> String {
        "\"" + text.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }

    private static func escapeLink(_ text: String) -> String {
        text.replacingOccurrences(of: "[", with: "(").replacingOccurrences(of: "]", with: ")")
    }

    // MARK: Writing

    /// True when the folder can be written without creating a fake mount point under /Volumes.
    var folderReachable: Bool {
        let path = settings.secondBrainURL.path
        let parts = path.split(separator: "/")
        if parts.first == "Volumes", parts.count >= 2 {
            let mount = "/Volumes/\(parts[1])"
            var isDir: ObjCBool = false
            return FileManager.default.fileExists(atPath: mount, isDirectory: &isDir) && isDir.boolValue
                && FileManager.default.isWritableFile(atPath: mount)
        }
        return true
    }

    @discardableResult
    func export(_ video: Video) -> Bool {
        guard settings.secondBrainEnabled, video.status == .done || video.status == .summarizing else { return false }
        guard folderReachable else {
            video.exportPending = true
            return false
        }
        let channelFolder = Self.sanitize(video.channelTitle.isEmpty ? "Unknown channel" : video.channelTitle, limit: 80)
        let folder = settings.secondBrainURL.appendingPathComponent(channelFolder, isDirectory: true)
        let target: URL
        if let existing = video.secondBrainPath, existing.hasPrefix(settings.secondBrainURL.path) {
            target = URL(fileURLWithPath: existing)
        } else {
            target = folder.appendingPathComponent(Self.fileName(for: video))
        }
        do {
            try FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Self.markdown(for: video).write(to: target, atomically: true, encoding: .utf8)
            video.secondBrainPath = target.path
            video.exportPending = false
            return true
        } catch {
            video.exportPending = true
            return false
        }
    }

    func retryPending(in context: ModelContext) {
        guard settings.secondBrainEnabled, folderReachable else { return }
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.exportPending == true })
        for video in (try? context.fetch(descriptor)) ?? [] { export(video) }
        try? context.save()
    }
}
