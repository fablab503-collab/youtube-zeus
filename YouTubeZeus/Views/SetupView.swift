import AppKit
import SwiftUI

/// The setup page: everything YouTube Zeus uses, checked on this Mac and installed in one go.
struct SetupView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var showDetails = false

    var body: some View {
        let setup = app.setup
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    header
                    if let hardware = setup.hardware { MacCard(hardware: hardware) }
                    SetupSection(title: "Installer", note: "Homebrew installs and updates the free tools Zeus uses.",
                                 items: [.commandLineTools, .homebrew])
                    SetupSection(title: "Reading YouTube", note: "Required to eat videos, playlists and channels.",
                                 items: [.youtube])
                    SetupSection(title: "Listening", note: "For videos and podcasts without captions, and your own recordings.",
                                 items: [.ffmpeg, .whisper, .speech])
                    SetupSection(title: "Local AI", note: "Free and private: nothing leaves this Mac.", items: [.ollama])
                    ModelPicker()
                    VisionCard()
                    AppleIntelligenceCard()
                    NotesFolderCard()
                    if showDetails { SetupLog(lines: setup.log) }
                }
                .padding(28)
            }
            footer
        }
        .frame(width: 780, height: 780)
        .task { await setup.refresh() }
    }

    private var header: some View {
        let setup = app.setup
        return VStack(alignment: .leading, spacing: 6) {
            Text("Set up YouTube Zeus").font(.largeTitle.bold())
            Text(setup.everythingReady
                 ? "Everything Zeus uses is ready on this Mac."
                 : "Zeus checks what this Mac already has and installs the rest. Homebrew's installer asks for your Mac password once; everything else is automatic.")
                .font(.title3)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var footer: some View {
        let setup = app.setup
        return HStack(spacing: 12) {
            Toggle("Show details", isOn: $showDetails).toggleStyle(.switch).controlSize(.small)
            Spacer()
            if setup.running {
                ProgressView().controlSize(.small)
                Button("Stop") { setup.stop() }.buttonStyle(.glass)
            }
            Button(setup.everythingReady ? "Done" : "Later") { dismiss() }
                .buttonStyle(.glass)
                .keyboardShortcut(setup.everythingReady ? KeyboardShortcut.defaultAction : KeyboardShortcut.cancelAction)
            if !setup.everythingReady {
                Button { setup.setUpEverything() } label: {
                    Label("Set up everything", systemImage: "wand.and.sparkles")
                }
                .buttonStyle(.glassProminent)
                .disabled(setup.running || setup.refreshing || !setup.checked)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 14)
        .background(.bar)
    }
}

private struct MacCard: View {
    @Environment(AppModel.self) private var app
    let hardware: MacHardware

    var body: some View {
        HStack(spacing: 16) {
            Image(systemName: hardware.name.localizedCaseInsensitiveContains("book") ? "laptopcomputer" : "desktopcomputer")
                .font(.title)
                .foregroundStyle(.tint)
                .frame(width: 52, height: 52)
                .background(Color.accentColor.opacity(0.12), in: .circle)
            VStack(alignment: .leading, spacing: 3) {
                Text(hardware.name).font(.headline)
                Text(hardware.summary).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if let best = app.setup.recommended {
                VStack(alignment: .trailing, spacing: 3) {
                    Text("Best local AI for this Mac").font(.caption).foregroundStyle(.secondary)
                    Text(best.name).font(.headline)
                }
            }
        }
        .padding(18)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

private struct SetupSection: View {
    let title: String
    let note: String
    let items: [SetupItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.title3.bold())
            Text(note).font(.callout).foregroundStyle(.secondary)
            VStack(spacing: 0) {
                ForEach(items) { item in
                    SetupRow(item: item)
                    if item != items.last { Divider().padding(.leading, 70) }
                }
            }
            .glassEffect(.regular, in: .rect(cornerRadius: 22))
        }
    }
}

private struct SetupRow: View {
    @Environment(AppModel.self) private var app
    let item: SetupItem

    var body: some View {
        let setup = app.setup
        let state = setup.state(item)
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: item.symbol)
                .font(.title3)
                .foregroundStyle(.tint)
                .frame(width: 40, height: 40)
                .background(Color.accentColor.opacity(0.12), in: .circle)
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text(item.title).font(.headline)
                    Spacer()
                    SetupBadge(state: state, required: item == .youtube)
                }
                Text(item.purpose).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                switch state {
                case .working(let text, let progress):
                    if let progress {
                        ProgressView(value: progress)
                    } else {
                        ProgressView().progressViewStyle(.linear)
                    }
                    Text(text).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                case .waiting(let text):
                    Label(text, systemImage: "hand.point.up.left.fill").font(.callout).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                case .failed(let message):
                    Text(message).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                case .ready(let detail):
                    Text(detail).font(.caption.monospaced()).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
                case .missing, .checking:
                    EmptyView()
                }
                actions(state)
            }
        }
        .padding(16)
    }

    @ViewBuilder
    private func actions(_ state: SetupState) -> some View {
        let setup = app.setup
        let canAct = !setup.running
        switch state {
        case .missing, .failed:
            HStack {
                Button(item == .speech ? "Download" : "Install") {
                    setup.start(item)
                }
                .buttonStyle(.glass)
                .disabled(!canAct || !prerequisitesMet)
                if item == .homebrew {
                    Button("Use Terminal instead") { setup.installHomebrewInTerminal() }
                        .buttonStyle(.link)
                        .disabled(!canAct)
                }
                if !prerequisitesMet {
                    Text("Comes after Homebrew").font(.caption).foregroundStyle(.secondary)
                }
            }
        case .waiting where item == .homebrew:
            Button("Use Terminal instead") { setup.installHomebrewInTerminal() }.buttonStyle(.link)
        default:
            EmptyView()
        }
    }

    /// Tools come from Homebrew; Ollama can also be there already as its own app.
    private var prerequisitesMet: Bool {
        switch item {
        case .youtube, .ffmpeg, .whisper: app.setup.brewPath != nil
        case .ollama: app.setup.brewPath != nil || SetupAssistant.ollamaInstalled
        default: true
        }
    }
}

private struct SetupBadge: View {
    let state: SetupState
    var required = false

    var body: some View {
        let (text, symbol, color) = look
        Label(text, systemImage: symbol)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: .capsule)
    }

    private var look: (String, String, Color) {
        switch state {
        case .checking: ("Checking…", "clock", .secondary)
        case .ready: ("Ready", "checkmark.circle.fill", .green)
        case .missing: (required ? "Required" : "Not installed", required ? "exclamationmark.circle.fill" : "circle.dashed",
                        required ? .red : .orange)
        case .working: ("Working…", "arrow.down.circle", .blue)
        case .waiting: ("Waiting for you", "hand.raised.fill", .orange)
        case .failed: ("Failed", "xmark.octagon.fill", .red)
        }
    }
}

/// The local AI models that suit this Mac, the strongest that runs comfortably preselected.
private struct ModelPicker: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let setup = app.setup
        let state = setup.state(.model)
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("Local AI model").font(.title3.bold())
                Spacer()
                SetupBadge(state: state)
            }
            Text(explanation).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                let options = setup.models.filter { $0.available != false }
                ForEach(options) { option in
                    row(option)
                    if option != options.last { Divider().padding(.leading, 46) }
                }
            }
            .glassEffect(.regular, in: .rect(cornerRadius: 22))
            footer(state)
        }
    }

    private var explanation: String {
        guard let hardware = app.setup.hardware else { return "Reading this Mac…" }
        var basis = "its \(hardware.memoryGB) GB of memory decide which models fit"
        if let cores = hardware.gpuCores { basis += ", its \(cores)-core GPU how fast they write" }
        return "Chosen for this Mac: \(basis) (\(hardware.freeDiskGB) GB free on the disk). Bigger models write better summaries and names, but are slower and use more memory. You can change it any time."
    }

    private func row(_ option: LocalModelOption) -> some View {
        let setup = app.setup
        let fit = setup.hardware.map { ModelCatalog.fit(option, on: $0) } ?? .comfortable
        let selected = setup.chosenModel == option.tag
        return Button {
            setup.chosenModel = option.tag
            Task { await setup.checkModel() }
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: selected ? "largecircle.fill.circle" : "circle")
                    .font(.title3)
                    .foregroundStyle(selected ? Color.accentColor : .secondary)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(option.name).font(.headline)
                        if setup.recommended?.key == option.key { chip("Recommended", .green) }
                        if app.settings.polishModel == option.tag { chip("In use", .blue) }
                        else if setup.isInstalled(option.tag) { chip("Downloaded", .secondary) }
                        if fit == .heavy {
                            let slow = setup.hardware.map { ModelCatalog.slow(option, on: $0) } ?? false
                            chip(slow ? "Slow on this Mac" : "Heavy for this Mac", .orange)
                        }
                        if fit == .tooBig { chip("Too big for this Mac", .red) }
                    }
                    Text(option.note).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                Text(option.sizeText).font(.callout.monospacedDigit()).foregroundStyle(.secondary)
            }
            .contentShape(.rect)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
        .buttonStyle(.plain)
        .disabled(fit == .tooBig || setup.running)
        .opacity(fit == .tooBig ? 0.45 : 1)
    }

    private func chip(_ text: String, _ color: Color) -> some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12), in: .capsule)
    }

    @ViewBuilder
    private func footer(_ state: SetupState) -> some View {
        let setup = app.setup
        let name = setup.displayName(setup.chosenModel)
        switch state {
        case .working(let text, let progress):
            VStack(alignment: .leading, spacing: 4) {
                if let progress { ProgressView(value: progress) } else { ProgressView().progressViewStyle(.linear) }
                HStack {
                    Text(text).font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    if let progress {
                        Text(progress.formatted(.percent.precision(.fractionLength(0)))).font(.caption.monospacedDigit())
                    }
                }
            }
        case .failed(let message):
            HStack {
                Text(message).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                Spacer()
                Button("Try again") { setup.start(.model) }.buttonStyle(.glass).disabled(setup.running)
            }
        default:
            HStack {
                Spacer()
                if setup.isInstalled(setup.chosenModel) {
                    if app.settings.polishModel != setup.chosenModel {
                        Button("Use \(name)") { setup.useChosenModel() }.buttonStyle(.glass)
                    }
                } else {
                    let size = setup.option(for: setup.chosenModel)?.sizeText
                    let title = size.map { "Download \(name) (\($0))" } ?? "Download \(name)"
                    Button(title) { setup.start(.model) }
                        .buttonStyle(.glass)
                        .disabled(setup.running || setup.refreshing || (setup.brewPath == nil && !SetupAssistant.ollamaInstalled))
                }
            }
        }
    }
}

/// The vision model of playbooks (3.2): optional, downloaded the first time a playbook is made, or here.
private struct VisionCard: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let setup = app.setup
        let state = setup.visionState
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: "eye")
                .font(.title3)
                .foregroundStyle(.tint)
                .frame(width: 40, height: 40)
                .background(Color.accentColor.opacity(0.12), in: .circle)
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Vision model (for playbooks)").font(.headline)
                    Spacer()
                    SetupBadge(state: state)
                }
                Text("Playbooks turn a tutorial into steps for an AI agent: a vision model looks at the frames where the screen changes, on this Mac.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                switch state {
                case .working(let text, let progress):
                    if let progress { ProgressView(value: progress) } else { ProgressView().progressViewStyle(.linear) }
                    Text(text).font(.caption).foregroundStyle(.secondary)
                case .failed(let message):
                    Text(message).font(.caption).foregroundStyle(.red).textSelection(.enabled)
                case .ready(let detail), .missing(let detail):
                    Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                case .waiting, .checking:
                    EmptyView()
                }
                if case .missing = state, setup.visionChoice != nil {
                    Button("Download now") { setup.downloadVision() }
                        .buttonStyle(.glass)
                        .disabled(setup.running || !SetupAssistant.ollamaInstalled)
                }
            }
        }
        .padding(16)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

private struct AppleIntelligenceCard: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let ready = app.summarizer.isAvailable
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: "apple.intelligence")
                .font(.title3)
                .foregroundStyle(.tint)
                .frame(width: 40, height: 40)
                .background(Color.accentColor.opacity(0.12), in: .circle)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Apple Intelligence").font(.headline)
                    Spacer()
                    SetupBadge(state: ready ? .ready("") : .missing(""))
                }
                Text(app.summarizer.availabilityMessage + " When it is ready, Zeus writes summaries with it and the local AI does the rest.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(16)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

private struct NotesFolderCard: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let reachable = app.exporter.folderReachable
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: "brain.head.profile")
                .font(.title3)
                .foregroundStyle(.tint)
                .frame(width: 40, height: 40)
                .background(Color.accentColor.opacity(0.12), in: .circle)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Where notes go").font(.headline)
                    Spacer()
                    SetupBadge(state: reachable ? .ready("") : .missing(""))
                }
                Text("Every video becomes a Markdown note in your Second Brain (an Obsidian vault or any folder).")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    Text(app.settings.secondBrainFolder).font(.caption.monospaced()).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Choose…") { choose() }.buttonStyle(.glass)
                }
            }
        }
        .padding(16)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Use this folder"
        panel.message = "Choose the folder for YouTube notes (for example SecondBrain/Sources/YouTube)."
        if panel.runModal() == .OK, let url = panel.url { app.settings.secondBrainFolder = url.path }
    }
}

private struct SetupLog: View {
    let lines: [String]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                        Text(line).font(.caption.monospaced()).textSelection(.enabled).id(index)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
            }
            .frame(height: 200)
            .glassEffect(.regular, in: .rect(cornerRadius: 16))
            .onChange(of: lines.count) { _, count in
                if count > 0 { proxy.scrollTo(count - 1, anchor: .bottom) }
            }
        }
    }
}
