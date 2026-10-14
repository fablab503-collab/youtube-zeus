import SwiftData
import SwiftUI

/// A playlist, a whole channel, Watch Later or Liked videos, in the original order.
struct CollectionView: View {
    @Environment(AppModel.self) private var app
    @Query private var lists: [VideoList]
    @Query private var videos: [Video]
    @State private var refreshing = false

    init(listID: String) {
        _lists = Query(filter: #Predicate<VideoList> { $0.listID == listID })
    }

    var body: some View {
        @Bindable var app = app
        if let list = lists.first {
            let byID = Dictionary(videos.filter { $0.status != .trashed }.map { ($0.videoID, $0) }, uniquingKeysWith: { first, _ in first })
            let eaten = list.videoIDs.filter { byID[$0]?.status.hasText == true }.count
            List(selection: $app.selectedVideoID) {
                Section {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(spacing: 12) {
                            Image(systemName: list.kind.symbol)
                                .font(.title)
                                .foregroundStyle(.white)
                                .frame(width: 52, height: 52)
                                .background(Color.zeus.gradient, in: .rect(cornerRadius: 14))
                            VStack(alignment: .leading, spacing: 3) {
                                Text(list.title).font(.title3.bold()).lineLimit(2)
                                Text("\(list.kind.label) · \(eaten) of \(list.videoIDs.count) eaten")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        ProgressView(value: Double(eaten), total: Double(max(1, list.videoIDs.count)))
                            .tint(.zeus)
                        if let pack = app.packResults[list.listID] {
                            Label("Claude pack: skill “\(pack.skillName)” · \(pack.videos) videos, \(pack.summarized) summarized · digest ≈ \(pack.digestTokens / 1000)k tokens · \(pack.parts) transcript part\(pack.parts == 1 ? "" : "s")",
                                  systemImage: "shippingbox.fill")
                                .font(.caption).foregroundStyle(.secondary)
                        } else if app.hasClaudePack(list) {
                            Label("Claude pack installed — it follows this collection as videos are eaten and summarized.", systemImage: "shippingbox.fill")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        // 4.5: the main action (eat what is missing, else copy for AI) and one More menu.
                        GlassEffectContainer(spacing: 8) {
                            FlowLayout(spacing: 8) {
                                if eaten < list.videoIDs.count {
                                    Button {
                                        let missing = list.videoIDs.filter { byID[$0]?.status.hasText != true }
                                        app.engine.enqueue(missing.map { (id: $0, title: byID[$0]?.title ?? "", channel: "", channelID: "") }, force: true)
                                        app.selection = .now
                                    } label: {
                                        Label("Eat the missing \(list.videoIDs.count - eaten)", systemImage: "fork.knife")
                                    }
                                    .buttonStyle(.glassProminent)
                                }
                                Menu {
                                    Button("Summaries and key points") { app.copyForAI(list, transcripts: false) }
                                    Button("Everything, with transcripts") { app.copyForAI(list, transcripts: true) }
                                    Divider()
                                    Button("Save the full AI pack as a file…") { app.exportPack(list) }
                                } label: {
                                    Label("Copy for AI", systemImage: "sparkles")
                                }
                                .menuStyle(.button)
                                .buttonStyle(.glass)
                                .fixedSize()
                                Menu {
                                    Button(refreshing ? "Checking…" : "Check for New Videos") {
                                        refreshing = true
                                        Task {
                                            await app.refresh(list)
                                            refreshing = false
                                        }
                                    }
                                    .disabled(refreshing)
                                    Divider()
                                    Menu(app.buildingPacks.contains(list.listID) ? "Claude Pack (packing…)" : "Claude Pack") {
                                        Button(app.hasClaudePack(list) ? "Update the Claude Pack" : "Make a Claude Pack") {
                                            Task { await app.makeClaudePack(list) }
                                        }
                                        if app.hasClaudePack(list) {
                                            Button("Copy the Digest Prompt") { app.copyDigest(list) }
                                            Button("Open the Pack in the Second Brain") {
                                                app.openInBrain(app.packVaultFolder(list).appendingPathComponent("README.md"))
                                            }
                                            Button("Show the Prompt Files") {
                                                NSWorkspace.shared.activateFileViewerSelecting([app.packLocalPrompts(list).appendingPathComponent("digest.md")])
                                            }
                                            Button("Show the Skill Zip for claude.ai") {
                                                NSWorkspace.shared.activateFileViewerSelecting([app.packZipURL(list)])
                                            }
                                        }
                                    }
                                    Divider()
                                    if let path = list.indexPath {
                                        Button(BrainLinks.openLabel) { app.openInBrain(URL(fileURLWithPath: path)) }
                                    }
                                    if let url = list.url {
                                        Button("Open on YouTube") { NSWorkspace.shared.open(url) }
                                    }
                                } label: {
                                    Label("More", systemImage: "ellipsis.circle")
                                }
                                .menuStyle(.button)
                                .buttonStyle(.glass)
                                .fixedSize()
                                .help("Check for new videos, the Claude pack (a skill, a digest, the transcripts in parts), the index note, YouTube")
                            }
                        }
                    }
                    .padding(.vertical, 8)
                }
                Section("Videos") {
                    ForEach(Array(list.videoIDs.enumerated()), id: \.element) { index, id in
                        if let video = byID[id] {
                            HStack(alignment: .top, spacing: 8) {
                                Text("\(index + 1)").font(.caption.monospacedDigit()).foregroundStyle(.tertiary).frame(width: 26, alignment: .trailing)
                                VideoRow(video: video)
                            }
                            .tag(video.videoID)
                            .contextMenu { VideoMenu(video: video) }
                        }
                    }
                }
            }
            .navigationTitle(list.title)
            .navigationSubtitle(list.kind.label)
        } else {
            EmptyState(symbol: "questionmark.folder", title: "Collection not found", message: "It may have been removed.")
        }
    }
}
