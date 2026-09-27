import SwiftData
import SwiftUI

extension RepoCheck {
    var tint: Color {
        switch verdict {
        case "active": criticalAdvisories > 0 ? .orange : .green
        case "new", "stale", "fork", "unchecked": .orange
        case "archived": .secondary
        default: .red
        }
    }
}

/// One checked repository: verdict, numbers, warnings, links.
struct RepoCard: View {
    @Environment(AppModel.self) private var app
    let repo: RepoCheck

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: repo.verdictSymbol).foregroundStyle(repo.tint)
                Text(repo.fullName).font(.headline).textSelection(.enabled)
                Spacer()
                Button { NSWorkspace.shared.open(repo.url) } label: {
                    Label("GitHub", systemImage: "arrow.up.right.square")
                }
                .buttonStyle(.glass)
                .controlSize(.small)
                if let note = app.repoNoteURL(repo) {
                    Button { NSWorkspace.shared.open(note) } label: {
                        Label("Note", systemImage: "doc.text")
                    }
                    .buttonStyle(.glass)
                    .controlSize(.small)
                    .help(note.path)
                }
            }
            if let description = repo.description {
                Text(description).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
            }
            FlowLayout(spacing: 6) {
                Chip(text: repo.verdictLabel, symbol: repo.verdictSymbol, tint: repo.tint)
                if repo.exists {
                    Chip(text: "\(repo.stars.formatted(.number.notation(.compactName))) stars", symbol: "star")
                    Chip(text: repo.license ?? "No license", symbol: "doc.plaintext")
                    if !repo.pushedDay.isEmpty { Chip(text: "Last push \(repo.pushedDay)", symbol: "clock") }
                    if let release = repo.latestRelease { Chip(text: release, symbol: "tag") }
                    if repo.linkedBack { Chip(text: "Links back to the video", symbol: "link", tint: .green) }
                    if repo.advisories > 0 {
                        Chip(text: "\(repo.advisories) security advisories\(repo.criticalAdvisories > 0 ? ", \(repo.criticalAdvisories) critical" : "")",
                             symbol: "exclamationmark.shield", tint: repo.criticalAdvisories > 0 ? .orange : .secondary)
                    }
                    if let since = repo.ownerSince {
                        Chip(text: "\(repo.ownerType ?? "Account") since \(since.prefix(4))", symbol: "person")
                    }
                }
                Chip(text: "Found in the \(repo.foundIn)", symbol: "text.magnifyingglass")
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.35), in: .rect(cornerRadius: 12))
    }
}

/// Compact repository line for the GitHub list (the videos that link it follow).
struct RepoRow: View {
    @Environment(AppModel.self) private var app
    let repo: RepoCheck

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: repo.verdictSymbol)
                .font(.title3)
                .foregroundStyle(repo.tint)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                Text(repo.fullName).font(.headline).lineLimit(1)
                Text(([repo.verdictLabel]
                      + (repo.exists ? ["\(repo.stars.formatted(.number.notation(.compactName))) stars", repo.license ?? "no license",
                                        repo.pushedDay.isEmpty ? "" : "pushed \(repo.pushedDay)"] : []))
                        .filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                if repo.advisories > 0 {
                    Label("\(repo.advisories) security advisories\(repo.criticalAdvisories > 0 ? ", \(repo.criticalAdvisories) critical" : "")",
                          systemImage: "exclamationmark.shield")
                        .font(.caption).foregroundStyle(repo.criticalAdvisories > 0 ? .orange : .secondary)
                }
            }
            Spacer(minLength: 4)
            Button { NSWorkspace.shared.open(repo.url) } label: {
                Image(systemName: "arrow.up.right.square")
            }
            .buttonStyle(.borderless)
            .help("Open on GitHub")
        }
    }
}

/// GitHub section of a video's Info tab.
struct VideoGitHubSection: View {
    @Environment(AppModel.self) private var app
    let video: Video

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("GitHub").font(.headline)
                Spacer()
                if app.engine.isCheckingGitHub(video.videoID) {
                    ProgressView().controlSize(.small)
                    Text("Checking…").font(.caption).foregroundStyle(.secondary)
                } else {
                    Button {
                        app.engine.recheckGitHub(video)
                    } label: {
                        Label(video.githubCheckedAt == nil ? "Find GitHub links" : "Check again", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.glass)
                    .controlSize(.small)
                }
            }
            if video.repos.isEmpty {
                Text(video.githubCheckedAt == nil
                     ? "GitHub links in the description are checked automatically after eating."
                     : "No GitHub repository linked in this video.")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                ForEach(video.repos) { RepoCard(repo: $0) }
                Text("Checked through the GitHub API\(video.githubCheckedAt.map { " \($0.relative)" } ?? ""). A link in a video is not a security review: read the code before running it.")
                    .font(.caption).foregroundStyle(.tertiary)
            }
        }
    }
}

private struct RepoEntry: Identifiable {
    var repo: RepoCheck
    var videos: [Video]
    var id: String { repo.id }
}

/// Sidebar › GitHub: every repository linked in eaten videos, with the videos that mention it.
struct GitHubView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \Video.addedAt, order: .reverse) private var videos: [Video]

    private var entries: [RepoEntry] {
        var byID: [String: RepoEntry] = [:]
        for video in videos where video.status.hasText {
            for repo in video.repos {
                if var entry = byID[repo.id] {
                    entry.videos.append(video)
                    if repo.checkedAt > entry.repo.checkedAt { entry.repo = repo }
                    byID[repo.id] = entry
                } else {
                    byID[repo.id] = RepoEntry(repo: repo, videos: [video])
                }
            }
        }
        return byID.values.sorted {
            $0.videos.count == $1.videos.count ? $0.repo.stars > $1.repo.stars : $0.videos.count > $1.videos.count
        }
    }

    var body: some View {
        @Bindable var app = app
        let entries = entries
        let pending = videos.filter { $0.status.hasText && $0.githubCheckedAt == nil }.count
        List(selection: $app.selectedVideoID) {
            if entries.isEmpty {
                Text(pending > 0
                     ? "Checking the descriptions of \(pending) videos for GitHub links…"
                     : "No GitHub repository linked in the videos eaten so far.")
                    .foregroundStyle(.secondary)
            }
            ForEach(entries) { entry in
                RepoRow(repo: entry.repo)
                    .padding(.top, 6)
                    .contextMenu {
                        Button("Open on GitHub") { NSWorkspace.shared.open(entry.repo.url) }
                        if let note = app.repoNoteURL(entry.repo) {
                            Button("Open Note") { NSWorkspace.shared.open(note) }
                        }
                        Button("Copy Link") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(entry.repo.url.absoluteString, forType: .string)
                        }
                    }
                ForEach(entry.videos) { video in
                    VideoRow(video: video)
                        .padding(.leading, 22)
                        .tag(video.videoID)
                        .contextMenu { VideoMenu(video: video) }
                }
            }
        }
        .navigationTitle("GitHub")
        .navigationSubtitle("\(entries.count) repositor\(entries.count == 1 ? "y" : "ies")\(pending > 0 ? " · \(pending) videos to check" : "")")
        .toolbar {
            ToolbarItem {
                Button {
                    app.copyGitHubForAI(entries.map { ($0.repo, $0.videos.map(\.snapshot)) })
                } label: {
                    Label("Copy for AI", systemImage: "sparkles")
                }
                .disabled(entries.isEmpty)
            }
            ToolbarItem {
                Button {
                    GitHubLinks.clearCache()
                    for video in videos where video.status.hasText { app.engine.scheduleGitHub(video.videoID, force: true) }
                    app.show("Checking every video's GitHub links again.")
                } label: {
                    Label("Check All Again", systemImage: "arrow.clockwise")
                }
            }
        }
    }
}
