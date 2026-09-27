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
                case .collection(let id): CollectionView(listID: id).id(id)
                case .topic(let topic): TopicView(topic: topic).id(topic)
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
            } else if app.selection == .skills {
                if let id = app.selectedSkillID {
                    SkillReviewView(skillID: id).id(id)
                } else {
                    EmptyState(symbol: "bolt.badge.checkmark", title: "No skill selected",
                               message: "Skills made from videos wait here for your review before they go to Codex.")
                }
            } else if let id = app.selectedVideoID {
                VideoDetailView(videoID: id).id(id)
            } else {
                WelcomeView()
            }
        }
        .overlay(alignment: .top) {
            if let toast = app.toast {
                ToastView(toast: toast).id(toast.id)
            }
        }
        .animation(.spring(duration: 0.35), value: app.toast)
        .dropDestination(for: URL.self) { urls, _ in
            let links = urls.map(\.absoluteString).filter { YouTubeLink.parse($0) != nil }
            guard !links.isEmpty else { return false }
            Task { for link in links { await app.eat(link) } }
            return true
        }
        .sheet(item: $app.channelOffer) { offer in
            ChannelOfferSheet(offer: offer)
        }
        .onOpenURL { url in app.handle(url) }
        .tint(.zeus)
    }
}

struct SidebarView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Channel.title) private var channels: [Channel]
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
                        Text("Codex skills")
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
                    ForEach(lists) { list in
                        Label {
                            HStack {
                                Text(list.title).lineLimit(1)
                                Spacer()
                                Text("\(list.videoIDs.count)").foregroundStyle(.secondary).monospacedDigit()
                            }
                        } icon: { Image(systemName: list.kind.symbol) }
                        .tag(SidebarItem.collection(list.listID))
                        .contextMenu {
                            Button("Copy for AI (summaries)") { app.copyForAI(list, transcripts: false) }
                            Button("Copy for AI (everything)") { app.copyForAI(list, transcripts: true) }
                            Button("Check for New Videos") { Task { await app.refresh(list) } }
                            Divider()
                            Button("Remove Collection", role: .destructive) { app.deleteList(list) }
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
            TextField("Paste a YouTube link", text: $text)
                .textFieldStyle(.plain)
                .focused($focused)
                .onSubmit(eat)
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
                Text("Paste a YouTube link below. Zeus eats the video and keeps its text:\ncaptions when they exist, Whisper when they don't.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            GlassEffectContainer(spacing: 12) {
                HStack(spacing: 12) {
                    Feature(symbol: "captions.bubble.fill", title: "Captions & Whisper")
                    Feature(symbol: "dot.radiowaves.left.and.right", title: "Watches channels")
                    Feature(symbol: "apple.intelligence", title: "On-device summaries")
                    Feature(symbol: "sparkles.rectangle.stack.fill", title: "Codex skills")
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
                Button("Only new ones") { dismiss() }
                    .buttonStyle(.glassProminent)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 520)
    }
}
