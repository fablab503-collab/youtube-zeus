import SwiftData
import SwiftUI

struct ChannelView: View {
    @Environment(AppModel.self) private var app
    @Query private var channels: [Channel]
    @Query private var videos: [Video]
    @State private var checking = false

    init(channelID: String) {
        _channels = Query(filter: #Predicate<Channel> { $0.channelID == channelID })
        _videos = Query(filter: #Predicate<Video> { $0.channelID == channelID },
                        sort: [SortDescriptor(\Video.addedAt, order: .reverse)])
    }

    var body: some View {
        @Bindable var app = app
        if let channel = channels.first {
            List(selection: $app.selectedVideoID) {
                Section {
                    header(channel)
                }
                let discovered = videos.filter { $0.status == .discovered }
                if !discovered.isEmpty {
                    Section {
                        ForEach(discovered) { video in
                            VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                        }
                    } header: {
                        HStack {
                            Text("New uploads (auto-eat is off)")
                            Spacer()
                            Button("Eat all") { discovered.forEach { app.engine.retry($0) } }.buttonStyle(.borderless)
                        }
                    }
                }
                Section("In the library") {
                    ForEach(videos.filter { $0.status != .discovered }) { video in
                        VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
                    }
                }
            }
            .navigationTitle(channel.title)
            .navigationSubtitle(channel.handle)
        } else {
            EmptyState(symbol: "person.crop.circle.badge.questionmark", title: "Channel not found", message: "It may have been unfollowed.")
        }
    }

    private func header(_ channel: Channel) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 14) {
                Avatar(url: channel.avatarURL, title: channel.title, size: 56)
                VStack(alignment: .leading, spacing: 3) {
                    Text(channel.title).font(.title3.bold())
                    Text(channel.lastCheckedAt.map { "Checked \($0.relative)" } ?? "Not checked yet")
                        .font(.caption).foregroundStyle(.secondary)
                    if let error = channel.lastError {
                        Text(error).font(.caption).foregroundStyle(.red).lineLimit(2)
                    }
                }
            }
            Toggle("Eat new uploads automatically", isOn: Binding(get: { channel.autoEat }, set: {
                channel.autoEat = $0
                try? app.context.save()
            }))
            .toggleStyle(.switch)
            GlassEffectContainer(spacing: 8) {
                FlowLayout(spacing: 8) {
                    Button {
                        checking = true
                        Task {
                            let count = await app.watcher.check(channel)
                            checking = false
                            app.show(count == 0 ? "No new uploads from \(channel.title)." : "\(count) new upload\(count == 1 ? "" : "s") from \(channel.title).")
                        }
                    } label: {
                        Label(checking ? "Checking…" : "Check now", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.glass)
                    .disabled(checking)
                    Menu {
                        ForEach([3, 10, 25], id: \.self) { count in
                            Button("Latest \(count)") {
                                Task {
                                    guard let ytdlp = app.settings.makeYTDLP() else { return }
                                    let listing = try? await ytdlp.flatList(url: channel.url.absoluteString + "/videos", limit: count)
                                    let entries = (listing?.entries ?? []).map { (id: $0.id, title: $0.title, channel: channel.title, channelID: channel.channelID) }
                                    app.engine.enqueue(entries)
                                    app.show("Eating \(entries.count) videos of \(channel.title).")
                                }
                            }
                        }
                    } label: {
                        Label("Eat latest", systemImage: "fork.knife")
                    }
                    .menuStyle(.button)
                    .buttonStyle(.glass)
                    .fixedSize()
                    Button {
                        Task { await app.eatWholeChannel(channel) }
                    } label: {
                        Label("Eat whole channel", systemImage: VideoListKind.channel.symbol)
                    }
                    .buttonStyle(.glass)
                    .help("Every video of the channel, organised as a collection with its own index note")
                    Button { NSWorkspace.shared.open(channel.url) } label: { Label("YouTube", systemImage: "play.rectangle") }
                        .buttonStyle(.glass)
                }
            }
        }
        .padding(.vertical, 8)
    }
}
