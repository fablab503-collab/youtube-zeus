import AppKit
import Foundation
import SwiftUI

/// Repo Scout in YouTube Zeus (4.4): GitHub's top repos of the day with Repo Scout's verdicts, its scanner, and a
/// message for any AI to analyze a repo.
extension AppModel {
    /// Loads Repo Scout's newest reports; without Repo Scout (or with no report from today), today's GitHub trending.
    func loadScout() async {
        scoutReports = RepoScout.loadReports()
        scoutPick = RepoScout.scoutPick().flatMap { $0.isToday ? $0 : nil } ?? scoutPick
        async let pick = RepoScout.repoOfTheDay()
        let fresh = scoutReports.first(where: { !$0.isPick && !$0.isCheck }).map { Date.now.timeIntervalSince($0.date) < 20 * 3600 } ?? false
        if !fresh, scoutTrending.isEmpty {
            do {
                scoutTrending = try await RepoScout.trending(since: "daily")
            } catch {
                scoutLog = error.localizedDescription
            }
        }
        let found = await pick
        if let found { scoutPick = found }
    }

    /// Runs Repo Scout's scanner: today's (or this week's / month's) trending repos, or the repos named.
    func scanScout(since: String = "daily", jury: Bool = true, repos: [String] = []) {
        guard RepoScout.isInstalled else {
            show(RepoScoutError.notInstalled.localizedDescription, error: true)
            return
        }
        guard scoutTask == nil else { return }
        var arguments = [RepoScout.script.path]
        arguments += repos.isEmpty ? ["--since", since] : repos
        if !jury { arguments.append("--no-jury") }
        scoutLog = repos.isEmpty ? "Reading GitHub trending (\(since))…" : "Checking \(repos.joined(separator: ", "))…"
        AppLog.write("REPO SCOUT \(arguments.dropFirst().joined(separator: " "))")
        scoutTask = Task {
            defer { scoutTask = nil }
            do {
                let output = try await ProcessRunner.run(RepoScout.python, arguments, currentDirectory: RepoScout.folder,
                                                         streamStdout: true) { line in
                    let text = line.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { return }
                    Task { @MainActor in self.scoutLog = text }
                }
                scoutReports = RepoScout.loadReports()
                let newest = scoutReports.first?.entries ?? []
                if let pick = scoutPick, let judged = newest.first(where: { $0.id == pick.entry.id }), judged.verdict != nil {
                    scoutPick?.entry.verdict = judged.verdict
                    scoutPick?.entry.reason = judged.reason
                    scoutPick?.entry.votes = judged.votes
                    scoutPick?.entry.health = judged.health ?? pick.entry.health
                }
                if output.status == 0 {
                    scoutLog = ""
                    let count = scoutReports.first?.entries.count ?? 0
                    show("Repo Scout is done: \(count) repo\(count == 1 ? "" : "s") in the new report.")
                } else if output.status != 143 {
                    scoutLog = String(output.stderr.suffix(300))
                    show("Repo Scout stopped: \(scoutLog)", error: true)
                }
            } catch {
                scoutLog = error.localizedDescription
                show("Repo Scout could not run: \(error.localizedDescription)", error: true)
            }
        }
    }

    func stopScout() {
        scoutTask?.cancel()
        scoutTask = nil
        scoutLog = "Stopped."
    }

    /// The message for any AI to analyze a repo (what it is, how it works, fit, risks, install, rebuild), copied.
    func copyRepoAnalysis(_ entry: RepoScout.Entry) {
        Task {
            show("Reading \(entry.repo)'s README…")
            async let readme = GitHubLinks.readmeExcerpt(owner: entry.owner, name: entry.name, lines: 150)
            async let check = GitHubLinks.verify(owner: entry.owner, name: entry.name, source: "Repo Scout", videoID: "", description: "")
            let needs = try? String(contentsOf: RepoScout.needs, encoding: .utf8)
            let checked = await check
            let text = RepoScout.analysisMessage(entry, readme: await readme, needs: needs, check: checked.exists ? checked : nil)
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            show("Analysis message for \(entry.repo) copied: paste it into Claude, ChatGPT, Mistral, DeepSeek…")
        }
    }

    /// Writes (or refreshes) the repo's note in Sources/GitHub, like the repos found in videos, and opens it.
    func saveRepoNote(_ entry: RepoScout.Entry) {
        Task {
            let check = await GitHubLinks.verify(owner: entry.owner, name: entry.name, source: "Repo Scout", videoID: "", description: "")
            guard check.exists else { show("\(entry.repo) was not found on GitHub.", error: true); return }
            let readme = await GitHubLinks.readmeExcerpt(owner: entry.owner, name: entry.name)
            let folder = settings.githubURL
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            _ = GitHubLinks.writeNote(check, seenIn: [], folder: folder, create: true, readme: readme)
            let note = folder.appendingPathComponent("\(check.noteName).md")
            if FileManager.default.fileExists(atPath: note.path) { openInBrain(note) }
        }
    }
}

/// Sidebar › GitHub › Today: Repo Scout's newest report (or today's trending without it).
struct RepoScoutView: View {
    @Environment(AppModel.self) private var app
    @State private var reportID: String?
    @State private var selection: String?

    var body: some View {
        let report = app.scoutReports.first { $0.id == reportID } ?? app.scoutReports.first { !$0.isPick && !$0.isCheck }
        // A report picked in Reports is always shown; the default one only while it is from the last 20 hours.
        let fresh = reportID != nil || (report.map { Date.now.timeIntervalSince($0.date) < 20 * 3600 } ?? false)
        let entries = RepoScout.ordered(fresh ? (report?.entries ?? []) : (app.scoutTrending.isEmpty ? (report?.entries ?? []) : app.scoutTrending))
        List(selection: $selection) {
            if let pick = app.scoutPick {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        ScoutRow(entry: pick.entry)
                        HStack(spacing: 8) {
                            Text(source(pick)).font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            if RepoScout.isInstalled, pick.entry.verdict == nil, app.scoutTask == nil {
                                Button("Ask the Jury") { app.scanScout(repos: [pick.entry.repo]) }
                                    .buttonStyle(.borderless).font(.caption)
                            }
                            Button("Open on GitHub") { NSWorkspace.shared.open(pick.entry.url) }
                                .buttonStyle(.borderless).font(.caption)
                        }
                    }
                    .tag("pick:" + pick.entry.id)
                    .contextMenu { menu(pick.entry) }
                } header: {
                    Label(pickTitle(pick), systemImage: "crown.fill")
                }
            }
            Section {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline) {
                        Label(RepoScout.isInstalled ? "Repo Scout" : "GitHub trending today", systemImage: "binoculars.fill")
                            .font(.headline)
                        Spacer()
                        if let report, fresh || app.scoutTrending.isEmpty {
                            Text("\(report.title) · \(report.date.formatted(date: .abbreviated, time: .shortened))")
                                .font(.caption).foregroundStyle(.secondary)
                        } else if !app.scoutTrending.isEmpty {
                            Text("github.com/trending · today").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if let report, fresh || app.scoutTrending.isEmpty, !report.jurors.isEmpty {
                        Text("Jury: " + report.jurors.joined(separator: ", ")
                             + (report.missingJurors.isEmpty ? "" : " · missing: " + report.missingJurors.joined(separator: ", ")))
                            .font(.caption).foregroundStyle(report.missingJurors.isEmpty ? Color.secondary : Color.orange)
                    }
                    if !RepoScout.isInstalled {
                        Text("Repo Scout is not on this Mac: Zeus shows github.com/trending without health scores or jury.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if !app.scoutLog.isEmpty {
                        HStack(spacing: 6) {
                            if app.scoutTask != nil { ProgressView().controlSize(.small) }
                            Text(app.scoutLog).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(2)
                        }
                    }
                }
                .padding(.vertical, 4)
            }
            Section {
                ForEach(entries) { entry in
                    ScoutRow(entry: entry)
                        .tag(entry.id)
                        .contextMenu { menu(entry) }
                }
                if entries.isEmpty {
                    Text(app.scoutTask != nil ? "Repo Scout is working…" : "No repos yet: press Scan today.")
                        .foregroundStyle(.secondary)
                }
            } header: {
                Text("\(entries.count) repos · approved first")
            }
        }
        .task { await app.loadScout() }
        .toolbar {
            ToolbarItem {
                if app.scoutTask != nil {
                    Button { app.stopScout() } label: { Label("Stop", systemImage: "stop.fill") }
                } else {
                    Menu {
                        Button("Today, with the jury") { app.scanScout(since: "daily") }
                        Button("Today, numbers only (no jury, no quota)") { app.scanScout(since: "daily", jury: false) }
                        Divider()
                        Button("This week") { app.scanScout(since: "weekly") }
                        Button("This month") { app.scanScout(since: "monthly") }
                    } label: {
                        Label("Scan today", systemImage: "binoculars")
                    } primaryAction: {
                        app.scanScout(since: "daily")
                    }
                    .disabled(!RepoScout.isInstalled)
                    .help("Run Repo Scout on today's GitHub trending: health, trust and the jury of AI models")
                }
            }
            if app.scoutReports.count > 1 {
                ToolbarItem {
                    Menu {
                        ForEach(app.scoutReports) { item in
                            Button("\(item.date.formatted(date: .abbreviated, time: .shortened)) · \(item.title) (\(item.entries.count))") { reportID = item.id }
                        }
                    } label: {
                        Label("Reports", systemImage: "clock.arrow.circlepath")
                    }
                }
            }
            ToolbarItem {
                Menu {
                    if let report { Button("Open the Report") { NSWorkspace.shared.open(FileManager.default.fileExists(atPath: report.markdown.path) ? report.markdown : report.file) } }
                    if let appURL = RepoScout.app { Button("Open Repo Scout") { NSWorkspace.shared.open(appURL) } }
                    if FileManager.default.fileExists(atPath: RepoScout.needs.path) { Button("Edit My Needs (needs.md)") { NSWorkspace.shared.open(RepoScout.needs) } }
                    Button("Reload") { Task { app.scoutTrending = []; await app.loadScout() } }
                } label: {
                    Label("Repo Scout", systemImage: "ellipsis.circle")
                }
            }
        }
    }

    private func pickTitle(_ pick: RepoScout.Pick) -> String {
        pick.isToday ? "Repo of the day" : "Repo of the day · " + pick.date.formatted(date: .abbreviated, time: .omitted)
    }

    private func source(_ pick: RepoScout.Pick) -> String {
        switch pick.source {
        case "Repo Scout": pick.entry.verdict == nil ? "Repo Scout's pick (Trendshift's #1), not judged yet" : "Repo Scout's pick: Trendshift's #1, judged by the jury"
        case "Trendshift": "Trendshift's #1 today (trendshift.io)" + (RepoScout.isInstalled ? " — not judged yet" : "")
        default: "GitHub trending's #1 today"
        }
    }

    @ViewBuilder
    private func menu(_ entry: RepoScout.Entry) -> some View {
        Button("Copy the Analysis Message for AI") { app.copyRepoAnalysis(entry) }
        Button("Open on GitHub") { NSWorkspace.shared.open(entry.url) }
        Button("Save a Note in the Second Brain") { app.saveRepoNote(entry) }
        if RepoScout.isInstalled {
            Button("Ask the Repo Scout Jury") { app.scanScout(repos: [entry.repo]) }
        }
        Button("Copy Link") {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(entry.url.absoluteString, forType: .string)
        }
    }
}

struct ScoutRow: View {
    @Environment(AppModel.self) private var app
    let entry: RepoScout.Entry

    private var verdictColor: Color {
        switch entry.verdictKind {
        case "approved": .green
        case "sandbox": .teal
        case "rejected": .secondary
        case "incomplete": .orange
        default: .indigo
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(entry.repo).font(.headline).lineLimit(1)
                if let verdict = entry.verdict {
                    Text(verdict).font(.caption2.weight(.semibold))
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(verdictColor.opacity(0.18), in: .capsule)
                        .foregroundStyle(verdictColor)
                }
                Spacer()
                if let health = entry.health {
                    Text("\(health)").font(.caption.monospacedDigit().weight(.semibold))
                        .foregroundStyle(health >= 60 ? Color.green : Color.orange)
                        .help("Repo Scout health score (pass mark 60)")
                }
            }
            if !entry.description.isEmpty {
                Text(entry.description).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            }
            if let reason = entry.reason, !reason.isEmpty {
                Text(reason).font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack(spacing: 10) {
                if let language = entry.language { Label(language, systemImage: "chevron.left.forwardslash.chevron.right") }
                if let stars = entry.stars { Label(stars.formatted(.number.notation(.compactName)), systemImage: "star") }
                if let period = entry.periodStars { Text("+\(period.formatted()) today").foregroundStyle(.orange) }
                Spacer()
                Button("Analyze") { app.copyRepoAnalysis(entry) }
                    .buttonStyle(.borderless)
                    .help("Copy a message for Claude, ChatGPT, Mistral or DeepSeek to analyze this repo: what it is, how it works, is it for you, risks, safe install, how to rebuild it")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if !entry.votes.isEmpty {
                // One line of text that wraps, so a narrow column never squeezes the jurors' names.
                Text(votesLine).font(.caption).lineLimit(3)
                    .help(votesHelp)
            }
        }
        .padding(.vertical, 4)
    }

    private var votesHelp: String {
        var lines: [String] = []
        for vote in entry.votes {
            lines.append(vote.reason.isEmpty ? "\(vote.juror): \(vote.verdict)" : "\(vote.juror): \(vote.verdict) — \(vote.reason)")
        }
        return lines.joined(separator: "\n")
    }

    /// ✓ approve, ~ sandbox, ✗ reject, one juror after the other.
    private var votesLine: AttributedString {
        var line = AttributedString()
        for (index, vote) in entry.votes.enumerated() {
            let verdict = vote.verdict.lowercased()
            let ok = verdict.hasPrefix("approve")
            let symbol = ok ? "✓" : verdict.contains("sandbox") ? "~" : "✗"
            var part = AttributedString((index == 0 ? "" : "   ") + symbol + " " + vote.juror)
            part.foregroundColor = ok ? Color.green : Color.secondary
            line.append(part)
        }
        return line
    }
}
