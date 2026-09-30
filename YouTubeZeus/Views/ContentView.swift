import SwiftData
import SwiftUI

struct ContentView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        @Bindable var app = app
        NavigationSplitView {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 210, ideal: 240, max: 320)
        } content: {
            Group {
                switch app.selection {
                case .eating: QueueView()
                case .skills: SkillsListView()
                case .channel(let id): ChannelView(channelID: id).id(id)
                case .podcast(let id): ChannelView(channelID: id).id(id)
                case .collection(let id): CollectionView(listID: id).id(id)
                case .topic(let topic): TopicView(topic: topic).id(topic)
                case .entities: EntitiesListView()
                case .digests: DigestsListView()
                case .ask: AskSourcesList()
                case .github: GitHubView()
                case .browser: AccountPanel()
                case .library, nil: LibraryView()
                }
            }
            .navigationSplitViewColumnWidth(min: 340, ideal: 420, max: 620)
            .safeAreaInset(edge: .bottom, spacing: 0) { EatBar() }
        } detail: {
            if app.selection == .browser {
                BrowserScreen()
            } else if app.selection == .ask {
                AskView()
            } else if app.selection == .entities, let name = app.selectedEntity {
                EntityDetailView(name: name).id(name)
            } else if app.selection == .digests, let week = app.selectedDigest {
                DigestDetailView(week: week).id(week)
            } else if app.selection == .skills {
                if let id = app.selectedSkillID {
                    SkillReviewView(skillID: id).id(id)
                } else {
                    EmptyState(symbol: "bolt.badge.checkmark", title: "No skill selected",
                               message: "Skills made from videos wait here for your review before they are published for your AI agents.")
                }
            } else if let id = app.selectedVideoID {
                VideoDetailView(videoID: id).id(id)
                    .safeAreaInset(edge: .bottom, spacing: 0) { PlayerBar() }
            } else {
                WelcomeView()
                    .safeAreaInset(edge: .bottom, spacing: 0) { PlayerBar() }
            }
        }
        .overlay(alignment: .top) {
            if let toast = app.toast {
                ToastView(toast: toast).id(toast.id)
            }
        }
        .animation(.spring(duration: 0.35), value: app.toast)
        .dropDestination(for: URL.self) { urls, _ in
            // YouTube links, podcast feeds, and your own audio or video files.
            let files = urls.filter { $0.isFileURL && MediaFiles.isMedia($0) }
            let links = urls.filter { !$0.isFileURL }.map(\.absoluteString)
                .filter { YouTubeLink.parse($0) != nil || PodcastFeed.looksLikePodcast($0) }
            guard !files.isEmpty || !links.isEmpty else { return false }
            Task {
                if !files.isEmpty { await app.eatFiles(files) }
                for link in links { await app.eat(link) }
            }
            return true
        }
        .sheet(item: $app.channelOffer) { offer in
            ChannelOfferSheet(offer: offer)
        }
        .sheet(item: $app.podcastOffer) { offer in
            PodcastOfferSheet(offer: offer)
        }
        .alert("This video is part of a playlist", isPresented: Binding(get: { app.playlistOffer != nil },
                                                                        set: { if !$0 { app.playlistOffer = nil } }),
               presenting: app.playlistOffer) { offer in
            Button("Eat the Whole Playlist") {
                Task { await app.eatList(url: "https://www.youtube.com/playlist?list=\(offer.listID)") }
            }
            Button("Only This Video", role: .cancel) {}
        } message: { _ in
            Text("The video is being eaten. Do you also want every video of its playlist, as a collection?")
        }
        .overlay(alignment: .bottom) {
            if let progress = app.importingPlaylists {
                Label(progress, systemImage: "rectangle.stack.badge.plus")
                    .font(.callout)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .glassEffect(.regular, in: .capsule)
                    .padding(.bottom, 70)
            }
        }
        .onOpenURL { url in app.handle(url) }
        .tint(.zeus)
    }
}

struct SidebarView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Channel.title) private var allChannels: [Channel]
    private var channels: [Channel] { allChannels.filter { !$0.isPodcast } }
    private var podcasts: [Channel] { allChannels.filter(\.isPodcast) }
    @Query(filter: #Predicate<Video> { $0.statusRaw == "done" }) private var eaten: [Video]
    @Query(filter: #Predicate<SkillDraft> { $0.statusRaw == "draft" }) private var drafts: [SkillDraft]
    @Query(filter: #Predicate<Video> { $0.statusRaw == "discovered" }) private var discovered: [Video]
    @Query(sort: \VideoList.updatedAt, order: .reverse) private var lists: [VideoList]
    @Query private var allVideos: [Video]

    private var repoCount: Int {
        Set(allVideos.filter(\.status.hasText).flatMap { $0.repos.map(\.id) }).count
    }

    private var topics: [(String, Int)] {
        var counts: [String: (name: String, count: Int)] = [:]
        for video in allVideos where video.status.hasText {
            for topic in video.digest?.topics ?? [] {
                let key = topic.lowercased()
                counts[key] = (counts[key]?.name ?? topic.capitalized, (counts[key]?.count ?? 0) + 1)
            }
        }
        return counts.values.filter { $0.count > 1 || counts.count < 12 }
            .sorted { $0.count == $1.count ? $0.name < $1.name : $0.count > $1.count }
            .prefix(15).map { ($0.name, $0.count) }
    }

    var body: some View {
        @Bindable var app = app
        List(selection: $app.selection) {
            Section {
                Label {
                    HStack {
                        Text("Eating now")
                        Spacer()
                        if app.engine.activeCount > 0 {
                            Text("\(app.engine.activeCount)").foregroundStyle(.secondary).monospacedDigit()
                        }
                    }
                } icon: {
                    Image(systemName: "bolt.fill").symbolEffect(.pulse, isActive: app.engine.activeCount > 0)
                }
                .tag(SidebarItem.eating)

                Label {
                    HStack {
                        Text("Library")
                        Spacer()
                        Text("\(eaten.count)").foregroundStyle(.secondary).monospacedDigit()
                    }
                } icon: { Image(systemName: "books.vertical.fill") }
                .tag(SidebarItem.library)

                Label {
                    HStack {
                        Text("Skills")
                        Spacer()
                        if !drafts.isEmpty {
                            Text("\(drafts.count)")
                                .font(.caption.weight(.bold))
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6)
                                .background(Color.zeus, in: .capsule)
                        }
                    }
                } icon: { Image(systemName: "sparkles.rectangle.stack.fill") }
                .tag(SidebarItem.skills)

                Label {
                    HStack {
                        Text("YouTube")
                        Spacer()
                        if app.account.isSignedIn {
                            Image(systemName: "person.crop.circle.badge.checkmark").foregroundStyle(.green)
                        }
                    }
                } icon: { Image(systemName: "globe") }
                .tag(SidebarItem.browser)

                Label("Ask your brain", systemImage: "brain.head.profile")
                    .tag(SidebarItem.ask)

                Label("People & tools", systemImage: "person.2.fill")
                    .tag(SidebarItem.entities)

                Label("Weekly digests", systemImage: "calendar")
                    .tag(SidebarItem.digests)

                Label {
                    HStack {
                        Text("GitHub")
                        Spacer()
                        if repoCount > 0 {
                            Text("\(repoCount)").foregroundStyle(.secondary).monospacedDigit()
                        }
                    }
                } icon: { Image(systemName: "chevron.left.forwardslash.chevron.right") }
                .tag(SidebarItem.github)
            }

            if !lists.isEmpty {
                Section("Collections") {
                    // Channels with several collections are grouped (e.g. all the playlists of one channel).
                    let groups = Dictionary(grouping: lists, by: { ClaudePack.channelShort($0.channelTitle) })
                    let grouped = groups.filter { $0.value.count > 1 && !$0.key.isEmpty }
                    ForEach(lists.filter { grouped[ClaudePack.channelShort($0.channelTitle)] == nil }) { list in
                        CollectionRow(list: list)
                    }
                    ForEach(grouped.keys.sorted(), id: \.self) { channel in
                        DisclosureGroup {
                            ForEach(grouped[channel] ?? []) { list in CollectionRow(list: list) }
                        } label: {
                            Label {
                                HStack {
                                    Text(channel).lineLimit(1)
                                    Spacer()
                                    Text("\(grouped[channel]?.count ?? 0)").foregroundStyle(.secondary).monospacedDigit()
                                }
                            } icon: { Image(systemName: "rectangle.stack") }
                        }
                    }
                }
            }

            let topicList = topics
            if !topicList.isEmpty {
                Section("Topics") {
                    ForEach(topicList, id: \.0) { topic in
                        Label {
                            HStack {
                                Text(topic.0).lineLimit(1)
                                Spacer()
                                Text("\(topic.1)").foregroundStyle(.secondary).monospacedDigit()
                            }
                        } icon: { Image(systemName: "number") }
                        .tag(SidebarItem.topic(topic.0))
                    }
                }
            }

            if !podcasts.isEmpty {
                Section("Podcasts") {
                    ForEach(podcasts) { show in
                        Label {
                            HStack {
                                Text(show.title.isEmpty ? "Podcast" : show.title).lineLimit(1)
                                Spacer()
                                let count = discovered.filter { $0.channelID == show.channelID }.count
                                if count > 0 {
                                    Text("\(count)").font(.caption.weight(.bold)).foregroundStyle(.tint)
                                } else if !show.autoEat {
                                    Image(systemName: "pause.circle").foregroundStyle(.tertiary)
                                }
                            }
                        } icon: {
                            Avatar(url: show.avatarURL, title: show.title, size: 20)
                        }
                        .tag(SidebarItem.podcast(show.channelID))
                        .contextMenu {
                            Button("Check Now") { Task { await app.watcher.check(show) } }
                            Button("Open the Website") { NSWorkspace.shared.open(show.url) }
                            Divider()
                            Button("Unfollow", role: .destructive) {
                                if app.selection == .podcast(show.channelID) { app.selection = .library }
                                app.watcher.unfollow(show)
                            }
                        }
                    }
                }
            }

            Section {
                ForEach(channels) { channel in
                    Label {
                        HStack {
                            Text(channel.title.isEmpty ? channel.channelID : channel.title).lineLimit(1)
                            Spacer()
                            let count = discovered.filter { $0.channelID == channel.channelID }.count
                            if count > 0 {
                                Text("\(count)").font(.caption.weight(.bold)).foregroundStyle(.tint)
                            } else if !channel.autoEat {
                                Image(systemName: "pause.circle").foregroundStyle(.tertiary)
                            }
                        }
                    } icon: {
                        Avatar(url: channel.avatarURL, title: channel.title, size: 20)
                    }
                    .tag(SidebarItem.channel(channel.channelID))
                    .contextMenu {
                        Button("Check Now") { Task { await app.watcher.check(channel) } }
                        Button("Open on YouTube") { NSWorkspace.shared.open(channel.url) }
                        Divider()
                        Button("Unfollow", role: .destructive) {
                            if app.selection == .channel(channel.channelID) { app.selection = .library }
                            app.watcher.unfollow(channel)
                        }
                    }
                }
                if channels.isEmpty {
                    Text("Paste a channel link below to follow it.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } header: {
                HStack {
                    Text("Channels")
                    Spacer()
                    if app.watcher.isChecking {
                        ProgressView().controlSize(.mini)
                    } else {
                        Button {
                            Task { await app.watcher.checkAll() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .buttonStyle(.borderless)
                        .help("Check all channels now")
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .onChange(of: app.selection) { _, newValue in
            if newValue == .skills, app.selectedSkillID == nil { app.selectedSkillID = drafts.first?.id }
        }
    }
}

struct EatBar: View {
    @Environment(AppModel.self) private var app
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bolt.fill")
                .foregroundStyle(Color.zeusGold)
                .font(.title3)
            TextField("Paste a YouTube or podcast link", text: $text)
                .textFieldStyle(.plain)
                .focused($focused)
                .onSubmit(eat)
            Button {
                app.chooseFiles()
            } label: {
                Image(systemName: "doc.badge.plus")
            }
            .buttonStyle(.borderless)
            .help("Eat your own audio or video files (a call, a lecture, a voice memo), on this Mac")
            if app.isResolvingLink {
                ProgressView().controlSize(.small)
            } else if text.isEmpty, let link = app.clipboardLink {
                Button {
                    text = link
                    eat()
                } label: {
                    Image(systemName: "doc.on.clipboard")
                }
                .buttonStyle(.glass)
                .help("Eat the link on the clipboard: \(link)")
            }
            Button(action: eat) {
                Label("Eat", systemImage: "fork.knife")
                    .fontWeight(.semibold)
            }
            .buttonStyle(.glassProminent)
            .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty)
            .keyboardShortcut(.defaultAction)
        }
        .padding(.leading, 16)
        .padding(.trailing, 8)
        .padding(.vertical, 8)
        .glassEffect(.regular.interactive(), in: .capsule)
        .padding(.horizontal, 14)
        .padding(.bottom, 14)
        .padding(.top, 6)
        .onChange(of: app.focusEatBar) { _, _ in focused = true }
    }

    private func eat() {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return }
        text = ""
        Task { await app.eat(value) }
    }
}

struct WelcomeView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        VStack(spacing: 22) {
            Image(systemName: "bolt.fill")
                .font(.system(size: 64, weight: .bold))
                .foregroundStyle(LinearGradient(colors: [.zeusGold, .orange], startPoint: .top, endPoint: .bottom))
                .padding(28)
                .glassEffect(.regular.tint(.zeus.opacity(0.35)), in: .circle)
            VStack(spacing: 8) {
                Text("YouTube Zeus").font(.largeTitle.bold())
                Text("Paste a YouTube or podcast link below, or drop your own recordings on the window.\nZeus eats them and keeps their text: captions when they exist, Whisper when they don't.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            GlassEffectContainer(spacing: 12) {
                HStack(spacing: 12) {
                    Feature(symbol: "captions.bubble.fill", title: "Captions & Whisper")
                    Feature(symbol: "mic.fill", title: "Podcasts & your files")
                    Feature(symbol: "dot.radiowaves.left.and.right", title: "Watches channels")
                    Feature(symbol: "apple.intelligence", title: "On-device summaries")
                    Feature(symbol: "sparkles.rectangle.stack.fill", title: "Agent skills")
                }
            }
            if app.clipboardLink != nil {
                Button {
                    app.eatClipboard()
                } label: {
                    Label("Eat the link on the clipboard", systemImage: "doc.on.clipboard")
                        .padding(.horizontal, 6)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    struct Feature: View {
        let symbol: String
        let title: String

        var body: some View {
            VStack(spacing: 8) {
                Image(systemName: symbol).font(.title2).foregroundStyle(.tint)
                Text(title).font(.caption.weight(.medium)).multilineTextAlignment(.center)
            }
            .frame(width: 118, height: 84)
            .glassEffect(.regular, in: .rect(cornerRadius: 18))
        }
    }
}

struct CollectionRow: View {
    @Environment(AppModel.self) private var app
    let list: VideoList

    var body: some View {
        Label {
            HStack {
                Text(list.title).lineLimit(1)
                if app.hasClaudePack(list) {
                    Image(systemName: "shippingbox.fill").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                Text("\(list.videoIDs.count)").foregroundStyle(.secondary).monospacedDigit()
            }
        } icon: { Image(systemName: list.kind.symbol) }
        .tag(SidebarItem.collection(list.listID))
        .contextMenu {
            Button("Copy for AI (summaries)") { app.copyForAI(list, transcripts: false) }
            Button("Copy for AI (everything)") { app.copyForAI(list, transcripts: true) }
            Button(app.hasClaudePack(list) ? "Update the Claude Pack" : "Make a Claude Pack") { Task { await app.makeClaudePack(list) } }
            Button("Check for New Videos") { Task { await app.refresh(list) } }
            Divider()
            Button("Remove Collection", role: .destructive) { app.deleteList(list) }
        }
    }
}

struct PodcastOfferSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let offer: PodcastOffer

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Following \(offer.title)", systemImage: "mic.fill")
                .font(.title2.bold())
            Text("New episodes are eaten automatically: the transcript published with the episode when there is one, otherwise Zeus listens on this Mac. Do you also want recent episodes?")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let first = offer.episodes.first {
                Text("Latest: \(first.title)\(first.published.map { " · \($0.shortDay)" } ?? "")")
                    .font(.callout).lineLimit(2)
            }
            HStack {
                ForEach([1, 3, 10].filter { $0 <= max(1, offer.episodes.count) }, id: \.self) { count in
                    Button("Latest \(min(count, offer.episodes.count))") {
                        if let channel = app.watcher.channel(offer.channelID) {
                            app.eatEpisodes(Array(offer.episodes.prefix(count)), channel: channel, feed: offer.feed, language: offer.language)
                        }
                        dismiss()
                    }
                    .buttonStyle(.glass)
                }
                Spacer()
                Button("Only new ones") { dismiss() }
                    .buttonStyle(.glassProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 520)
    }
}

struct ChannelOfferSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let offer: ChannelOffer

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Following \(offer.title)", systemImage: "dot.radiowaves.left.and.right")
                .font(.title2.bold())
            Text("From now on, every new upload is eaten automatically. Do you also want the text of recent videos?")
                .foregroundStyle(.secondary)
            HStack {
                ForEach([3, 10, 25].filter { $0 <= max(3, offer.latest.count) }, id: \.self) { count in
                    Button("Latest \(min(count, offer.latest.count))") { app.eatLatest(count, from: offer) }
                        .buttonStyle(.glass)
                }
                Spacer()
                Button("Import playlists") {
                    let url = URL(string: "https://www.youtube.com/channel/\(offer.channelID)")!
                    Task { await app.importPlaylists(from: url) }
                    dismiss()
                }
                .buttonStyle(.glass)
                .help("Every playlist of the channel becomes a collection, ready to eat")
                Button("Only new ones") { dismiss() }
                    .buttonStyle(.glassProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 520)
    }
}
