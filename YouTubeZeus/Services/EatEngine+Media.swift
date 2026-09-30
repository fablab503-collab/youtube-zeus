import AppKit
import Foundation
import SwiftData

/// 3.0: podcast episodes and your own files go through the same pipeline as videos once their text is in
/// (note, search, GitHub links, polishing, summary, people/tools/companies).
extension EatEngine {
    /// Adds podcast episodes to the library (and to the queue when `autoEat`). Returns their IDs.
    @discardableResult
    func addEpisodes(_ episodes: [PodcastEpisode], channel: Channel, feed: URL, language: String?, fromWatch: Bool,
                     autoEat: Bool) -> [String] {
        var ids: [String] = []
        for episode in episodes {
            let id = episode.id(feed: feed)
            if let existing = video(id) {
                if existing.status == .failed || existing.status == .discovered { ids.append(id) }
                continue
            }
            let item = Video(videoID: id, title: episode.title, channelTitle: channel.title, channelID: channel.channelID,
                             publishedAt: episode.published, status: .discovered, fromChannelWatch: fromWatch)
            item.kind = .podcast
            item.mediaURLString = episode.audioURL
            item.pageURLString = episode.link
            item.duration = episode.duration
            item.videoDescription = PodcastFeed.plainText(episode.about)
            item.thumbnailURLString = episode.image ?? channel.avatarURLString
            item.transcriptLinks = episode.transcripts
            item.language = String((language ?? "").split(separator: "-").first ?? "")
            context.insert(item)
            if let art = (episode.image ?? channel.avatarURLString).flatMap(URL.init(string:)) {
                Task.detached { await MediaFiles.saveArtwork(from: art, id: id) }
            }
            ids.append(id)
        }
        try? context.save()
        if autoEat, !ids.isEmpty {
            enqueue(ids.map { (id: $0, title: "", channel: "", channelID: "") }, fromWatch: fromWatch, force: true)
        }
        return ids
    }

    func runMedia(_ video: Video) async {
        let id = video.videoID
        let workDir = AppFolders.work.appendingPathComponent(id + "-" + UUID().uuidString.prefix(6), isDirectory: true)
        defer { try? FileManager.default.removeItem(at: workDir) }
        video.attempts += 1
        do {
            video.status = .fetching
            let result: TranscriptResult
            switch video.kind {
            case .podcast: result = try await eatEpisode(video, workDir: workDir)
            case .file: result = try await eatOwnFile(video, workDir: workDir)
            case .youtube: return
            }
            finishEating(video, result: result, fallbackLanguage: video.language)
        } catch {
            let cancelled = Self.isCancellation(error)
            video.status = .failed
            video.statusDetail = cancelled ? "Cancelled" : error.localizedDescription
            AppLog.write("FAILED \(id): \(video.statusDetail)")
        }
        try? context.save()
    }

    /// A transcript published with the episode when there is one, else the audio is downloaded and listened to.
    private func eatEpisode(_ video: Video, workDir: URL) async throws -> TranscriptResult {
        let id = video.videoID
        if let link = PodcastFeed.bestTranscript(video.transcriptLinks) {
            update(id, "Downloading the published transcript")
            do {
                let segments = try await PodcastFeed.downloadTranscript(link)
                if !segments.isEmpty {
                    return TranscriptResult(segments: segments, source: .published, language: video.language)
                }
            } catch {
                AppLog.write("PODCAST \(id): published transcript unusable (\(error.localizedDescription)), listening instead")
            }
        }
        guard settings.useWhisperFallback else { throw EatError.noCaptions }
        guard let audioURL = video.mediaURL else { throw PodcastError.notFound("no audio file in the feed") }
        let tools = try whisperTools()
        try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
        let ext = audioURL.pathExtension.isEmpty ? "mp3" : audioURL.pathExtension
        let file = workDir.appendingPathComponent("episode.\(ext)")
        update(id, "Downloading the episode", 0)
        _ = try await FileDownloader(destination: file) { value in
            Task { @MainActor in self.update(id, "Downloading the episode", value) }
        }.download(from: audioURL)
        if video.duration == 0 { video.duration = await MediaFiles.duration(file) }
        let whisper = makeWhisper(whisperPath: tools.whisper, ffmpegPath: tools.ffmpeg)
        try await waitForWhisper(id)
        defer { whisperBusy = false }
        try await prepareWhisper(whisper, id: id)
        var result = try await listen(whisper, audio: file, id: id, workDir: workDir)
        if result.language.isEmpty { result = TranscriptResult(segments: result.segments, source: result.source, language: video.language) }
        return result
    }

    /// Your own recording: listened to on this Mac; videos also get a picture (and their screen read, when on).
    private func eatOwnFile(_ video: Video, workDir: URL) async throws -> TranscriptResult {
        let id = video.videoID
        guard let url = video.mediaURL, url.isFileURL else { throw MediaFileError.missing(video.mediaURLString ?? "?") }
        guard FileManager.default.fileExists(atPath: url.path) else { throw MediaFileError.missing(url.path) }
        update(id, "Reading the file")
        if video.duration == 0 { video.duration = await MediaFiles.duration(url) }
        if MediaFiles.isVideo(url), video.thumbnailURLString == nil, let picture = await MediaFiles.makeThumbnail(for: url, id: id) {
            video.thumbnailURLString = picture.absoluteString
        }
        let tools = try whisperTools()
        let whisper = makeWhisper(whisperPath: tools.whisper, ffmpegPath: tools.ffmpeg)
        try await waitForWhisper(id)
        defer { whisperBusy = false }
        try await prepareWhisper(whisper, id: id)
        return try await listen(whisper, audio: url, id: id, workDir: workDir)
    }

    // MARK: Text on screen (a lane of its own, beside polishing and summaries)

    func isReadingScreen(_ id: String) -> Bool { screenCurrent == id || screenQueue.contains(id) }

    /// Reads the text on screen of an item (YouTube videos are downloaded without sound for it, then deleted).
    func scheduleScreen(_ id: String, force: Bool = false) {
        guard let item = video(id), item.status.hasText || item.status == .done else { return }
        if !force, item.screenReadAt != nil { return }
        if !screenQueue.contains(id), screenCurrent != id { screenQueue.append(id) }
        guard !screenRunning else { return }
        screenRunning = true
        Task {
            while !screenQueue.isEmpty {
                let next = screenQueue.removeFirst()
                screenCurrent = next
                if let item = video(next) { await readScreen(item) }
                screenCurrent = nil
            }
            screenRunning = false
        }
    }

    private func readScreen(_ video: Video) async {
        let id = video.videoID
        guard let ffmpeg = ToolLocator.find("ffmpeg", override: settings.ffmpegPath) else {
            video.screenError = EatError.missing("ffmpeg").localizedDescription
            return
        }
        let workDir = AppFolders.work.appendingPathComponent("screen-" + id + "-" + UUID().uuidString.prefix(6), isDirectory: true)
        defer { try? FileManager.default.removeItem(at: workDir) }
        video.screenError = nil
        do {
            let source: URL
            switch video.kind {
            case .file:
                guard let file = video.mediaURL, FileManager.default.fileExists(atPath: file.path) else {
                    throw MediaFileError.missing(video.mediaURL?.path ?? "?")
                }
                source = file
            case .youtube:
                guard let ytdlp = settings.makeYTDLP() else { throw EatError.missing("yt-dlp") }
                jobs[id] = JobState(step: "Downloading the picture to read the screen", progress: 0)
                source = try await ytdlp.downloadVideo(videoID: id, workDir: workDir.appendingPathComponent("video")) { value in
                    Task { @MainActor in self.jobs[id] = JobState(step: "Downloading the picture to read the screen", progress: value) }
                }
            case .podcast:
                guard let media = video.mediaURL, MediaFiles.isVideo(media) else {
                    throw MediaFileError.notMedia("An audio podcast")
                }
                let file = workDir.appendingPathComponent("episode." + media.pathExtension)
                try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
                _ = try await FileDownloader(destination: file) { _ in }.download(from: media)
                source = file
            }
            let reader = ScreenReader(ffmpegPath: ffmpeg, interval: Double(max(1, settings.screenInterval)))
            // Off the main thread: decoding frames and Vision are heavy.
            let items = try await offMain {
                try await reader.read(video: source, workDir: workDir) { step, value in
                    Task { @MainActor in self.jobs[id] = JobState(step: step, progress: value) }
                }
            }
            video.screen = items
            video.screenReadAt = .now
            AppLog.write("SCREEN \(id): \(items.filter { $0.kind == .title }.count) titles, \(items.filter { $0.kind == .command }.count) commands, \(items.filter { $0.kind == .code }.count) code blocks")
        } catch {
            video.screenError = Self.isCancellation(error) ? "Cancelled" : error.localizedDescription
            AppLog.write("SCREEN \(id) failed: \(video.screenError ?? "")")
        }
        if tasks[id] == nil { jobs[id] = nil }
        try? context.save()
        exporter.export(video)
        try? context.save()
        onChanged?(id)
    }

    // MARK: People, tools and companies (the local AI, one item at a time, after its summary)

    func isFindingNames(_ id: String) -> Bool { entityCurrent == id || entityQueue.contains(id) }

    func scheduleEntities(_ id: String, force: Bool = false) {
        guard settings.entitiesEnabled || force, polisher.isInstalled, let item = video(id), item.status == .done else { return }
        if !force, item.entitiesAt != nil { return }
        if !entityQueue.contains(id), entityCurrent != id { entityQueue.append(id) }
        guard !entityRunning else { return }
        entityRunning = true
        Task {
            while !entityQueue.isEmpty {
                // Polishing and summaries first (names are read from the summary), and never while Whisper listens:
                // two big models on the GPU at once slow both down a lot.
                while polishRunning || summaryRunning || whisperBusy { try? await Task.sleep(for: .seconds(5)) }
                let next = entityQueue.removeFirst()
                entityCurrent = next
                if let item = video(next) { await findNames(item) }
                entityCurrent = nil
            }
            entityRunning = false
        }
    }

    private func findNames(_ video: Video) async {
        let id = video.videoID
        let snapshot = video.snapshot
        do {
            let model = settings.polishModel
            // Off the main thread: NaturalLanguage reads the whole transcript.
            let found = try await offMain {
                try await EntityExtractor(model: model).extract(snapshot)
            }.filter { !Self.isPlainWords($0.name) }
            video.entities = found
            video.entitiesAt = .now
            AppLog.write("NAMES \(id): \(found.map(\.name).joined(separator: ", "))")
        } catch {
            // Tried again at the next launch.
            AppLog.write("NAMES \(id) failed: \(error.localizedDescription)")
            return
        }
        try? context.save()
        exporter.export(video)
        try? context.save()
        onChanged?(id)
        onNames?(id)
    }

    /// "authentication", "feedback loop": ordinary lowercase words the dictionary knows are not names
    /// ("n8n", "npm" and "ffmpeg" are kept).
    static func isPlainWords(_ name: String) -> Bool {
        guard name == name.lowercased(), !name.contains(where: \.isNumber) else { return false }
        let checker = NSSpellChecker.shared
        return name.split(separator: " ").allSatisfy { word in
            checker.checkSpelling(of: String(word), startingAt: 0).location == NSNotFound
        }
    }

    private func whisperTools() throws -> (whisper: String, ffmpeg: String) {
        guard let ffmpeg = ToolLocator.find("ffmpeg", override: settings.ffmpegPath) else { throw EatError.missing("ffmpeg") }
        // whisper.cpp is the fallback of MLX; with MLX available it is not strictly required.
        let whisper = ToolLocator.find("whisper-cli", override: settings.whisperPath) ?? ""
        if whisper.isEmpty, MLXWhisper.executable == nil { throw EatError.missing("whisper-cli") }
        return (whisper, ffmpeg)
    }
}
