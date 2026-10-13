import AppKit
import SwiftUI

/// Research inside the playbook (4.5): the open questions of a video for agents that can search, and their answers.
extension AppModel {
    func researchBrief(_ video: Video) -> ResearchBrief {
        Research.brief(video.snapshot, playbook: video.playbook)
    }

    /// Copies the brief for an agent (rules, how to save, what is known, open questions, answers so far).
    func copyResearch(_ video: Video) {
        let text = Research.agentMarkdown(researchBrief(video))
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        show("Research questions copied: paste them to Claude, ChatGPT, Mistral or any agent that can search.")
    }

    /// Saves an answer typed in the app, then refreshes the playbook note.
    func addResearch(_ video: Video, question: String, answer: String, sources: [String]) {
        let finding = ResearchFinding(question: question, answer: answer, sources: sources, by: "Daniel")
        do {
            try Research.add(finding, to: video.videoID, title: video.displayTitle)
            researchVersion += 1
            if video.playbook != nil { _ = exporter.exportPlaybook(video) }
            show("Answer saved in the playbook.")
        } catch {
            show("Could not save the answer: \(error.localizedDescription)", error: true)
        }
    }

    func removeResearch(_ video: Video, _ finding: ResearchFinding) {
        Research.remove(finding.id, from: video.videoID, title: video.displayTitle)
        researchVersion += 1
        if video.playbook != nil { _ = exporter.exportPlaybook(video) }
    }
}

/// The Research card at the end of the playbook: answers found, open questions with their searches.
struct PlaybookResearchCard: View {
    @Environment(AppModel.self) private var app
    let video: Video
    @State private var brief: ResearchBrief?
    @State private var answering: ResearchQuestion?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let brief {
                header(brief)
                if !brief.findings.isEmpty {
                    ForEach(brief.findings.sorted { $0.at < $1.at }) { finding in
                        FindingRow(finding: finding)
                            .contextMenu {
                                Button("Remove This Answer", role: .destructive) { app.removeResearch(video, finding) }
                            }
                    }
                }
                let open = brief.open
                if !open.isEmpty {
                    Text("Open questions").font(.subheadline.weight(.semibold)).padding(.top, 4)
                    ForEach(open) { question in
                        QuestionRow(question: question) { answering = question }
                    }
                }
            } else {
                ProgressView().controlSize(.small)
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: .rect(cornerRadius: 22))
        .task(id: "\(video.videoID)-\(app.researchVersion)-\(video.playbook?.madeAt.timeIntervalSince1970 ?? 0)") {
            brief = app.researchBrief(video)
        }
        .sheet(item: $answering) { question in
            ResearchAnswerSheet(video: video, question: question)
        }
    }

    private func header(_ brief: ResearchBrief) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Label("Research", systemImage: "magnifyingglass.circle.fill").font(.headline)
                Text("\(brief.findings.count) answered · \(brief.open.count) open")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button {
                    app.copyResearch(video)
                } label: {
                    Label("Copy for an Agent", systemImage: "doc.on.doc")
                }
                .buttonStyle(.glass)
                .controlSize(.small)
                .help("Copy the open questions with their searches, and how to save the answers (zeus research … --add)")
                Button {
                    self.brief = app.researchBrief(video)
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Reload: show answers saved by agents since")
            }
            Text("What to check before you trust the steps. An agent that can search (Claude Code, Codex…) answers them with `zeus research \(video.videoID) --add …`; the answers appear here and in the playbook note.")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if !brief.known.isEmpty {
                DisclosureGroup("What Zeus already knows (\(brief.known.count))") {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(brief.known, id: \.self) { item in
                            Text("• " + item).font(.caption).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(.top, 4)
                }
                .font(.caption)
            }
        }
    }

    private struct FindingRow: View {
        let finding: ResearchFinding

        var body: some View {
            VStack(alignment: .leading, spacing: 4) {
                Label(finding.question, systemImage: "checkmark.circle.fill")
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.green)
                Text(finding.answer).font(.callout).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                HStack(spacing: 8) {
                    ForEach(finding.sources.prefix(4), id: \.self) { source in
                        if let url = URL(string: source), source.hasPrefix("http") {
                            Link(url.host ?? source, destination: url).font(.caption)
                        } else {
                            Text(source).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    Text("\(finding.by) · \(finding.at.formatted(date: .abbreviated, time: .omitted))")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.green.opacity(0.06), in: .rect(cornerRadius: 12))
        }
    }

    private struct QuestionRow: View {
        let question: ResearchQuestion
        let answer: () -> Void

        var body: some View {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: symbol).foregroundStyle(.secondary).frame(width: 18)
                VStack(alignment: .leading, spacing: 3) {
                    Text(question.question).font(.callout).fixedSize(horizontal: false, vertical: true)
                    if let known = question.known, !known.isEmpty {
                        Text(known).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                    HStack(spacing: 6) {
                        ForEach(question.queries, id: \.self) { query in
                            Button(query) { Self.search(query) }
                                .buttonStyle(.borderless)
                                .font(.caption)
                                .lineLimit(1)
                                .help("Search the web for this")
                        }
                    }
                }
                Spacer(minLength: 6)
                Button("Answer…", action: answer)
                    .buttonStyle(.borderless)
                    .font(.caption)
            }
        }

        private var symbol: String {
            switch question.topic {
            case "tool": "wrench.and.screwdriver"
            case "repo": "chevron.left.forwardslash.chevron.right"
            case "step": "list.number"
            case "claim": "quote.bubble"
            default: "link"
            }
        }

        static func search(_ query: String) {
            if query.hasPrefix("http"), let url = URL(string: query) {
                NSWorkspace.shared.open(url)
                return
            }
            var components = URLComponents(string: "https://duckduckgo.com/")!
            components.queryItems = [URLQueryItem(name: "q", value: query)]
            if let url = components.url { NSWorkspace.shared.open(url) }
        }
    }
}

/// Typing an answer yourself (agents use `zeus research --add` or the MCP tool `add_research`).
struct ResearchAnswerSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let video: Video
    let question: ResearchQuestion
    @State private var answer = ""
    @State private var source = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Answer").font(.title3.bold())
            Text(question.question).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $answer)
                .font(.body)
                .frame(minHeight: 110)
                .scrollContentBackground(.hidden)
                .padding(8)
                .background(.quaternary.opacity(0.4), in: .rect(cornerRadius: 10))
            TextField("Source (a link, optional)", text: $source)
                .textFieldStyle(.roundedBorder)
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Save") {
                    let trimmed = source.trimmingCharacters(in: .whitespacesAndNewlines)
                    app.addResearch(video, question: question.question,
                                    answer: answer.trimmingCharacters(in: .whitespacesAndNewlines),
                                    sources: trimmed.isEmpty ? [] : [trimmed])
                    dismiss()
                }
                .buttonStyle(.glassProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(22)
        .frame(width: 520)
    }
}
