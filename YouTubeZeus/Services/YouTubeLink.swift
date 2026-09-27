import Foundation

nonisolated enum YouTubeLink: Equatable, Sendable {
    case video(String)
    case playlist(String)
    case channel(URL)

    /// Understands watch, youtu.be, shorts, live, embed, playlist and channel links,
    /// plus a bare 11-character video ID.
    static func parse(_ raw: String) -> YouTubeLink? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        if text.count == 11, text.range(of: "^[A-Za-z0-9_-]{11}$", options: .regularExpression) != nil {
            return .video(text)
        }
        if text.hasPrefix("@"), !text.contains("/") {
            return URL(string: "https://www.youtube.com/\(text)").map { .channel($0) }
        }

        var candidate = text
        if !candidate.lowercased().hasPrefix("http") { candidate = "https://" + candidate }
        guard let components = URLComponents(string: candidate), let host = components.host?.lowercased() else {
            return nil
        }
        let query = Dictionary((components.queryItems ?? []).map { ($0.name, $0.value ?? "") },
                               uniquingKeysWith: { first, _ in first })
        let parts = components.path.split(separator: "/").map(String.init)

        if host.hasSuffix("youtu.be") {
            guard let id = parts.first, isVideoID(id) else { return nil }
            return .video(id)
        }
        guard host.hasSuffix("youtube.com") || host.hasSuffix("youtube-nocookie.com") else { return nil }

        if let v = query["v"], isVideoID(v) { return .video(v) }
        if let first = parts.first {
            switch first {
            case "shorts", "live", "embed", "v", "e":
                if parts.count > 1, isVideoID(parts[1]) { return .video(parts[1]) }
            case "playlist":
                if let list = query["list"], !list.isEmpty { return .playlist(list) }
            case "channel", "c", "user":
                if parts.count > 1 {
                    return URL(string: "https://www.youtube.com/\(first)/\(parts[1])").map { .channel($0) }
                }
            default:
                if first.hasPrefix("@") {
                    return URL(string: "https://www.youtube.com/\(first)").map { .channel($0) }
                }
            }
        }
        if let list = query["list"], !list.isEmpty { return .playlist(list) }
        return nil
    }

    static func isVideoID(_ value: String) -> Bool {
        value.range(of: "^[A-Za-z0-9_-]{11}$", options: .regularExpression) != nil
    }

    /// Finds the first YouTube link inside any text (for the clipboard).
    static func firstLink(in text: String) -> String? {
        let pattern = #"(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/[^\s"'<>]+"#
        guard let range = text.range(of: pattern, options: .regularExpression) else { return nil }
        let link = String(text[range])
        return parse(link) != nil ? link : nil
    }
}
