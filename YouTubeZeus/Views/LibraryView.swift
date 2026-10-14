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

/// What part of the library is shown (3.5): every video, the starred ones, or one YouTube channel's.
enum LibraryScope: Hashable {
    case all
    case favorites
    case channel(String)
    /// 4.5: one kind of item (Other sources in the sidebar).
    case kind(MediaKind)

    var title: String {
        switch self {
        case .all: "Videos"
        case .favorites: "Favorites"
        case .channel(let name): name.isEmpty ? "Unknown channel" : name
        case .kind(let kind):
            switch kind {
            case .youtube: "YouTube videos"
            case .podcast: "Podcast episodes"
            case .file: "Your files"
            case .social: "Posts & pages"
            }
        }
    }
}

struct LibraryView: View {
    @Environment(AppModel.self) private var app
    var scope: LibraryScope = .all
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]
    @State private var filter: LibraryFilter = .all
    @State private var confirmTrashAll = false
    @State private var hits: [SearchHit] = []
    @State private var searching = false

    private var query: String { app.librarySearch.trimmingCharacters(in: .whitespaces) }

    /// With the search index, a search shows the matching moments (full-text, ranked); without it, the old filter.
    private var usesIndex: Bool { app.indexer.isAvailable && !query.isEmpty }

    private var shown: [Video] {
        videos.filter { video in
            guard video.status != .discovered, video.status != .trashed else { return false }
            switch scope {
            case .all: break
            case .favorites: if !video.isFavorite { return false }
            case .channel(let name): if video.channelTitle != name || video.kind != .youtube { return false }
            case .kind(let kind): if video.kind != kind { return false }
            }
            switch filter {
            case .all: break
            case .captions: if !(video.source == .captions || video.source == .autoCaptions) { return false }
            case .whisper: if video.source != .whisper { return false }
            case .summarized: if video.digestData == nil { return false }
            case .failed: if video.status != .failed { return false }
            }
            guard !query.isEmpty, !usesIndex else { return true }
            return video.title.localizedStandardContains(query)
                || video.channelTitle.localizedStandardContains(query)
                || video.transcriptText.localizedStandardContains(query)
        }
    }

    /// The videos shown (summaries; full transcripts when 5 or fewer match a search), for any AI.
    private func copyForAI(_ list: [Video]) {
        let matching = usesIndex ? orderedVideos(for: hits) : list
        let packs = matching.filter { $0.status.hasText }.map(\.snapshot)
        let text = AIPack.collection(title: query.isEmpty ? "YouTube library" : "Videos about “\(query)”",
                                     kind: "YouTube Zeus library", url: "YouTube Zeus", videos: packs, missing: 0,
                                     includeTranscripts: !query.isEmpty && packs.count <= 5)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        app.show("Copied \(packs.count) videos for AI.")
    }

    var body: some View {
        @Bindable var app = app
        let list = shown
        Group {
            if usesIndex {
                SearchResultsList(hits: hits, videos: videos, searching: searching, query: query)
            } else {
                List(selection: $app.selectedVideoID) {
                    ForEach(list) { video in
                        VideoRow(video: video, highlight: query)
                            .tag(video.videoID)
                            .contextMenu { VideoMenu(video: video) }
                    }
                }
                .overlay {
                    if scope == .favorites, list.isEmpty, query.isEmpty, filter == .all {
                        EmptyState(symbol: "star", title: "No favorites yet",
                                   message: "Star a video (right-click › Add to Favorites, or the star at the top of a video) to keep it here.")
                    } else if case .kind(let kind) = scope, list.isEmpty, query.isEmpty, filter == .all {
                        EmptyState(symbol: "tray", title: "Nothing here yet",
                                   message: kind == .file ? "Drop an audio or video file on the window, or use + in the bar below."
                                                          : "Paste a link in the bar below.")
                    } else if videos.isEmpty || (list.isEmpty && query.isEmpty && filter == .all) {
                        EmptyState(symbol: "fork.knife", title: "Nothing eaten yet",
                                   message: "Paste a YouTube link, a podcast feed or drop a video or audio file on the window.")
                    } else if list.isEmpty {
                        ContentUnavailableView.search(text: query)
                    }
                }
            }
        }
        .searchable(text: $app.librarySearch, placement: .toolbar, prompt: "Search titles, transcripts, summaries")
        .task(id: query) {
            guard usesIndex else { hits = []; return }
            searching = true
            try? await Task.sleep(for: .milliseconds(180))
            guard !Task.isCancelled else { return }
            hits = await app.indexer.search(query)
            searching = false
        }
        .navigationTitle(scope.title)
        .navigationSubtitle(usesIndex ? "\(Set(hits.map(\.videoID)).count) item\(Set(hits.map(\.videoID)).count == 1 ? "" : "s") match"
                                      : "\(list.count) item\(list.count == 1 ? "" : "s")")
        .toolbar {
            // 4.5: one menu instead of four buttons.
            ToolbarItem {
                Menu {
                    Picker("Show", selection: $filter) {
                        ForEach(LibraryFilter.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .disabled(usesIndex)
                    Button("Copy for AI") { copyForAI(list) }
                    if case .channel(let name) = scope, let url = app.channelURL(forTitle: name) {
                        Divider()
                        Button("Get the Text of Every Video") { Task { await app.eatAllChannelText(name: name, url: url) } }
                            .disabled(app.isResolvingLink)
                        if app.keepsTextFiles(name) {
                            Button("Open the Full Text") { app.openChannelAllText(name) }
                        }
                    }
                    Divider()
                    Button(scope == .all ? "Move All to the Trash…" : "Move These to the Trash…", role: .destructive) {
                        confirmTrashAll = true
                    }
                    .disabled(scope == .all ? videos.allSatisfy { $0.status == .trashed } : list.isEmpty)
                } label: {
                    Label("More", systemImage: "ellipsis.circle")
                }
                .help("Show only some, copy for AI, the text of every video of a channel, move to the Trash")
            }
        }
        .confirmationDialog(scope == .all ? "Move every video of the library to the Trash?" : "Move the \(list.count) videos of “\(scope.title)” to the Trash?",
                            isPresented: $confirmTrashAll) {
            Button(scope == .all ? "Move all to the Trash" : "Move to the Trash", role: .destructive) {
                if scope == .all { app.trashLibrary() } else { app.trash(list) }
            }
        } message: {
            Text("The library starts empty. Everything can be restored from the Trash until you empty it; the notes in the Second Brain stay.")
        }
        .onDeleteCommand {
            if let id = app.selectedVideoID, let video = videos.first(where: { $0.videoID == id }) { app.trash(video) }
        }
    }

    private func orderedVideos(for hits: [SearchHit]) -> [Video] {
        var seen = Set<String>()
        return hits.compactMap { hit in
            guard seen.insert(hit.videoID).inserted else { return nil }
            return videos.first { $0.videoID == hit.videoID }
        }
    }
}

/// Full-text search results: each item with its best moments; a click opens the transcript at that moment.
struct SearchResultsList: View {
    @Environment(AppModel.self) private var app
    let hits: [SearchHit]
    let videos: [Video]
    let searching: Bool
    let query: String

    private struct Group: Identifiable {
        let id: String
        let items: [SearchHit]
    }

    var body: some View {
        let groups = grouped
        List {
            ForEach(groups) { group in
                let id = group.id
                let items = group.items
                Section {
                    ForEach(items) { hit in
                        Button {
                            app.open(video: hit.videoID, at: hit.section == "title" || hit.section == "description" ? nil : hit.start)
                        } label: {
                            HitRow(hit: hit)
                        }
                        .buttonStyle(.plain)
                    }
                } header: {
                    if let video = videos.first(where: { $0.videoID == id }) {
                        Button {
                            app.open(video: id)
                        } label: {
                            VideoRow(video: video).contentShape(.rect)
                        }
                        .buttonStyle(.plain)
                        .contextMenu { VideoMenu(video: video) }
                    } else {
                        Text(items.first?.title ?? id).font(.headline)
                    }
                }
            }
        }
        .overlay {
            if hits.isEmpty {
                if searching {
                    ProgressView()
                } else {
                    ContentUnavailableView {
                        Label("No match for “\(query)”", systemImage: "magnifyingglass")
                    } description: {
                        Text("Search looks in titles, summaries, transcripts and text read on screen. Try fewer words, a word* prefix, or OR.")
                    }
                }
            }
        }
    }

    private var grouped: [Group] {
        var order: [String] = []
        var byID: [String: [SearchHit]] = [:]
        for hit in hits {
            if byID[hit.videoID] == nil { order.append(hit.videoID) }
            byID[hit.videoID, default: []].append(hit)
        }
        return order.map { Group(id: $0, items: byID[$0] ?? []) }
    }
}

struct HitRow: View {
    let hit: SearchHit

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            if hit.section == "title" || hit.section == "description" {
                Image(systemName: hit.section == "title" ? "textformat" : "text.alignleft")
                    .foregroundStyle(.secondary).frame(width: 52, alignment: .trailing)
            } else {
                Text(hit.start.timestamp)
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(.tint)
                    .frame(width: 52, alignment: .trailing)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(highlighted).font(.callout).lineLimit(3)
                if hit.section != "transcript" {
                    Text(hit.sectionLabel).font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 2)
        .contentShape(.rect)
    }

    private var highlighted: AttributedString {
        var result = AttributedString()
        for part in hit.markedParts {
            var piece = AttributedString(part.text)
            if part.matched {
                piece.backgroundColor = Color.zeusGold.opacity(0.4)
                piece.foregroundColor = .primary
            }
            result += piece
        }
        return result
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
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    if video.isFavorite {
                        Image(systemName: "star.fill").font(.caption).foregroundStyle(.yellow).help("In your Favorites")
                    }
                    Text(video.displayTitle)
                        .font(.headline)
                        .lineLimit(2)
                }
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
                        if !video.skills.isEmpty { Image(systemName: "sparkles").help("Has skill drafts") }
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
        switch video.kind {
        case .youtube: Button("Open on YouTube") { NSWorkspace.shared.open(video.url) }
        case .podcast: Button("Open the Episode Page") { NSWorkspace.shared.open(video.url) }
        case .social: Button("Open the Post") { NSWorkspace.shared.open(video.url) }
        case .file:
            if let file = video.mediaURL, file.isFileURL { Button("Show the File in Finder") { app.reveal(file.path) } }
        }
        if video.status.hasText {
            Button("Copy for AI") { app.copyForAI(video) }
            Button("Copy Transcript") { app.copyTranscript(video) }
            Button("Copy as Markdown Note") { app.copyMarkdown(video) }
            Button("Export Markdown…") { app.exportMarkdown(video) }
            Button("Save to Second Brain") { app.saveToSecondBrain(video) }
            if let path = video.secondBrainPath {
                Button(BrainLinks.openLabel) { app.openInBrain(URL(fileURLWithPath: path)) }
                Button("Show in Finder") { app.reveal(path) }
            }
            Button("Copy Zeus Link") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(BrainLinks.zeus(video: video.videoID), forType: .string)
            }
            Divider()
            Button("Summarize") { app.summarize(video) }
            Button("Polish with Local AI") { app.engine.polishNow(video) }
            Button("Make Skills…") { app.compileSkills(video) }
            if video.kind != .podcast { Button("Read the Screen") { app.readScreen(video) } }
            if app.effectsInput(for: video.videoID) != nil {
                Button("Make an Effect…") { app.openEffects(video: video.videoID) }
            }
            Divider()
            Button("Eat Again") { app.engine.retry(video) }
            Button("Listen Again with Whisper") { app.engine.retry(video, withWhisper: true) }
        } else if video.status == .failed || video.status == .discovered || video.status == .waiting {
            Button("Eat Now") { app.engine.retry(video) }
        } else if video.status == .queued || video.status.isBusy {
            Button("Cancel") { app.engine.cancel(video.videoID) }
        }
        Divider()
        if video.status != .trashed {
            Button(video.isFavorite ? "Remove from Favorites" : "Add to Favorites") { app.toggleFavorite(video) }
        }
        if video.status == .trashed {
            Button("Restore") { app.restore([video]) }
            Button("Delete Forever", role: .destructive) { app.delete(video) }
        } else {
            Button("Move to Trash", role: .destructive) { app.trash(video) }
        }
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

    /// Several rows can be selected (⌘-click, ⇧-click) for Cancel, Retry and Delete; one row opens the video.
    @State private var selection: Set<String> = []
    @State private var confirmDelete = false

    private var selected: [Video] { videos.filter { selection.contains($0.videoID) } }

    var body: some View {
        List(selection: $selection) {
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
                // 4.5: videos no longer on YouTube are not retried (it cannot work); they can go to Deleted at once.
                let gone = failed.filter { EatFailure.isGone($0.statusDetail) }
                let retryable = failed.filter { !EatFailure.isGone($0.statusDetail) }
                let refused = failed.filter { EatFailure.isRefused($0.statusDetail) }.count
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        if refused > 0 {
                            let refusedText: String = refused == 1
                                ? "1 was refused by YouTube though it is probably online: Retry reads it other ways (without the sign-in, as Safari, as the mobile site…)."
                                : "\(refused) were refused by YouTube though they are probably online: Retry reads them other ways (without the sign-in, as Safari, as the mobile site…)."
                            Text(refusedText)
                                .font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if !gone.isEmpty {
                            Text("\(gone.count) \(gone.count == 1 ? "is" : "are") not on YouTube any more (removed, private or blocked here): retrying cannot work.")
                                .font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        HStack(spacing: 8) {
                            if !gone.isEmpty {
                                Button("Move \(gone.count == 1 ? "It" : "Them") to Deleted") { app.trash(gone) }
                                    .help("Out of the library; you can restore them from YouTube › Deleted")
                            }
                            if !retryable.isEmpty {
                                Button(gone.isEmpty ? "Retry All" : "Retry the Other \(retryable.count)") {
                                    retryable.forEach { app.engine.retry($0) }
                                }
                            }
                        }
                        .buttonStyle(.glass)
                        .controlSize(.small)
                    }
                    .padding(.vertical, 4)
                    .selectionDisabled()
                    ForEach(failed) { video in
                        VStack(alignment: .leading, spacing: 4) {
                            VideoRow(video: video)
                            Text(EatFailure.explain(video.statusDetail)).font(.caption).foregroundStyle(.red).lineLimit(3)
                                .help(video.statusDetail)
                        }
                        .tag(video.videoID)
                        .contextMenu { VideoMenu(video: video) }
                    }
                } header: {
                    Text("Failed (\(failed.count))")
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
        .navigationSubtitle(app.engine.eatingPaused ? "Paused" : (active.isEmpty ? "Idle" : "\(active.count) in progress"))
        .toolbar {
            ToolbarItemGroup {
                if !selection.isEmpty {
                    let busy = selected.filter { $0.status.isBusy || $0.status == .queued }
                    let stopped = selected.filter { $0.status == .failed || $0.status == .waiting }
                    if !busy.isEmpty {
                        Button { busy.forEach { app.engine.cancel($0.videoID) } } label: {
                            Label("Cancel \(busy.count)", systemImage: "stop.circle")
                        }
                        .help("Stop eating the selected videos")
                    }
                    if !stopped.isEmpty {
                        Button { stopped.forEach { app.engine.retry($0) } } label: {
                            Label("Retry \(stopped.count)", systemImage: "arrow.clockwise")
                        }
                    }
                    Button(role: .destructive) { confirmDelete = true } label: {
                        Label("Trash \(selection.count)", systemImage: "trash")
                    }
                    .help("Move the selected videos to the Trash (restore them from there)")
                    Button("Select none") { selection.removeAll() }
                }
                Button {
                    if app.engine.eatingPaused { app.engine.resumeEating() } else { app.engine.pauseEating() }
                } label: {
                    Label(app.engine.eatingPaused ? "Resume" : "Pause",
                          systemImage: app.engine.eatingPaused ? "play.fill" : "pause.fill")
                }
                .help(app.engine.eatingPaused ? "Start eating again" : "Pause eating: what is running waits at the front of the line")
            }
        }
        .confirmationDialog("Move \(selection.count) video\(selection.count == 1 ? "" : "s") to the Trash?",
                            isPresented: $confirmDelete) {
            Button("Move to Trash", role: .destructive) {
                app.trash(selected)
                selection.removeAll()
            }
        } message: {
            Text("Eating stops. Restore them from the Trash; the notes already written stay in the Second Brain.")
        }
        .onChange(of: selection) { _, ids in
            if ids.count == 1, let id = ids.first { app.selectedVideoID = id }
        }
        .onAppear { if let id = app.selectedVideoID { selection = [id] } }
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
                Image(systemName: "stop.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Cancel: stop eating this video")
            Button {
                app.trash(video)
            } label: {
                Image(systemName: "trash.circle.fill").foregroundStyle(.red.opacity(0.8))
            }
            .buttonStyle(.borderless)
            .help("Move to Trash: stop and take it out of the library (restore it from the Trash)")
        }
        .padding(.vertical, 4)
    }
}
