import CryptoKit
import Foundation

/// A podcast and its episodes, read from its public RSS feed (no account, no API key).
nonisolated struct PodcastShow: Sendable {
    var title: String
    var author: String
    var about: String
    var image: String?
    var link: String?
    var language: String?
    var feedURL: URL
    var episodes: [PodcastEpisode]

    var id: String { PodcastFeed.showID(feedURL) }
}

nonisolated struct PodcastEpisode: Sendable, Hashable {
    var guid: String
    var title: String
    var published: Date?
    var audioURL: String
    var audioType: String?
    var duration: Double
    var about: String
    var link: String?
    var image: String?
    /// Podcasting 2.0 `<podcast:transcript>` links: "type|url".
    var transcripts: [String]

    func id(feed: URL) -> String { PodcastFeed.episodeID(guid: guid.isEmpty ? audioURL : guid, feed: feed) }
    var isVideo: Bool { audioType?.hasPrefix("video/") == true }
}

nonisolated enum PodcastError: LocalizedError, Sendable {
    case notAFeed
    case notFound(String)
    case http(Int)

    var errorDescription: String? {
        switch self {
        case .notAFeed: "That link is not a podcast feed. Paste the RSS feed or the Apple Podcasts link of the show."
        case .notFound(let what): "Zeus could not find the podcast's feed (\(what))."
        case .http(let code): "The podcast's server answered HTTP \(code)."
        }
    }
}

nonisolated enum PodcastFeed {
    /// "pod-show-<hash>" for the show (a followed Channel), "pod-<hash>" for each episode (a library item).
    static func showID(_ feed: URL) -> String { "pod-show-" + hash(normalized(feed)) }

    static func episodeID(guid: String, feed: URL) -> String { "pod-" + hash(normalized(feed) + "|" + guid) }

    private static func normalized(_ feed: URL) -> String {
        var text = feed.absoluteString.lowercased()
        for prefix in ["https://", "http://"] where text.hasPrefix(prefix) { text.removeFirst(prefix.count) }
        return text.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    private static func hash(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).prefix(6).map { String(format: "%02x", $0) }.joined()
    }

    /// True for links that look like a podcast (Apple Podcasts, an RSS/XML feed, known feed hosts).
    static func looksLikePodcast(_ text: String) -> Bool {
        let lower = text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
        guard lower.hasPrefix("http") || lower.hasPrefix("feed:") || lower.hasPrefix("podcast") else { return false }
        if lower.contains("podcasts.apple.com") || lower.contains("itunes.apple.com") { return true }
        if lower.hasPrefix("feed:") || lower.hasPrefix("podcast:") { return true }
        let feedHints = [".rss", ".xml", "/rss", "/feed", "feeds.", "anchor.fm/s/", "rss.art19.com", "feeds.megaphone.fm",
                         "feeds.simplecast.com", "feed.podbean.com", "rss.acast.com", "feeds.buzzsprout.com", "omny.fm",
                         "feeds.transistor.fm", "audioboom.com/channels", "feeds.libsyn.com", "feeds.captivate.fm"]
        return feedHints.contains { lower.contains($0) }
    }

    /// The RSS feed behind a link: Apple Podcasts pages go through Apple's free lookup API.
    static func resolve(_ link: String) async throws -> URL {
        var text = link.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.lowercased().hasPrefix("feed:") { text = String(text.dropFirst(5)).replacingOccurrences(of: "//", with: "", options: .anchored) }
        if text.lowercased().hasPrefix("podcast:") { text = "https:" + text.dropFirst(8) }
        if !text.lowercased().hasPrefix("http") { text = "https://" + text }
        guard let url = URL(string: text) else { throw PodcastError.notAFeed }
        if let host = url.host?.lowercased(), host.contains("podcasts.apple.com") || host.contains("itunes.apple.com") {
            guard let match = url.absoluteString.range(of: #"id(\d{5,})"#, options: .regularExpression) else {
                throw PodcastError.notFound("no show id in the Apple Podcasts link")
            }
            let id = url.absoluteString[match].dropFirst(2)
            let lookup = URL(string: "https://itunes.apple.com/lookup?id=\(id)&entity=podcast")!
            let (data, _) = try await URLSession.shared.data(from: lookup)
            let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            let results = json?["results"] as? [[String: Any]] ?? []
            guard let feed = results.compactMap({ $0["feedUrl"] as? String }).first, let feedURL = URL(string: feed) else {
                throw PodcastError.notFound("Apple does not list a public feed for this show")
            }
            return feedURL
        }
        return url
    }

    static func fetch(_ feed: URL) async throws -> PodcastShow {
        var request = URLRequest(url: feed)
        request.timeoutInterval = 30
        request.setValue("YouTube Zeus/3.0 (Macintosh; podcast reader)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) { throw PodcastError.http(http.statusCode) }
        // The link that was followed stays the show's identity, even when the server redirects. Big feeds (1,500
        // episodes) are parsed off the main thread.
        guard let show = await Task.detached(priority: .userInitiated, operation: { PodcastRSSParser.parse(data, feed: feed) }).value else {
            throw PodcastError.notAFeed
        }
        return show
    }

    // MARK: Transcripts published with an episode

    /// Picks the best published transcript: JSON (timed) first, then WebVTT, then SRT.
    static func bestTranscript(_ links: [String]) -> (type: String, url: URL)? {
        let parsed = links.compactMap { link -> (String, URL)? in
            let parts = link.split(separator: "|", maxSplits: 1).map(String.init)
            guard parts.count == 2, let url = URL(string: parts[1]) else { return nil }
            return (parts[0].lowercased(), url)
        }
        let order = ["json", "vtt", "srt", "subrip"]
        for wanted in order {
            if let match = parsed.first(where: { $0.0.contains(wanted) || $0.1.pathExtension.lowercased() == wanted }) {
                return (type: match.0, url: match.1)
            }
        }
        return nil
    }

    static func downloadTranscript(_ link: (type: String, url: URL)) async throws -> [TranscriptSegment] {
        let (data, response) = try await URLSession.shared.data(from: link.url)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) { throw PodcastError.http(http.statusCode) }
        let text = String(decoding: data, as: UTF8.self)
        if link.type.contains("json") || link.url.pathExtension.lowercased() == "json" {
            return parseJSONTranscript(data)
        }
        if link.type.contains("vtt") || text.hasPrefix("WEBVTT") {
            return CaptionParser.parseVTT(text)
        }
        return parseSRT(text)
    }

    /// Podcasting 2.0 JSON: {"segments":[{"startTime":…,"endTime":…,"body":"…","speaker":"…"}]}
    static func parseJSONTranscript(_ data: Data) -> [TranscriptSegment] {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let segments = json["segments"] as? [[String: Any]] else { return [] }
        return segments.compactMap { item in
            let body = (item["body"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !body.isEmpty else { return nil }
            let start = item["startTime"] as? Double ?? Double(item["startTime"] as? Int ?? 0)
            let end = item["endTime"] as? Double ?? start
            return TranscriptSegment(start: start, end: end, text: body)
        }
    }

    static func parseSRT(_ text: String) -> [TranscriptSegment] {
        var segments: [TranscriptSegment] = []
        for block in text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n\n") {
            let lines = block.split(separator: "\n").map(String.init)
            guard let timing = lines.firstIndex(where: { $0.contains("-->") }) else { continue }
            let times = lines[timing].components(separatedBy: "-->")
            guard times.count == 2 else { continue }
            let body = lines[(timing + 1)...].joined(separator: " ")
                .replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
            guard !body.isEmpty else { continue }
            segments.append(TranscriptSegment(start: CaptionParser.seconds(times[0]), end: CaptionParser.seconds(times[1]), text: body))
        }
        return segments
    }

    /// "1:02:03", "62:03" or "3723" → seconds.
    static func duration(_ text: String) -> Double {
        let parts = text.trimmingCharacters(in: .whitespaces).split(separator: ":").compactMap { Double($0) }
        guard !parts.isEmpty else { return 0 }
        return parts.reduce(0) { $0 * 60 + $1 }
    }

    /// Show notes are HTML: keep the text and the links' addresses (GitHub links are checked later).
    static func plainText(_ html: String) -> String {
        var text = html
            .replacingOccurrences(of: #"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>"#, with: "$2 ($1)", options: .regularExpression)
            .replacingOccurrences(of: #"<(br|/p|/li|/h\d)[^>]*>"#, with: "\n", options: [.regularExpression, .caseInsensitive])
            .replacingOccurrences(of: #"<li[^>]*>"#, with: "- ", options: [.regularExpression, .caseInsensitive])
            .replacingOccurrences(of: #"<[^>]+>"#, with: "", options: .regularExpression)
        text = CaptionParser.decodeEntities(text)
            .replacingOccurrences(of: "&#8217;", with: "’").replacingOccurrences(of: "&#8220;", with: "“")
            .replacingOccurrences(of: "&#8221;", with: "”").replacingOccurrences(of: "&#8230;", with: "…")
        return text.replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// RSS 2.0 with the iTunes and Podcasting 2.0 namespaces.
nonisolated final class PodcastRSSParser: NSObject, XMLParserDelegate {
    private var show = PodcastShow(title: "", author: "", about: "", image: nil, link: nil, language: nil,
                                   feedURL: URL(string: "https://example.com")!, episodes: [])
    private var episode: PodcastEpisode?
    private var text = ""
    private var path: [String] = []
    private var isRSS = false

    static func parse(_ data: Data, feed: URL) -> PodcastShow? {
        let delegate = PodcastRSSParser()
        delegate.show.feedURL = feed
        let parser = XMLParser(data: data)
        parser.shouldProcessNamespaces = false
        parser.delegate = delegate
        parser.parse()
        guard delegate.isRSS, !delegate.show.title.isEmpty || !delegate.show.episodes.isEmpty else { return nil }
        return delegate.show
    }

    func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName: String?,
                attributes: [String: String] = [:]) {
        let element = name.lowercased()
        path.append(element)
        text = ""
        switch element {
        case "rss", "channel": isRSS = true
        case "item": episode = PodcastEpisode(guid: "", title: "", published: nil, audioURL: "", audioType: nil, duration: 0,
                                             about: "", link: nil, image: nil, transcripts: [])
        case "enclosure":
            if let url = attributes["url"], episode != nil {
                episode?.audioURL = url
                episode?.audioType = attributes["type"]
            }
        case "itunes:image":
            if let href = attributes["href"] {
                if episode != nil { episode?.image = href } else if show.image == nil { show.image = href }
            }
        case "podcast:transcript":
            if let url = attributes["url"], episode != nil {
                episode?.transcripts.append((attributes["type"] ?? "") + "|" + url)
            }
        default: break
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) { text += string }

    func parser(_ parser: XMLParser, foundCDATA CDATABlock: Data) { text += String(decoding: CDATABlock, as: UTF8.self) }

    func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName: String?) {
        let element = name.lowercased()
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let parent = path.dropLast().last ?? ""
        if episode != nil {
            switch element {
            case "title" where parent == "item": episode?.title = value
            case "guid": episode?.guid = value
            case "pubdate": episode?.published = Self.date(value)
            case "itunes:duration": episode?.duration = PodcastFeed.duration(value)
            case "description", "content:encoded", "itunes:summary":
                if value.count > (episode?.about.count ?? 0) { episode?.about = value }
            case "link" where parent == "item": episode?.link = value
            case "item":
                if let item = episode, !item.audioURL.isEmpty { show.episodes.append(item) }
                episode = nil
            default: break
            }
        } else {
            switch element {
            case "title" where parent == "channel": show.title = value
            case "itunes:author" where parent == "channel": show.author = value
            case "description" where parent == "channel", "itunes:summary" where parent == "channel":
                if value.count > show.about.count { show.about = value }
            case "link" where parent == "channel": if show.link == nil, !value.isEmpty { show.link = value }
            case "language" where parent == "channel": show.language = value.lowercased()
            case "url" where parent == "image": if show.image == nil, !value.isEmpty { show.image = value }
            default: break
            }
        }
        path.removeLast()
        text = ""
    }

    private static let formats = ["EEE, dd MMM yyyy HH:mm:ss Z", "EEE, d MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm:ss zzz",
                                  "EEE, d MMM yyyy HH:mm:ss zzz", "dd MMM yyyy HH:mm:ss Z", "EEE, dd MMM yyyy HH:mm Z",
                                  "yyyy-MM-dd'T'HH:mm:ssZ", "EEE, dd MMM yyyy"]

    static func date(_ text: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        for format in formats {
            formatter.dateFormat = format
            if let date = formatter.date(from: text) { return date }
        }
        return ISO8601DateFormatter().date(from: text)
    }
}
