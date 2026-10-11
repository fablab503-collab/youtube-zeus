import AVKit
import ImageIO
import SwiftData
import SwiftUI
import UniformTypeIdentifiers

// Effects (3.8): a free, local Genjutsu. Source → Prepare → Review and approve → Generate (with consent) → Result.
// Nothing leaves the Mac and there is no cloud engine in this tab.

/// Content column: earlier jobs (from `list --json`), newest first.
struct EffectsJobsList: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        @Bindable var effects = app.effects
        List(selection: $effects.selectedJobID) {
            Section {
                Button {
                    effects.selectedJobID = nil
                } label: {
                    Label("New effect", systemImage: "plus.circle.fill")
                        .foregroundStyle(effects.selectedJobID == nil ? AnyShapeStyle(.tint) : AnyShapeStyle(.primary))
                }
                .buttonStyle(.plain)
                .help("Pick a source and prepare a new clip")
            }
            Section("Jobs") {
                ForEach(effects.jobs) { job in
                    EffectsJobRow(job: job)
                        .tag(job.id)
                        .contextMenu {
                            Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([job.final ?? job.folder]) }
                            if job.final != nil { Button("Save to Movies") { app.saveEffectToMovies(job) } }
                            Button("Copy the Job Folder Path") {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(job.folder.path, forType: .string)
                            }
                        }
                }
                if effects.jobs.isEmpty {
                    Text("Prepared clips and results show here.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Effects")
        .navigationSubtitle(effects.jobs.isEmpty ? "" : "\(effects.jobs.count) job\(effects.jobs.count == 1 ? "" : "s")")
        .toolbar {
            ToolbarItem {
                Button {
                    Task { await effects.refreshJobs() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .help("Read the jobs again")
            }
        }
        .task { await effects.refreshJobs() }
    }
}

private struct EffectsJobRow: View {
    @Environment(AppModel.self) private var app
    let job: EffectsJob

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                if let preview = job.file("preview.jpg") {
                    // The contact sheet's first cell (4 x 4 grid): the first frame of the clip.
                    EffectsSheetCell(url: preview, stamp: job.stamp)
                } else {
                    LinearGradient(colors: [.zeus.opacity(0.35), .zeus.opacity(0.12)], startPoint: .topLeading, endPoint: .bottomTrailing)
                    Image(systemName: "wand.and.stars").foregroundStyle(.white.opacity(0.8))
                }
            }
            .frame(width: 72, height: 40)
            .clipShape(.rect(cornerRadius: 7))
            VStack(alignment: .leading, spacing: 3) {
                Text(app.effectsTitle(job)).font(.callout.weight(.medium)).lineLimit(2)
                HStack(spacing: 5) {
                    Text(EffectsMode.label(job.mode))
                    Text("·")
                    Text(job.range).monospacedDigit()
                    if let created = job.created {
                        Text("·")
                        Text(created.relative)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
            Spacer(minLength: 4)
            if job.final != nil {
                Image(systemName: "film.fill").foregroundStyle(.green).help("Generated")
            } else if job.isApproved {
                Image(systemName: "checkmark.seal.fill").foregroundStyle(.tint).help("Approved")
            }
        }
        .padding(.vertical, 2)
    }
}

/// Detail column: engine status, then the source form or the selected job (review, generate, result).
struct EffectsView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let effects = app.effects
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                EffectsEngineCard()
                if effects.isBusy || effects.lastError != nil || !effects.output.isEmpty {
                    EffectsProgressCard()
                }
                if let job = effects.selectedJob {
                    EffectsJobHeader(job: job)
                    if job.isPrepared {
                        EffectsReviewCard(job: job)
                        EffectsGenerateCard(job: job)
                    }
                    if let final = job.final { EffectsResultCard(job: job, final: final) }
                } else {
                    EffectsSourceCard()
                }
                Text("Free and private: every step runs on this Mac with open models (no paid API, no key, no upload). The idea comes from Sirio Berati's open-source Genjutsu workflow (MIT). Results are marked as AI-generated in their metadata.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(24)
            .frame(maxWidth: 880, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("Effects")
        .navigationSubtitle("Keep the movement, replace the person, keep the sound")
        .task { await app.effects.refreshIfNeeded() }
    }
}

// MARK: - Engine

private struct EffectsEngineCard: View {
    @Environment(AppModel.self) private var app
    @State private var showChecks = false
    @State private var confirmFullReplace = false

    var body: some View {
        let effects = app.effects
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Label("Engine", systemImage: "cpu").font(.headline)
                Spacer()
                if effects.isChecking {
                    ProgressView().controlSize(.small)
                } else if let doctor = effects.doctor {
                    Chip(text: doctor.ready ? "Ready" : "Needs attention",
                         symbol: doctor.ready ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                         tint: doctor.ready ? .green : .orange)
                    Chip(text: "Face swap", symbol: chipSymbol(doctor.faceSwapReady), tint: chipTint(doctor.faceSwapReady))
                    Chip(text: "Full replace", symbol: chipSymbol(doctor.fullReplaceReady), tint: chipTint(doctor.fullReplaceReady))
                }
            }
            if effects.engineFolder == nil {
                Text("The Effects engine is missing from this copy of the app. Rebuild YouTube Zeus: the build copies the repository's effects/ folder into the app.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if !effects.isSetUp {
                Text("Set it up once: it makes its own Python with uv, installs the depth, mask and face-mesh tools and downloads their small open weights into Application Support › YouTube Zeus › Effects. Free, nothing to sign in to.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button { app.setUpEffects(.main) } label: { Label("Set up", systemImage: "arrow.down.circle") }
                    .buttonStyle(.glassProminent)
                    .disabled(effects.isBusy)
            } else {
                if let error = effects.doctorError {
                    Text(error).font(.callout).foregroundStyle(.orange).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 10) {
                    if effects.doctor?.faceSwapReady != true {
                        Button { app.setUpEffects(.faceSwap) } label: { Label("Add face swap engine", systemImage: "face.smiling") }
                            .buttonStyle(.glass)
                            .help("FaceFusion (open source), in its own folder next to the engine")
                    }
                    if effects.doctor?.fullReplaceReady != true {
                        Button { confirmFullReplace = true } label: { Label("Add full replace engine (19 GB)", systemImage: "person.fill.viewfinder") }
                            .buttonStyle(.glass)
                            .help("Wan2.1-VACE 1.3B (Apache-2.0): about 19 GB of open weights")
                    }
                    Menu {
                        Button("Check Again") { Task { await effects.refreshDoctor() } }
                        Button("Set Up Again") { app.setUpEffects(.main) }
                        Divider()
                        Button("Show the Effects Folder") {
                            try? FileManager.default.createDirectory(at: EffectsPaths.home, withIntermediateDirectories: true)
                            NSWorkspace.shared.open(EffectsPaths.home)
                        }
                        if FileManager.default.fileExists(atPath: EffectsPaths.log.path) {
                            Button("Open the Engine Log") { NSWorkspace.shared.open(EffectsPaths.log) }
                        }
                    } label: {
                        Label("More", systemImage: "ellipsis.circle")
                    }
                    .menuStyle(.button)
                    .buttonStyle(.glass)
                    .fixedSize()
                }
                .disabled(effects.isBusy)
                if let doctor = effects.doctor, !doctor.checks.isEmpty {
                    DisclosureGroup("What the check found", isExpanded: $showChecks) {
                        Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 4) {
                            ForEach(doctor.checks) { check in
                                GridRow {
                                    Image(systemName: check.info ? "info.circle" : (check.ok ? "checkmark.circle.fill" : "xmark.circle.fill"))
                                        .foregroundStyle(check.info ? Color.secondary : (check.ok ? Color.green : Color.orange))
                                    Text(check.name.capitalizedFirst).font(.callout)
                                    Text(check.detail).font(.caption).foregroundStyle(.secondary).lineLimit(2).textSelection(.enabled)
                                }
                            }
                        }
                        .padding(.top, 6)
                    }
                    .font(.callout)
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
        .confirmationDialog("Download the full replace engine?", isPresented: $confirmFullReplace) {
            Button("Download about 19 GB") { app.setUpEffects(.fullReplace) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Wan2.1-VACE 1.3B (open weights, Apache-2.0) goes into Application Support › YouTube Zeus › Effects › models. The download can be resumed. It runs on this Mac only and is slow on a laptop.")
        }
    }

    private func chipSymbol(_ ready: Bool?) -> String {
        switch ready {
        case true?: "checkmark.circle.fill"
        case false?: "plus.circle"
        case nil: "questionmark.circle"
        }
    }

    private func chipTint(_ ready: Bool?) -> Color { ready == true ? .green : .secondary }
}

private struct EffectsProgressCard: View {
    @Environment(AppModel.self) private var app
    @State private var showOutput = false

    var body: some View {
        let effects = app.effects
        VStack(alignment: .leading, spacing: 10) {
            if let work = effects.work {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(work.label).font(.headline)
                    if !effects.stage.isEmpty { Chip(text: effects.stage, tint: .zeus) }
                    Spacer()
                    Button("Cancel", role: .cancel) { effects.cancel() }
                        .buttonStyle(.glass)
                        .help("Stop the engine and everything it started")
                }
                if let fraction = effects.fraction {
                    ProgressView(value: fraction)
                }
                if !effects.message.isEmpty {
                    Text(effects.message).font(.callout).foregroundStyle(.secondary).lineLimit(2)
                }
            } else if let error = effects.lastError {
                HStack(alignment: .top) {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer()
                    Button { effects.lastError = nil } label: { Image(systemName: "xmark") }
                        .buttonStyle(.borderless)
                        .help("Hide")
                }
            } else if !effects.message.isEmpty {
                Label(effects.message, systemImage: "checkmark.circle").foregroundStyle(.secondary)
            }
            if !effects.output.isEmpty {
                DisclosureGroup("Details", isExpanded: $showOutput) {
                    ScrollView {
                        Text(effects.output.joined(separator: "\n"))
                            .font(.caption.monospaced())
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .defaultScrollAnchor(.bottom)
                    .frame(maxHeight: 220)
                }
                .font(.callout)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
        .onChange(of: effects.work) { _, work in
            if case .setup? = work { showOutput = true }
        }
    }
}

// MARK: - Source

private struct EffectsSourceCard: View {
    @Environment(AppModel.self) private var app
    @State private var picking = false
    @State private var targeted = false

    var body: some View {
        @Bindable var effects = app.effects
        VStack(alignment: .leading, spacing: 14) {
            Label("1 · Source", systemImage: "film").font(.headline)
            Text("A short clip — up to 10 seconds — of a video you ate, or of a video file. Zeus keeps how the people in it move.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .center, spacing: 14) {
                sourcePreview
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
                GridRow {
                    Text("Mode").foregroundStyle(.secondary)
                    Picker("Mode", selection: $effects.mode) {
                        ForEach(EffectsMode.allCases) { mode in Text(mode.label).tag(mode) }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                }
            }
            Text(effects.mode.explanation).font(.caption).foregroundStyle(.secondary)

            HStack(spacing: 12) {
                Button { app.prepareEffect() } label: { Label("Prepare", systemImage: "wand.and.stars") }
                    .buttonStyle(.glassProminent)
                    .controlSize(.large)
                    .disabled(effects.isBusy || !effects.isSetUp || app.effectsSourceInput == nil)
                Text(hint(effects))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }

    private func hint(_ effects: EffectsEngine) -> String {
        if !effects.isSetUp { return "Set up the engine first (above)." }
        if app.effectsSourceInput == nil { return "Pick a video of the library, choose a file, or drop one here." }
        return "Download and trim, then depth, people mask and face mesh on this Mac. A 5-second clip takes a minute or two."
    }

    @ViewBuilder private var sourcePreview: some View {
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
                Text("No source yet: pick a video of the library, or drop a video file here.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// The videos of the library that have a picture (YouTube videos and your own video files).
private struct EffectsVideoPicker: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]
    @State private var search = ""
    let onPick: (Video) -> Void

    var body: some View {
        let usable = videos.filter { video in
            video.status != .trashed && video.status != .discovered && app.effectsInput(for: video.videoID) != nil
                && (search.isEmpty || video.displayTitle.localizedCaseInsensitiveContains(search)
                    || video.channelTitle.localizedCaseInsensitiveContains(search))
        }
        VStack(alignment: .leading, spacing: 10) {
            TextField("Search the library", text: $search)
                .textFieldStyle(.roundedBorder)
            List(usable.prefix(300)) { video in
                Button { onPick(video) } label: {
                    HStack(spacing: 10) {
                        Thumbnail(url: video.thumbnailURL, width: 72, radius: 6)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(video.displayTitle).font(.callout.weight(.medium)).lineLimit(2)
                            Text(video.channelTitle.isEmpty ? video.kind.label : video.channelTitle)
                                .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
            }
            .listStyle(.plain)
            .overlay {
                if usable.isEmpty {
                    Text(search.isEmpty ? "Eat a YouTube video or one of your video files first." : "Nothing matches “\(search)”.")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .frame(width: 440, height: 480)
    }
}

// MARK: - Job

private struct EffectsJobHeader: View {
    @Environment(AppModel.self) private var app
    let job: EffectsJob

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(app.effectsTitle(job)).font(.title2.bold()).lineLimit(2)
                Spacer()
                Button { app.effects.selectedJobID = nil } label: { Label("New effect", systemImage: "plus") }
                    .buttonStyle(.glass)
            }
            HStack(spacing: 8) {
                Chip(text: EffectsMode.label(job.mode), symbol: "square.3.layers.3d")
                Chip(text: job.range, symbol: "timer")
                Chip(text: job.statusLabel, symbol: job.final != nil ? "film.fill" : (job.isApproved ? "checkmark.seal.fill" : "circle.dashed"),
                     tint: job.final != nil ? .green : (job.isApproved ? .zeus : .secondary))
                if let created = job.created { Chip(text: created.formatted(date: .abbreviated, time: .shortened), symbol: "calendar") }
                Spacer()
                Button { NSWorkspace.shared.activateFileViewerSelecting([job.folder]) } label: { Image(systemName: "folder") }
                    .buttonStyle(.borderless)
                    .help("Show the job folder in Finder: \(job.folder.path)")
            }
        }
    }
}

private struct EffectsReviewCard: View {
    @Environment(AppModel.self) private var app
    let job: EffectsJob

    /// The columns of preview.jpg (no depth in the pose mode, a pose column when the motion was captured).
    private var previewHelp: String {
        var columns = ["work"]
        if job.file("depth.mp4") != nil { columns.append("depth") }
        columns.append("mask")
        if job.file("pose.mp4") != nil && job.mode != "pose" { columns.append("pose") }
        columns.append(job.mode == "pose" ? "control = pose" : "control")
        return "4 frames: " + columns.joined(separator: " · ")
    }

    var body: some View {
        let clips = [("Control (what the model follows)", "control.mp4"), ("Depth", "depth.mp4"), ("Mask (white = replaced)", "mask.mp4"), ("Pose (body, hands, face)", "pose.mp4")]
            .compactMap { title, name in job.file(name).map { (title, $0) } }
        VStack(alignment: .leading, spacing: 14) {
            Label("2 · Review", systemImage: "eye").font(.headline)
            Text("Check what the models will see before anything is generated. The white mask is what gets replaced; depth and the face mesh carry the movement. If it looks wrong, start a new effect with another start, length or mode.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let preview = job.file("preview.jpg") {
                EffectsImage(url: preview, stamp: job.stamp)
                    .frame(maxWidth: .infinity)
                    .clipShape(.rect(cornerRadius: 14))
                    .help(previewHelp)
            }
            if !clips.isEmpty {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(clips, id: \.1) { title, url in
                        VStack(alignment: .leading, spacing: 6) {
                            Text(title).font(.caption.weight(.semibold)).foregroundStyle(.secondary).lineLimit(1)
                            EffectsClip(url: url, stamp: job.stamp, height: 170)
                        }
                    }
                }
            }
            if job.isApproved {
                Label(approvedText, systemImage: "checkmark.seal.fill")
                    .font(.callout)
                    .foregroundStyle(.green)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                HStack(spacing: 12) {
                    Button { app.approveEffect(job) } label: { Label("Approve this mask", systemImage: "checkmark.seal") }
                        .buttonStyle(.glassProminent)
                        .disabled(app.effects.isBusy)
                    Text("Approving records a fingerprint of every prepared file.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }

    private var approvedText: String {
        var when = ""
        if let text = job.approvedAt, !text.isEmpty {
            when = " " + (EffectsJob.date(text)?.formatted(date: .abbreviated, time: .shortened) ?? text)
        }
        return "Approved\(when). Generation is bound to these exact files (SHA-256): if one changes, it refuses."
    }
}

private struct EffectsGenerateCard: View {
    @Environment(AppModel.self) private var app
    let job: EffectsJob
    @State private var advanced = false

    var body: some View {
        @Bindable var effects = app.effects
        let installed = effects.isInstalled(effects.generator)
        let hasReference = effects.referencePhoto != nil || effects.secondPhoto != nil || effects.facePack != nil
        let ready = job.isApproved && effects.consent && hasReference && !effects.isBusy && installed != false
            && effects.readingPack == nil
        VStack(alignment: .leading, spacing: 14) {
            Label("3 · Generate", systemImage: "sparkles").font(.headline)
            HStack(alignment: .top, spacing: 14) {
                EffectsPhotoSlot(title: "Reference photo", subtitle: effects.facePack == nil ? "required" : "optional (face pack set)",
                                 url: $effects.referencePhoto) {
                    app.chooseEffectsPhoto(second: false)
                }
                EffectsPhotoSlot(title: "Second photo", subtitle: "optional (another angle)", url: $effects.secondPhoto) {
                    app.chooseEffectsPhoto(second: true)
                }
                VStack(alignment: .leading, spacing: 10) {
                    TextField("Prompt (optional): clothes, hair, light, style…", text: $effects.prompt, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .lineLimit(2...4)
                    Picker("Engine", selection: $effects.generator) {
                        ForEach(EffectsGenerator.allCases) { engine in Text(engine.label).tag(engine) }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    Text(effects.generator.explanation).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if effects.generator == .faceswap {
                        Toggle("Sharpen the face (about 20 times slower)", isOn: $effects.enhanceFace)
                            .font(.caption)
                    }
                    if installed == false {
                        Label("Not installed yet: add it in Engine, above.", systemImage: "arrow.up.circle")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            }
            EffectsFacePackRow()

            DisclosureGroup("Advanced (full replace)", isExpanded: $advanced) {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    GridRow {
                        Text("Frames").foregroundStyle(.secondary)
                        Picker("Frames", selection: $effects.frames) {
                            ForEach([17, 33, 49, 65, 81], id: \.self) { count in
                                Text("\(count) (\(String(format: "%.1f", Double(count) / 16)) s)").tag(count)
                            }
                        }
                        .labelsHidden()
                        .fixedSize()
                    }
                    GridRow {
                        Text("Steps").foregroundStyle(.secondary)
                        Stepper(value: $effects.steps, in: 4...50) { Text("\(effects.steps)").monospacedDigit() }
                            .fixedSize()
                    }
                    GridRow {
                        Text("Size").foregroundStyle(.secondary)
                        Picker("Size", selection: $effects.size) {
                            Text("360p").tag("360p")
                            Text("480p (2x slower)").tag("480p")
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .fixedSize()
                    }
                    GridRow {
                        Text("Seed").foregroundStyle(.secondary)
                        TextField("random", text: $effects.seedText)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 110)
                    }
                }
                .padding(.top, 6)
                Text("Frames per window at 16 fps (longer clips are made in overlapping windows), diffusion steps and size. Face swap works on the whole clip and ignores them.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .font(.callout)

            Toggle(isOn: $effects.consent) {
                Text(effects.facePack == nil ? "These photos show me or people who agreed"
                     : "This face pack and these photos show me or people who agreed").fontWeight(.medium)
            }
            .toggleStyle(.checkbox)

            HStack(spacing: 12) {
                Button { app.generateEffect(job) } label: { Label("Generate", systemImage: "wand.and.stars") }
                    .buttonStyle(.glassProminent)
                    .controlSize(.large)
                    .disabled(!ready)
                Text(hint(effects, installed: installed))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }

    private func hint(_ effects: EffectsEngine, installed: Bool?) -> String {
        if !job.isApproved { return "Approve the mask first (Review, above)." }
        if effects.readingPack != nil { return "Reading the face pack…" }
        if effects.referencePhoto == nil && effects.secondPhoto == nil && effects.facePack == nil {
            return "Choose a reference photo or a face pack: a clear, front-facing face works best."
        }
        if !effects.consent { return "Tick the box: only use photos of yourself or of people who agreed." }
        if installed == false { return "Add this engine first, or pick the other one." }
        return "The result keeps the clip's complete original sound and is marked as AI-generated."
    }
}

/// "Use a face pack…": a pack from Cinema 101's Scan my face (several angles of one face, picked by the engine).
private struct EffectsFacePackRow: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let effects = app.effects
        HStack(alignment: .center, spacing: 12) {
            if let pack = effects.facePack {
                Image(systemName: "face.smiling").font(.title2).foregroundStyle(Color.zeus)
                VStack(alignment: .leading, spacing: 2) {
                    Text(pack.name).font(.callout.weight(.semibold)).lineLimit(1).truncationMode(.middle)
                    Text(pack.summary(for: effects.generator)).font(.caption).foregroundStyle(.secondary)
                }
                .help(pack.folder.path)
                HStack(spacing: 6) {
                    ForEach(Array(pack.picked.enumerated()), id: \.element.id) { index, pick in
                        EffectsThumb(url: pick.file)
                            .frame(width: 46, height: 46)
                            .clipShape(.rect(cornerRadius: 8))
                            .opacity(effects.generator == .vace && index >= 2 ? 0.35 : 1)
                            .help(pick.why.capitalizedFirst)
                    }
                }
                Spacer(minLength: 0)
                Button { effects.facePack = nil } label: { Label("Clear", systemImage: "xmark.circle.fill") }
                    .buttonStyle(.borderless)
                    .help("Stop using this face pack")
            } else if let reading = effects.readingPack {
                ProgressView().controlSize(.small)
                Text("Reading \(EffectsFacePacks.title(reading))…").font(.callout).foregroundStyle(.secondary)
            } else {
                if effects.foundPacks.isEmpty {
                    Button { app.chooseEffectsFacePack() } label: { Label("Use a face pack…", systemImage: "face.dashed") }
                        .buttonStyle(.glass)
                } else {
                    Menu {
                        Section("Found on this Mac") {
                            ForEach(effects.foundPacks, id: \.self) { url in
                                Button("\(EffectsFacePacks.title(url)) — \(EffectsFacePacks.place(url))") { app.useEffectsFacePack(url) }
                            }
                        }
                        Divider()
                        Button("Choose a face pack…") { app.chooseEffectsFacePack() }
                    } label: {
                        Label("Use a face pack…", systemImage: "face.dashed")
                    } primaryAction: {
                        app.chooseEffectsFacePack()
                    }
                    .menuStyle(.button)
                    .buttonStyle(.glass)
                    .fixedSize()
                }
                Text("From Cinema 101 › Scan my face: the sharpest frames from several angles are used.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await effects.findFacePacks() }
    }
}

/// A small picture from a local file (ImageIO thumbnail, never the full photo).
private struct EffectsThumb: View {
    let url: URL
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                Rectangle().fill(.quaternary)
            }
        }
        .task(id: url) { image = Self.thumbnail(url) }
    }

    nonisolated static func thumbnail(_ url: URL, size: Int = 160) -> NSImage? {
        let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                        kCGImageSourceCreateThumbnailWithTransform: true,
                                        kCGImageSourceThumbnailMaxPixelSize: size]
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return NSImage(cgImage: image, size: NSSize(width: image.width, height: image.height))
    }
}

private struct EffectsPhotoSlot: View {
    let title: String
    let subtitle: String
    @Binding var url: URL?
    let choose: () -> Void
    @State private var targeted = false

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                if let url {
                    EffectsImage(url: url, stamp: 0, fill: true)
                } else {
                    Image(systemName: "person.crop.square").font(.largeTitle).foregroundStyle(.tertiary)
                }
            }
            .frame(width: 118, height: 118)
            .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 14))
            .clipShape(.rect(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14)
                    .strokeBorder(targeted ? Color.zeus : Color.clear, lineWidth: 2)
            }
            .dropDestination(for: URL.self) { urls, _ in
                guard let image = urls.first(where: { url in
                    url.isFileURL && (UTType(filenameExtension: url.pathExtension)?.conforms(to: .image) ?? false)
                }) else { return false }
                url = image
                return true
            } isTargeted: { targeted = $0 }
            Text(title).font(.caption.weight(.semibold))
            Text(subtitle).font(.caption2).foregroundStyle(.secondary)
            HStack(spacing: 6) {
                Button("Choose…", action: choose).buttonStyle(.glass).controlSize(.small)
                if url != nil {
                    Button { url = nil } label: { Image(systemName: "xmark") }
                        .buttonStyle(.borderless)
                        .controlSize(.small)
                        .help("Remove")
                }
            }
        }
        .frame(width: 130)
    }
}

private struct EffectsResultCard: View {
    @Environment(AppModel.self) private var app
    let job: EffectsJob
    let final: URL

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("4 · Result", systemImage: "play.rectangle.fill").font(.headline)
            EffectsClip(url: final, stamp: job.stamp, height: 420)
            let engine = job.generations.last?.engine ?? ""
            Text("AI-generated with YouTube Zeus Effects\(engine.isEmpty ? "" : " (\(engine))") — written in the file's metadata. The sound is the clip's original soundtrack, untouched.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Button { NSWorkspace.shared.activateFileViewerSelecting([final]) } label: {
                    Label("Reveal in Finder", systemImage: "folder")
                }
                .buttonStyle(.glass)
                Button { app.saveEffectToMovies(job) } label: {
                    Label("Save to Movies", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(.glassProminent)
                .help("Copies it to Movies › YouTube Zeus Effects")
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }
}

// MARK: - Small pieces

/// A picture from a local file, loaded again when the job changes.
private struct EffectsImage: View {
    let url: URL
    let stamp: Double
    var fill = false
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: fill ? .fill : .fit)
            } else {
                Rectangle().fill(.quaternary)
            }
        }
        .task(id: "\(url.path)|\(stamp)") { image = NSImage(contentsOf: url) }
    }
}

/// The top-left cell of preview.jpg (4 columns x 4 rows), cropped and filling its frame: the job's thumbnail.
private struct EffectsSheetCell: View {
    let url: URL
    let stamp: Double
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
            } else {
                Rectangle().fill(.quaternary)
            }
        }
        .task(id: "\(url.path)|\(stamp)") { image = Self.firstCell(of: url) }
    }

    nonisolated static func firstCell(of url: URL) -> NSImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let sheet = CGImageSourceCreateImageAtIndex(source, 0, nil),
              let cell = sheet.cropping(to: CGRect(x: 0, y: 0, width: sheet.width / 4, height: sheet.height / 4))
        else { return nil }
        return NSImage(cgImage: cell, size: NSSize(width: cell.width, height: cell.height))
    }
}

/// A video of the job, with the system player controls.
private struct EffectsClip: View {
    let url: URL
    let stamp: Double
    var height: CGFloat = 200
    @State private var player: AVPlayer?

    var body: some View {
        ZStack {
            Color.black
            if let player { PlayerView(player: player) }
        }
        .frame(maxWidth: .infinity)
        .frame(height: height)
        .clipShape(.rect(cornerRadius: 12))
        .task(id: "\(url.path)|\(stamp)") {
            player?.pause()
            player = AVPlayer(url: url)
        }
        .onDisappear { player?.pause() }
    }
}

private extension String {
    var capitalizedFirst: String { prefix(1).uppercased() + dropFirst() }
}
