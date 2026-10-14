import Foundation
import SwiftData

/// 4.5: the state of the library for you and for agents — `zeus status` (counts, work left, every kind of error) and
/// `zeus failed` (each item that could not be eaten, why, and where it came from).
enum LibraryReport {
    /// "Video unavailable" → the same group for 50 videos; numbers and ids are blanked so similar errors group together.
    static func group(_ message: String) -> String {
        var text = EatFailure.explain(message)
        text = text.replacingOccurrences(of: #"\[youtube\] [A-Za-z0-9_-]{11}"#, with: "[youtube] <id>", options: .regularExpression)
        text = text.replacingOccurrences(of: #"\d+"#, with: "N", options: .regularExpression)
        return String(text.prefix(160))
    }

    static func counted(_ messages: [String]) -> [(message: String, count: Int)] {
        var counts: [String: Int] = [:]
        for message in messages { counts[group(message), default: 0] += 1 }
        return counts.map { (message: $0.key, count: $0.value) }.sorted { $0.count > $1.count }
    }

    static func status(context: ModelContext, settings: AppSettings) -> (text: String, json: [String: Any]) {
        let all = (try? context.fetch(FetchDescriptor<Video>())) ?? []
        let live = all.filter { $0.status != .trashed }
        var byStatus: [String: Int] = [:]
        for video in all { byStatus[video.status.rawValue, default: 0] += 1 }
        var byKind: [String: Int] = [:]
        for video in live { byKind[video.kind.rawValue, default: 0] += 1 }
        let eaten = live.filter { $0.status.hasText }
        let noSummary = eaten.filter { !$0.textOnly && $0.digestData == nil && $0.digestError == nil }
        // Only what will be polished (the same rules as the app: auto-captions and Whisper, captions when chosen).
        func willPolish(_ video: Video) -> Bool {
            guard settings.polishEnabled, !video.textOnly else { return false }
            switch video.source {
            case .autoCaptions, .whisper: return true
            case .captions, .published: return settings.polishCaptionsToo
            case .post, .none: return false
            }
        }
        let noPolish = eaten.filter { willPolish($0) && $0.polishedData == nil && $0.polishError == nil }
        let failed = live.filter { $0.status == .failed }
        let failedGroups = counted(failed.map(\.statusDetail))
        let summaryErrors = counted(eaten.compactMap(\.digestError))
        let polishErrors = counted(eaten.compactMap(\.polishError))
        let playbookErrors = counted(eaten.compactMap(\.playbookError))
        let screenErrors = counted(eaten.compactMap(\.screenError))

        var lines: [String] = ["# YouTube Zeus — the library now", ""]
        lines.append("Items: \(live.count) (" + byKind.sorted { $0.value > $1.value }.map { "\($0.value) \($0.key)" }.joined(separator: ", ") + ")")
        lines.append("By state: " + byStatus.sorted { $0.value > $1.value }.map { "\($0.key) \($0.value)" }.joined(separator: " · "))
        lines.append("Work left: \(noSummary.count) eaten without a summary yet, \(noPolish.count) without polished text yet")
        func section(_ title: String, _ groups: [(message: String, count: Int)]) {
            guard !groups.isEmpty else { return }
            lines += ["", "## \(title) (\(groups.reduce(0) { $0 + $1.count }))"]
            for group in groups.prefix(10) { lines.append("- \(group.count) × \(group.message)") }
        }
        section("Could not be eaten", failedGroups)
        section("Summary errors", summaryErrors)
        section("Polish errors", polishErrors)
        section("Playbook errors", playbookErrors)
        section("Text-on-screen errors", screenErrors)
        lines += ["", "Each failed item, why and where it came from: `zeus failed`."]

        func rows(_ groups: [(message: String, count: Int)]) -> [[String: Any]] {
            groups.map { ["message": $0.message, "count": $0.count] }
        }
        let json: [String: Any] = [
            "items": live.count, "by_kind": byKind, "by_state": byStatus,
            "without_summary": noSummary.count, "without_polish": noPolish.count,
            "failed": rows(failedGroups), "summary_errors": rows(summaryErrors), "polish_errors": rows(polishErrors),
            "playbook_errors": rows(playbookErrors), "screen_errors": rows(screenErrors),
        ]
        return (lines.joined(separator: "\n"), json)
    }

    static func failed(context: ModelContext) -> (text: String, json: [[String: Any]]) {
        let failedRaw = EatStatus.failed.rawValue
        let failed = (try? context.fetch(FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == failedRaw },
                                                                 sortBy: [SortDescriptor(\.addedAt)]))) ?? []
        let lists = (try? context.fetch(FetchDescriptor<VideoList>())) ?? []
        var lines: [String] = ["# Could not be eaten (\(failed.count))", ""]
        var json: [[String: Any]] = []
        for video in failed {
            let from = lists.filter { $0.videoIDs.contains(video.videoID) }.map(\.title)
            let gone = EatFailure.isGone(video.statusDetail)
            var origin: [String] = []
            if video.fromChannelWatch { origin.append("channel watch") }
            origin += from.map { "list “\($0)”" }
            lines.append("- `\(video.videoID)` \(video.displayTitle) — \(video.channelTitle.isEmpty ? video.kind.rawValue : video.channelTitle)")
            lines.append("  \(EatFailure.explain(video.statusDetail))")
            if EatFailure.explain(video.statusDetail) != video.statusDetail { lines.append("  raw: \(video.statusDetail.prefix(240))") }
            lines.append("  added \(video.addedAt.formatted(date: .abbreviated, time: .shortened)) · \(video.attempts) attempt\(video.attempts == 1 ? "" : "s")"
                         + (origin.isEmpty ? "" : " · from " + origin.joined(separator: ", ")))
            json.append(["id": video.videoID, "title": video.displayTitle, "channel": video.channelTitle,
                         "kind": video.kind.rawValue, "reason": EatFailure.explain(video.statusDetail),
                         "raw": video.statusDetail, "gone": gone, "attempts": video.attempts,
                         "added": ISO8601DateFormatter().string(from: video.addedAt), "from": origin])
        }
        if failed.isEmpty { lines.append("Nothing failed.") }
        return (lines.joined(separator: "\n"), json)
    }
}
