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
                enqueue([(id: id, title: "", channel: "", channelID: "")], force: true)
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
        enqueue([(id: id, title: "", channel: "", channelID: "")], force: true)
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
            hasSound = video.duration > 0 || formats.contains { ($0["acodec"] as? String ?? "none") != "none" }
                || (json["url"] as? String) != nil
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
            } else if !readByYTDLP {
                throw SocialError.unreadable(site.name)
            }
        }
        video.videoDescription = postText
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
        if let spoken, spoken.segments.map(\.text).joined(separator: " ").split(separator: " ").count >= 8 {
            return spoken
        }
        guard !postText.isEmpty else { throw SocialError.nothing(site.name) }
        let segments = postText.components(separatedBy: "\n\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }.map { TranscriptSegment(start: 0, end: 0, text: $0) }
        return TranscriptResult(segments: segments, source: .post, language: "")
    }

    /// A readable title: the site's title when it says something, else the first words of the post.
    nonisolated static func postTitle(title: String, text: String, site: SocialSite) -> String {
        let generic = title.isEmpty || title.lowercased().hasPrefix("video by") || title.lowercased().hasPrefix("post by")
            || title.lowercased() == site.name.lowercased() || title.lowercased().hasSuffix(" on x") || title.hasPrefix("Instagram")
        if !generic, title.count <= 140 { return title }
        let words = text.replacingOccurrences(of: "\n", with: " ").split(separator: " ").prefix(14).joined(separator: " ")
        if !words.isEmpty { return words.count < text.count ? words + "…" : words }
        return title.isEmpty ? site.itemName : String(title.prefix(140))
    }
}
