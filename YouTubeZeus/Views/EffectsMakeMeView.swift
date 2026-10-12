import AVKit
import SwiftUI
import UniformTypeIdentifiers

// Make it me (3.8, effects/MAKE-IT-ME.md): one button. Clip · Me (self-video, face pack or photos) · Voice · options ·
// consent → prepare, references, approve (automatic), face swap, hat, voice, final — all on this Mac. A job from The
// Swap (iPhone) is the same job, started by the watcher, and shows here like any other.

/// The form for a new effect: Make it me, or the step-by-step flow (Prepare → Review → Generate).
struct EffectsNewEffectView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        @Bindable var effects = app.effects
        Picker("Flow", selection: $effects.flow) {
            ForEach(EffectsEngine.Flow.allCases) { flow in Text(flow.label).tag(flow) }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .fixedSize()
        if effects.flow == .makeMe {
            EffectsMakeMeCard()
        } else {
            EffectsSourceCard()
        }
    }
}

// MARK: - The card

struct EffectsMakeMeCard: View {
    @Environment(AppModel.self) private var app
    @State private var picking = false

    var body: some View {
        @Bindable var effects = app.effects
        let who = effects.makeMeWho
        let voiceFileMissing = effects.voiceSource == .file && effects.voiceFile == nil
        let ready = effects.isSetUp && !effects.isBusy && app.effectsSourceInput != nil && who != nil
            && effects.makeMeConsent && effects.readingPack == nil && !voiceFileMissing
        VStack(alignment: .leading, spacing: 16) {
            Label("Make it me", systemImage: "person.crop.circle.badge.checkmark").font(.headline)
            Text("One button: the clip becomes you — your face (natural quality), the hat gone if you want, your voice saying the same words. Prepare, references, approve, face swap, hat, voice and final run one after the other on this Mac; nothing leaves it.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // 1 · Clip
            EffectsMakeMeSection(title: "Clip", symbol: "film")
            EffectsMakeMeSourceRow(picking: $picking)
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 10) {
                GridRow {
                    Text("Start").foregroundStyle(.secondary)
                    HStack(spacing: 6) {
                        TextField("0:00", text: $effects.startText)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 90)
                            .help("Seconds or minutes:seconds, e.g. 75 or 1:15")
                        Text("= \(effects.startSeconds.timestamp)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
                GridRow {
                    Text("Length").foregroundStyle(.secondary)
                    Stepper(value: $effects.length, in: 1...EffectsArguments.maxDuration, step: 1) {
                        Text("\(Int(effects.length)) s").monospacedDigit()
                    }
                    .fixedSize()
                }
            }

            // 2 · Me
            EffectsMakeMeSection(title: "Me", symbol: "person.crop.square")
            Picker("Me", selection: $effects.meSource) {
                ForEach(EffectsMeSource.allCases) { source in Text(source.label).tag(source) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            .onChange(of: effects.meSource) { _, source in
                if source != .selfVideo, effects.voiceSource == .selfVideo { effects.voiceSource = .keep }
                if source == .selfVideo, effects.voiceSource == .keep, effects.voiceFile == nil { effects.voiceSource = .selfVideo }
            }
            Text(effects.meSource.explanation).font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            switch effects.meSource {
            case .selfVideo:
                EffectsSelfVideoRow()
            case .facePack:
                EffectsFacePackRow()
            case .photos:
                HStack(alignment: .top, spacing: 14) {
                    EffectsPhotoSlot(title: "Photo", subtitle: "front, required", url: $effects.referencePhoto) {
                        app.chooseEffectsPhoto(second: false)
                    }
                    EffectsPhotoSlot(title: "Second photo", subtitle: "optional (a side)", url: $effects.secondPhoto) {
                        app.chooseEffectsPhoto(second: true)
                    }
                }
            }

            // 3 · Voice
            EffectsMakeMeSection(title: "Voice", symbol: "waveform")
            Picker("Voice", selection: $effects.voiceSource) {
                ForEach(EffectsVoiceSource.allCases) { source in
                    if source != .selfVideo || effects.meSource == .selfVideo {
                        Text(source.label).tag(source)
                    }
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            if effects.voiceSource == .file {
                HStack(spacing: 10) {
                    Button { app.chooseEffectsVoiceFile() } label: { Label("Choose a voice file…", systemImage: "waveform.badge.plus") }
                        .buttonStyle(.glass)
                    Text(effects.voiceFile?.lastPathComponent ?? "10–60 seconds of clear speech (audio or video)")
                        .font(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    if effects.voiceFile != nil {
                        Button { effects.voiceFile = nil } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.borderless).help("Remove")
                    }
                }
            }
            Text(voiceHint(effects)).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)

            // 4 · Options
            EffectsMakeMeSection(title: "Options", symbol: "slider.horizontal.3")
            HStack(spacing: 16) {
                Picker("Quality", selection: $effects.makeMeQuality) {
                    Text("Natural").tag("natural")
                    Text("Fast").tag("fast")
                }
                .pickerStyle(.segmented)
                .fixedSize()
                Toggle("Remove the hat or cap", isOn: $effects.hatRemove)
                Toggle("Parts map", isOn: $effects.makeMeParts)
                    .help("The prepare step also builds the parts map (effects/PARTS.md): parts.mp4, control-parts.mp4, preview-parts.jpg")
            }
            if effects.hatRemove {
                TextField("Your hair, for the new hair (optional): e.g. short dark brown hair", text: $effects.hairText)
                    .textFieldStyle(.roundedBorder)
                Text("A hat or cap found on the head is replaced with hair: BiSeNet hat mask, VACE and a full-resolution composite — about an hour for 6 seconds on this Mac. Skipped with a note when no hat is found.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if effects.makeMeParts {
                Text("Prepare also builds the parts map: one tone per body part, the face in detail, hair or hat, the background by depth (parts.mp4, parts.npz, parts.json, control-parts.mp4 and preview-parts.jpg in the job folder) — about half a minute more. The face swap, hat and voice do not use it; it is there for models that replace regions.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if effects.meSource == .selfVideo {
                TextField("Your name (written into the face pack made from the self-video, optional)", text: $effects.personName)
                    .textFieldStyle(.roundedBorder)
            }

            Toggle(isOn: $effects.makeMeConsent) {
                Text("The clip is mine to use, and the face and the voice are mine or of someone who agreed").fontWeight(.medium)
            }
            .toggleStyle(.checkbox)

            HStack(spacing: 12) {
                Button { app.makeMeEffect() } label: { Label("Make it me", systemImage: "wand.and.stars") }
                    .buttonStyle(.glassProminent)
                    .controlSize(.large)
                    .disabled(!ready)
                Text(hint(effects, who: who, voiceFileMissing: voiceFileMissing))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }

    private func voiceHint(_ effects: EffectsEngine) -> String {
        switch effects.voiceSource {
        case .selfVideo: "Your speech in the self-video is cleaned (Demucs, silences trimmed, level matched) and used as the voice sample. Talk for 20 seconds in it."
        case .file: "Chatterbox voice conversion: the same words and timing, in this voice. Level and EQ are matched to the original."
        case .keep: "The result keeps the clip's original voice."
        }
    }

    private func hint(_ effects: EffectsEngine, who: EffectsMe?, voiceFileMissing: Bool) -> String {
        if !effects.isSetUp { return "Set up the engine first (above)." }
        if app.effectsSourceInput == nil { return "Pick a video of the library, or a video file." }
        if effects.readingPack != nil { return "Reading the face pack…" }
        if who == nil {
            switch effects.meSource {
            case .selfVideo: return "Choose your self-video."
            case .facePack: return "Choose a face pack (.c101face)."
            case .photos: return "Choose a photo of the face."
            }
        }
        if voiceFileMissing { return "Choose the voice file, or keep the original voice." }
        if !effects.makeMeConsent { return "Tick the box: your clip, your face, your voice (or of people who agreed)." }
        var parts = ["Face swap about 10 s per second of video"]
        if effects.makeMeParts { parts.append("the parts map about half a minute") }
        if effects.hatRemove { parts.append("the hat about an hour") }
        if effects.voiceSource != .keep { parts.append("the voice about a minute") }
        return parts.joined(separator: "; ") + ". The result is marked as AI-generated."
    }
}

private struct EffectsMakeMeSection: View {
    let title: String
    let symbol: String

    var body: some View {
        Label(title, systemImage: symbol).font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            .padding(.top, 4)
    }
}

/// The clip: a video of the library (its YouTube link or file) or a video file, with a drop zone.
private struct EffectsMakeMeSourceRow: View {
    @Environment(AppModel.self) private var app
    @Binding var picking: Bool
    @State private var targeted = false

    var body: some View {
        let effects = app.effects
        HStack(alignment: .center, spacing: 14) {
            preview
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 8) {
                Button { picking = true } label: { Label("From the library…", systemImage: "play.rectangle.on.rectangle") }
                    .buttonStyle(.glass)
                    .popover(isPresented: $picking, arrowEdge: .bottom) {
                        EffectsVideoPicker { video in
                            effects.sourceVideoID = video.videoID
                            effects.sourceFile = nil
                            picking = false
                        }
                    }
                Button { app.chooseEffectsFile() } label: { Label("Choose a file…", systemImage: "doc.badge.plus") }
                    .buttonStyle(.glass)
            }
        }
        .padding(12)
        .background(targeted ? Color.zeus.opacity(0.12) : Color.clear, in: .rect(cornerRadius: 16))
        .contentShape(.rect)
        .dropDestination(for: URL.self) { urls, _ in
            guard let file = urls.first(where: { $0.isFileURL && MediaFiles.isVideo($0) }) else { return false }
            effects.sourceFile = file
            effects.sourceVideoID = nil
            return true
        } isTargeted: { targeted = $0 }
    }

    @ViewBuilder private var preview: some View {
        let effects = app.effects
        if let file = effects.sourceFile {
            HStack(spacing: 10) {
                Image(systemName: "film").font(.title).foregroundStyle(.tint).frame(width: 104, height: 58)
                    .background(.quaternary, in: .rect(cornerRadius: 9))
                VStack(alignment: .leading, spacing: 3) {
                    Text(file.lastPathComponent).font(.headline).lineLimit(2)
                    Text("Your file").font(.caption).foregroundStyle(.secondary)
                }
            }
        } else if let id = effects.sourceVideoID {
            let video = app.engine.video(id)
            HStack(spacing: 10) {
                Thumbnail(url: video?.thumbnailURL ?? (MediaKind.of(id: id) == .youtube ? URL(string: "https://i.ytimg.com/vi/\(id)/hqdefault.jpg") : nil),
                          width: 104, radius: 9)
                VStack(alignment: .leading, spacing: 3) {
                    Text(video?.displayTitle ?? "YouTube video \(id)").font(.headline).lineLimit(2)
                    Text(video.map { $0.channelTitle.isEmpty ? $0.kind.label : $0.channelTitle } ?? "Not in the library").font(.caption).foregroundStyle(.secondary)
                    if app.effectsInput(for: id) == nil {
                        Text("This item has no picture to work from.").font(.caption).foregroundStyle(.orange)
                    }
                }
            }
        } else {
            HStack(spacing: 10) {
                Image(systemName: "arrow.down.doc").font(.title2).foregroundStyle(.secondary).frame(width: 104, height: 58)
                    .overlay { RoundedRectangle(cornerRadius: 9).strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [5])).foregroundStyle(.tertiary) }
                Text("No clip yet: pick a video of the library, or drop a video file here (up to 10 seconds are used).")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// "Film yourself": the self-video file, with a drop zone.
private struct EffectsSelfVideoRow: View {
    @Environment(AppModel.self) private var app
    @State private var targeted = false

    var body: some View {
        let effects = app.effects
        HStack(spacing: 12) {
            Image(systemName: effects.selfVideo == nil ? "video.badge.plus" : "video.fill")
                .font(.title2)
                .foregroundStyle(effects.selfVideo == nil ? Color.secondary : Color.zeus)
                .frame(width: 44, height: 44)
                .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 10))
            VStack(alignment: .leading, spacing: 2) {
                if let video = effects.selfVideo {
                    Text(video.lastPathComponent).font(.callout.weight(.semibold)).lineLimit(1).truncationMode(.middle)
                        .help(video.path)
                    Text("Frames per angle (eyes open, sharpest) and the voice are taken from it; the pack goes into the job as me.c101face.")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                } else {
                    Text("No self-video yet").font(.callout.weight(.semibold))
                    Text("Drop it here, or choose it: 30–60 s from the iPhone or the Mac's camera.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            Button { app.chooseEffectsSelfVideo() } label: { Label("Choose…", systemImage: "folder") }
                .buttonStyle(.glass)
            if effects.selfVideo != nil {
                Button { effects.selfVideo = nil } label: { Image(systemName: "xmark.circle.fill") }
                    .buttonStyle(.borderless)
                    .help("Remove")
            }
        }
        .padding(10)
        .background(targeted ? Color.zeus.opacity(0.12) : Color.clear, in: .rect(cornerRadius: 14))
        .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(targeted ? Color.zeus : Color.clear, lineWidth: 2) }
        .contentShape(.rect)
        .dropDestination(for: URL.self) { urls, _ in
            guard let file = urls.first(where: { $0.isFileURL && MediaFiles.isVideo($0) }) else { return false }
            effects.selfVideo = file
            effects.meSource = .selfVideo
            return true
        } isTargeted: { targeted = $0 }
    }
}

// MARK: - Progress: the steps

/// prepare › refs › approve › faceswap › hat › voice › final, with the steps done, the current one and the rest.
struct EffectsStepsRow: View {
    let steps: [String]
    let current: String

    var body: some View {
        let index = steps.firstIndex(of: current) ?? (current == "plan" ? -1 : steps.count)
        HStack(spacing: 6) {
            ForEach(Array(steps.enumerated()), id: \.offset) { position, step in
                let state: Int = position < index ? 2 : (position == index ? 1 : 0)
                let tint: Color = state == 2 ? .green : (state == 1 ? .zeus : .secondary)
                HStack(spacing: 4) {
                    Image(systemName: state == 2 ? "checkmark.circle.fill" : (state == 1 ? "circle.dotted.circle" : "circle"))
                    Text(EffectsMakeMe.label(step))
                }
                .font(.caption.weight(.medium))
                .foregroundStyle(tint)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(tint.opacity(0.12), in: Capsule())
                .help(EffectsMakeMe.explanation(step))
                if position < steps.count - 1 {
                    Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
    }
}

// MARK: - A Make it me job

/// The selected job when it is a Make it me job: its steps, the result (with the voice when it ran), the side by side.
struct EffectsMakeMeJobCard: View {
    @Environment(AppModel.self) private var app
    let job: EffectsJob

    var body: some View {
        let effects = app.effects
        if let makeMe = job.makeMe {
            let runningHere = effects.isBusy && effects.selectedJobID == job.id
            VStack(alignment: .leading, spacing: 14) {
                HStack(spacing: 8) {
                    Label("Make it me", systemImage: "person.crop.circle.badge.checkmark").font(.headline)
                    Spacer()
                    Chip(text: makeMe.statusLabel, symbol: statusSymbol(makeMe), tint: statusTint(makeMe))
                    if makeMe.fromTheSwap { Chip(text: "From The Swap", symbol: "iphone", tint: .zeus) }
                }
                if !makeMe.steps.isEmpty {
                    Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 5) {
                        ForEach(makeMe.steps) { step in
                            GridRow {
                                Image(systemName: stepSymbol(step.status))
                                    .foregroundStyle(stepTint(step.status))
                                Text(step.label).font(.callout)
                                Text(step.message ?? EffectsMakeMe.explanation(step.name))
                                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                Text(step.seconds.map { seconds($0) } ?? "")
                                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
                if let parts = job.partsInfo {
                    Label("Parts map", systemImage: "paintpalette").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                    EffectsPartsSummary(parts: parts)
                    if let partsPreview = job.partsPreview {
                        EffectsImage(url: partsPreview, stamp: job.stamp)
                            .frame(maxWidth: .infinity)
                            .clipShape(.rect(cornerRadius: 14))
                            .help("4 frames: work · depth · mask · parts · control-parts, and the legend of the classes found")
                    }
                }
                if let error = makeMe.error, !makeMe.isDone {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if makeMe.canContinue {
                    HStack(spacing: 12) {
                        Button { app.continueMakeMe(job) } label: { Label("Continue", systemImage: "arrow.clockwise") }
                            .buttonStyle(.glassProminent)
                            .disabled(effects.isBusy)
                        Text("Goes on from the step that stopped; the steps already done are kept.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                } else if makeMe.isRunning, !runningHere {
                    HStack(spacing: 12) {
                        ProgressView().controlSize(.small)
                        Text(makeMe.fromTheSwap ? "Running for The Swap (Settings › Extras shows its progress)." : "Running outside the app (command line).")
                            .font(.callout).foregroundStyle(.secondary)
                        Spacer()
                        Button("Stop", role: .cancel) { app.cancelMakeMe(job) }.buttonStyle(.glass)
                    }
                }
                if let result = job.resultVideo {
                    Text(job.file("final-voice.mp4") != nil ? "Result, with the new voice" : "Result")
                        .font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                    EffectsClip(url: result, stamp: job.stamp, height: 400)
                    if let compare = job.compare {
                        Text("Side by side: the original on the left, the result on the right")
                            .font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                        EffectsClip(url: compare, stamp: job.stamp, height: 260)
                    }
                    Text(makeMe.comment ?? "AI-generated with YouTube Zeus Effects — written in the file's metadata.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 10) {
                        Button { NSWorkspace.shared.activateFileViewerSelecting([result]) } label: {
                            Label("Reveal in Finder", systemImage: "folder")
                        }
                        .buttonStyle(.glass)
                        if let swap = job.swapResult, makeMe.hatFound == true {
                            Button { NSWorkspace.shared.activateFileViewerSelecting([swap]) } label: {
                                Label("Before the hat step", systemImage: "hat.cap")
                            }
                            .buttonStyle(.glass)
                            .help("final-swap.mp4: the face swap alone, hat still on")
                        }
                        Button { app.saveEffectToMovies(job) } label: {
                            Label("Save to Movies", systemImage: "square.and.arrow.down")
                        }
                        .buttonStyle(.glassProminent)
                        .help("Copies it to Movies › YouTube Zeus Effects")
                    }
                } else if job.file("preview.jpg") != nil, !makeMe.isDone {
                    EffectsImage(url: job.file("preview.jpg")!, stamp: job.stamp)
                        .frame(maxWidth: .infinity)
                        .clipShape(.rect(cornerRadius: 14))
                        .help("The prepared clip: 4 frames × work · depth · mask · control (kept for the record)")
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(.regular, in: .rect(cornerRadius: 22))
        }
    }

    private func seconds(_ value: Double) -> String {
        value >= 90 ? String(format: "%.0f min", value / 60) : String(format: "%.0f s", value)
    }

    private func statusSymbol(_ makeMe: EffectsMakeMe) -> String {
        switch makeMe.status {
        case "done": "checkmark.circle.fill"
        case "running": "circle.dotted.circle"
        case "failed": "exclamationmark.triangle.fill"
        case "cancelled": "stop.circle"
        default: "circle"
        }
    }

    private func statusTint(_ makeMe: EffectsMakeMe) -> Color {
        switch makeMe.status {
        case "done": .green
        case "running": .zeus
        case "failed": .orange
        default: .secondary
        }
    }

    private func stepSymbol(_ status: String) -> String {
        switch status {
        case "done": "checkmark.circle.fill"
        case "skipped": "minus.circle"
        case "running": "circle.dotted.circle"
        case "failed": "xmark.circle.fill"
        case "cancelled": "stop.circle"
        default: "circle"
        }
    }

    private func stepTint(_ status: String) -> Color {
        switch status {
        case "done": .green
        case "running": .zeus
        case "failed": .orange
        default: .secondary
        }
    }
}
