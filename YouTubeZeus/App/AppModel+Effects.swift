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
            } else if let error = effects.lastError {
                show(error, error: true)
            }
        }
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
