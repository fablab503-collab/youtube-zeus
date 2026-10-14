import Foundation
import SwiftData

/// Posts from X, Instagram, Facebook and Threads (3.8): the words of the post, and what is said in its video or audio.
extension EatEngine {
    /// Adds a post to the library and eats it. Returns its id.
    @discardableResult
    func addPost(_ link: String) -> String? {
        guard let id = SocialLinks.id(for: link), let page = SocialLinks.canonical(link) else { return nil }
        if let existing = video(id) {
            if existing.status != .done, !existing.status.isBusy, existing.status != .queued {
                enqueue([(id: id, title: "", channel: "", channelID: "")], force: true, front: true)
            }
            return id
        }
        let site = SocialSite.of(link)
        let item = Video(videoID: id, title: site?.itemName ?? "Post", channelTitle: SocialLinks.handle(in: page) ?? "",
                         channelID: "", status: .queued)
        item.kind = .social
        item.pageURLString = page.absoluteString
        context.insert(item)
        try? context.save()
        enqueue([(id: id, title: "", channel: "", channelID: "")], force: true, front: true)
        return id
    }

    /// Reads the post (yt-dlp first, the page's own tags when yt-dlp cannot), then listens to its sound.
    func eatSocial(_ video: Video, workDir: URL) async throws -> TranscriptResult {
        let id = video.videoID
        guard let page = video.pageURLString else { throw SocialError.unreadable("The site") }
        let site = SocialSite.of(page) ?? .x
        update(id, "Reading the \(site.name) post")
        var postText = ""
        var hasSound = false
        var audioLink: URL?
        let ytdlp = settings.makeSocialYTDLP()
        var readByYTDLP = false
        if let ytdlp, let json = try? await ytdlp.pageInfo(url: page) {
            readByYTDLP = true
            postText = (json["description"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let title = (json["title"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let author = json["uploader"] as? String ?? json["channel"] as? String ?? ""
            let handle = (json["uploader_id"] as? String).map { $0.hasPrefix("@") || Int($0) != nil ? $0 : "@" + $0 }
            if !author.isEmpty { video.channelTitle = author } else if let handle { video.channelTitle = handle }
            if let handle, Int(handle) == nil { video.channelID = site.rawValue + ":" + handle }
            video.title = Self.postTitle(title: title, text: postText, site: site)
            if let ts = json["timestamp"] as? Double { video.publishedAt = Date(timeIntervalSince1970: ts) }
            if let thumb = json["thumbnail"] as? String { video.thumbnailURLString = thumb }
            if let duration = json["duration"] as? Double { video.duration = duration }
            video.viewCount = json["view_count"] as? Int ?? video.viewCount
            video.likeCount = json["like_count"] as? Int ?? video.likeCount
            let formats = json["formats"] as? [[String: Any]] ?? []
            // A photo post gives a picture's link: no sound to take there.
            let ext = (json["ext"] as? String ?? "").lowercased()
            let picture = ["jpg", "jpeg", "png", "webp", "gif", "heic", "avif"].contains(ext)
            hasSound = !picture && (video.duration > 0 || formats.contains { ($0["acodec"] as? String ?? "none") != "none" }
                || (json["url"] as? String) != nil)
        }
        if !readByYTDLP || postText.isEmpty {
            // The page's own tags (link previews): text, picture, and its video when it shows one.
            if let url = URL(string: page), let meta = await SocialLinks.pageMeta(url) {
                if postText.isEmpty { postText = meta.description }
                if !readByYTDLP {
                    video.title = Self.postTitle(title: meta.title, text: postText, site: site)
                    video.thumbnailURLString = meta.image ?? video.thumbnailURLString
                    audioLink = (meta.video ?? meta.audio).flatMap(URL.init(string:))
                    hasSound = audioLink != nil
                }
            } else if !readByYTDLP, site != .web {
                throw SocialError.unreadable(site.name)
            }
        }
        // Any other page without sound (an article, a blog post, documentation): its readable text (4.3).
        // (Some sites refuse the link-preview reader but serve the page to a browser: the page's own title then.)
        if site == .web, !hasSound, let url = URL(string: page), let article = await Self.readableText(of: url),
           article.text.count > postText.count {
            postText = article.text
            if !readByYTDLP, video.title.isEmpty || video.title.hasPrefix("http") || video.title == page || video.title == site.itemName {
                video.title = Self.postTitle(title: article.title, text: postText, site: site)
            }
        }
        if site == .web, postText.isEmpty, !hasSound { throw SocialError.unreadable("The page") }
        video.videoDescription = String(postText.prefix(4_000))
        try? context.save()

        var spoken: TranscriptResult?
        if hasSound, settings.useWhisperFallback {
            do {
                let tools = try whisperTools()
                update(id, "Downloading the sound of the post", 0)
                let audio: URL
                if let audioLink {
                    let file = workDir.appendingPathComponent("post." + (audioLink.pathExtension.isEmpty ? "mp4" : audioLink.pathExtension))
                    try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
                    _ = try await FileDownloader(destination: file) { value in
                        Task { @MainActor in self.update(id, "Downloading the sound of the post", value) }
                    }.download(from: audioLink)
                    audio = file
                } else if let ytdlp {
                    audio = try await ytdlp.downloadAudio(url: page, workDir: workDir.appendingPathComponent("audio")) { value in
                        Task { @MainActor in self.update(id, "Downloading the sound of the post", value) }
                    }
                } else {
                    throw EatError.missing("yt-dlp")
                }
                if video.duration == 0 { video.duration = await MediaFiles.duration(audio) }
                let whisper = makeWhisper(whisperPath: tools.whisper, ffmpegPath: tools.ffmpeg)
                try await waitForWhisper(id)
                defer { whisperBusy = false }
                try await prepareWhisper(whisper, id: id)
                spoken = try await listen(whisper, audio: audio, id: id, workDir: workDir)
            } catch {
                if Self.isCancellation(error) { throw error }
                AppLog.write("POST \(id): no sound taken (\(error.localizedDescription)), keeping the words of the post")
            }
        }
        // What was said, when there is enough of it; else the words of the post.
        let spokenWords = spoken?.segments.map(\.text).joined(separator: " ").split(separator: " ").count ?? 0
        if let spoken, spokenWords >= 8 || (postText.isEmpty && spokenWords > 0) {
            return spoken
        }
        guard !postText.isEmpty else { throw hasSound ? SocialError.noSpeech(site.name) : SocialError.nothing(site.name) }
        let segments = postText.components(separatedBy: "\n\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }.map { TranscriptSegment(start: 0, end: 0, text: $0) }
        return TranscriptResult(segments: segments, source: .post, language: "")
    }

    /// The text of a web page without its scripts, styles, menus and footers (free, on this Mac).
    nonisolated static func readableText(of url: URL) async -> (title: String, text: String)? {
        var request = URLRequest(url: url, timeoutInterval: 25)
        request.setValue("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15",
                         forHTTPHeaderField: "User-Agent")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.mimeType?.contains("html") ?? true else { return nil }
        var html = String(decoding: data.prefix(3_000_000), as: UTF8.self)
        let pageTitle = SocialLinks.tag(html, "og:title")
            ?? html.range(of: #"<title[^>]*>[\s\S]*?</title>"#, options: [.regularExpression, .caseInsensitive]).map {
                SocialLinks.decodeEntities(String(html[$0]).replacingOccurrences(of: #"<[^>]+>"#, with: "", options: .regularExpression))
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            } ?? ""
        for tag in ["script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form"] {
            html = html.replacingOccurrences(of: "<\(tag)[^>]*>[\\s\\S]*?</\(tag)>", with: " ",
                                             options: [.regularExpression, .caseInsensitive])
        }
        // The article itself when the page marks it.
        if let range = html.range(of: #"<(article|main)[^>]*>[\s\S]*</(article|main)>"#, options: [.regularExpression, .caseInsensitive]) {
            html = String(html[range])
        }
        // Block elements end a line, so headings, dates and paragraphs do not run into each other.
        html = html.replacingOccurrences(of: #"</(div|section|time|figure|figcaption|blockquote|pre|tr|ul|ol|table|dd|dt|header|p)>"#,
                                         with: "\n", options: [.regularExpression, .caseInsensitive])
            .replacingOccurrences(of: #"<(h\d|p)(\s[^>]*)?>"#, with: "\n$0", options: [.regularExpression, .caseInsensitive])
        let text = PodcastFeed.plainText(html)
            .replacingOccurrences(of: #"[ \t]+"#, with: " ", options: .regularExpression)
            .replacingOccurrences(of: #" *\n *"#, with: "\n", options: .regularExpression)
            .replacingOccurrences(of: #"\n\s*\n\s*\n+"#, with: "\n\n", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return text.split(separator: " ").count >= 40 ? (title: pageTitle, text: String(text.prefix(300_000))) : nil
    }

    /// A readable title: the site's title when it says something, else the first words of the post.
    nonisolated static func postTitle(title: String, text: String, site: SocialSite) -> String {
        let generic = title.isEmpty || title.lowercased().hasPrefix("video by") || title.lowercased().hasPrefix("post by")
            || title.lowercased() == site.name.lowercased() || title.lowercased().hasSuffix(" on x") || title.hasPrefix("Instagram")
            || site == .telegram   // Telegram's page title is the channel's name, not the post's
        if !generic, title.count <= 140 { return title }
        let words = text.replacingOccurrences(of: "\n", with: " ").split(separator: " ").prefix(14).joined(separator: " ")
        if !words.isEmpty { return words.count < text.count ? words + "…" : words }
        return title.isEmpty ? site.itemName : String(title.prefix(140))
    }
}
