import Foundation

nonisolated struct CaptionTrack: Sendable, Equatable {
    let language: String
    let isAuto: Bool
}

nonisolated struct VideoInfo: Sendable {
    var id: String
    var title: String
    var channel: String
    var channelID: String
    var description: String
    var language: String?
    var liveStatus: String?
    var publishedAt: Date?
    var duration: Double
    var thumbnail: String?
    var chapters: [VideoChapter]
    var subtitles: [String]
    var autoCaptions: [String]
    var isShort: Bool
    var tags: [String] = []
    var viewCount: Int = 0
    var likeCount: Int = 0
    var comments: [VideoComment] = []

    var isLiveOrUpcoming: Bool { liveStatus == "is_live" || liveStatus == "is_upcoming" }
}

nonisolated struct PlaylistEntry: Sendable {
    let id: String
    let title: String
    let channel: String
    let channelID: String
}

nonisolated struct ChannelListing: Sendable {
    let channelID: String
    let title: String
    let handle: String
    let avatar: String?
    let entries: [PlaylistEntry]
    var listID: String = ""
    var listTitle: String = ""
}

nonisolated struct ChannelEntry: Sendable {
    let channelID: String
    let title: String
    let url: String
}

nonisolated struct TranscriptResult: Sendable {
    let segments: [TranscriptSegment]
    let source: TranscriptSource
    let language: String
}

nonisolated enum YTDLPError: LocalizedError, Sendable {
    case badOutput(String)
    case noCaptionFile
    case emptyTranscript

    var errorDescription: String? {
        switch self {
        case .badOutput(let what): "yt-dlp returned something unexpected (\(what)). Try updating yt-dlp in Settings › Tools."
        case .noCaptionFile: "The captions could not be downloaded."
        case .emptyTranscript: "The transcript was empty."
        }
    }
}

nonisolated struct YTDLP: Sendable {
    let executable: String
    /// `--cookies <file>` or `--cookies-from-browser <name>` when signed in to YouTube.
    var cookieArguments: [String] = []
    var comments: Int = 0

    static func watchURL(_ id: String) -> String { "https://www.youtube.com/watch?v=\(id)" }

    // MARK: Metadata

    func info(videoID: String) async throws -> VideoInfo {
        var arguments = ["-J", "--skip-download", "--no-warnings", "--no-playlist"] + cookieArguments
        if comments > 0 {
            arguments += ["--get-comments", "--extractor-args", "youtube:max_comments=\(comments),\(comments),0,0;comment_sort=top"]
        }
        let output = try await ProcessRunner.check(executable, arguments + ["--", Self.watchURL(videoID)])
        guard let json = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any] else {
            throw YTDLPError.badOutput("video info")
        }
        return Self.parseInfo(json, fallbackID: videoID)
    }

    static func parseInfo(_ json: [String: Any], fallbackID: String) -> VideoInfo {
        var published: Date?
        if let ts = json["timestamp"] as? Double { published = Date(timeIntervalSince1970: ts) }
        else if let ts = json["release_timestamp"] as? Double { published = Date(timeIntervalSince1970: ts) }
        else if let day = json["upload_date"] as? String {
            let f = DateFormatter()
            f.dateFormat = "yyyyMMdd"
            f.timeZone = TimeZone(identifier: "UTC")
            published = f.date(from: day)
        }
        let chapters = (json["chapters"] as? [[String: Any]] ?? []).compactMap { item -> VideoChapter? in
            guard let start = item["start_time"] as? Double, let title = item["title"] as? String else { return nil }
            return VideoChapter(start: start, title: title)
        }
        let subtitles = (json["subtitles"] as? [String: Any] ?? [:]).keys.filter { $0 != "live_chat" }.sorted()
        let auto = (json["automatic_captions"] as? [String: Any] ?? [:]).keys.sorted()
        let webpage = json["webpage_url"] as? String ?? ""
        let width = json["width"] as? Double ?? 0
        let height = json["height"] as? Double ?? 0
        let duration = json["duration"] as? Double ?? 0
        let isShort = webpage.contains("/shorts/") || (height > width && duration > 0 && duration <= 180)
        let comments = (json["comments"] as? [[String: Any]] ?? []).compactMap { item -> VideoComment? in
            guard let text = item["text"] as? String, !text.isEmpty, (item["parent"] as? String ?? "root") == "root" else { return nil }
            return VideoComment(author: item["author"] as? String ?? "", text: text, likes: item["like_count"] as? Int ?? 0)
        }
        return VideoInfo(
            id: json["id"] as? String ?? fallbackID,
            title: json["title"] as? String ?? "",
            channel: json["channel"] as? String ?? json["uploader"] as? String ?? "",
            channelID: json["channel_id"] as? String ?? "",
            description: json["description"] as? String ?? "",
            language: (json["language"] as? String)?.lowercased(),
            liveStatus: json["live_status"] as? String,
            publishedAt: published,
            duration: duration,
            thumbnail: json["thumbnail"] as? String,
            chapters: chapters,
            subtitles: subtitles,
            autoCaptions: auto,
            isShort: isShort,
            tags: json["tags"] as? [String] ?? [],
            viewCount: json["view_count"] as? Int ?? 0,
            likeCount: json["like_count"] as? Int ?? 0,
            comments: Array(comments.sorted { $0.likes > $1.likes }.prefix(50)))
    }

    // MARK: Playlists and channels

    func flatList(url: String, limit: Int? = nil) async throws -> ChannelListing {
        var arguments = ["-J", "--flat-playlist", "--no-warnings"] + cookieArguments
        if let limit { arguments += ["--playlist-items", "1:\(limit)"] }
        arguments += ["--", url]
        let output = try await ProcessRunner.check(executable, arguments)
        guard let json = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any] else {
            throw YTDLPError.badOutput("list")
        }
        let channelID = json["channel_id"] as? String ?? ""
        let title = json["channel"] as? String ?? json["uploader"] as? String ?? json["title"] as? String ?? ""
        let handle = json["uploader_id"] as? String ?? ""
        let thumbnails = json["thumbnails"] as? [[String: Any]] ?? []
        let avatar = thumbnails.first(where: { ($0["id"] as? String) == "avatar_uncropped" })?["url"] as? String
        let entries = (json["entries"] as? [[String: Any]] ?? []).compactMap { entry -> PlaylistEntry? in
            guard let id = entry["id"] as? String, YouTubeLink.isVideoID(id) else { return nil }
            return PlaylistEntry(
                id: id,
                title: entry["title"] as? String ?? "",
                channel: entry["channel"] as? String ?? title,
                channelID: entry["channel_id"] as? String ?? channelID)
        }
        return ChannelListing(channelID: channelID, title: title, handle: handle, avatar: avatar, entries: entries,
                              listID: json["id"] as? String ?? "", listTitle: json["title"] as? String ?? "")
    }

    /// The playlists a channel shows on its Playlists tab (id, title), plus the channel's name and id.
    func playlists(ofChannel url: String) async throws -> (channel: String, channelID: String, lists: [(id: String, title: String)]) {
        var base = url.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        for tab in ["/videos", "/playlists", "/featured", "/shorts", "/streams", "/about"] where base.hasSuffix(tab) {
            base = String(base.dropLast(tab.count))
        }
        let output = try await ProcessRunner.check(executable, ["-J", "--flat-playlist", "--no-warnings"] + cookieArguments
                                                   + ["--", base + "/playlists"])
        guard let json = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any] else {
            throw YTDLPError.badOutput("playlists")
        }
        let channel = json["channel"] as? String ?? json["uploader"] as? String ?? ""
        let channelID = json["channel_id"] as? String ?? ""
        var seen = Set<String>()
        let lists: [(id: String, title: String)] = (json["entries"] as? [[String: Any]] ?? []).compactMap { entry in
            guard let id = entry["id"] as? String, !YouTubeLink.isVideoID(id), id.count > 12, seen.insert(id).inserted else { return nil }
            return (id, entry["title"] as? String ?? id)
        }
        return (channel, channelID, lists)
    }

    /// The channels of the signed-in account (https://www.youtube.com/feed/channels).
    func subscribedChannels() async throws -> [ChannelEntry] {
        let output = try await ProcessRunner.check(executable, ["-J", "--flat-playlist", "--no-warnings"] + cookieArguments
                                                   + ["--", "https://www.youtube.com/feed/channels"])
        guard let json = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any] else {
            throw YTDLPError.badOutput("subscriptions")
        }
        return (json["entries"] as? [[String: Any]] ?? []).compactMap { entry in
            let id = entry["id"] as? String ?? entry["channel_id"] as? String ?? ""
            guard id.hasPrefix("UC") else { return nil }
            return ChannelEntry(channelID: id, title: entry["title"] as? String ?? entry["channel"] as? String ?? id,
                                url: entry["url"] as? String ?? "https://www.youtube.com/channel/\(id)")
        }
    }

    // MARK: Captions

    /// Picks the best caption track. YouTube now lists several "-orig" speech-recognition tracks
    /// (AI dubbing), so the video's own language decides first, then the preferred languages.
    static func chooseTrack(_ info: VideoInfo, preferred: [String]) -> CaptionTrack? {
        func base(_ code: String) -> String {
            String(code.lowercased().split(separator: "-").first ?? Substring(code.lowercased()))
        }
        let manual = info.subtitles
        let auto = info.autoCaptions
        let originals = auto.filter { $0.hasSuffix("-orig") }
        let videoLanguage = info.language.map(base)
        var order: [String] = []
        if let videoLanguage { order.append(videoLanguage) }
        order += preferred.map(base).filter { !order.contains($0) }

        // 1. Captions written by the channel, in the video's language.
        if let lang = videoLanguage, let match = manual.first(where: { base($0) == lang }) {
            return CaptionTrack(language: match, isAuto: false)
        }
        // 2. The original speech-recognition track in the video's language.
        if let lang = videoLanguage, let match = originals.first(where: { base($0) == lang }) {
            return CaptionTrack(language: match, isAuto: true)
        }
        // 3. A single original track is the spoken language.
        if originals.count == 1, manual.isEmpty { return CaptionTrack(language: originals[0], isAuto: true) }
        // 4. Older videos: an auto track named exactly like the video's language.
        if let full = info.language?.lowercased(), let match = auto.first(where: { $0.lowercased() == full || $0.lowercased() == videoLanguage }) {
            return CaptionTrack(language: match, isAuto: true)
        }
        // 5. Captions or original tracks in the preferred languages.
        for lang in order {
            if let match = manual.first(where: { base($0) == lang }) { return CaptionTrack(language: match, isAuto: false) }
        }
        for lang in order {
            if let match = originals.first(where: { base($0) == lang }) { return CaptionTrack(language: match, isAuto: true) }
        }
        if let first = manual.first { return CaptionTrack(language: first, isAuto: false) }
        if let first = originals.first { return CaptionTrack(language: first, isAuto: true) }
        return nil
    }

    func downloadCaptions(videoID: String, track: CaptionTrack, workDir: URL) async throws -> TranscriptResult {
        try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
        _ = try await ProcessRunner.check(executable, cookieArguments + [
            "--skip-download", "--no-warnings", "--no-progress", "--no-playlist",
            track.isAuto ? "--write-auto-subs" : "--write-subs",
            "--sub-langs", track.language,
            "--sub-format", "json3/vtt/srv3/best",
            "-o", workDir.appendingPathComponent("sub.%(ext)s").path,
            "--", Self.watchURL(videoID),
        ])
        let files = (try? FileManager.default.contentsOfDirectory(at: workDir, includingPropertiesForKeys: nil)) ?? []
        guard let file = files.first(where: { $0.lastPathComponent.hasPrefix("sub.") }) else {
            throw YTDLPError.noCaptionFile
        }
        let data = try Data(contentsOf: file)
        let segments: [TranscriptSegment]
        switch file.pathExtension.lowercased() {
        case "json3": segments = CaptionParser.parseJSON3(data)
        case "srv3", "xml": segments = CaptionParser.parseSRV3(data)
        default: segments = CaptionParser.parseVTT(String(decoding: data, as: UTF8.self))
        }
        guard !segments.isEmpty else { throw YTDLPError.emptyTranscript }
        let language = String(track.language.replacingOccurrences(of: "-orig", with: "").split(separator: "-").first ?? "")
        return TranscriptResult(segments: segments, source: track.isAuto ? .autoCaptions : .captions, language: language)
    }

    // MARK: Audio (for Whisper)

    func downloadAudio(videoID: String, workDir: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
        let output = try await ProcessRunner.check(executable, cookieArguments + [
            "-f", "bestaudio/best", "--no-playlist", "--no-warnings", "--newline",
            "-o", workDir.appendingPathComponent("audio.%(ext)s").path,
            "--print", "after_move:filepath",
            "--", Self.watchURL(videoID),
        ], streamStdout: true) { line in
            if line.hasPrefix("[download]"), let percent = Self.percent(in: line) { progress(percent) }
        }
        let path = output.stdoutString
            .split(whereSeparator: \.isNewline)
            .map(String.init)
            .last(where: { $0.hasPrefix("/") })
        if let path, FileManager.default.fileExists(atPath: path) { return URL(fileURLWithPath: path) }
        let files = (try? FileManager.default.contentsOfDirectory(at: workDir, includingPropertiesForKeys: nil)) ?? []
        guard let audio = files.first(where: { $0.lastPathComponent.hasPrefix("audio.") }) else {
            throw YTDLPError.badOutput("audio file")
        }
        return audio
    }

    static func percent(in line: String) -> Double? {
        guard let range = line.range(of: #"(\d+(\.\d+)?)%"#, options: .regularExpression) else { return nil }
        return Double(line[range].dropLast()).map { $0 / 100 }
    }

    func version() async -> String {
        (try? await ProcessRunner.check(executable, ["--version"]).stdoutString
            .trimmingCharacters(in: .whitespacesAndNewlines)) ?? "?"
    }
}

// MARK: - Caption formats

nonisolated enum CaptionParser {
    static func parseJSON3(_ data: Data) -> [TranscriptSegment] {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let events = json["events"] as? [[String: Any]] else { return [] }
        var segments: [TranscriptSegment] = []
        for event in events {
            guard let segs = event["segs"] as? [[String: Any]] else { continue }
            let text = segs.compactMap { $0["utf8"] as? String }.joined()
                .replacingOccurrences(of: "\n", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            let start = (event["tStartMs"] as? Double ?? 0) / 1000
            let duration = (event["dDurationMs"] as? Double ?? 0) / 1000
            segments.append(TranscriptSegment(start: start, end: start + duration, text: clean(text)))
        }
        return fixOverlaps(segments)
    }

    static func parseSRV3(_ data: Data) -> [TranscriptSegment] {
        // <p t="12559" d="4480">text<s>more</s></p>
        let xml = String(decoding: data, as: UTF8.self)
        guard let regex = try? NSRegularExpression(pattern: #"<p t="(\d+)" d="(\d+)"[^>]*>(.*?)</p>"#,
                                                   options: [.dotMatchesLineSeparators]) else { return [] }
        let ns = xml as NSString
        return regex.matches(in: xml, range: NSRange(location: 0, length: ns.length)).compactMap { match in
            let start = Double(ns.substring(with: match.range(at: 1))) ?? 0
            let duration = Double(ns.substring(with: match.range(at: 2))) ?? 0
            let body = ns.substring(with: match.range(at: 3))
                .replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression)
            let text = decodeEntities(body).trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { return nil }
            return TranscriptSegment(start: start / 1000, end: (start + duration) / 1000, text: clean(text))
        }
    }

    /// WebVTT, removing the "rolling" duplicate lines of YouTube auto-captions.
    static func parseVTT(_ text: String) -> [TranscriptSegment] {
        var segments: [TranscriptSegment] = []
        var lastLine = ""
        let blocks = text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n\n")
        for block in blocks {
            let lines = block.split(separator: "\n", omittingEmptySubsequences: true).map(String.init)
            guard let timing = lines.firstIndex(where: { $0.contains("-->") }) else { continue }
            let times = lines[timing].components(separatedBy: "-->")
            guard times.count == 2 else { continue }
            let start = seconds(times[0])
            let end = seconds(String(times[1].split(separator: " ").first ?? ""))
            for raw in lines[(timing + 1)...] {
                let line = decodeEntities(raw.replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression))
                    .trimmingCharacters(in: .whitespaces)
                guard !line.isEmpty, line != lastLine else { continue }
                lastLine = line
                segments.append(TranscriptSegment(start: start, end: end, text: clean(line)))
            }
        }
        return fixOverlaps(segments)
    }

    static func seconds(_ stamp: String) -> Double {
        let parts = stamp.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".").split(separator: ":")
        var total = 0.0
        for part in parts { total = total * 60 + (Double(part) ?? 0) }
        return total
    }

    static func clean(_ text: String) -> String {
        text.replacingOccurrences(of: "\u{200B}", with: "")
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
    }

    static func decodeEntities(_ text: String) -> String {
        text.replacingOccurrences(of: "&amp;", with: "&")
            .replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&#39;", with: "'")
            .replacingOccurrences(of: "&nbsp;", with: " ")
    }

    private static func fixOverlaps(_ segments: [TranscriptSegment]) -> [TranscriptSegment] {
        var result = segments.sorted { $0.start < $1.start }
        for index in result.indices.dropLast() where result[index].end > result[index + 1].start {
            result[index].end = max(result[index].start, result[index + 1].start)
        }
        return result
    }
}
