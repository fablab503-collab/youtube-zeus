import Foundation
import Observation
import SwiftData
import UserNotifications

nonisolated enum EatError: LocalizedError, Sendable {
    case noCaptions
    case missing(String)

    var errorDescription: String? {
        switch self {
        case .noCaptions: "This video has no captions, and Whisper is turned off in Settings."
        case .missing(let tool): "\(tool) was not found. Install it with Homebrew or set its path in Settings › Tools."
        }
    }
}

/// The eating pipeline: video info → captions (or Whisper) → library → Second Brain → summary.
@Observable
final class EatEngine {
    struct JobState: Equatable {
        var step: String
        var progress: Double?
    }

    private(set) var jobs: [String: JobState] = [:]
    private(set) var queue: [String] = []
    private var tasks: [String: Task<Void, Never>] = [:]
    private var whisperBusy = false
    private var summarizing: Set<String> = []
    private var forceWhisper: Set<String> = []

    let context: ModelContext
    let settings: AppSettings
    let summarizer: Summarizer
    let exporter: SecondBrainExporter

    init(context: ModelContext, settings: AppSettings, summarizer: Summarizer, exporter: SecondBrainExporter) {
        self.context = context
        self.settings = settings
        self.summarizer = summarizer
        self.exporter = exporter
    }

    var activeCount: Int { jobs.count + queue.count }

    func state(for id: String) -> JobState? { jobs[id] }

    // MARK: Queue

    func video(_ id: String) -> Video? {
        var descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.videoID == id })
        descriptor.fetchLimit = 1
        return try? context.fetch(descriptor).first
    }

    /// Adds videos (creating them when new) and starts eating.
    @discardableResult
    func enqueue(_ entries: [(id: String, title: String, channel: String, channelID: String)], fromWatch: Bool = false,
                 force: Bool = false) -> [Video] {
        var added: [Video] = []
        for entry in entries {
            let video: Video
            if let existing = self.video(entry.id) {
                video = existing
                if !force, existing.status == .done || existing.status.isBusy || existing.status == .queued { added.append(existing); continue }
            } else {
                video = Video(videoID: entry.id, title: entry.title, channelTitle: entry.channel,
                              channelID: entry.channelID, fromChannelWatch: fromWatch)
                context.insert(video)
            }
            video.status = .queued
            video.statusDetail = ""
            if !queue.contains(entry.id), jobs[entry.id] == nil { queue.append(entry.id) }
            added.append(video)
        }
        try? context.save()
        pump()
        return added
    }

    func enqueue(videoIDs: [String]) {
        enqueue(videoIDs.map { (id: $0, title: "", channel: "", channelID: "") })
    }

    func retry(_ video: Video, withWhisper: Bool = false) {
        if withWhisper { forceWhisper.insert(video.videoID) }
        enqueue([(id: video.videoID, title: video.title, channel: video.channelTitle, channelID: video.channelID)], force: true)
    }

    /// Eats a video by listening to it with Whisper even when captions exist (better punctuation than auto-captions).
    func eatWithWhisper(_ id: String) {
        forceWhisper.insert(id)
        enqueue([(id: id, title: "", channel: "", channelID: "")], force: true)
    }

    func cancel(_ id: String) {
        queue.removeAll { $0 == id }
        tasks[id]?.cancel()
        if let video = video(id), video.status == .queued {
            video.status = .failed
            video.statusDetail = "Cancelled"
        }
    }

    /// After a restart, jobs that were running are queued again.
    func resumeInterrupted() {
        let busy = [EatStatus.queued, .fetching, .transcribing].map(\.rawValue)
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { busy.contains($0.statusRaw) },
                                                sortBy: [SortDescriptor(\.addedAt)])
        for video in (try? context.fetch(descriptor)) ?? [] {
            video.status = .queued
            if !queue.contains(video.videoID) { queue.append(video.videoID) }
        }
        let summarizingRaw = EatStatus.summarizing.rawValue
        let stuck = FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == summarizingRaw })
        for video in (try? context.fetch(stuck)) ?? [] { video.status = .done }
        try? context.save()
        pump()
    }

    /// Live streams, premieres and too-fresh uploads are retried on each channel check.
    func retryWaiting() {
        let waiting = EatStatus.waiting.rawValue
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == waiting })
        let videos = (try? context.fetch(descriptor)) ?? []
        guard !videos.isEmpty else { return }
        enqueue(videos.map { (id: $0.videoID, title: $0.title, channel: $0.channelTitle, channelID: $0.channelID) }, force: true)
    }

    private func pump() {
        while jobs.count < max(1, settings.maxParallel), !queue.isEmpty {
            let id = queue.removeFirst()
            jobs[id] = JobState(step: "Starting")
            tasks[id] = Task {
                await self.run(id)
                self.jobs[id] = nil
                self.tasks[id] = nil
                self.pump()
            }
        }
    }

    private func update(_ id: String, _ step: String, _ progress: Double? = nil) {
        jobs[id] = JobState(step: step, progress: progress)
        video(id)?.statusDetail = step
    }

    // MARK: The pipeline

    private func run(_ id: String) async {
        guard let video = video(id) else { return }
        guard let ytdlpPath = ToolLocator.find("yt-dlp", override: settings.ytdlpPath) else {
            video.status = .failed
            video.statusDetail = EatError.missing("yt-dlp").localizedDescription
            return
        }
        let ytdlp = YTDLP(executable: ytdlpPath)
        let workDir = AppFolders.work.appendingPathComponent(id + "-" + UUID().uuidString.prefix(6), isDirectory: true)
        defer { try? FileManager.default.removeItem(at: workDir) }
        video.attempts += 1

        do {
            video.status = .fetching
            update(id, "Reading video info")
            let info = try await ytdlp.info(videoID: id)
            apply(info, to: video)
            try? context.save()

            if info.isLiveOrUpcoming {
                video.status = .waiting
                video.statusDetail = info.liveStatus == "is_upcoming"
                    ? "Premiere or live not started yet — Zeus will try again later."
                    : "Live right now — Zeus will eat it after the stream ends."
                return
            }

            var result: TranscriptResult?
            var captionError: Error?
            let whisperOnly = forceWhisper.remove(id) != nil
            if whisperOnly {
                result = try await transcribeWithWhisper(id: id, ytdlp: ytdlp, workDir: workDir)
            } else if let track = YTDLP.chooseTrack(info, preferred: settings.preferredLanguageList) {
                update(id, track.isAuto ? "Downloading auto-captions (\(track.language))" : "Downloading captions (\(track.language))")
                do {
                    result = try await ytdlp.downloadCaptions(videoID: id, track: track,
                                                              workDir: workDir.appendingPathComponent("subs"))
                } catch {
                    if Self.isCancellation(error) { throw error }
                    captionError = error
                }
            }

            if result == nil {
                if let published = info.publishedAt, Date.now.timeIntervalSince(published) < 3 * 3600, video.attempts < 6 {
                    video.status = .waiting
                    video.statusDetail = "Just uploaded, captions not ready yet — Zeus will try again later."
                    return
                }
                guard settings.useWhisperFallback else { throw captionError ?? EatError.noCaptions }
                result = try await transcribeWithWhisper(id: id, ytdlp: ytdlp, workDir: workDir)
            }

            guard let result else { throw EatError.noCaptions }
            video.segments = result.segments
            video.transcriptText = Paragrapher.paragraphs(from: result.segments).map(\.text).joined(separator: "\n\n")
            video.source = result.source
            video.language = result.language.isEmpty ? (info.language ?? "") : result.language
            video.eatenAt = .now
            video.status = .done
            video.statusDetail = ""
            AppLog.write("EATEN \(id) [\(result.source.rawValue), \(video.language)] \(video.displayTitle)")
            try? context.save()
            exporter.export(video)
            try? context.save()

            if settings.autoSummarize, canSummarize {
                await summarize(video)
            }
            if video.fromChannelWatch, settings.notifyWhenEaten { Notifier.post(title: "Eaten: \(video.displayTitle)", body: video.channelTitle) }
        } catch {
            let cancelled = Self.isCancellation(error)
            video.status = .failed
            video.statusDetail = cancelled ? "Cancelled" : error.localizedDescription
            AppLog.write("FAILED \(id): \(video.statusDetail)")
        }
        try? context.save()
    }

    static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let failure = error as? ProcessFailure, case .cancelled = failure { return true }
        return false
    }

    private func apply(_ info: VideoInfo, to video: Video) {
        if !info.title.isEmpty { video.title = info.title }
        if !info.channel.isEmpty { video.channelTitle = info.channel }
        if !info.channelID.isEmpty { video.channelID = info.channelID }
        video.publishedAt = info.publishedAt ?? video.publishedAt
        video.duration = info.duration
        video.videoDescription = info.description
        video.chapters = info.chapters
        video.isShort = info.isShort
        if let thumbnail = info.thumbnail { video.thumbnailURLString = thumbnail }
    }

    private func transcribeWithWhisper(id: String, ytdlp: YTDLP, workDir: URL) async throws -> TranscriptResult {
        guard let whisperPath = ToolLocator.find("whisper-cli", override: settings.whisperPath) else { throw EatError.missing("whisper-cli") }
        guard let ffmpegPath = ToolLocator.find("ffmpeg", override: settings.ffmpegPath) else { throw EatError.missing("ffmpeg") }
        let model = WhisperModel(rawValue: settings.whisperModel) ?? .turbo
        let whisper = WhisperTranscriber(whisperPath: whisperPath, ffmpegPath: ffmpegPath, model: model)

        video(id)?.status = .transcribing
        if whisperBusy { update(id, "Waiting for Whisper (another video is being listened to)") }
        while whisperBusy {
            try await Task.sleep(for: .seconds(1))
        }
        whisperBusy = true
        defer { whisperBusy = false }

        if !whisper.hasModel {
            update(id, "Downloading the Whisper model (once)", 0)
            try await whisper.ensureModel { value in
                Task { @MainActor in self.update(id, "Downloading the Whisper model (once)", value) }
            }
        }
        update(id, "No captions — downloading audio", 0)
        let audio = try await ytdlp.downloadAudio(videoID: id, workDir: workDir.appendingPathComponent("audio")) { value in
            Task { @MainActor in self.update(id, "No captions — downloading audio", value) }
        }
        update(id, "Listening with Whisper", 0)
        return try await whisper.transcribe(audio: audio, workDir: workDir) { value in
            Task { @MainActor in self.update(id, "Listening with Whisper", value) }
        }
    }

    // MARK: Summary

    func isSummarizing(_ id: String) -> Bool { summarizing.contains(id) }

    /// Apple Intelligence on this Mac, or Codex when allowed (Settings › AI).
    var useAppleIntelligence: Bool { settings.summaryEngine != "codex" && summarizer.isAvailable }

    var codexSummaryAllowed: Bool {
        settings.summaryEngine != "apple" && settings.openAIConsent && CodexLocator.path != nil
    }

    var canSummarize: Bool { useAppleIntelligence || codexSummaryAllowed }

    var summaryUnavailableMessage: String {
        if settings.summaryEngine == "apple" { return summarizer.availabilityMessage }
        if !settings.openAIConsent { return summarizer.availabilityMessage + " To use Codex instead, allow sending transcripts to OpenAI in Settings › Codex skills." }
        return summarizer.availabilityMessage + " The Codex CLI was not found either."
    }

    func summarize(_ video: Video) async {
        let id = video.videoID
        guard !summarizing.contains(id) else { return }
        summarizing.insert(id)
        defer { summarizing.remove(id) }
        let previous = video.status
        video.status = .summarizing
        video.digestError = nil
        jobs[id] = jobs[id] ?? JobState(step: "Summarizing")
        do {
            let digest: VideoDigest
            if useAppleIntelligence {
                digest = try await summarizer.summarize(
                    title: video.displayTitle,
                    channel: video.channelTitle,
                    paragraphs: video.paragraphs,
                    videoLanguage: video.language,
                    youtubeChapters: video.chapters,
                    outputLanguage: settings.summaryLanguage) { step in
                        self.jobs[id] = JobState(step: step)
                        video.statusDetail = step
                    }
            } else if codexSummaryAllowed, let codex = CodexLocator.path {
                jobs[id] = JobState(step: "Summarizing with Codex")
                video.statusDetail = "Summarizing with Codex"
                let language = settings.summaryLanguage == "auto" ? video.language : settings.summaryLanguage
                digest = try await CodexSummarizer(executable: codex, model: settings.codexModel).summarize(
                    title: video.displayTitle, channel: video.channelTitle, paragraphs: video.paragraphs,
                    language: language.isEmpty ? "en" : language, youtubeChapters: video.chapters)
            } else {
                throw SummaryError.unavailable(summaryUnavailableMessage)
            }
            video.digest = digest
            AppLog.write("SUMMARY \(id) ok")
        } catch {
            video.digestError = error.localizedDescription
            AppLog.write("SUMMARY \(id) failed: \(error.localizedDescription)")
        }
        video.status = previous == .summarizing ? .done : (previous == .failed ? .failed : .done)
        video.statusDetail = ""
        if tasks[id] == nil { jobs[id] = nil }
        try? context.save()
        exporter.export(video)
        try? context.save()
    }
}

enum Notifier {
    static func requestPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
