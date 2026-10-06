import AppKit
import Foundation

/// YouTube Zeus 3.2: playbooks — a tutorial turned into steps for an AI agent (and the person working with it).
extension AppModel {
    /// `force`: even when the quick check says the video is not a tutorial.
    func makePlaybook(_ video: Video, force: Bool = false) {
        guard video.status.hasText else {
            show(PlaybookError.notEaten.localizedDescription, error: true)
            return
        }
        engine.schedulePlaybook(video.videoID, force: force)
        show("Making the playbook of “\(video.displayTitle)” on this Mac: frames, vision model, then the steps…")
    }

    func copyPlaybook(_ video: Video) {
        guard let playbook = video.playbook else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(PlaybookRender.markdown(playbook, video: video.snapshot, style: .clipboard), forType: .string)
        show("Playbook copied: paste it into Claude, ChatGPT, Mistral or any AI. It starts by showing you the plan.")
    }

    /// Opens Terminal in a work folder (PLAYBOOK.md + frames/) with Claude Code or Codex started on the playbook.
    func runPlaybook(_ video: Video, with agent: PlaybookRunner.Agent) {
        guard let playbook = video.playbook else { return }
        do {
            let (folder, script) = try PlaybookRunner.prepare(playbook, video: video.snapshot, root: settings.playbookWorkURL, agent: agent)
            NSWorkspace.shared.open(script)
            AppLog.write("PLAYBOOK run \(video.videoID) with \(agent.rawValue) in \(folder.path)")
            show("\(agent.label) starts in Terminal, in “\(folder.lastPathComponent)”. It shows you the plan first.")
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    func installPlaybookSkill(_ video: Video) {
        guard let playbook = video.playbook else { return }
        do {
            let folder = try PlaybookRunner.installSkill(playbook, video: video.snapshot)
            show("Installed for Claude Code as the skill “\(folder.lastPathComponent)”.")
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    func playbookNoteURL(_ video: Video) -> URL {
        settings.playbookNotesFolder.appendingPathComponent(PlaybookFiles.noteName(video.snapshot) + ".md")
    }

    func openPlaybookNote(_ video: Video) {
        let file = playbookNoteURL(video)
        if !BrainLinks.open(file) {
            show("The playbook note is not in the Second Brain yet (is the folder reachable?).", error: true)
        }
    }

    func revealPlaybookFolder() {
        let folder = settings.playbookWorkURL
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }

    /// Installs Claude Code or Codex with Homebrew (the setup page installs Homebrew when it is missing).
    func installAgent(_ agent: PlaybookRunner.Agent) async -> Bool {
        guard let brew = setup.brewPath else {
            show("Homebrew is needed first: the setup page installs it.", error: true)
            showSetup = true
            return false
        }
        show("Installing \(agent.label) with Homebrew…")
        do {
            try await SetupAssistant.runBrew(brew, agent.brewArguments) { _ in }
            AppLog.write("PLAYBOOK installed \(agent.rawValue)")
            show("\(agent.label) is installed.")
            return agent.path != nil
        } catch {
            show(error.localizedDescription, error: true)
            return false
        }
    }
}
