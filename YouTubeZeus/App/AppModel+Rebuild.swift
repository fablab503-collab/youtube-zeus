import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

/// Rebuild (4.2): the final message to give any AI to reverse-engineer and redo what an item shows, plus
/// evidence from REA (Reverse Engineer Anything, https://github.com/morluto/rea) when the video uses existing software.
extension AppModel {
    func rebuildBrief(_ video: Video, for reader: RebuildBrief.Reader = .claude) -> String {
        RebuildBrief.make(video.snapshot, playbook: video.playbook, evidence: video.rebuildEvidence, for: reader)
    }

    func copyRebuild(_ video: Video, for reader: RebuildBrief.Reader) {
        let text = rebuildBrief(video, for: reader)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        let words = text.split(whereSeparator: \.isWhitespace).count
        show("Rebuild message for \(reader.rawValue) copied (\(words.formatted()) words): paste it as your first message.")
    }

    /// Writes the message to `Sources/YouTube/Rebuild/` and opens it.
    @discardableResult
    func saveRebuild(_ video: Video, open: Bool = true) -> URL? {
        let folder = settings.secondBrainURL.appendingPathComponent("Rebuild", isDirectory: true)
        let day = SecondBrainExporter.dayFormatter.string(from: .now)
        let file = folder.appendingPathComponent("Rebuild - \(day) - \(SecondBrainExporter.sanitize(video.displayTitle, limit: 90)).md")
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let text = RebuildBrief.note(video.snapshot, brief: rebuildBrief(video), noteName: video.noteName)
            try text.write(to: file, atomically: true, encoding: .utf8)
            AppLog.write("REBUILD \(video.videoID) → \(file.path)")
            if open { openInBrain(file) }
            return file
        } catch {
            show(error.localizedDescription, error: true)
            return nil
        }
    }

    /// Lets you pick an app, a folder, an `.asar`, a binary or a HAR capture, and runs REA on it (free, local).
    func addREAEvidence(_ video: Video) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.treatsFilePackagesAsDirectories = false
        panel.message = "Choose what the video uses or imitates — an app, a folder, an .asar, a binary — for REA to read on this Mac."
        guard panel.runModal() == .OK, let target = panel.url else { return }
        Task { await runREA(on: target, for: video) }
    }

    func runREA(on target: URL, for video: Video) async {
        guard let npx = ToolLocator.find("npx", override: "") else {
            show("REA needs Node.js (npx). Install it once with: brew install node", error: true)
            return
        }
        let id = video.videoID
        reaRunning.insert(id)
        defer { reaRunning.remove(id) }
        show("REA is reading \(target.lastPathComponent) on this Mac… (the first time it downloads itself, a minute)")
        AppLog.write("REA \(id): analyze \(target.path)")
        do {
            let output = try await ProcessRunner.run(npx, ["-y", "rea-agents@latest", "analyze", target.path, "--json"])
            let text = output.stdoutString.trimmingCharacters(in: .whitespacesAndNewlines)
            guard output.status == 0 || !text.isEmpty else {
                let reason = String(output.stderr.suffix(400))
                show("REA could not read \(target.lastPathComponent): \(reason.isEmpty ? "no result" : reason)", error: true)
                return
            }
            guard video.isAlive else { return }
            let block = "#### REA · \(target.lastPathComponent) · \(SecondBrainExporter.dayFormatter.string(from: .now))\n```json\n\(String(text.prefix(80_000)))\n```"
            video.rebuildEvidence = [video.rebuildEvidence, block].compactMap { $0 }.joined(separator: "\n\n")
            try? context.save()
            AppLog.write("REA \(id): \(text.count) characters of evidence")
            show("REA evidence added to the rebuild message (\(target.lastPathComponent)).")
        } catch {
            show("REA failed: \(error.localizedDescription)", error: true)
        }
    }

    func clearREAEvidence(_ video: Video) {
        video.rebuildEvidence = nil
        try? context.save()
    }

    /// Copies the command that gives REA to Claude Code, Codex and other agents (run it once in Terminal).
    func copyREASetup() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString("npx rea-agents setup", forType: .string)
        show("Copied “npx rea-agents setup”: paste it in Terminal once to give REA to your agents.")
    }
}

/// The Rebuild tab: the final message for any AI, and REA.
struct RebuildBody: View {
    @Environment(AppModel.self) private var app
    let video: Video
    @State private var reader: RebuildBrief.Reader = .claude

    var body: some View {
        let missing = app.missingParts(video)
        let brief = app.rebuildBrief(video, for: reader)
        VStack(alignment: .leading, spacing: 14) {
            Text("One message to give to Claude, ChatGPT, Mistral, DeepSeek or any AI so it can reverse-engineer what this video shows and redo it: the summary, the key moments, the playbook steps, the code and commands on screen, the links and the transcript, with the rules to follow and what to send back.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !missing.isEmpty {
                Label("Better with everything made first (still missing: \(AppModel.list(missing))).", systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            GlassEffectContainer(spacing: 8) {
                FlowLayout(spacing: 8) {
                    Menu {
                        ForEach(RebuildBrief.Reader.allCases) { option in
                            Button("Copy for \(option.menuTitle)") { reader = option; app.copyRebuild(video, for: option) }
                        }
                    } label: {
                        Label("Copy the message", systemImage: "doc.on.doc.fill")
                    } primaryAction: {
                        app.copyRebuild(video, for: reader)
                    }
                    .menuStyle(.button)
                    .buttonStyle(.glassProminent)
                    .fixedSize()
                    .help("Click: copy for \(reader.rawValue). Arrow: choose the AI (smaller windows get the moments that matter)")
                    Button { app.saveRebuild(video) } label: { Label("Save to Second Brain", systemImage: "brain.head.profile") }
                        .buttonStyle(.glass)
                    Button { app.addREAEvidence(video) } label: {
                        Label(app.reaRunning.contains(video.videoID) ? "REA is reading…" : "Add evidence with REA…", systemImage: "cpu")
                    }
                    .buttonStyle(.glass)
                    .disabled(app.reaRunning.contains(video.videoID))
                    .help("When the video uses an existing app, site or binary: REA (Reverse Engineer Anything, free, local) reads it and its evidence goes into the message")
                    Menu {
                        Button("Copy “npx rea-agents setup” (give REA to my agents)") { app.copyREASetup() }
                        Button("Open REA on GitHub") { NSWorkspace.shared.open(URL(string: RebuildBrief.reaURL)!) }
                        if video.rebuildEvidence != nil {
                            Divider()
                            Button("Remove the REA evidence", role: .destructive) { app.clearREAEvidence(video) }
                        }
                    } label: {
                        Label("REA", systemImage: "ellipsis.circle")
                    }
                    .menuStyle(.button)
                    .buttonStyle(.glass)
                    .fixedSize()
                }
            }
            HStack(spacing: 10) {
                Picker("For", selection: $reader) {
                    ForEach(RebuildBrief.Reader.allCases) { Text($0.menuTitle).tag($0) }
                }
                .pickerStyle(.menu)
                .fixedSize()
                Text("\(brief.split(whereSeparator: \.isWhitespace).count.formatted()) words")
                    .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                if video.rebuildEvidence != nil {
                    Chip(text: "REA evidence", symbol: "cpu", tint: .indigo)
                }
            }
            ScrollView {
                Text(brief)
                    .font(.system(.callout, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
            }
            .frame(minHeight: 260, maxHeight: 520)
            .background(.quaternary.opacity(0.4), in: .rect(cornerRadius: 12))
        }
    }
}
