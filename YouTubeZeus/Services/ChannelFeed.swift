import Foundation

nonisolated struct FeedEntry: Sendable, Hashable {
    let videoID: String
    let title: String
    let published: Date?
    let isShort: Bool
}

nonisolated struct ChannelFeed: Sendable {
    let title: String
    let entries: [FeedEntry]
}

/// Reads a channel's public RSS feed (latest ~15 uploads). No account or API key needed.
nonisolated enum ChannelFeedReader {
    static func url(for channelID: String) -> URL {
        URL(string: "https://www.youtube.com/feeds/videos.xml?channel_id=\(channelID)")!
    }

    static func fetch(channelID: String) async throws -> ChannelFeed {
        var request = URLRequest(url: url(for: channelID))
        request.timeoutInterval = 20
        request.setValue("YouTube Zeus/2.0 (Macintosh)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw URLError(.badServerResponse, userInfo: [NSLocalizedDescriptionKey: "Feed answered HTTP \(http.statusCode)"])
        }
        return FeedParser.parse(data)
    }
}

nonisolated final class FeedParser: NSObject, XMLParserDelegate {
    private var entries: [FeedEntry] = []
    private var feedTitle = ""
    private var inEntry = false
    private var text = ""
    private var videoID = ""
    private var title = ""
    private var published: Date?
    private var link = ""
    private let dateParser = ISO8601DateFormatter()

    static func parse(_ data: Data) -> ChannelFeed {
        let delegate = FeedParser()
        let parser = XMLParser(data: data)
        parser.delegate = delegate
        parser.parse()
        return ChannelFeed(title: delegate.feedTitle, entries: delegate.entries)
    }

    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                qualifiedName qName: String?, attributes attributeDict: [String: String] = [:]) {
        text = ""
        if elementName == "entry" {
            inEntry = true
            videoID = ""; title = ""; published = nil; link = ""
        } else if elementName == "link", inEntry, let href = attributeDict["href"] {
            link = href
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) { text += string }

    func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        switch elementName {
        case "yt:videoId", "videoId": if inEntry { videoID = value }
        case "title": if inEntry { title = value } else if feedTitle.isEmpty { feedTitle = value }
        case "published": if inEntry { published = dateParser.date(from: value) }
        case "entry":
            if !videoID.isEmpty {
                entries.append(FeedEntry(videoID: videoID, title: title, published: published,
                                         isShort: link.contains("/shorts/")))
            }
            inEntry = false
        default: break
        }
        text = ""
    }
}

/// Google Takeout › YouTube › subscriptions.csv (Channel Id, Channel Url, Channel Title).
nonisolated enum SubscriptionsCSV {
    struct Row: Sendable { let channelID: String; let title: String }

    static func parse(_ text: String) -> [Row] {
        var rows: [Row] = []
        for (index, line) in text.split(whereSeparator: \.isNewline).enumerated() {
            let fields = splitCSV(String(line))
            guard fields.count >= 3 else { continue }
            if index == 0, !fields[0].hasPrefix("UC") { continue }
            let id = fields[0].trimmingCharacters(in: .whitespaces)
            guard id.hasPrefix("UC") else { continue }
            rows.append(Row(channelID: id, title: fields[2].trimmingCharacters(in: .whitespaces)))
        }
        return rows
    }

    private static func splitCSV(_ line: String) -> [String] {
        var fields: [String] = []
        var current = ""
        var quoted = false
        var iterator = line.makeIterator()
        while let char = iterator.next() {
            if char == "\"" {
                if quoted, let next = iterator.next() {
                    if next == "\"" { current.append("\"") } else {
                        quoted = false
                        if next == "," { fields.append(current); current = "" } else { current.append(next) }
                    }
                } else { quoted.toggle() }
            } else if char == ",", !quoted {
                fields.append(current); current = ""
            } else {
                current.append(char)
            }
        }
        fields.append(current)
        return fields
    }
}
