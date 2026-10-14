import SwiftData
import SwiftUI

/// The Eat page (4.5): paste a link and watch one thing at a time — the item being eaten now, big, with its step;
/// the rest of the line stays out of sight (a count). When an item is done, its whole output opens on the right
/// (the Everything tab: summary, playbook with research, skills, on screen, info, rebuild, transcript).
struct NowEatingView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]
    /// Open each item's output as soon as it is done.
    @AppStorage("nowFollow") private var follow = true
    @State private var selection: String?

    /// Being eaten: reading captions, downloading, listening. (Progress is read by NowCard only, so the page itself is
    /// not redrawn at every progress tick.)
    private var running: [Video] {
        videos.filter { $0.status == .fetching || $0.status == .transcribing }
    }

    private var waiting: Int { videos.filter { $0.status == .queued }.count }
    /// Premieres, live streams and just-uploaded videos: eaten later, at a channel check.
    private var later: Int { videos.filter { $0.status == .waiting }.count }
    /// Failed, not counting the ones you stopped yourself; and how many of them are not on YouTube any more.
    private var failedCounts: (all: Int, gone: Int) {
        let failed = videos.filter { $0.status == .failed && $0.statusDetail != "Cancelled" }
        return (failed.count, failed.filter { EatFailure.isGone($0.statusDetail) }.count)
    }

    /// Eaten, now being finished by the local AI (polished, summarized).
    private var finishing: [Video] { videos.filter { $0.status == .polishing || $0.status == .summarizing } }

    /// Done (or being polished and summarized), newest first.
    private var finished: [Video] {
        Array(videos.filter { $0.status.hasText && $0.eatenAt != nil }
            .sorted { ($0.eatenAt ?? .distantPast) > ($1.eatenAt ?? .distantPast) }
            .prefix(8))
    }

    var body: some View {
        let done = finished
        let latest = done.first?.videoID
        let latestAt = done.first?.eatenAt
        let eating = running
        let current = eating.first
        let waitingCount = waiting
        let failedCount = failedCounts
        let polishing = finishing
        let afterCount = app.engine.postQueue.count
        List(selection: $selection) {
            Section {
                if let current {
                    NowCard(video: current, waiting: waitingCount, alongside: eating.count - 1)
                        .tag(current.videoID)
                } else {
                    IdleCard(waiting: waitingCount, paused: app.engine.eatingPaused, resolving: app.isResolvingLink)
                }
            } header: {
                Text(current == nil ? "Eat" : "Being eaten")
            }
            // 4.5: after eating, the local AI polishes and summarizes — shown here so the page never looks idle
            // while the Mac is still working.
            if !polishing.isEmpty || afterCount > 0 {
                Section {
                    ForEach(polishing) { video in
                        FinishingRow(video: video).tag(video.videoID)
                    }
                    if afterCount > 0 {
                        Button {
                            app.selection = .eating
                        } label: {
                            Label("\(afterCount) more waiting for polish and summary", systemImage: "clock")
                        }
                        .buttonStyle(.borderless)
                        .foregroundStyle(.secondary)
                        .font(.caption)
                    }
                } header: {
                    Text("Being finished — local AI")
                }
            }
            if !done.isEmpty {
                Section {
                    ForEach(done) { video in
                        VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                    }
                } header: {
                    Text("Just eaten")
                }
            }
            if failedCount.all > 0 {
                Section {
                    Button {
                        app.selection = .eating
                    } label: {
                        Label(failedCount.gone > 0
                              ? "\(failedCount.all) could not be eaten (\(failedCount.gone) not on YouTube any more) — see why"
                              : "\(failedCount.all) could not be eaten — see why, retry",
                              systemImage: "exclamationmark.triangle")
                    }
                    .buttonStyle(.borderless)
                    .foregroundStyle(.orange)
                }
            }
        }
        .navigationTitle("Eat")
        .navigationSubtitle(subtitle(running: eating.count, waiting: waitingCount, later: later, finishing: polishing.count + afterCount))
        .toolbar {
            ToolbarItemGroup {
                Toggle(isOn: $follow) {
                    Label("Follow", systemImage: "eye")
                }
                .help("Follow: open each item's output (everything it gave) as soon as it is eaten — never while you are reading another one")
                Button {
                    if app.engine.eatingPaused { app.engine.resumeEating() } else { app.engine.pauseEating() }
                } label: {
                    Label(app.engine.eatingPaused ? "Resume" : "Pause",
                          systemImage: app.engine.eatingPaused ? "play.fill" : "pause.fill")
                }
                .help(app.engine.eatingPaused ? "Start eating again" : "Pause eating")
                Button {
                    app.selection = .eating
                } label: {
                    Label("The whole line", systemImage: "list.bullet")
                }
                .help("Every item waiting, being eaten, failed or just eaten (cancel, retry, trash several at once)")
            }
        }
        .onChange(of: selection) { _, id in
            if let id, id != app.selectedVideoID { app.selectedVideoID = id }
        }
        // Opened elsewhere (a pasted link, the menu bar, a link): the list follows.
        .onChange(of: app.selectedVideoID) { _, id in
            if selection != id { selection = id }
        }
        .onChange(of: latestAt) { old, new in
            // A newer item is done: show its output — unless a finished item is open (you are reading it).
            guard follow, let new, new > (old ?? .distantPast), let id = latest else { return }
            if let shown = app.selectedVideoID, shown != id,
               videos.first(where: { $0.videoID == shown })?.status.hasText == true { return }
            selection = id
            app.initialTab = DetailTab.everything.rawValue
            app.selectedVideoID = id
        }
        .onAppear {
            if app.selectedVideoID == nil { app.selectedVideoID = current?.videoID ?? latest }
            selection = app.selectedVideoID
        }
    }

    private func subtitle(running: Int, waiting: Int, later: Int, finishing: Int = 0) -> String {
        if app.engine.eatingPaused { return "Paused" }
        var tail = later > 0 ? " · \(later) later" : ""
        if finishing > 0 { tail += " · \(finishing) being finished" }
        if running > 0 { return (waiting > 0 ? "\(running) being eaten · \(waiting) waiting" : "\(running) being eaten") + tail }
        return (waiting > 0 ? "\(waiting) waiting" : "Paste a link below") + tail
    }
}

/// The item being eaten: big picture, title, the step it is on, the progress.
private struct NowCard: View {
    @Environment(AppModel.self) private var app
    let video: Video
    let waiting: Int
    /// Other items being eaten at the same time (Settings: several at once).
    var alongside: Int = 0

    var body: some View {
        let job = app.engine.state(for: video.videoID)
        VStack(alignment: .leading, spacing: 10) {
            Thumbnail(url: video.thumbnailURL, width: 300, radius: 14)
                .frame(maxWidth: .infinity, alignment: .center)
            Text(video.displayTitle).font(.title3.bold()).lineLimit(3)
            if !video.channelTitle.isEmpty {
                Text(video.channelTitle).font(.callout).foregroundStyle(.secondary).lineLimit(1)
            }
            HStack(spacing: 8) {
                Image(systemName: video.status.symbol)
                    .foregroundStyle(video.status.color)
                    .symbolEffect(.pulse, isActive: true)
                Text(job?.step ?? video.statusDetail).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
            if let progress = job?.progress {
                ProgressView(value: progress).tint(video.status.color)
            } else {
                ProgressView().progressViewStyle(.linear).tint(video.status.color)
            }
            HStack {
                if alongside > 0 {
                    Text("and \(alongside) more being eaten").font(.caption).foregroundStyle(.secondary)
                }
                if waiting > 0 {
                    Text("\(waiting) waiting").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Stop") { app.engine.cancel(video.videoID) }
                    .buttonStyle(.borderless)
                    .font(.caption)
                    .help("Stop eating this item (the next one starts)")
            }
        }
        .padding(.vertical, 8)
    }
}

/// An eaten item the local AI is polishing or summarizing now: small picture, title, step.
private struct FinishingRow: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        HStack(spacing: 10) {
            Thumbnail(url: video.thumbnailURL, width: 72, radius: 6)
            VStack(alignment: .leading, spacing: 3) {
                Text(video.displayTitle).font(.callout.weight(.medium)).lineLimit(2)
                HStack(spacing: 6) {
                    Image(systemName: video.status.symbol)
                        .foregroundStyle(video.status.color)
                        .symbolEffect(.pulse, isActive: true)
                    Text(app.engine.state(for: video.videoID)?.step ?? video.status.label)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
        }
        .padding(.vertical, 2)
    }
}

/// Nothing being eaten: what to paste.
private struct IdleCard: View {
    let waiting: Int
    let paused: Bool
    /// A playlist or channel link is being read (its videos are not in the line yet).
    var resolving: Bool = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(resolving ? "Reading the link…" : (paused ? "Eating is paused" : "Nothing is being eaten"),
                  systemImage: resolving ? "link" : (paused ? "pause.circle" : "bolt.fill"))
                .font(.headline)
            Text("Paste a link in the bar below: a YouTube video, a playlist or a whole channel, a podcast, a post (X, Instagram, Telegram…) or any web page. Zeus eats one item at a time and shows it here; when it is done, everything it gave opens on the right.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if waiting > 0 {
                Text("\(waiting) waiting").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 8)
    }
}
