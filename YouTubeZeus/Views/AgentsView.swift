import SwiftUI

/// Sidebar › Tools › Agents (4.5): what any AI agent connected to Zeus (Claude Code, Codex, Gemini CLI… with the
/// `zeus` command, or an app through `zeus mcp`) can look up and do for you now — the items with open research
/// questions, the briefing they start with, and Zeus's own checks.
struct AgentsView: View {
    @Environment(AppModel.self) private var app
    @State private var rows: [Research.BriefingItem] = []
    @State private var loaded = false
    @State private var selection: String?

    var body: some View {
        let open = rows.filter { $0.open > 0 }
        List(selection: $selection) {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    Label("Agents can research for you", systemImage: "person.2.badge.gearshape.fill")
                        .font(.headline)
                    Text("An agent on this Mac starts with `zeus agent` (an app connected through MCP: the tool `agent_briefing`). It reads the open questions of your items, searches, and saves each answer into the playbook. Copy the briefing and paste it into Claude Code or Codex to start one.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 8) {
                        Button {
                            app.copyAgentBriefing()
                        } label: {
                            Label("Copy the Briefing", systemImage: "doc.on.doc")
                        }
                        .buttonStyle(.glassProminent)
                        Button {
                            app.autoResearchRecent()
                        } label: {
                            if app.researchChecking.contains("*") {
                                ProgressView().controlSize(.small)
                            } else {
                                Label("Let Zeus Check", systemImage: "checkmark.seal")
                            }
                        }
                        .buttonStyle(.glass)
                        .disabled(app.researchChecking.contains("*"))
                        .help("Zeus checks what it can itself for these items, for free: Homebrew, npm and PyPI versions and install commands, GitHub activity, releases, advisories and open issues")
                    }
                }
                .padding(.vertical, 6)
            }
            Section {
                ForEach(open, id: \.id) { item in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.title).font(.headline).lineLimit(2)
                        Text(item.countsLine)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        ForEach(item.firstQuestions.prefix(2), id: \.self) { question in
                            Text("• " + question).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                    .padding(.vertical, 4)
                    .tag(item.id)
                }
                if loaded, open.isEmpty {
                    Text("Nothing open: the items eaten last have no question left, or nothing to check.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if !loaded {
                    ProgressView().controlSize(.small)
                }
            } header: {
                Text("Open research — the items eaten last")
            }
        }
        .navigationTitle("Agents")
        .navigationSubtitle(open.isEmpty ? "" : "\(open.count) item\(open.count == 1 ? "" : "s") with open questions")
        .task(id: app.researchVersion) {
            rows = app.agentRows()
            loaded = true
        }
        .onChange(of: selection) { _, id in
            guard let id else { return }
            app.initialTab = DetailTab.playbook.rawValue
            app.selectedVideoID = id
        }
    }
}
