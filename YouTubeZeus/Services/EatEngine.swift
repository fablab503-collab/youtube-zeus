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

    var jobs: [String: JobState] = [:]
    private(set) var queue: [String] = []
    private(set) var tasks: [String: Task<Void, Never>] = [:]
    /// Eating paused (3.3): nothing new starts; what was running goes back to the front of the line.
    private(set) var eatingPaused = false
    private var pausedRunning: Set<String> = []
    var whisperBusy = false
    private var summarizing: Set<String> = []
    /// 4.5: items Apple Intelligence already declined: the local AI summarizes them directly (no minutes lost on a
    /// refusal), also after a restart.
    var appleDeclined: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: "appleDeclinedIDs") ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: "appleDeclinedIDs") }
    }
    /// Summaries already tried again after a timeout (once each).
    var summaryRetried: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: "summaryRetriedIDs") ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: "summaryRetriedIDs") }
    }
    @ObservationIgnored private var appleFailuresInARow = 0
    /// After 3 refusals in a row, Apple Intelligence rests for 6 hours and the local AI summarizes (Auto only).
    var appleSummaryPausedUntil: Date? {
        get { UserDefaults.standard.object(forKey: "appleSummaryPausedUntil") as? Date }
        set { UserDefaults.standard.set(newValue, forKey: "appleSummaryPausedUntil") }
    }
    private var forceWhisper: Set<String> = []
    /// After eating, two lanes run side by side: the local AI polishes, Codex/Apple Intelligence summarizes.
    private(set) var polishQueue: [String] = []
    private(set) var summaryQueue: [String] = []
    private(set) var polishRunning = false
    private(set) var summaryRunning = false
    private(set) var polishCurrent: String?
    private(set) var summaryCurrent: String?

    /// Videos waiting for polishing or a summary, in order (for the queue view).
    var postQueue: [String] {
        var seen = Set<String>()
        return (polishQueue + summaryQueue).filter { seen.insert($0).inserted }
    }

    private func inPost(_ id: String) -> Bool {
        polishQueue.contains(id) || summaryQueue.contains(id) || polishCurrent == id || summaryCurrent == id
    }
    private(set) var polishing: Set<String> = []
    private var forcePolish: Set<String> = []
    // People, tools and companies (3.0)
    var entityQueue: [String] = []
    var entityRunning = false
    var entityCurrent: String?
    @ObservationIgnored var onNames: ((String) -> Void)?
    // Text on screen (3.0)
    var screenQueue: [String] = []
    var screenRunning = false
    var screenCurrent: String?
    // Playbooks (3.2)
    var playbookQueue: [String] = []
    var playbookRunning = false
    var playbookCurrent: String?
    @ObservationIgnored var playbookTask: Task<Void, Never>?
    /// Made even when the video does not look like a tutorial.
    @ObservationIgnored var playbookForced: Set<String> = []
    /// The last step written for `zeus playbook` and the MCP server (`PlaybookFiles.setMaking`).
    @ObservationIgnored var playbookMarks: [String: String] = [:]
    private(set) var githubQueue: [String] = []
    private var githubRunning = false
    private(set) var githubChecking: String?

    let context: ModelContext
    let settings: AppSettings
    let summarizer: Summarizer
    let exporter: SecondBrainExporter
    let polisher: Polisher

    init(context: ModelContext, settings: AppSettings, summarizer: Summarizer, exporter: SecondBrainExporter, polisher: Polisher) {
        self.context = context
        self.settings = settings
        self.summarizer = summarizer
        self.exporter = exporter
        self.polisher = polisher
    }

    var activeCount: Int { jobs.count + queue.count + postQueue.count }

    func state(for id: String) -> JobState? { jobs[id] }

    // MARK: Queue

    func video(_ id: String) -> Video? {
        var descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.videoID == id })
        descriptor.fetchLimit = 1
        return try? context.fetch(descriptor).first
    }

    /// Adds videos (creating them when new) and starts eating. `front` (4.5): what you paste or ask for yourself goes
    /// before a long line (a playlist, a channel), so the Eat page shows it at once.
    @discardableResult
    func enqueue(_ entries: [(id: String, title: String, channel: String, channelID: String)], fromWatch: Bool = false,
                 force: Bool = false, front: Bool = false) -> [Video] {
        var added: [Video] = []
        var frontIndex = 0
        for entry in entries {
            let video: Video
            if let existing = self.video(entry.id) {
                video = existing
                if existing.status == .trashed {
                    // A watched channel does not bring back what you threw away; eating it yourself does.
                    if fromWatch { continue }
                    existing.trashedAt = nil
                    existing.trashedFromRaw = nil
                    existing.status = .failed
                }
                if !force, existing.status == .done || existing.status.isBusy || existing.status == .queued {
                    // Pasted again while it waits in a long line: it goes first.
                    if front, existing.status == .queued, jobs[entry.id] == nil {
                        queue.removeAll { $0 == entry.id }
                        queue.insert(entry.id, at: min(frontIndex, queue.count))
                        frontIndex += 1
                    }
                    added.append(existing)
                    continue
                }
            } else {
                video = Video(videoID: entry.id, title: entry.title, channelTitle: entry.channel,
                              channelID: entry.channelID, fromChannelWatch: fromWatch)
                context.insert(video)
            }
            video.status = .queued
            video.statusDetail = ""
            if jobs[entry.id] == nil {
                if front {
                    queue.removeAll { $0 == entry.id }
                    queue.insert(entry.id, at: min(frontIndex, queue.count))
                    frontIndex += 1
                } else if !queue.contains(entry.id) {
                    queue.append(entry.id)
                }
            }
            added.append(video)
        }
        try? context.save()
        pump()
        return added
    }

    func enqueue(videoIDs: [String]) {
        enqueue(videoIDs.map { (id: $0, title: "", channel: "", channelID: "") })
    }

    /// Several at once, in their order, before the rest of the line.
    func retry(_ videos: [Video]) {
        enqueue(videos.map { (id: $0.videoID, title: $0.title, channel: $0.channelTitle, channelID: $0.channelID) },
                force: true, front: true)
    }

    func retry(_ video: Video, withWhisper: Bool = false) {
        if withWhisper { forceWhisper.insert(video.videoID) }
        enqueue([(id: video.videoID, title: video.title, channel: video.channelTitle, channelID: video.channelID)], force: true,
                front: true)
    }

    /// Eats a video by listening to it with Whisper even when captions exist (better punctuation than auto-captions).
    func eatWithWhisper(_ id: String) {
        forceWhisper.insert(id)
        enqueue([(id: id, title: "", channel: "", channelID: "")], force: true, front: true)
    }

    /// Pauses eating: what is running stops and waits at the front of the line, nothing new starts.
    func pauseEating() {
        guard !eatingPaused else { return }
        eatingPaused = true
        for (id, task) in tasks {
            pausedRunning.insert(id)
            task.cancel()
        }
        for id in queue { video(id)?.statusDetail = "Paused" }
        try? context.save()
        AppLog.write("EAT paused (\(tasks.count) stopped, \(queue.count) waiting)")
    }

    func resumeEating() {
        guard eatingPaused else { return }
        eatingPaused = false
        for id in queue { video(id)?.statusDetail = "" }
        AppLog.write("EAT resumed")
        pump()
        // 4.5: Pause also holds the local AI's polishing and summaries; they continue now.
        runPolishLane()
        runSummaryLane()
    }

    /// A video leaves the library: it is taken out of every lane (what is running stops at its next step).
    func forget(_ id: String) {
        cancel(id)
        polishQueue.removeAll { $0 == id }
        summaryQueue.removeAll { $0 == id }
        screenQueue.removeAll { $0 == id }
        entityQueue.removeAll { $0 == id }
        playbookQueue.removeAll { $0 == id }
        jobs[id] = nil
    }

    func cancel(_ id: String) {
        pausedRunning.remove(id)
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
        let afterEating = [EatStatus.summarizing, .polishing].map(\.rawValue)
        let stuck = FetchDescriptor<Video>(predicate: #Predicate { afterEating.contains($0.statusRaw) })
        for video in (try? context.fetch(stuck)) ?? [] {
            video.status = .done
            // Finish what was interrupted: polishing and summary.
            if video.polishedData == nil || video.digestData == nil { schedulePost(video.videoID) }
        }
        try? context.save()
        pump()
        resumePending()
    }

    /// The after-eating queue lives in memory: after a restart (or a crash), finish polishing and
    /// summaries that were still waiting. Also called at each channel check.
    func resumePending() {
        // Summaries that failed only because the Codex usage limit was reached are tried again.
        let failed = FetchDescriptor<Video>(predicate: #Predicate { $0.digestError != nil })
        var cleared = 0
        for video in (try? context.fetch(failed)) ?? [] {
            let message = video.digestError ?? ""
            // 4.5: also the ones Apple Intelligence declined or failed, now that the local AI takes over (in "auto").
            let appleFailed = localSummaryAllowed && settings.summaryEngine == "auto"
                && (message.contains("Apple Intelligence") || message.contains("FoundationModels") || message.contains("SensitiveContentAnalysis"))
            // 4.5: summaries that timed out while the two lanes waited for the local AI, tried once more (the
            // local AI now answers one request at a time).
            let timedOut = message.lowercased().contains("timed out") && !summaryRetried.contains(video.videoID)
            if timedOut { summaryRetried.insert(video.videoID) }
            guard Self.isUsageLimit(SummaryError.model(message)) || appleFailed || timedOut else { continue }
            if appleFailed { appleDeclined.insert(video.videoID) }
            video.digestError = nil
            cleared += 1
        }
        if cleared > 0 {
            try? context.save()
            AppLog.write("SUMMARY \(cleared) summaries that failed (Codex limit, Apple Intelligence) are queued again")
        }
        let doneRaw = EatStatus.done.rawValue
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == doneRaw },
                                                sortBy: [SortDescriptor(\.addedAt)])
        for video in (try? context.fetch(descriptor)) ?? [] {
            if needsPost(video) { schedulePost(video.videoID) }
            if settings.githubEnabled, video.githubCheckedAt == nil { scheduleGitHub(video.videoID) }
        }
    }

    func needsPost(_ video: Video) -> Bool {
        guard video.status == .done, !inPost(video.videoID),
              !polishing.contains(video.videoID),
              !summarizing.contains(video.videoID) else { return false }
        guard !video.textOnly else { return false }
        let wantsPolish = shouldPolish(video) && video.polishedData == nil && video.polishError == nil
        let wantsSummary = settings.autoSummarize && canSummarize && video.digestData == nil && video.digestError == nil
        return wantsPolish || wantsSummary
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
        guard !eatingPaused else { return }
        // Only eating tasks count here: polishing and summaries run in their own queue.
        while tasks.count < max(1, settings.maxParallel), !queue.isEmpty {
            let id = queue.removeFirst()
            jobs[id] = JobState(step: "Starting")
            tasks[id] = Task {
                await self.run(id)
                self.jobs[id] = nil
                self.tasks[id] = nil
                if self.pausedRunning.remove(id) != nil, let video = self.video(id), video.status != .done {
                    // Stopped by Pause: back to the front of the line, eaten again from the start on Resume.
                    video.status = .queued
                    video.statusDetail = "Paused"
                    if !self.queue.contains(id) { self.queue.insert(id, at: 0) }
                    try? self.context.save()
                }
                self.pump()
            }
        }
    }

    func update(_ id: String, _ step: String, _ progress: Double? = nil) {
        jobs[id] = JobState(step: step, progress: progress)
        video(id)?.statusDetail = step
    }

    // MARK: The pipeline

    private func run(_ id: String) async {
        guard let video = video(id) else { return }
        if video.kind != .youtube {
            await runMedia(video)
            return
        }
        guard let firstReader = settings.makeYTDLP(withComments: settings.commentsCount > 0) else {
            video.status = .failed
            video.statusDetail = EatError.missing("yt-dlp").localizedDescription
            return
        }
        let workDir = AppFolders.work.appendingPathComponent(id + "-" + UUID().uuidString.prefix(6), isDirectory: true)
        defer { try? FileManager.default.removeItem(at: workDir) }
        video.attempts += 1

        do {
            video.status = .fetching
            update(id, "Reading video info")
            // 4.5: when YouTube says "Video unavailable" to one way of reading, the other ways are tried.
            let (ytdlp, info) = try await firstReader.infoTryingOtherWays(videoID: id) { way in
                Task { @MainActor in self.update(id, "Reading video info — trying \(way)") }
            }
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
                AppLog.write("TRACKS \(id): picked \(track.language) · language \(info.language ?? "?") · spoken \(info.spokenLanguage ?? "?") · captions \(info.subtitles.joined(separator: ",")) · originals \(info.autoCaptions.filter { $0.hasSuffix("-orig") }.joined(separator: ",")) · \(info.autoCaptions.count) auto")
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
            finishEating(video, result: result, fallbackLanguage: info.language ?? "")
        } catch {
            guard video.isAlive else { return }   // deleted while it was eaten
            let cancelled = Self.isCancellation(error)
            video.status = .failed
            let raw = error.localizedDescription
            video.statusDetail = cancelled ? "Cancelled" : EatFailure.explain(raw)
            AppLog.write("FAILED \(id): \(raw)")
        }
        try? context.save()
    }

    /// The text is in: save it, write the note, then GitHub checks, polishing and the summary follow.
    func finishEating(_ video: Video, result: TranscriptResult, fallbackLanguage: String) {
        guard video.isAlive else { return }
        let id = video.videoID
        video.segments = result.segments
        video.transcriptText = Paragrapher.paragraphs(from: result.segments).map(\.text).joined(separator: "\n\n")
        video.source = result.source
        video.language = result.language.isEmpty ? fallbackLanguage : result.language
        if video.duration == 0 { video.duration = result.segments.last?.end ?? 0 }
        video.eatenAt = .now
        video.status = .done
        video.statusDetail = ""
        AppLog.write("EATEN \(id) [\(result.source.rawValue), \(video.language)] \(video.displayTitle)")
        video.polished = []
        video.polishError = nil
        try? context.save()
        exporter.export(video)
        try? context.save()
        onChanged?(id)
        scheduleGitHub(id)
        schedulePost(id)
        onEaten?(id)
    }

    nonisolated static func isCancellation(_ error: Error) -> Bool {
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
        video.tags = info.tags
        video.viewCount = info.viewCount
        video.likeCount = info.likeCount
        if !info.comments.isEmpty { video.comments = info.comments }
        if let thumbnail = info.thumbnail { video.thumbnailURLString = thumbnail }
    }

    // MARK: After eating: polish and summarize in two lanes, then save

    private func schedulePost(_ id: String) {
        guard let video = video(id) else { return }
        let forced = forcePolish.contains(id)
        let wantsPolish = forced || (!video.textOnly && shouldPolish(video) && video.polishedData == nil && video.polishError == nil)
        let wantsSummary = !video.textOnly && settings.autoSummarize && canSummarize && video.digestData == nil
        // 4.5: newest first — what was just eaten is finished before an old backlog (a whole channel, last night's
        // podcasts), so its output is ready while you are looking at it.
        if wantsPolish, !polishQueue.contains(id), polishCurrent != id { polishQueue.insert(id, at: 0) }
        if wantsSummary, !summaryQueue.contains(id), summaryCurrent != id { summaryQueue.insert(id, at: 0) }
        if !wantsPolish, !wantsSummary { finish(video) }
        runPolishLane()
        runSummaryLane()
    }

    /// Takes the next video that the other lane is not working on (falls back to the first one).
    private static func take(from queue: inout [String], avoiding other: String?) -> String? {
        guard !queue.isEmpty else { return nil }
        let index = queue.firstIndex { $0 != other } ?? 0
        return queue.remove(at: index)
    }

    private func runPolishLane() {
        guard !polishRunning, !polishQueue.isEmpty else { return }
        polishRunning = true
        Task {
            while !eatingPaused {
                // 4.5: a playbook (asked for, minutes, a vision model) goes first; polishing waits.
                while playbookCurrent != nil { try? await Task.sleep(for: .seconds(3)) }
                guard !eatingPaused, let id = Self.take(from: &polishQueue, avoiding: summaryCurrent) else { break }
                polishCurrent = id
                if let video = video(id) {
                    forcePolish.remove(id)
                    await polish(video)
                    finishIfIdle(video, fromPolish: true)
                }
                polishCurrent = nil
            }
            polishRunning = false
        }
    }

    private func runSummaryLane() {
        guard !summaryRunning, !summaryQueue.isEmpty else { return }
        summaryRunning = true
        Task {
            while !eatingPaused {
                while playbookCurrent != nil { try? await Task.sleep(for: .seconds(3)) }
                guard !eatingPaused, let id = Self.take(from: &summaryQueue, avoiding: polishCurrent) else { break }
                summaryCurrent = id
                if let video = video(id), video.digestData == nil { await summarize(video) }
                if let video = video(id) { finishIfIdle(video, fromPolish: false) }
                summaryCurrent = nil
            }
            summaryRunning = false
        }
    }

    /// The last lane to finish with a video saves its note and notifies.
    private func finishIfIdle(_ video: Video, fromPolish: Bool) {
        let id = video.videoID
        let otherCurrent = fromPolish ? summaryCurrent : polishCurrent
        guard !polishQueue.contains(id), !summaryQueue.contains(id), otherCurrent != id else { return }
        finish(video)
    }

    private func finish(_ video: Video) {
        guard video.isAlive else { return }
        exporter.export(video)
        exporter.scheduleIndexes(in: context)
        try? context.save()
        onProcessed?(video.videoID)
        scheduleEntities(video.videoID)
        if video.fromChannelWatch, settings.notifyWhenEaten {
            Notifier.post(title: "Eaten: \(video.displayTitle)", body: video.channelTitle)
        }
    }

    /// Called when a video has finished all its after-eating work (used to refresh Claude packs).
    @ObservationIgnored var onProcessed: ((String) -> Void)?
    /// Called whenever an item's searchable content changes (eaten, polished, summarized, screen read…).
    @ObservationIgnored var onChanged: ((String) -> Void)?
    /// Called once the text of an item is in (used to read the screen of your own videos).
    @ObservationIgnored var onEaten: ((String) -> Void)?

    // MARK: GitHub links (fast, beside polishing and summaries)

    func isCheckingGitHub(_ id: String) -> Bool { githubChecking == id || githubQueue.contains(id) }

    /// Finds the GitHub repositories linked in a video and checks them through the GitHub API.
    func scheduleGitHub(_ id: String, force: Bool = false) {
        guard settings.githubEnabled || force else { return }
        if !githubQueue.contains(id), githubChecking != id { githubQueue.append(id) }
        guard !githubRunning else { return }
        githubRunning = true
        Task {
            while !githubQueue.isEmpty {
                let next = githubQueue.removeFirst()
                githubChecking = next
                if let video = video(next) { await checkGitHub(video) }
                githubChecking = nil
            }
            githubRunning = false
        }
    }

    /// Checks again now (forgets the 3-day cache).
    func recheckGitHub(_ video: Video) {
        GitHubLinks.clearCache()
        scheduleGitHub(video.videoID, force: true)
    }

    private func checkGitHub(_ video: Video) async {
        guard video.status.hasText else { return }
        let id = video.videoID
        let checks = await GitHubLinks.check(videoID: id, description: video.videoDescription, channel: video.channelTitle,
                                             comments: video.comments, transcript: video.transcriptText)
        video.repos = checks
        // When GitHub's hourly limit was reached, try again at the next launch or channel check.
        video.githubCheckedAt = checks.contains { $0.verdict == "unchecked" } ? nil : .now
        try? context.save()
        guard !checks.isEmpty else { return }
        AppLog.write("GITHUB \(id): \(checks.map { "\($0.fullName)=\($0.verdict)" }.joined(separator: ", "))")
        if settings.secondBrainEnabled, SecondBrainExporter.reachable(settings.githubURL.deletingLastPathComponent()) {
            let folder = settings.githubURL
            let note = video.noteName ?? String(SecondBrainExporter.fileName(for: video).dropLast(3))
            for check in checks where check.exists {
                let file = folder.appendingPathComponent("\(check.noteName).md")
                guard !FileManager.default.fileExists(atPath: file.path) else { continue }
                let readme = await GitHubLinks.readmeExcerpt(owner: check.owner, name: check.name)
                let mention = GitHubLinks.Mention(note: note, title: video.displayTitle, source: check.foundIn)
                GitHubLinks.writeNote(check, seenIn: [mention], folder: folder, create: true, readme: readme)
            }
        }
        exporter.export(video)
        exporter.scheduleIndexes(in: context)
        try? context.save()
    }

    func shouldPolish(_ video: Video) -> Bool {
        guard settings.polishEnabled, polisher.isInstalled else { return false }
        switch video.source {
        case .autoCaptions, .whisper: return true
        case .captions, .published: return settings.polishCaptionsToo
        case .post: return false       // the words of a post as written (3.9): nothing to clean up
        case .none: return false
        }
    }

    func isPolishing(_ id: String) -> Bool { polishing.contains(id) }

    /// Local AI clean-up of the transcript (Ollama). Keeps the original next to it.
    func polish(_ video: Video) async {
        let id = video.videoID
        guard !polishing.contains(id), video.status.hasText else { return }
        polishing.insert(id)
        defer { polishing.remove(id) }
        let previous = video.status
        video.status = .polishing
        video.polishError = nil
        do {
            jobs[id] = JobState(step: "Starting the local AI (\(settings.polishModel))")
            let texts = try await polisher.polish(title: video.displayTitle, channel: video.channelTitle,
                                                  paragraphs: video.paragraphs, hints: video.tags + video.chapters.map(\.title)) { done, total in
                self.jobs[id] = JobState(step: "Polishing with \(self.settings.polishModel) — \(done)/\(total)",
                                         progress: total > 0 ? Double(done) / Double(total) : nil)
            }
            guard video.isAlive else { return }   // deleted while it was polished
            video.polished = texts
            video.polishModel = settings.polishModel
            video.transcriptText = video.displayParagraphs.map(\.text).joined(separator: "\n\n")
            AppLog.write("POLISHED \(id) with \(settings.polishModel)")
        } catch {
            guard video.isAlive else { return }
            video.polishError = error.localizedDescription
            AppLog.write("POLISH \(id) failed: \(error.localizedDescription)")
        }
        video.status = previous == .polishing ? .done : previous
        if video.status != .failed { video.status = .done }
        if tasks[id] == nil { jobs[id] = nil }
        // The moments of the summary follow the polished text.
        if let digest = video.digest { video.digest = Grounder.ground(digest, paragraphs: video.displayParagraphs) }
        try? context.save()
        exporter.export(video)
        try? context.save()
        onChanged?(id)
    }

    /// Polish a video by hand (from the detail view or the menu).
    func polishNow(_ video: Video) {
        forcePolish.insert(video.videoID)
        schedulePost(video.videoID)
    }

    private func transcribeWithWhisper(id: String, ytdlp: YTDLP, workDir: URL) async throws -> TranscriptResult {
        guard let whisperPath = ToolLocator.find("whisper-cli", override: settings.whisperPath) else { throw EatError.missing("whisper-cli") }
        guard let ffmpegPath = ToolLocator.find("ffmpeg", override: settings.ffmpegPath) else { throw EatError.missing("ffmpeg") }
        let whisper = makeWhisper(whisperPath: whisperPath, ffmpegPath: ffmpegPath)
        try await waitForWhisper(id)
        defer { whisperBusy = false }
        try await prepareWhisper(whisper, id: id)
        update(id, "No captions — downloading audio", 0)
        let audio = try await ytdlp.downloadAudio(videoID: id, workDir: workDir.appendingPathComponent("audio")) { value in
            Task { @MainActor in self.update(id, "No captions — downloading audio", value) }
        }
        return try await listen(whisper, audio: audio, id: id, workDir: workDir)
    }

    func makeWhisper(whisperPath: String, ffmpegPath: String) -> WhisperTranscriber {
        WhisperTranscriber(whisperPath: whisperPath, ffmpegPath: ffmpegPath,
                           model: WhisperModel(rawValue: settings.whisperModel) ?? .turbo,
                           engine: SpeechEngine(rawValue: settings.speechEngine) ?? .auto)
    }

    /// One item listens at a time (the recognizer uses the whole GPU).
    func waitForWhisper(_ id: String) async throws {
        video(id)?.status = .transcribing
        if whisperBusy { update(id, "Waiting to listen (another item is being listened to)") }
        while whisperBusy {
            try await Task.sleep(for: .seconds(1))
        }
        whisperBusy = true
    }

    func prepareWhisper(_ whisper: WhisperTranscriber, id: String) async throws {
        if whisper.resolvedEngine == .whisperCpp, !whisper.hasModel {
            update(id, "Downloading the Whisper model (once)", 0)
            try await whisper.ensureModel { value in
                Task { @MainActor in self.update(id, "Downloading the Whisper model (once)", value) }
            }
        }
    }

    func listen(_ whisper: WhisperTranscriber, audio: URL, id: String, workDir: URL) async throws -> TranscriptResult {
        let step = "Listening with \(whisper.engineLabel)"
        update(id, step, 0)
        let started = Date.now
        let result = try await offMain(.userInitiated) {
            try await whisper.transcribe(audio: audio, workDir: workDir) { value in
                Task { @MainActor in self.update(id, step, value) }
            }
        }
        let seconds = Date.now.timeIntervalSince(started)
        let length = result.segments.last?.end ?? 0
        AppLog.write("WHISPER \(id) \(whisper.engineLabel): \(Int(length))s of audio in \(Int(seconds))s")
        return result
    }

    // MARK: Summary

    func isSummarizing(_ id: String) -> Bool { summarizing.contains(id) }

    /// The library's topic tags, most used first.
    func knownTopics() -> [String] {
        let doneRaw = EatStatus.done.rawValue
        let videos = (try? context.fetch(FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == doneRaw }))) ?? []
        var counts: [String: (name: String, count: Int)] = [:]
        for topic in videos.flatMap({ $0.digest?.topics ?? [] }) {
            let key = topic.lowercased()
            counts[key] = (counts[key]?.name ?? topic, (counts[key]?.count ?? 0) + 1)
        }
        return counts.values.sorted { $0.count > $1.count }.prefix(40).map(\.name)
    }

    /// Apple Intelligence on this Mac, or Codex when allowed (Settings › AI).
    var useAppleIntelligence: Bool {
        settings.summaryEngine != "codex" && settings.summaryEngine != "local" && summarizer.isAvailable
    }

    /// Codex is never used unless chosen explicitly ("codex") and allowed (Settings › Cloud AI).
    var codexSummaryAllowed: Bool {
        settings.summaryEngine == "codex" && settings.openAIConsent && CodexLocator.path != nil && !codexPaused
    }

    /// The local AI (Ollama, free) summarizes when chosen, in "auto" when Apple Intelligence is not ready,
    /// and while Codex is paused by its usage limit.
    var localSummaryAllowed: Bool {
        settings.summaryEngine != "apple" && polisher.isInstalled
    }

    var canSummarize: Bool { useAppleIntelligence || codexSummaryAllowed || localSummaryAllowed }

    /// 4.5: which engine will summarize this item, in one sentence (the same choice the summary lane makes).
    func summaryPlan(for id: String) -> String {
        guard canSummarize else { return summaryUnavailableMessage }
        let appleResting = (appleSummaryPausedUntil ?? .distantPast) > .now
        if useAppleIntelligence, !(localSummaryAllowed && (appleDeclined.contains(id) || appleResting)) {
            return "Apple Intelligence will summarize it on this Mac."
        }
        if codexSummaryAllowed, !useAppleIntelligence { return "Codex will write the summary (you chose it in Settings)." }
        let waiting = summaryQueue.firstIndex(of: id).map { " — \($0 + 1) in line" } ?? ""
        if useAppleIntelligence, appleDeclined.contains(id) {
            return "Apple Intelligence declined this one: the local AI will write the summary on this Mac (free)" + waiting + "."
        }
        return "The local AI will write the summary on this Mac (free)" + waiting + "."
    }

    // MARK: Codex usage limit

    /// When the ChatGPT plan's Codex limit is reached, Zeus stops using Codex until the time Codex gives,
    /// so the user's own Codex work is not blocked, and summaries continue with the local AI.
    var codexPausedUntil: Date? {
        get { UserDefaults.standard.object(forKey: "codexPausedUntil") as? Date }
        set { UserDefaults.standard.set(newValue, forKey: "codexPausedUntil") }
    }

    var codexPaused: Bool { (codexPausedUntil ?? .distantPast) > .now }

    nonisolated static func isUsageLimit(_ error: Error) -> Bool {
        let text = error.localizedDescription.lowercased()
        return text.contains("usage limit") || text.contains("rate limit") || text.contains("quota")
    }

    /// "…try again at 8:01 PM." → today (or tomorrow) at 20:01; otherwise in one hour.
    func pauseCodex(after error: Error) {
        let text = error.localizedDescription
        var until = Date.now.addingTimeInterval(3600)
        if let match = text.range(of: #"try again at (\d{1,2}):(\d{2}) ?([AaPp][Mm])?"#, options: .regularExpression) {
            let clock = text[match].replacingOccurrences(of: "try again at ", with: "")
            let digits = clock.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
            if digits.count >= 2 {
                var hour = digits[0]
                let pm = clock.lowercased().contains("pm"), am = clock.lowercased().contains("am")
                if pm, hour < 12 { hour += 12 }
                if am, hour == 12 { hour = 0 }
                var parts = Calendar.current.dateComponents([.year, .month, .day], from: .now)
                parts.hour = hour
                parts.minute = digits[1]
                if var date = Calendar.current.date(from: parts) {
                    if date <= .now { date = date.addingTimeInterval(86_400) }
                    until = date.addingTimeInterval(120)
                }
            }
        }
        codexPausedUntil = until
        AppLog.write("CODEX usage limit reached: paused until \(until.formatted(date: .omitted, time: .shortened)); summaries continue with the local AI")
    }

    var summaryUnavailableMessage: String {
        if settings.summaryEngine == "apple" { return summarizer.availabilityMessage }
        if settings.summaryEngine == "local" || settings.summaryEngine == "auto" {
            return summarizer.availabilityMessage + " The local AI (Ollama) is not installed either: install it from ollama.com, it is free."
        }
        if codexPaused, let until = codexPausedUntil {
            return "Codex reached the ChatGPT usage limit; it is used again after \(until.formatted(date: .omitted, time: .shortened))."
        }
        if !settings.openAIConsent { return "Codex is chosen for summaries but not allowed: allow it in Settings › Cloud AI, or pick a free engine in Settings › AI." }
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
            var result: VideoDigest?
            let appleResting = (appleSummaryPausedUntil ?? .distantPast) > .now
            if useAppleIntelligence, !(localSummaryAllowed && (appleDeclined.contains(id) || appleResting)) {
                do {
                    result = try await summarizer.summarize(
                        title: video.displayTitle,
                        channel: video.channelTitle,
                        paragraphs: video.displayParagraphs,
                        videoLanguage: video.language,
                        youtubeChapters: video.chapters,
                        outputLanguage: settings.summaryLanguage,
                        knownTopics: knownTopics()) { step in
                            self.jobs[id] = JobState(step: step)
                            video.statusDetail = step
                        }
                    appleFailuresInARow = 0
                } catch let error where localSummaryAllowed && !Self.isCancellation(error) {
                    // 4.5: Apple Intelligence declined or failed (refusal, sensitive content, model error): the free local AI
                    // summarizes instead, in "auto" (with "Apple Intelligence only", the error stays).
                    guard video.isAlive else { return }
                    appleDeclined.insert(id)
                    appleFailuresInARow += 1
                    if appleFailuresInARow >= 3 {
                        appleSummaryPausedUntil = Date.now.addingTimeInterval(6 * 3600)
                        appleFailuresInARow = 0
                        AppLog.write("SUMMARY Apple Intelligence failed 3 times in a row: the local AI summarizes for the next 6 hours")
                    }
                    AppLog.write("SUMMARY \(id): Apple Intelligence failed (\(error.localizedDescription.prefix(120))), the local AI summarizes instead")
                }
            } else if codexSummaryAllowed, let codex = CodexLocator.path {
                jobs[id] = JobState(step: "Summarizing with Codex")
                video.statusDetail = "Summarizing with Codex"
                let language = settings.summaryLanguage == "auto" ? video.language : settings.summaryLanguage
                do {
                    result = try await CodexSummarizer(executable: codex, model: settings.codexModel).summarize(
                        title: video.displayTitle, channel: video.channelTitle, paragraphs: video.displayParagraphs,
                        language: language.isEmpty ? "en" : language, youtubeChapters: video.chapters,
                        knownTopics: knownTopics())
                } catch let error where Self.isUsageLimit(error) {
                    pauseCodex(after: error)
                    guard localSummaryAllowed else { throw error }
                }
            }
            if result == nil, localSummaryAllowed {
                let language = settings.summaryLanguage == "auto" ? video.language : settings.summaryLanguage
                jobs[id] = JobState(step: "Summarizing with the local AI")
                video.statusDetail = "Summarizing with the local AI"
                result = try await LocalSummarizer(model: settings.polishModel).summarize(
                    title: video.displayTitle, channel: video.channelTitle, paragraphs: video.displayParagraphs,
                    language: language.isEmpty ? "en" : language, youtubeChapters: video.chapters,
                    knownTopics: knownTopics()) { step in
                        Task { @MainActor in self.jobs[id] = JobState(step: step) }
                    }
            }
            guard video.isAlive else { return }   // deleted while it was summarized
            guard let digest = result else { throw SummaryError.unavailable(summaryUnavailableMessage) }
            // Every key point and summary sentence gets the moment of the video it comes from.
            video.digest = Grounder.ground(digest, paragraphs: video.displayParagraphs)
            let found = (video.digest?.keyPointTimes ?? []).compactMap { $0 }.count
            AppLog.write("SUMMARY \(id) ok (\(digest.engine)), \(found)/\(digest.keyPoints.count) key points placed in the video")
        } catch {
            guard video.isAlive else { return }
            if Self.isUsageLimit(error) {
                // Not the video's fault: it is summarized again when Codex is back.
                video.digestError = nil
                AppLog.write("SUMMARY \(id) waits for Codex: \(error.localizedDescription.prefix(120))")
            } else {
                video.digestError = error.localizedDescription
                AppLog.write("SUMMARY \(id) failed: \(error.localizedDescription)")
            }
        }
        guard video.isAlive else { return }
        video.status = previous == .summarizing ? .done : (previous == .failed ? .failed : .done)
        video.statusDetail = ""
        if tasks[id] == nil { jobs[id] = nil }
        try? context.save()
        exporter.export(video)
        exporter.scheduleIndexes(in: context)
        try? context.save()
        onChanged?(id)
    }
}

/// Runs heavy work off the main thread; cancelling the caller cancels the work (and the processes it runs).
nonisolated func offMain<T: Sendable>(_ priority: TaskPriority = .utility,
                                      _ operation: @escaping @Sendable () async throws -> T) async throws -> T {
    let work = Task.detached(priority: priority) { try await operation() }
    return try await withTaskCancellationHandler { try await work.value } onCancel: { work.cancel() }
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

/// 4.5: why an item could not be eaten, in words (yt-dlp's errors are for programmers).
nonisolated enum EatFailure {
    /// The explanation an earlier 4.5 build stored for "Video unavailable" (those videos were mostly online).
    private static let oldRefusedSentence = "is blocked in your country."

    /// Blocked in your country by its owner (copyright): reading it another way will not help.
    static func isCountryBlocked(_ detail: String) -> Bool {
        let text = detail.lowercased()
        return text.contains("blocked it in your country") || text.contains("not made this video available in your country")
            || text.hasPrefix("blocked in your country")
    }

    /// YouTube is limiting this Mac's requests ("This content isn't available, try again later", HTTP 429).
    static func isRateLimited(_ detail: String) -> Bool {
        let text = detail.lowercased()
        return text.contains("content isn't available") || text.contains("rate-limited") || text.contains("rate limited")
            || text.contains("http error 429") || text.contains("too many requests") || text.hasPrefix("youtube is limiting")
    }

    /// The video is not on YouTube any more (removed, made private, account closed): retrying will not help.
    static func isGone(_ detail: String) -> Bool {
        let text = detail.lowercased()
        if text.hasPrefix("not on youtube any more") { return !text.hasSuffix(oldRefusedSentence) }
        return text.contains("private video") || text.contains("has been removed") || text.contains("no longer available")
            || text.contains("account associated with this video has been terminated")
    }

    /// YouTube said "Video unavailable" to Zeus without a reason, though the video may well play in a browser
    /// (seen on 50 Lex Fridman episodes that are online): worth retrying, other ways of reading are tried.
    static func isRefused(_ detail: String) -> Bool {
        let text = detail.lowercased()
        if text.hasPrefix("not on youtube any more") { return text.hasSuffix(oldRefusedSentence) }
        if isGone(detail) || isCountryBlocked(detail) || isRateLimited(detail) { return false }
        return text.contains("video unavailable") || text.hasPrefix("youtube refused")
    }

    /// A clear sentence for the error yt-dlp gave (the original text is kept in the log).
    static func explain(_ detail: String) -> String {
        let text = detail.lowercased()
        if text.hasPrefix("youtube refused") || text.hasPrefix("youtube is limiting") || text.hasPrefix("blocked in your country") {
            return detail
        }
        if isRefused(detail) {
            return "YouTube refused to show this video to Zeus (it may play fine in a browser). Zeus tried other ways of reading it; retry later."
        }
        if isGone(detail) {
            return text.hasPrefix("not on youtube any more") ? detail
                : "Not on YouTube any more: the video was removed, made private, or its channel was closed."
        }
        if isCountryBlocked(detail) { return "Blocked in your country by its owner." }
        if isRateLimited(detail) { return "YouTube is limiting requests from this Mac. Retry in an hour." }
        if text.contains("confirm you") && text.contains("not a bot") {
            return "YouTube asked to confirm you are not a bot. Sign in to YouTube in Zeus (YouTube › Accounts), then retry."
        }
        if text.contains("confirm your age") || text.contains("age-restricted") || text.contains("inappropriate for some users") {
            return "Age-restricted. Sign in to YouTube in Zeus (YouTube › Accounts), then retry."
        }
        if text.contains("members-only") || text.contains("join this channel") || text.contains("available to this channel's members") {
            return "Members-only. Sign in with the YouTube account that is a member (YouTube › Accounts), then retry."
        }
        return detail
    }
}
