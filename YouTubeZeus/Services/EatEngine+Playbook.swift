import Foundation
import SwiftData

/// Playbooks (3.2): a lane of their own, one video at a time, never while Whisper, polishing or summaries use the GPU.
extension EatEngine {
    func isMakingPlaybook(_ id: String) -> Bool { playbookCurrent == id || playbookQueue.contains(id) }

    /// Writes the playbook of an eaten item (again when it exists). `force`: even when it does not look like a tutorial.
    func schedulePlaybook(_ id: String, force: Bool = false) {
        guard let item = video(id), item.status.hasText else { return }
        if force { playbookForced.insert(id) }
        if !playbookQueue.contains(id), playbookCurrent != id {
            playbookQueue.append(id)
            playbookProgress(id, "Waiting for the local AI (another playbook or Whisper first)", nil)
        }
        guard !playbookRunning else { return }
        playbookRunning = true
        playbookTask = Task {
            // A playbook takes minutes: the Mac does not go to sleep in the middle (the screen still can).
            let activity = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "YouTube Zeus is making a playbook")
            defer { ProcessInfo.processInfo.endActivity(activity) }
            while !playbookQueue.isEmpty {
                // Two big models on the GPU at once slow both down a lot: not while Whisper listens. (4.5: no longer
                // after the whole polishing and summary backlog; those lanes hold while a playbook is made.)
                while whisperBusy {
                    try? await Task.sleep(for: .seconds(5))
                    if Task.isCancelled { break }
                }
                if Task.isCancelled { break }
                let next = playbookQueue.removeFirst()
                playbookCurrent = next
                if let item = video(next) { await makePlaybook(item) }
                playbookCurrent = nil
            }
            // Stopped while some were waiting.
            for id in playbookQueue { playbookDone(id) }
            playbookQueue.removeAll()
            playbookForced.removeAll()
            playbookCurrent = nil
            playbookRunning = false
        }
    }

    /// Stops the playbook being made (and the ones waiting).
    func cancelPlaybooks() {
        playbookTask?.cancel()
    }

    /// The step shown in the app, also written for `zeus playbook` and the MCP server (other processes).
    func playbookProgress(_ id: String, _ step: String, _ value: Double?) {
        // Waiting in the queue shows in the Playbook tab only (not as a job of the item).
        if playbookCurrent == id { jobs[id] = JobState(step: step, progress: value) }
        let text = value.map { "\(step) (\(Int(($0 * 10).rounded(.down)) * 10) %)" } ?? step
        guard playbookMarks[id] != text else { return }
        playbookMarks[id] = text
        PlaybookFiles.setMaking(id, text)
    }

    private func playbookDone(_ id: String) {
        playbookMarks[id] = nil
        PlaybookFiles.setMaking(id, nil)
    }

    private func makePlaybook(_ video: Video) async {
        let id = video.videoID
        let force = playbookForced.remove(id) != nil
        let workDir = AppFolders.work.appendingPathComponent("playbook-" + id + "-" + UUID().uuidString.prefix(6), isDirectory: true)
        defer { try? FileManager.default.removeItem(at: workDir) }
        video.playbookError = nil
        // `zeus playbook` and the MCP server read the library: no old error while this one is made.
        try? context.save()
        playbookProgress(id, "Getting the local AI ready", nil)
        do {
            let client = OllamaClient()
            try await client.ensureRunning()
            let model = settings.polishModel
            let hasModel = await client.hasModel(model)
            guard hasModel || ModelCatalog.isDownloaded(model) else { throw PlaybookError.noModel(model) }
            let snapshot = video.snapshot
            let language = settings.skillLanguage
            if !force {
                // Seconds, before minutes of downloading and looking at frames.
                playbookProgress(id, "Checking that the video shows something to do", nil)
                try await offMain { try await PlaybookMaker.checkTutorial(snapshot, model: model, language: language) }
            }
            let ffmpeg = ToolLocator.find("ffmpeg", override: settings.ffmpegPath)
            var media: URL?
            if ffmpeg != nil { media = try await picture(of: video, workDir: workDir) }
            var vision: String?
            if media != nil { vision = try await visionModelForPlaybooks(id) }
            let maker = PlaybookMaker(ffmpegPath: ffmpeg, model: model, visionModel: vision,
                                      language: language, maxFrames: settings.playbookFrames)
            let framesFolder = PlaybookFiles.folder(id)
            let source = media
            // Off the main thread: frames, Vision and the local AI are heavy.
            let playbook = try await offMain {
                try await maker.make(video: snapshot, media: source, workDir: workDir, framesFolder: framesFolder) { step, value in
                    Task { @MainActor in self.playbookProgress(id, step, value) }
                }
            }
            guard video.isAlive else { playbookDone(id); return }   // deleted while it was made
            video.playbook = playbook
            video.playbookAt = .now
            AppLog.write("PLAYBOOK \(id): \(playbook.steps.count) steps (\(playbook.agentSteps) agent, \(playbook.yourSteps) you), model \(model), vision \(playbook.visionModel ?? "none")")
        } catch {
            guard video.isAlive else { playbookDone(id); return }
            video.playbookError = Self.isCancellation(error) ? "Cancelled" : error.localizedDescription
            AppLog.write("PLAYBOOK \(id) failed: \(video.playbookError ?? "")")
        }
        // 4.5: research inside the playbook — what Zeus can check itself (Homebrew, npm, PyPI, GitHub), before the note is written.
        // Only for a playbook made just now (not after a failure or a cancel).
        if let made = video.playbook, let madeAt = video.playbookAt, Date.now.timeIntervalSince(madeAt) < 600 {
            playbookProgress(id, "Checking the tools and repositories", nil)
            let researchSnapshot = video.snapshot
            let found = (try? await offMain { await AutoResearch.run(researchSnapshot, playbook: made) }) ?? 0
            guard video.isAlive else { playbookDone(id); return }   // deleted while Zeus was checking
            if found > 0 { AppLog.write("RESEARCH \(id): \(found) fact(s) found by Zeus") }
        }
        if tasks[id] == nil { jobs[id] = nil }
        try? context.save()
        if video.playbook != nil { exporter.exportPlaybook(video) }
        exporter.export(video)
        try? context.save()
        playbookDone(id)
        onChanged?(id)
    }

    /// The picture of the item, for its frames: YouTube videos are downloaded without sound (deleted afterwards),
    /// your own videos are read where they are. Audio-only items have none.
    private func picture(of video: Video, workDir: URL) async throws -> URL? {
        let id = video.videoID
        switch video.kind {
        case .file:
            guard let file = video.mediaURL, MediaFiles.isVideo(file), FileManager.default.fileExists(atPath: file.path) else { return nil }
            return file
        case .youtube:
            guard let ytdlp = settings.makeYTDLP() else { throw EatError.missing("yt-dlp") }
            let step = "Downloading the picture to look at the frames"
            playbookProgress(id, step, 0)
            return try await ytdlp.downloadVideo(videoID: id, workDir: workDir.appendingPathComponent("video")) { value in
                Task { @MainActor in self.playbookProgress(id, step, value) }
            }
        case .social:
            guard video.duration > 0, let ytdlp = settings.makeSocialYTDLP(), let page = video.pageURLString else { return nil }
            let step = "Downloading the picture to look at the frames"
            playbookProgress(id, step, 0)
            return try? await ytdlp.downloadVideo(url: page, workDir: workDir.appendingPathComponent("video")) { value in
                Task { @MainActor in self.playbookProgress(id, step, value) }
            }
        case .podcast:
            guard let media = video.mediaURL, MediaFiles.isVideo(media) else { return nil }
            let file = workDir.appendingPathComponent("episode." + media.pathExtension)
            try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
            _ = try await FileDownloader(destination: file) { _ in }.download(from: media)
            return file
        }
    }

    /// The vision model for playbooks, downloaded the first time (nil: turned off, or none fits this Mac).
    func visionModelForPlaybooks(_ id: String) async throws -> String? {
        guard settings.playbookVision else { return nil }
        let setting = settings.visionModel
        let tag: String
        if let option = ModelCatalog.visionOptions.first(where: { $0.key == setting }) {
            tag = await ModelCatalog.firstAvailable(option)
        } else if !setting.isEmpty {
            tag = setting
        } else if ModelCatalog.canSee(settings.polishModel) {
            return settings.polishModel
        } else {
            let mac = await MacHardware.current()
            let options = await ModelCatalog.resolve(ModelCatalog.visionOptions)
            guard let best = ModelCatalog.recommended(options, on: mac) else { return nil }
            tag = best.tag
        }
        let client = OllamaClient()
        let installed = await client.hasModel(tag)
        if installed || ModelCatalog.isDownloaded(tag) { return tag }
        let step = "Downloading the vision model \(tag) (only the first time)"
        playbookProgress(id, step, 0)
        try await client.pull(tag) { value in
            Task { @MainActor in self.playbookProgress(id, step, value) }
        }
        AppLog.write("PLAYBOOK vision model \(tag) downloaded")
        return tag
    }
}
