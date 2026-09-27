import SwiftData
import SwiftUI

enum LibraryFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case captions = "Captions"
    case whisper = "Whisper"
    case summarized = "Summarized"
    case failed = "Failed"

    var id: String { rawValue }
}

struct LibraryView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]
    @State private var search = ""
    @State private var filter: LibraryFilter = .all

    private var shown: [Video] {
        videos.filter { video in
            guard video.status != .discovered else { return false }
            switch filter {
            case .all: break
            case .captions: if !(video.source == .captions || video.source == .autoCaptions) { return false }
            case .whisper: if video.source != .whisper { return false }
            case .summarized: if video.digestData == nil { return false }
            case .failed: if video.status != .failed { return false }
            }
            let query = search.trimmingCharacters(in: .whitespaces)
            guard !query.isEmpty else { return true }
            return video.title.localizedStandardContains(query)
                || video.channelTitle.localizedStandardContains(query)
                || video.transcriptText.localizedStandardContains(query)
        }
    }

    var body: some View {
        @Bindable var app = app
        let list = shown
        List(selection: $app.selectedVideoID) {
            ForEach(list) { video in
                VideoRow(video: video, highlight: search)
                    .tag(video.videoID)
                    .contextMenu { VideoMenu(video: video) }
            }
        }
        .overlay {
            if videos.isEmpty {
                EmptyState(symbol: "fork.knife", title: "Nothing eaten yet",
                           message: "Paste a YouTube link in the bar below, or drop one on the window.")
            } else if list.isEmpty {
                ContentUnavailableView.search(text: search)
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Search titles, channels and transcripts")
        .navigationTitle("Library")
        .navigationSubtitle("\(list.count) video\(list.count == 1 ? "" : "s")")
        .toolbar {
            ToolbarItem {
                Button {
                    let packs = list.filter { $0.status.hasText }.map(\.snapshot)
                    let text = AIPack.collection(title: search.isEmpty ? "YouTube library" : "Videos about “\(search)”",
                                                 kind: "YouTube Zeus library", url: "YouTube Zeus", videos: packs, missing: 0,
                                                 includeTranscripts: !search.isEmpty && packs.count <= 5)
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                    app.show("Copied \(packs.count) videos for AI.")
                } label: {
                    Label("Copy for AI", systemImage: "sparkles")
                }
                .help("Copy the videos shown (summaries; full transcripts when 5 or fewer match a search) for any AI")
            }
            ToolbarItem {
                Picker("Show", selection: $filter) {
                    ForEach(LibraryFilter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.menu)
            }
        }
        .onDeleteCommand {
            if let id = app.selectedVideoID, let video = videos.first(where: { $0.videoID == id }) { app.delete(video) }
        }
    }
}

struct VideoRow: View {
    @Environment(AppModel.self) private var app
    let video: Video
    var highlight: String = ""

    var body: some View {
        HStack(spacing: 12) {
            ZStack(alignment: .bottomTrailing) {
                Thumbnail(url: video.thumbnailURL, width: 104, radius: 9)
                if video.duration > 0 {
                    Text(video.duration.timestamp)
                        .font(.caption2.monospacedDigit().weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(.black.opacity(0.65), in: .rect(cornerRadius: 4))
                        .padding(4)
                }
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(video.displayTitle)
                    .font(.headline)
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(video.channelTitle.isEmpty ? "…" : video.channelTitle).lineLimit(1)
                    if let date = video.publishedAt {
                        Text("·")
                        Text(date.shortDay)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    if video.status == .done {
                        Image(systemName: video.source.symbol).help(video.source.label)
                        if video.polishedData != nil { Image(systemName: "wand.and.stars").help("Polished by the local AI") }
                        if video.digestData != nil { Image(systemName: "apple.intelligence").help("Summarized") }
                        if video.secondBrainPath != nil { Image(systemName: "brain.head.profile").help("In the Second Brain") }
                        if !video.skills.isEmpty { Image(systemName: "sparkles").help("Has Codex skills") }
                        Text("\(video.wordCount.formatted()) words").foregroundStyle(.tertiary)
                    } else {
                        StatusBadge(status: video.status, compact: true)
                        if let job = app.engine.state(for: video.videoID) {
                            Text(job.step).lineLimit(1).foregroundStyle(.secondary)
                        }
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if !highlight.isEmpty, let snippet = snippet() {
                    Text(snippet).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func snippet() -> AttributedString? {
        let text = video.transcriptText
        guard let range = text.range(of: highlight, options: [.caseInsensitive, .diacriticInsensitive]) else { return nil }
        let start = text.index(range.lowerBound, offsetBy: -60, limitedBy: text.startIndex) ?? text.startIndex
        let end = text.index(range.upperBound, offsetBy: 80, limitedBy: text.endIndex) ?? text.endIndex
        var result = AttributedString("…" + String(text[start..<range.lowerBound]))
        var match = AttributedString(String(text[range]))
        match.foregroundColor = .primary
        match.backgroundColor = Color.zeusGold.opacity(0.35)
        result += match
        result += AttributedString(String(text[range.upperBound..<end]) + "…")
        return result
    }
}

struct VideoMenu: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        Button("Open on YouTube") { NSWorkspace.shared.open(video.url) }
        if video.status.hasText {
            Button("Copy for AI") { app.copyForAI(video) }
            Button("Copy Transcript") { app.copyTranscript(video) }
            Button("Copy as Markdown Note") { app.copyMarkdown(video) }
            Button("Export Markdown…") { app.exportMarkdown(video) }
            Button("Save to Second Brain") { app.saveToSecondBrain(video) }
            if let path = video.secondBrainPath {
                Button("Show in Second Brain") { app.reveal(path) }
            }
            Divider()
            Button("Summarize") { app.summarize(video) }
            Button("Polish with Local AI") { app.engine.polishNow(video) }
            Button("Make Codex Skills…") { app.compileSkills(video) }
            Divider()
            Button("Eat Again") { app.engine.retry(video) }
            Button("Listen Again with Whisper") { app.engine.retry(video, withWhisper: true) }
        } else if video.status == .failed || video.status == .discovered || video.status == .waiting {
            Button("Eat Now") { app.engine.retry(video) }
        } else if video.status == .queued || video.status.isBusy {
            Button("Cancel") { app.engine.cancel(video.videoID) }
        }
        Divider()
        Button("Delete", role: .destructive) { app.delete(video) }
    }
}

struct QueueView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]

    private var active: [Video] { videos.filter { $0.status.isBusy || $0.status == .queued } }
    private var waiting: [Video] { videos.filter { $0.status == .waiting } }
    private var failed: [Video] { videos.filter { $0.status == .failed } }
    private var recent: [Video] {
        Array(videos.filter { $0.status == .done }.sorted { ($0.eatenAt ?? .distantPast) > ($1.eatenAt ?? .distantPast) }.prefix(12))
    }

    var body: some View {
        @Bindable var app = app
        List(selection: $app.selectedVideoID) {
            if !active.isEmpty {
                Section("Eating") {
                    ForEach(active) { video in
                        JobRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                    }
                }
            }
            let afterEating = app.engine.postQueue.compactMap { id in videos.first { $0.videoID == id } }
            if !afterEating.isEmpty {
                Section("Next: polish and summarize (\(afterEating.count))") {
                    ForEach(afterEating) { video in
                        VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                    }
                }
            }
            if !waiting.isEmpty {
                Section("Waiting — retried at each channel check") {
                    ForEach(waiting) { video in
                        VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                    }
                }
            }
            if !failed.isEmpty {
                Section {
                    ForEach(failed) { video in
                        VStack(alignment: .leading, spacing: 4) {
                            VideoRow(video: video)
                            Text(video.statusDetail).font(.caption).foregroundStyle(.red).lineLimit(3)
                        }
                        .tag(video.videoID)
                        .contextMenu { VideoMenu(video: video) }
                    }
                } header: {
                    HStack {
                        Text("Failed")
                        Spacer()
                        Button("Retry all") { failed.forEach { app.engine.retry($0) } }
                            .buttonStyle(.borderless)
                    }
                }
            }
            if !recent.isEmpty {
                Section("Recently eaten") {
                    ForEach(recent) { video in
                        VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                    }
                }
            }
        }
        .overlay {
            if videos.isEmpty {
                EmptyState(symbol: "bolt.fill", title: "Hungry",
                           message: "Paste a video, playlist or channel link below.")
            }
        }
        .navigationTitle("Eating now")
        .navigationSubtitle(active.isEmpty ? "Idle" : "\(active.count) in progress")
    }
}

struct JobRow: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        HStack(spacing: 12) {
            Thumbnail(url: video.thumbnailURL, width: 88, radius: 8)
            VStack(alignment: .leading, spacing: 5) {
                Text(video.displayTitle).font(.headline).lineLimit(1)
                let job = app.engine.state(for: video.videoID)
                HStack {
                    StatusBadge(status: video.status, compact: true)
                    Text(job?.step ?? (video.status == .queued ? "In line" : video.statusDetail))
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if let progress = job?.progress {
                    ProgressView(value: progress).progressViewStyle(.linear).tint(video.status.color)
                } else if video.status.isBusy {
                    ProgressView().progressViewStyle(.linear).tint(video.status.color)
                }
            }
            Button {
                app.engine.cancel(video.videoID)
            } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Cancel")
        }
        .padding(.vertical, 4)
    }
}
