import AppKit
import SwiftUI

/// The Playbook tab: the tutorial as steps for an AI agent, shown as an infographic (3.2).
struct PlaybookBody: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        let making = app.engine.isMakingPlaybook(video.videoID)
        let playbook = video.playbook
        VStack(alignment: .leading, spacing: 18) {
            if making { PlaybookProgressCard(video: video) }
            if let playbook {
                PlaybookOverviewCard(video: video, playbook: playbook, making: making)
                PlaybookAgentBar(video: video)
                if !playbook.tools.isEmpty || !playbook.accounts.isEmpty { PlaybookBeforeCard(playbook: playbook) }
                PlaybookSteps(video: video, playbook: playbook)
                if !playbook.verify.isEmpty || !playbook.troubleshooting.isEmpty { PlaybookChecksCard(playbook: playbook) }
            } else if !making {
                PlaybookIntroCard(video: video)
            }
        }
    }
}

private struct PlaybookIntroCard: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        // A talk or an opinion: the quick check stopped it, the button makes it anyway.
        let notTutorial = video.playbookError.map(PlaybookError.isNotTutorial) ?? false
        VStack(alignment: .leading, spacing: 12) {
            Label("Turn this video into steps for an AI agent", systemImage: "list.number").font(.title3.bold())
            Text("Zeus looks at the moments where the screen changes, reads what is shown and said, and writes a playbook: what to install, every command, link and click, who does each step (the agent or you), what to expect after it, and which steps the agent asks you about first. Claude Code, Codex, ChatGPT or Mistral can then follow it from zero to a working result.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let error = video.playbookError {
                Text(error).font(.callout).foregroundStyle(notTutorial ? Color.orange : Color.red).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 10) {
                Button { app.makePlaybook(video, force: notTutorial) } label: {
                    Label(notTutorial ? "Make it anyway" : "Make the playbook", systemImage: "wand.and.sparkles")
                }
                    .buttonStyle(.glassProminent)
                    .disabled(!app.polisher.isInstalled)
                if !app.polisher.isInstalled {
                    Button("Set up the local AI…") { app.showSetup = true }.buttonStyle(.glass)
                }
            }
            Text("Free and private: the local AI and a vision model run on this Mac (the vision model downloads the first time). For YouTube videos the picture is downloaded without sound, then deleted.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

private struct PlaybookProgressCard: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        let state = app.engine.state(for: video.videoID)
        let waiting = app.engine.playbookCurrent != video.videoID
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                ProgressView().controlSize(.small)
                Text(waiting ? "Waiting for the local AI (listening, polishing and summaries go first)…" : (state?.step ?? "Making the playbook…"))
                    .font(.callout)
                Spacer()
                Button("Stop") { app.engine.cancelPlaybooks() }.buttonStyle(.glass)
            }
            if !waiting, let progress = state?.progress { ProgressView(value: progress) }
            Text("Three passes on this Mac: the summary, the frames where the screen changes (described by a vision model), then the steps for an agent. A long tutorial takes a few minutes.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

private struct PlaybookOverviewCard: View {
    let video: Video
    let playbook: Playbook
    let making: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Label("Goal", systemImage: "flag.checkered").font(.headline).foregroundStyle(.tint)
                Spacer()
                Text("Made \(playbook.madeAt.formatted(date: .abbreviated, time: .shortened))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(playbook.goal)
                .font(.title3)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if !playbook.result.isEmpty {
                Label(playbook.result, systemImage: "checkmark.seal")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                PlaybookStat(value: playbook.steps.count, label: "steps", symbol: "list.number", color: .accentColor)
                PlaybookStat(value: playbook.agentSteps, label: "by the agent", symbol: StepActor.agent.symbol, color: .blue)
                PlaybookStat(value: playbook.yourSteps, label: "by you", symbol: StepActor.you.symbol, color: .orange)
                PlaybookStat(value: playbook.downloadSteps, label: "downloads", symbol: "arrow.down.circle", color: .teal)
                PlaybookStat(value: playbook.askFirstSteps, label: "asked first", symbol: "exclamationmark.triangle", color: .red)
            }
            Text(PlaybookRender.madeBy(playbook)).font(.caption).foregroundStyle(.tertiary)
            if let error = video.playbookError, !making {
                Text("Last try: \(error)").font(.caption).foregroundStyle(.red)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

private struct PlaybookStat: View {
    let value: Int
    let label: String
    let symbol: String
    let color: Color

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: symbol)
            Text("\(value)").fontWeight(.semibold).monospacedDigit()
            Text(label).foregroundStyle(.secondary)
        }
        .font(.caption)
        .foregroundStyle(color)
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(color.opacity(0.12), in: .capsule)
    }
}

/// Run with Claude Code (or Codex), copy for any AI, the skill, the note.
private struct PlaybookAgentBar: View {
    @Environment(AppModel.self) private var app
    let video: Video
    @State private var installing: PlaybookRunner.Agent?
    @State private var refresh = 0

    var body: some View {
        let claude = PlaybookRunner.Agent.claude.path != nil
        let codex = PlaybookRunner.Agent.codex.path != nil
        VStack(alignment: .leading, spacing: 10) {
            Text("Hand it to an agent").font(.headline)
            HStack(spacing: 10) {
                if claude {
                    Button { app.runPlaybook(video, with: .claude) } label: {
                        Label("Run with Claude Code", systemImage: "play.fill")
                    }
                    .buttonStyle(.glassProminent)
                } else {
                    Button { install(.claude) } label: {
                        Label(installing == .claude ? "Installing Claude Code…" : "Install Claude Code", systemImage: "arrow.down.circle")
                    }
                    .buttonStyle(.glassProminent)
                    .disabled(installing != nil)
                }
                if codex {
                    Button { app.runPlaybook(video, with: .codex) } label: { Label("Run with Codex", systemImage: "play") }
                        .buttonStyle(.glass)
                }
                Button { app.copyPlaybook(video) } label: { Label("Copy for any AI", systemImage: "doc.on.doc") }
                    .buttonStyle(.glass)
                    .help("For ChatGPT, Mistral, Claude.ai, Gemini…")
                Menu {
                    Button("Install as a Claude Code skill") { app.installPlaybookSkill(video) }
                    Button(BrainLinks.openLabel) { app.openPlaybookNote(video) }
                    Button("Show the agents' folder") { app.revealPlaybookFolder() }
                    Divider()
                    Button("Make it again") { app.makePlaybook(video) }
                } label: {
                    Label("More", systemImage: "ellipsis.circle")
                }
                .menuStyle(.button)
                .buttonStyle(.glass)
                .fixedSize()
            }
            Text("The agent works in \(app.settings.playbookFolder). It first shows you the plan — what it will do, download and install, and what only you can do — then does the rest and stops only at your steps.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .id(refresh)
    }

    private func install(_ agent: PlaybookRunner.Agent) {
        installing = agent
        Task {
            _ = await app.installAgent(agent)
            installing = nil
            refresh += 1
        }
    }
}

private struct PlaybookBeforeCard: View {
    let playbook: Playbook

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Before you start").font(.headline)
            if !playbook.tools.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(playbook.tools, id: \.name) { tool in
                        let installed = tool.isInstalled
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Image(systemName: installed ? "checkmark.circle.fill" : "arrow.down.circle")
                                .foregroundStyle(installed ? Color.green : Color.teal)
                            VStack(alignment: .leading, spacing: 2) {
                                HStack(spacing: 6) {
                                    Text(tool.name).font(.body.weight(.semibold))
                                    if installed { Text("on this Mac").font(.caption).foregroundStyle(.green) }
                                }
                                if !tool.why.isEmpty {
                                    Text(tool.why).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            Spacer()
                            if !installed, let install = tool.install {
                                Text(install).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled)
                            }
                        }
                    }
                }
            }
            if !playbook.accounts.isEmpty {
                Divider()
                Text("Only you").font(.subheadline.weight(.semibold)).foregroundStyle(.orange)
                ForEach(playbook.accounts, id: \.self) { account in
                    Label(account, systemImage: StepActor.you.symbol)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

/// The steps as a timeline: number, who, frame of the video, what to do, command, what to expect.
private struct PlaybookSteps: View {
    let video: Video
    let playbook: Playbook

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Steps").font(.headline).padding(.bottom, 12)
            ForEach(playbook.steps) { step in
                // A new stage of the job starts (3.2.1).
                if let phase = step.phase, phase != playbook.steps.first(where: { $0.number == step.number - 1 })?.phase {
                    Label(phase, systemImage: "flag.fill")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .padding(.bottom, 10)
                }
                PlaybookStepRow(video: video, step: step, last: step.number == playbook.steps.count)
            }
        }
    }
}

private struct PlaybookStepRow: View {
    @Environment(AppModel.self) private var app
    let video: Video
    let step: PlaybookStep
    let last: Bool

    private var color: Color {
        if step.risk == .danger { return .red }
        return step.who == .you ? .orange : .blue
    }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            VStack(spacing: 0) {
                Text("\(step.number)")
                    .font(.callout.weight(.bold))
                    .monospacedDigit()
                    .foregroundStyle(.white)
                    .frame(width: 32, height: 32)
                    .background(color.gradient, in: .circle)
                if !last {
                    Rectangle().fill(color.opacity(0.25)).frame(width: 2).frame(maxHeight: .infinity)
                }
            }
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(step.title).font(.headline).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    if let seconds = step.seconds { MomentButton(video: video, seconds: seconds) }
                }
                HStack(spacing: 6) {
                    PlaybookBadge(text: step.who.label, symbol: step.who.symbol, color: step.who == .you ? .orange : .blue)
                    if step.prompt != nil { PlaybookBadge(text: "Prompt", symbol: "text.bubble.fill", color: .purple) }
                    if step.downloads != nil { PlaybookBadge(text: "Downloads", symbol: "arrow.down.circle", color: .teal) }
                    if step.risk == .danger {
                        PlaybookBadge(text: "Asks you first", symbol: "exclamationmark.triangle.fill", color: .red)
                    } else if step.risk == .caution {
                        PlaybookBadge(text: "Careful", symbol: "exclamationmark.circle", color: .orange)
                    }
                }
                HStack(alignment: .top, spacing: 14) {
                    if let frame = step.frame {
                        PlaybookFrame(url: PlaybookFiles.frameURL(video.videoID, frame))
                            .frame(width: 240)
                            .clipShape(.rect(cornerRadius: 10))
                            .onTapGesture { app.playMoment(video, at: frame) }
                            .help("Play the video from \(frame.timestamp)")
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        if let said = step.said {
                            Label("“\(said)”", systemImage: "quote.opening")
                                .font(.callout.italic())
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                        }
                        if !step.detail.isEmpty {
                            Text((try? AttributedString(markdown: step.detail, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(step.detail)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        }
                        if let prompt = step.prompt {
                            Text("Prompt — the agent does this task itself").font(.caption.weight(.semibold)).foregroundStyle(.purple)
                            PlaybookCode(text: prompt, isCommand: false)
                        }
                        if let url = step.url, let link = URL(string: url), link.scheme?.hasPrefix("http") == true {
                            Link(destination: link) {
                                Label(url, systemImage: "safari").lineLimit(1).truncationMode(.middle)
                            }
                            .font(.callout)
                        }
                        if let command = step.command { PlaybookCode(text: command, isCommand: true) }
                        if let file = step.file { Label(file, systemImage: "doc").font(.callout.monospaced()) }
                        if let code = step.code { PlaybookCode(text: code, isCommand: false) }
                        if let downloads = step.downloads {
                            Label(downloads, systemImage: "arrow.down.circle").font(.callout).foregroundStyle(.teal)
                        }
                        if let expected = step.expected {
                            Label(expected, systemImage: "checkmark.circle")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if step.risk != .safe, let reason = step.riskReason {
                            Label(reason, systemImage: "exclamationmark.triangle")
                                .font(.callout)
                                .foregroundStyle(step.risk == .danger ? Color.red : Color.orange)
                        }
                        ForEach(step.notes, id: \.self) { note in
                            Text(note).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(16)
            .glassEffect(.regular, in: .rect(cornerRadius: 18))
            .padding(.bottom, 14)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

private struct PlaybookBadge: View {
    let text: String
    let symbol: String
    let color: Color

    var body: some View {
        Label(text, systemImage: symbol)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: .capsule)
    }
}

private struct PlaybookFrame: View {
    let url: URL
    @State private var image: NSImage?

    var body: some View {
        ZStack {
            if let image {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fit)
            } else {
                Rectangle().fill(.quaternary).aspectRatio(16 / 9, contentMode: .fit)
            }
        }
        .task(id: url) { image = NSImage(contentsOf: url) }
    }
}

private struct PlaybookCode: View {
    @Environment(AppModel.self) private var app
    let text: String
    let isCommand: Bool

    var body: some View {
        HStack(alignment: .top) {
            Text((isCommand ? "$ " : "") + text)
                .font(.system(size: 12, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
                app.show("Copied. Check it before running it: it was read on the video's frames.")
            } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.borderless)
            .help("Copy")
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 10))
    }
}

private struct PlaybookChecksCard: View {
    let playbook: Playbook

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !playbook.verify.isEmpty {
                Text("Check that it works").font(.headline)
                ForEach(playbook.verify, id: \.self) { item in
                    Label(item, systemImage: "checkmark.circle").font(.callout).fixedSize(horizontal: false, vertical: true)
                }
            }
            if !playbook.troubleshooting.isEmpty {
                Text("If something goes wrong").font(.headline).padding(.top, 6)
                ForEach(playbook.troubleshooting, id: \.self) { item in
                    Label(item, systemImage: "wrench.adjustable").font(.callout).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}
