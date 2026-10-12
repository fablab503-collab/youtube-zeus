import AppKit
import Foundation
import UniformTypeIdentifiers

/// YouTube Zeus 3.8: Effects — a free, local Genjutsu. Everything runs on this Mac through the engine in effects/.
extension AppModel {
    /// Opens the Effects tab; with an item of the library (or a YouTube id) as the source (youtubezeus://effects?video=…).
    func openEffects(video id: String? = nil, start: Double? = nil) {
        selection = .effects
        if let id {
            effects.selectedJobID = nil
            effects.sourceVideoID = id
            effects.sourceFile = nil
        }
        if let start { effects.startText = start.timestamp }
    }

    /// What the engine reads: the YouTube link of a video, or the file of one of your own videos (podcasts have no picture).
    func effectsInput(for id: String) -> String? {
        if let video = engine.video(id) {
            switch video.kind {
            case .youtube: return "https://www.youtube.com/watch?v=\(id)"
            case .file:
                guard let file = video.mediaURL, file.isFileURL, MediaFiles.isVideo(file) else { return nil }
                return file.path
            case .podcast: return nil
            case .social: return video.duration > 0 ? video.pageURLString : nil   // a post with a video (3.9)
            }
        }
        return MediaKind.of(id: id) == .youtube ? "https://www.youtube.com/watch?v=\(id)" : nil
    }

    var effectsSourceInput: String? {
        if let file = effects.sourceFile { return file.path }
        return effects.sourceVideoID.flatMap(effectsInput(for:))
    }

    /// The title to show for a job: the library's title of its YouTube video, else the file name.
    func effectsTitle(_ job: EffectsJob) -> String {
        if let id = job.youtubeID, let video = engine.video(id) { return video.displayTitle }
        return job.inputName
    }

    func setUpEffects(_ setup: EffectsSetup) {
        let effects = self.effects
        Task {
            if await effects.setUp(setup) {
                show(setup == .main ? "The Effects engine is set up on this Mac." : "\(setup.label.replacingOccurrences(of: "Adding", with: "Added")).")
                if setup == .main { startSwapWatcherIfEnabled() }
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    // MARK: Make it me (effects/MAKE-IT-ME.md)

    /// One button: prepare, references, approve, face swap, hat, voice, final — on this Mac, with consent.
    func makeMeEffect() {
        guard let input = effectsSourceInput else {
            show("Pick a video of the library or a video file first.", error: true)
            return
        }
        let effects = self.effects
        Task {
            if let job = await effects.makeMe(input: input) {
                if settings.notifyWhenEaten { Notifier.post(title: "Make it me is done", body: effectsTitle(job)) }
                show("Done: “\(effectsTitle(job))” is you now. Play it, compare, or save it to Movies.")
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    /// A failed or cancelled Make it me job goes on from the step that stopped.
    func continueMakeMe(_ job: EffectsJob) {
        let effects = self.effects
        Task {
            if await effects.continueMakeMe(job) {
                show("Done: “\(effectsTitle(job))” is finished.")
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    func chooseEffectsSelfVideo() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie]
        panel.message = "Choose your self-video (30–60 s, chest up, soft light, no hat: turn left and right, up and down, talk, smile)."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        effects.selfVideo = url
        effects.meSource = .selfVideo
    }

    func chooseEffectsVoiceFile() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.audio, .movie]
        panel.message = "Choose a recording of the voice: 10–60 seconds of clear speech, yours or of someone who agreed."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        effects.voiceFile = url
        effects.voiceSource = .file
    }

    // MARK: Do jobs from The Swap

    /// Starts the shared-folder watcher when the setting is on and the engine is set up (at launch, after set-up,
    /// when the setting is turned on).
    func startSwapWatcherIfEnabled() {
        guard settings.swapJobsEnabled, EffectsPaths.isSetUp else { return }
        let effects = self.effects
        swapWatcher.onJob = { [weak self] event in
            guard let self else { return }
            Task { await effects.refreshJobs() }
            if event.status == "done" {
                show("The Swap: “\(event.name)” is done; the result is in the shared folder.")
                if settings.notifyWhenEaten { Notifier.post(title: "The Swap job is done", body: event.name) }
            } else if event.status == "error" {
                show("The Swap: “\(event.name)” failed — \(event.message)", error: true)
            }
        }
        swapWatcher.start(cleanDays: settings.swapJobsCleanDays)
    }

    func setSwapJobs(enabled: Bool) {
        settings.swapJobsEnabled = enabled
        if enabled {
            startSwapWatcherIfEnabled()
        } else {
            swapWatcher.stop()
        }
    }

    func setSwapJobsCleanDays(_ days: Int) {
        settings.swapJobsCleanDays = days
        if settings.swapJobsEnabled, swapWatcher.isRunning { swapWatcher.restart(cleanDays: days) }
    }

    func prepareEffect() {
        guard let input = effectsSourceInput else {
            show("Pick a video of the library or a video file first.", error: true)
            return
        }
        let effects = self.effects
        Task {
            if let job = await effects.prepare(input: input) {
                show("“\(effectsTitle(job))” is prepared: look at the mask, then approve it.")
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    func approveEffect(_ job: EffectsJob) {
        let effects = self.effects
        Task {
            if await effects.approve(job) {
                show("Approved. Generation is bound to these exact files.")
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    func generateEffect(_ job: EffectsJob) {
        let effects = self.effects
        Task {
            if await effects.generate(job) {
                if settings.notifyWhenEaten { Notifier.post(title: "Effect ready", body: effectsTitle(job)) }
                show("The effect is ready, with the clip's original sound.")
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    func saveEffectToMovies(_ job: EffectsJob) {
        do {
            let file = try effects.saveToMovies(job)
            NSWorkspace.shared.activateFileViewerSelecting([file])
            show("Saved to Movies › YouTube Zeus Effects.")
        } catch {
            show("Could not save it: \(error.localizedDescription)", error: true)
        }
    }

    /// Stops a running Make it me job of the engine (`cancel --job`), when it was not started by this app.
    func cancelMakeMe(_ job: EffectsJob) {
        if effects.isBusy, effects.selectedJobID == job.id {
            effects.cancel()
            return
        }
        guard let script = EffectsPaths.script else { return }
        let process = Process()
        process.executableURL = EffectsPaths.python
        process.arguments = [script.path, "cancel", "--job", job.folder.path]
        process.environment = EffectsPaths.environment()
        process.currentDirectoryURL = EffectsPaths.engine
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
        show("Stopping “\(effectsTitle(job))”…")
    }

    func chooseEffectsFile() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.movie]
        panel.message = "Choose a video: Zeus Effects uses up to 10 seconds of it, on this Mac."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        effects.sourceFile = url
        effects.sourceVideoID = nil
    }

    /// "Use a face pack…": a `.c101face` folder (or package) from Cinema 101's Scan my face.
    func chooseEffectsFacePack() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseFiles = true                 // a pack registered as a package shows as a file
        panel.canChooseDirectories = true
        panel.treatsFilePackagesAsDirectories = false
        panel.allowedContentTypes = [.directory]    // folders and packages only
        panel.prompt = "Use Face Pack"
        panel.message = "Choose a face pack (.c101face) from Cinema 101 › Scan my face: you, or someone who agreed."
        if let latest = effects.foundPacks.first {
            panel.directoryURL = latest.deletingLastPathComponent()
        } else {
            panel.directoryURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
        }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        useEffectsFacePack(url)
    }

    func useEffectsFacePack(_ url: URL) {
        let effects = self.effects
        Task {
            if await effects.useFacePack(url), let pack = effects.facePack {
                show("Face pack “\(pack.name)”: \(pack.picked.count) of its \(pack.usable) usable frames will be used.")
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
    }

    func chooseEffectsPhoto(second: Bool) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.image]
        panel.message = "Choose a clear photo of the face: you, or someone who agreed."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if second { effects.secondPhoto = url } else { effects.referencePhoto = url }
    }
}
