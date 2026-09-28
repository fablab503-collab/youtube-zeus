import SwiftData
import SwiftUI

/// Ask a question to everything Zeus has eaten; the answer cites videos and timestamps.
struct AskView: View {
    @Environment(AppModel.self) private var app
    @State private var question = ""
    @FocusState private var focused: Bool

    var body: some View {
        @Bindable var app = app
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(spacing: 8) {
                    Image(systemName: "brain.head.profile").foregroundStyle(.tint)
                    TextField("Ask your YouTube brain…", text: $question, axis: .vertical)
                        .textFieldStyle(.plain)
                        .lineLimit(1...4)
                        .focused($focused)
                        .onSubmit(send)
                    Button(action: send) {
                        Image(systemName: "arrow.up.circle.fill").font(.title2)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.tint)
                    .disabled(question.trimmingCharacters(in: .whitespaces).isEmpty || app.isAsking)
                }
                .padding(12)
                .glassEffect(.regular.interactive(), in: .rect(cornerRadius: 18))

                if app.isAsking {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Reading \(Set(app.askPassages.map(\.videoID)).count) videos with \(app.askEngineLabel)…")
                            .foregroundStyle(.secondary)
                    }
                }
                if let error = app.askError {
                    Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                }
                if let answer = app.askAnswer {
                    Text(app.askQuestion).font(.headline)
                    Text(LocalizedStringKey(answer.answer))
                        .textSelection(.enabled)
                        .lineSpacing(3)
                    if !answer.sources.isEmpty {
                        Text("Sources").font(.headline).padding(.top, 6)
                        ForEach(Array(answer.sources.enumerated()), id: \.offset) { index, source in
                            SourceRow(number: index + 1, source: source, title: title(for: source.video_id))
                        }
                    }
                    HStack {
                        Button {
                            var text = "Q: \(app.askQuestion)\n\n\(answer.answer)\n\nSources:\n"
                            text += answer.sources.map {
                                "- \(title(for: $0.video_id)) [\(($0.seconds).timestamp)] https://www.youtube.com/watch?v=\($0.video_id)&t=\(Int($0.seconds))s — “\($0.quote)”"
                            }.joined(separator: "\n")
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(text, forType: .string)
                            app.show("Answer copied with its sources.")
                        } label: {
                            Label("Copy answer", systemImage: "doc.on.doc")
                        }
                        .buttonStyle(.glass)
                    }
                } else if !app.isAsking, app.askError == nil {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Ask anything about the videos you have eaten.").foregroundStyle(.secondary)
                        ForEach(["What are the steps to set up Claude skills?", "Which video explains Cowork best, and why?",
                                 "Quels conseils reviennent le plus souvent ?"], id: \.self) { example in
                            Button {
                                question = example
                                send()
                            } label: {
                                Label(example, systemImage: "text.bubble")
                            }
                            .buttonStyle(.plain)
                            .foregroundStyle(.tint)
                        }
                    }
                    .padding(.top, 8)
                }
            }
            .padding(24)
            .frame(maxWidth: 820, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("Ask your YouTube brain")
        .onAppear { focused = true; question = app.askQuestion }
    }

    private func send() {
        let text = question
        Task { await app.ask(text) }
    }

    private func title(for id: String) -> String {
        app.askPassages.first { $0.videoID == id }?.title ?? id
    }
}

private struct SourceRow: View {
    @Environment(AppModel.self) private var app
    let number: Int
    let source: BrainAnswer.Source
    let title: String

    var body: some View {
        Button {
            if let url = URL(string: "https://www.youtube.com/watch?v=\(source.video_id)&t=\(Int(source.seconds))s") {
                NSWorkspace.shared.open(url)
            }
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("[\(number)]").font(.caption.monospacedDigit().weight(.bold)).foregroundStyle(.secondary)
                    Text(source.seconds.timestamp).font(.caption.monospacedDigit().weight(.semibold)).foregroundStyle(.tint)
                    Text(title).font(.callout.weight(.medium)).lineLimit(1)
                }
                Text("“\(source.quote)”").font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(.regular, in: .rect(cornerRadius: 12))
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .help("Play on YouTube at \(source.seconds.timestamp)")
        .contextMenu {
            Button("Open in the Library") {
                app.selection = .library
                app.selectedVideoID = source.video_id
            }
        }
    }
}

/// Every video whose summary has this topic.
struct TopicView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]
    let topic: String

    var body: some View {
        @Bindable var app = app
        let matching = videos.filter { video in
            video.digest?.topics.contains { $0.caseInsensitiveCompare(topic) == .orderedSame } == true
        }
        List(selection: $app.selectedVideoID) {
            ForEach(matching) { video in
                VideoRow(video: video).tag(video.videoID).contextMenu { VideoMenu(video: video) }
            }
        }
        .navigationTitle(topic)
        .navigationSubtitle("\(matching.count) video\(matching.count == 1 ? "" : "s")")
        .toolbar {
            ToolbarItem {
                Button {
                    let text = AIPack.collection(title: "Topic: \(topic)", kind: "YouTube Zeus topic", url: "YouTube Zeus",
                                                 videos: matching.map(\.snapshot), missing: 0, includeTranscripts: false)
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                    app.show("Copied \(matching.count) videos about \(topic) for AI.")
                } label: {
                    Label("Copy for AI", systemImage: "sparkles")
                }
            }
        }
    }
}

/// Content column next to Ask: the videos the answer was built from.
struct AskSourcesList: View {
    @Environment(AppModel.self) private var app
    @Query private var videos: [Video]

    var body: some View {
        let ids = app.askPassages.map(\.videoID).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        let used = ids.compactMap { id in videos.first { $0.videoID == id } }
        List {
            if used.isEmpty {
                Text("The videos used for the answer will appear here.").foregroundStyle(.secondary)
            }
            ForEach(used) { video in
                Button {
                    app.selection = .library
                    app.selectedVideoID = video.videoID
                } label: {
                    VideoRow(video: video)
                }
                .buttonStyle(.plain)
            }
        }
        .navigationTitle("Read for the answer")
        .navigationSubtitle(used.isEmpty ? "" : "\(used.count) videos")
    }
}
