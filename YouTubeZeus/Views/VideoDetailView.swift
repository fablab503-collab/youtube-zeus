import SwiftData
import SwiftUI

enum DetailTab: String, CaseIterable, Identifiable {
    case transcript = "Transcript"
    case summary = "Summary"
    case screen = "On screen"
    case skills = "Skills"
    case info = "Info"

    var id: String { rawValue }
}

struct VideoDetailView: View {
    @Environment(AppModel.self) private var app
    @Query private var matches: [Video]
    @State private var tab: DetailTab = .transcript
    @State private var find = ""
    @State private var paragraphs: [TranscriptParagraph] = []
    @State private var polishedParagraphs: [TranscriptParagraph] = []
    @State private var showOriginal = false
    @State private var highlight: Int?

    init(videoID: String) {
        _matches = Query(filter: #Predicate<Video> { $0.videoID == videoID })
    }

    var body: some View {
        if let video = matches.first {
            content(video)
        } else {
            EmptyState(symbol: "questionmark.video", title: "Not found", message: "This video is no longer in the library.")
        }
    }

    private func applyInitialTab() {
        guard let name = app.initialTab,
              let initial = DetailTab.allCases.first(where: { $0.rawValue.lowercased() == name.lowercased() }) else { return }
        tab = initial
        app.initialTab = nil
    }

    /// youtubezeus://open?video=…&t=… (a key point, a search hit, an Ask source): show the transcript at that moment.
    private func handleJump(_ proxy: ScrollViewProxy, _ video: Video) {
        guard let jump = app.jump, jump.videoID == video.videoID, video.status.hasText else { return }
        tab = .transcript
        find = ""
        let shown = polishedParagraphs.isEmpty || showOriginal ? paragraphs : polishedParagraphs
        // Not loaded yet: `.task(id: paragraphs.count)` calls again once the transcript is there.
        guard !shown.isEmpty else { return }
        app.jump = nil
        guard let target = shown.last(where: { $0.start <= jump.seconds + 0.5 }) ?? shown.first else { return }
        highlight = target.id
        Task {
            try? await Task.sleep(for: .milliseconds(120))
            withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo("p\(target.id)", anchor: .center) }
            try? await Task.sleep(for: .seconds(4))
            if highlight == target.id { withAnimation { highlight = nil } }
        }
    }

    @ViewBuilder
    private func content(_ video: Video) -> some View {
        ScrollViewReader { proxy in
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Header(video: video)
                if video.status.hasText {
                    HStack(spacing: 12) {
                        Picker("View", selection: $tab) {
                            ForEach(DetailTab.allCases) { tab in Text(tab.rawValue).tag(tab) }
                        }
                        .pickerStyle(.segmented)
                        .labelsHidden()
                        .frame(maxWidth: 400)
                        Spacer()
                        if tab == .transcript, !polishedParagraphs.isEmpty {
                            Picker("Text", selection: $showOriginal) {
                                Text("Polished").tag(false)
                                Text("Original").tag(true)
                            }
                            .pickerStyle(.segmented)
                            .labelsHidden()
                            .fixedSize()
                            .help("Polished by \(video.polishModel ?? "the local AI") on this Mac, or the original captions")
                        }
                        if tab == .transcript {
                            HStack(spacing: 6) {
                                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                                TextField("Find in transcript", text: $find)
                                    .textFieldStyle(.plain)
                                    .frame(width: 170)
                                if !find.isEmpty {
                                    Button { find = "" } label: { Image(systemName: "xmark.circle.fill") }
                                        .buttonStyle(.plain)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .glassEffect(.regular.interactive(), in: .capsule)
                        }
                    }

                    switch tab {
                    case .transcript:
                        TranscriptBody(video: video,
                                       paragraphs: polishedParagraphs.isEmpty || showOriginal ? paragraphs : polishedParagraphs,
                                       find: find, isPolished: !polishedParagraphs.isEmpty && !showOriginal, highlight: highlight)
                    case .summary: SummaryBody(video: video)
                    case .screen: ScreenBody(video: video)
                    case .skills: SkillsBody(video: video)
                    case .info: InfoBody(video: video)
                    }
                } else {
                    ProgressCard(video: video)
                    if !video.videoDescription.isEmpty { InfoBody(video: video) }
                }
            }
            .padding(28)
            .frame(maxWidth: 900, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .onChange(of: app.jump) { handleJump(proxy, video) }
        .onAppear { handleJump(proxy, video) }
        .task(id: paragraphs.count) { handleJump(proxy, video) }
        }
        .environment(\.openURL, OpenURLAction { url in
            guard url.scheme == BrainLinks.scheme else { return .systemAction }
            app.handle(url)
            return .handled
        })
        .navigationTitle(video.displayTitle)
        .toolbar {
            ToolbarItemGroup {
                Button { app.playMoment(video, at: 0) } label: {
                    Label(video.kind == .youtube ? "Open on YouTube" : "Play", systemImage: video.kind == .youtube ? "play.rectangle" : "play.fill")
                }
                .help(video.kind == .youtube ? "Open on YouTube" : "Play in YouTube Zeus")
                if video.status.hasText {
                    Button { app.copyForAI(video) } label: { Label("Copy for AI", systemImage: "sparkles") }
                        .help("Copy a knowledge pack for Claude, ChatGPT, Gemini, Grok, GLM…")
                    Button { app.copyTranscript(video) } label: { Label("Copy transcript", systemImage: "doc.on.doc") }
                        .help("Copy the transcript")
                    Menu {
                        Button("Export Markdown…") { app.exportMarkdown(video) }
                        Button("Copy as Markdown Note") { app.copyMarkdown(video) }
                        Button("Save to Second Brain") { app.saveToSecondBrain(video) }
                        if let path = video.secondBrainPath {
                            Button("Show in Second Brain") { app.reveal(path) }
                        }
                    } label: {
                        Label("Export", systemImage: "square.and.arrow.up")
                    }
                    .help("Export")
                }
            }
        }
        .task(id: "\(video.eatenAt?.timeIntervalSince1970 ?? 0)-\(video.polishedData?.count ?? 0)") {
            paragraphs = video.paragraphs
            let polished = video.displayParagraphs
            polishedParagraphs = video.polished.isEmpty ? [] : polished
        }
        .onAppear { applyInitialTab() }
        .onChange(of: app.initialTab) { applyInitialTab() }
    }
}

private struct Header: View {
    @Environment(AppModel.self) private var app
    let video: Video

    private var isEaten: Bool { video.status.hasText }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .top, spacing: 18) {
                Button { app.playMoment(video, at: 0) } label: {
                    ZStack {
                        Thumbnail(url: video.thumbnailURL, width: 220, radius: 16)
                        Image(systemName: "play.fill")
                            .font(.title2)
                            .foregroundStyle(.white)
                            .padding(14)
                            .glassEffect(.clear, in: .circle)
                    }
                }
                .buttonStyle(.plain)
                .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
                .help(video.kind == .youtube ? "Play on YouTube" : "Play in YouTube Zeus")

                VStack(alignment: .leading, spacing: 8) {
                    Text(video.displayTitle)
                        .font(.title2.bold())
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    Text([video.channelTitle,
                          video.publishedAt?.shortDay,
                          video.duration > 0 ? video.duration.timestamp : nil]
                        .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                        .foregroundStyle(.secondary)
                }
            }

            FlowLayout(spacing: 6) {
                StatusBadge(status: video.status)
                if isEaten {
                    Chip(text: video.source.label, symbol: video.source.symbol, tint: .zeus)
                    if !video.language.isEmpty {
                        Chip(text: Summarizer.languageName(video.language), symbol: "globe")
                    }
                    Chip(text: "\(video.wordCount.formatted()) words", symbol: "text.alignleft")
                    if video.secondBrainPath != nil {
                        Chip(text: "Second Brain", symbol: "brain.head.profile", tint: .green)
                    } else if video.exportPending {
                        Chip(text: "Second Brain pending", symbol: "clock", tint: .orange)
                    }
                    if video.digestData != nil {
                        Chip(text: "Summarized", symbol: "apple.intelligence", tint: .pink)
                    }
                    if !video.polished.isEmpty {
                        Chip(text: "Polished", symbol: "wand.and.stars", tint: .mint)
                    }
                    let repos = video.repos
                    if !repos.isEmpty {
                        Chip(text: "\(repos.count) GitHub repo\(repos.count == 1 ? "" : "s")",
                             symbol: "chevron.left.forwardslash.chevron.right",
                             tint: repos.contains(where: \.needsCare) ? .orange : .indigo)
                    }
                    if video.viewCount > 0 {
                        Chip(text: "\(video.viewCount.formatted(.number.notation(.compactName))) views", symbol: "eye")
                    }
                }
            }

            if isEaten {
                GlassEffectContainer(spacing: 8) {
                    FlowLayout(spacing: 8) {
                        Button {
                            app.copyForAI(video)
                        } label: {
                            Label("Copy for AI", systemImage: "sparkles")
                        }
                        .buttonStyle(.glassProminent)
                        .help("A knowledge pack to paste into Claude, ChatGPT, Gemini, Grok, GLM…")
                        Button {
                            app.summarize(video)
                        } label: {
                            Label(video.digestData == nil ? "Summarize" : "Summarize again", systemImage: "apple.intelligence")
                        }
                        .buttonStyle(.glass)
                        .disabled(app.engine.isSummarizing(video.videoID))
                        Button {
                            app.compileSkills(video)
                        } label: {
                            Label("Make skills", systemImage: "sparkles.rectangle.stack")
                        }
                        .buttonStyle(.glass)
                        .disabled(app.compiler.compiling.contains(video.videoID))
                        if let note = app.noteURL(for: video), FileManager.default.fileExists(atPath: note.path) {
                            Button {
                                app.openInBrain(note)
                            } label: {
                                Label(BrainLinks.openLabel, systemImage: "brain.head.profile")
                            }
                            .buttonStyle(.glass)
                            .help("Open this video's note in the Second Brain, at its page")
                        } else {
                            Button {
                                app.saveToSecondBrain(video)
                            } label: {
                                Label("Save to Second Brain", systemImage: "brain.head.profile")
                            }
                            .buttonStyle(.glass)
                        }
                        if app.polisher.isInstalled {
                            Button {
                                app.engine.polishNow(video)
                            } label: {
                                Label(video.polished.isEmpty ? "Polish with local AI" : "Polish again", systemImage: "wand.and.stars")
                            }
                            .buttonStyle(.glass)
                            .disabled(app.engine.isPolishing(video.videoID))
                            .help("Fix punctuation, capitals and misheard words with \(app.settings.polishModel), on this Mac")
                        }
                        if video.kind != .podcast || video.mediaURL.map(MediaFiles.isVideo) == true {
                            Button {
                                app.readScreen(video)
                            } label: {
                                Label(video.screenReadAt == nil ? "Read the screen" : "Read the screen again", systemImage: "text.viewfinder")
                            }
                            .buttonStyle(.glass)
                            .disabled(app.engine.isReadingScreen(video.videoID))
                            .help("Read slide titles, code and commands shown in the video, with Apple's Vision on this Mac")
                        }
                        if video.source == .autoCaptions {
                            Button {
                                app.engine.retry(video, withWhisper: true)
                            } label: {
                                Label("Listen with Whisper", systemImage: "waveform")
                            }
                            .buttonStyle(.glass)
                            .help("Auto-captions can have mistakes. Whisper listens to the audio on this Mac for a cleaner text.")
                        }
                    }
                }
                if let error = video.polishError {
                    Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
                }
                if app.engine.isSummarizing(video.videoID) || app.compiler.compiling.contains(video.videoID) || app.engine.isPolishing(video.videoID)
                    || app.engine.isReadingScreen(video.videoID) {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text(app.compiler.compiling.contains(video.videoID)
                             ? "\(app.compiler.engineLabel) is reading the transcript… (about a minute)"
                             : (app.engine.state(for: video.videoID)?.step ?? "Summarizing…"))
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

/// Lays out views left to right and wraps onto new lines (chips and buttons).
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var widest: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: min(widest, maxWidth), height: y + rowHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX
        var y = bounds.minY
        var rowHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX {
                x = bounds.minX
                y += rowHeight + spacing
                rowHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
        }
    }
}

private struct ProgressCard: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        let job = app.engine.state(for: video.videoID)
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Image(systemName: video.status.symbol)
                    .font(.title)
                    .foregroundStyle(video.status.color)
                    .symbolEffect(.pulse, isActive: video.status.isBusy || video.status == .queued)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.title3.bold())
                    Text(job?.step ?? video.statusDetail)
                        .foregroundStyle(video.status == .failed ? .red : .secondary)
                        .textSelection(.enabled)
                }
                Spacer()
            }
            if let progress = job?.progress {
                ProgressView(value: progress).tint(video.status.color)
                Text(progress.formatted(.percent.precision(.fractionLength(0)))).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            } else if video.status.isBusy || video.status == .queued {
                ProgressView().progressViewStyle(.linear).tint(video.status.color)
            }
            HStack {
                switch video.status {
                case .failed, .discovered, .waiting:
                    Button { app.engine.retry(video) } label: { Label("Eat now", systemImage: "fork.knife") }
                        .buttonStyle(.glassProminent)
                case .queued, .fetching, .transcribing:
                    Button(role: .cancel) { app.engine.cancel(video.videoID) } label: { Label("Cancel", systemImage: "xmark") }
                        .buttonStyle(.glass)
                default: EmptyView()
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
    }

    private var title: String {
        switch video.status {
        case .discovered: "New upload — not eaten yet"
        case .queued: "In line"
        case .fetching: "Eating…"
        case .transcribing: "Listening with Whisper…"
        case .summarizing: "Summarizing…"
        case .polishing: "Polishing the text…"
        case .waiting: "Waiting"
        case .failed: "Could not eat this video"
        case .done: "Eaten"
        }
    }
}

struct TranscriptBody: View {
    @Environment(AppModel.self) private var app
    let video: Video
    let paragraphs: [TranscriptParagraph]
    let find: String
    var isPolished = false
    var highlight: Int? = nil

    var body: some View {
        let query = find.trimmingCharacters(in: .whitespaces)
        let visible = query.isEmpty ? paragraphs : paragraphs.filter { $0.text.localizedStandardContains(query) }
        VStack(alignment: .leading, spacing: 4) {
            if !query.isEmpty {
                Text("\(visible.count) paragraph\(visible.count == 1 ? "" : "s") with “\(query)”")
                    .font(.callout).foregroundStyle(.secondary).padding(.bottom, 8)
            }
            if isPolished {
                Label("Polished on this Mac by \(video.polishModel ?? "the local AI"): punctuation, capitals and misheard words fixed. Switch to Original to compare.", systemImage: "wand.and.stars")
                    .font(.caption).foregroundStyle(.secondary).padding(.bottom, 8)
            } else if video.source == .autoCaptions {
                Label("YouTube auto-captions: some words may be misheard. “Polish with local AI” or “Listen with Whisper” can improve it.", systemImage: "info.circle")
                    .font(.caption).foregroundStyle(.secondary).padding(.bottom, 8)
            }
            LazyVStack(alignment: .leading, spacing: 16) {
                ForEach(visible) { paragraph in
                    HStack(alignment: .firstTextBaseline, spacing: 14) {
                        Button {
                            app.playMoment(video, at: paragraph.start)
                        } label: {
                            Text(paragraph.start.timestamp)
                                .font(.callout.monospacedDigit().weight(.semibold))
                                .foregroundStyle(.tint)
                                .frame(width: 62, alignment: .trailing)
                        }
                        .buttonStyle(.plain)
                        .help(video.kind == .youtube ? "Play from here on YouTube" : "Play from here")
                        Text(highlighted(paragraph.text, query))
                            .font(.system(size: 14.5))
                            .lineSpacing(4)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(.vertical, highlight == paragraph.id ? 6 : 0)
                    .padding(.horizontal, highlight == paragraph.id ? 8 : 0)
                    .background(highlight == paragraph.id ? Color.zeusGold.opacity(0.22) : .clear, in: .rect(cornerRadius: 10))
                    .id("p\(paragraph.id)")
                }
            }
        }
    }

    private func highlighted(_ text: String, _ query: String) -> AttributedString {
        var result = AttributedString(text)
        guard !query.isEmpty else { return result }
        var searchStart = result.startIndex
        while let range = result[searchStart...].range(of: query, options: [.caseInsensitive, .diacriticInsensitive]) {
            result[range].backgroundColor = Color.zeusGold.opacity(0.45)
            result[range].foregroundColor = .primary
            searchStart = range.upperBound
        }
        return result
    }
}

struct SummaryBody: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        if let digest = video.digest {
            VStack(alignment: .leading, spacing: 22) {
                VStack(alignment: .leading, spacing: 10) {
                    Label("Summary", systemImage: "apple.intelligence").font(.headline).foregroundStyle(.tint)
                    Text(timedSummary(digest)).font(.system(size: 15)).lineSpacing(4).textSelection(.enabled)
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
                .glassEffect(.regular.tint(.zeus.opacity(0.12)), in: .rect(cornerRadius: 22))

                if !digest.keyPoints.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Key points").font(.headline)
                        ForEach(Array(digest.keyPoints.enumerated()), id: \.offset) { index, point in
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text("\(index + 1)")
                                    .font(.caption.bold().monospacedDigit())
                                    .foregroundStyle(.white)
                                    .frame(width: 20, height: 20)
                                    .background(Color.zeus, in: .circle)
                                Text(point).textSelection(.enabled)
                                if let seconds = digest.keyPointTime(index) {
                                    MomentButton(video: video, seconds: seconds)
                                }
                            }
                        }
                    }
                }
                if !digest.chapters.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Chapters").font(.headline)
                        ForEach(digest.chapters, id: \.self) { chapter in
                            Button {
                                app.open(video: video.videoID, at: chapter.start)
                            } label: {
                                HStack(spacing: 12) {
                                    Text(chapter.start.timestamp).monospacedDigit().foregroundStyle(.tint).frame(width: 62, alignment: .trailing)
                                    Text(chapter.title)
                                    Spacer()
                                }
                                .contentShape(.rect)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                if !digest.topics.isEmpty {
                    HStack { ForEach(digest.topics, id: \.self) { Chip(text: $0, symbol: "number", tint: .zeus) } }
                }
                Text(digest.engine == "Apple Intelligence"
                     ? "Made by Apple Intelligence on this Mac, \(digest.generatedAt.relative)."
                     : "Made by \(digest.engine), \(digest.generatedAt.relative).")
                    .font(.caption).foregroundStyle(.tertiary)
            }
        } else {
            VStack(alignment: .leading, spacing: 12) {
                if app.engine.isSummarizing(video.videoID) {
                    HStack { ProgressView().controlSize(.small); Text(app.engine.state(for: video.videoID)?.step ?? "Summarizing…") }
                } else {
                    Text(video.digestError ?? "No summary yet.")
                        .foregroundStyle(video.digestError == nil ? Color.secondary : Color.red)
                    Text(app.engine.canSummarize
                         ? (app.engine.useAppleIntelligence ? "Apple Intelligence will summarize this video on this Mac."
                            : (app.engine.codexSummaryAllowed ? "Codex will write the summary (you chose it in Settings)."
                               : "The local AI will write the summary on this Mac (free)."))
                         : app.engine.summaryUnavailableMessage)
                        .font(.caption).foregroundStyle(.secondary)
                    Button { app.summarize(video) } label: { Label("Summarize", systemImage: "apple.intelligence") }
                        .buttonStyle(.glassProminent)
                        .disabled(!app.engine.canSummarize)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(.regular, in: .rect(cornerRadius: 22))
        }
    }
}

extension SummaryBody {
    /// The summary with a small link after each sentence placed in the video (opens the transcript at that moment).
    func timedSummary(_ digest: VideoDigest) -> AttributedString {
        guard let lines = digest.summaryLines, !lines.isEmpty else { return AttributedString(digest.summary) }
        var result = AttributedString()
        for (index, line) in lines.enumerated() {
            if index > 0 { result += AttributedString(" ") }
            result += AttributedString(line.text)
            if let seconds = line.seconds, let url = URL(string: BrainLinks.zeus(video: video.videoID, t: seconds)) {
                var moment = AttributedString(" \(seconds.timestamp)")
                moment.link = url
                moment.font = .system(size: 12, weight: .semibold).monospacedDigit()
                moment.foregroundColor = .zeus
                result += moment
            }
        }
        return result
    }
}

/// "12:34 ▶": the moment of a key point or a source. Click: the transcript at that moment; ▶: play it.
struct MomentButton: View {
    @Environment(AppModel.self) private var app
    let video: Video
    let seconds: Double

    var body: some View {
        HStack(spacing: 2) {
            Button {
                app.open(video: video.videoID, at: seconds)
            } label: {
                Text(seconds.timestamp).font(.caption.monospacedDigit().weight(.semibold))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.tint)
            .help("Show this moment in the transcript")
            Button {
                app.playMoment(video, at: seconds)
            } label: {
                Image(systemName: "play.fill").font(.system(size: 9))
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help(video.kind == .youtube ? "Play on YouTube from \(seconds.timestamp)" : "Play from \(seconds.timestamp)")
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Color.zeus.opacity(0.1), in: .capsule)
        .fixedSize()
    }
}

/// Text read on screen: slide titles, commands and code, each with its moment.
struct ScreenBody: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        let items = video.screen
        VStack(alignment: .leading, spacing: 14) {
            if items.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    if app.engine.isReadingScreen(video.videoID) {
                        HStack { ProgressView().controlSize(.small); Text(app.engine.state(for: video.videoID)?.step ?? "Reading the screen…") }
                    } else {
                        Text(video.screenError ?? (video.screenReadAt == nil
                             ? "Zeus has not read this video's screen yet."
                             : "Nothing readable was found on screen (no slides, code or commands)."))
                            .foregroundStyle(video.screenError == nil ? Color.secondary : Color.red)
                        Text("Frames are taken every \(app.settings.screenInterval) seconds and read by Apple's Vision on this Mac. For YouTube videos the picture is downloaded without sound (up to 1080p), then deleted.")
                            .font(.caption).foregroundStyle(.secondary)
                        Button { app.readScreen(video) } label: { Label("Read the screen", systemImage: "text.viewfinder") }
                            .buttonStyle(.glassProminent)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
                .glassEffect(.regular, in: .rect(cornerRadius: 22))
            } else {
                Text("\(items.count) things read on screen by Apple's Vision on this Mac. OCR can misread characters: check a command before running it.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        MomentButton(video: video, seconds: item.start)
                        Image(systemName: item.kind.symbol).foregroundStyle(.secondary).frame(width: 18)
                        if item.kind == .code || item.text.contains("\n") {
                            Text(item.text)
                                .font(.system(size: 12, design: .monospaced))
                                .textSelection(.enabled)
                                .padding(10)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 10))
                        } else if item.kind == .command {
                            Text(item.text).font(.system(size: 13, design: .monospaced)).textSelection(.enabled)
                        } else {
                            Text(item.text).font(item.kind == .title ? .headline : .body).textSelection(.enabled)
                        }
                        Spacer(minLength: 0)
                        if item.kind == .code || item.kind == .command {
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(item.text, forType: .string)
                                app.show("Copied. Check it before running it: OCR can misread characters.")
                            } label: { Image(systemName: "doc.on.doc") }
                                .buttonStyle(.borderless)
                                .help("Copy")
                        }
                    }
                }
            }
        }
    }
}

struct SkillsBody: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Zeus asks \(app.compiler.engineLabel) to find reusable know-how in this transcript and turns it into Agent Skills (for Claude Code, Codex, Gemini CLI…). Every quote is checked against the transcript, and each skill waits for your approval before it goes to \(app.settings.publishFolder).")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !app.compiler.isReady {
                Label(app.compiler.notReadyMessage, systemImage: "key.fill")
                    .foregroundStyle(.orange)
                SettingsLink { Text("Open Settings…") }.buttonStyle(.glass)
            } else {
                Button { app.compileSkills(video) } label: {
                    Label(app.compiler.compiling.contains(video.videoID) ? "Working… (about a minute)" : "Make skills with \(app.compiler.engineLabel)",
                          systemImage: "sparkles.rectangle.stack")
                }
                .buttonStyle(.glassProminent)
                .disabled(app.compiler.compiling.contains(video.videoID))
            }
            if !video.skills.isEmpty {
                Text("Skills from this video").font(.headline).padding(.top, 8)
                ForEach(video.skills.sorted { $0.createdAt > $1.createdAt }) { skill in
                    Button {
                        app.selectedSkillID = skill.id
                        app.selection = .skills
                    } label: {
                        HStack {
                            Image(systemName: skill.status == .published ? "checkmark.seal.fill" : "doc.badge.clock")
                                .foregroundStyle(skill.status == .published ? .green : .orange)
                            VStack(alignment: .leading) {
                                Text(skill.title).font(.body.weight(.medium))
                                Text("\(skill.name) · \(skill.status.label)").font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                        }
                        .padding(12)
                        .contentShape(.rect)
                        .glassEffect(.regular, in: .rect(cornerRadius: 14))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

struct InfoBody: View {
    let video: Video

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if video.status.hasText {
                VideoGitHubSection(video: video).padding(.bottom, 6)
            }
            if !video.chapters.isEmpty {
                Text("YouTube chapters").font(.headline)
                ForEach(video.chapters, id: \.self) { chapter in
                    HStack(spacing: 12) {
                        Text(chapter.start.timestamp).monospacedDigit().foregroundStyle(.tint).frame(width: 62, alignment: .trailing)
                        Text(chapter.title)
                    }
                }
            }
            if video.viewCount > 0 || video.likeCount > 0 {
                HStack(spacing: 8) {
                    if video.viewCount > 0 { Chip(text: "\(video.viewCount.formatted()) views", symbol: "eye") }
                    if video.likeCount > 0 { Chip(text: "\(video.likeCount.formatted()) likes", symbol: "hand.thumbsup") }
                }
            }
            if !video.tags.isEmpty {
                Text("YouTube tags").font(.headline).padding(.top, 6)
                FlowLayout(spacing: 6) { ForEach(video.tags, id: \.self) { Chip(text: $0, symbol: "number", tint: .zeus) } }
            }
            if !video.videoDescription.isEmpty {
                Text("Description").font(.headline).padding(.top, 6)
                Text(video.videoDescription).textSelection(.enabled).foregroundStyle(.secondary)
            }
            let comments = video.comments
            if !comments.isEmpty {
                Text("Top comments").font(.headline).padding(.top, 6)
                ForEach(Array(comments.prefix(30).enumerated()), id: \.offset) { _, comment in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(comment.author).font(.caption.weight(.semibold))
                            if comment.likes > 0 { Text("♥ \(comment.likes)").font(.caption).foregroundStyle(.secondary) }
                        }
                        Text(comment.text).font(.callout).textSelection(.enabled)
                    }
                    .padding(.vertical, 2)
                }
            }
            Text("Video ID \(video.videoID) · added \(video.addedAt.relative)\(video.eatenAt.map { " · eaten \($0.relative)" } ?? "")")
                .font(.caption).foregroundStyle(.tertiary)
            if let path = video.secondBrainPath {
                Text(path).font(.caption.monospaced()).foregroundStyle(.tertiary).textSelection(.enabled)
            }
        }
    }
}
