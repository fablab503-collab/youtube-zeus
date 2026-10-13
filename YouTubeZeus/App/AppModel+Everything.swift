import SwiftUI

/// "Everything" (4.1): one button that makes every part of an item that is still missing (summary, polished text,
/// skills, playbook, text on screen) and one page that shows them all, one after the other.
extension AppModel {
    /// What is still missing for this item, in plain words.
    func missingParts(_ video: Video) -> [String] {
        guard video.status.hasText else { return [] }
        var parts: [String] = []
        if video.digestData == nil { parts.append("summary") }
        if video.polishedData == nil, video.source != .post { parts.append("polished text") }
        if video.skills.isEmpty { parts.append("skills") }
        if video.playbook == nil, video.playbookError == nil { parts.append("playbook") }
        if canReadScreen(video), video.screenData == nil { parts.append("text on screen") }
        return parts
    }

    /// Only things with a picture can have their screen read.
    func canReadScreen(_ video: Video) -> Bool {
        switch video.kind {
        case .youtube: true
        case .file: video.mediaURL.map(MediaFiles.isVideo) ?? false
        case .podcast: video.mediaURL.map(MediaFiles.isVideo) ?? false
        case .social: video.duration > 0
        }
    }

    /// Starts everything still missing; each part runs in its own lane on this Mac (free, local).
    func doEverything(_ video: Video) {
        guard video.status.hasText else {
            show("“\(video.displayTitle)” is still being eaten: Everything starts once its text is in.", error: true)
            return
        }
        let missing = missingParts(video)
        guard !missing.isEmpty else {
            show("Everything is already made for “\(video.displayTitle)”.")
            return
        }
        if video.digestData == nil { summarize(video) }
        if video.polishedData == nil, video.source != .post { engine.polishNow(video) }
        if canReadScreen(video), video.screenData == nil { readScreen(video, quiet: true) }
        if video.playbook == nil, video.playbookError == nil { engine.schedulePlaybook(video.videoID) }
        if video.skills.isEmpty { compileSkills(video) }
        AppLog.write("EVERYTHING \(video.videoID): \(missing.joined(separator: ", "))")
        show("Making the " + Self.list(missing) + " of “\(video.displayTitle)” on this Mac. They appear here one by one.")
    }

    nonisolated static func list(_ items: [String]) -> String {
        guard items.count > 1 else { return items.first ?? "" }
        return items.dropLast().joined(separator: ", ") + " and " + items.last!
    }
}

/// The Everything tab: the button, then every part of the item on one page.
struct EverythingBody: View {
    @Environment(AppModel.self) private var app
    let video: Video
    let paragraphs: [TranscriptParagraph]
    let isPolished: Bool
    let open: (DetailTab) -> Void

    var body: some View {
        let missing = app.missingParts(video)
        VStack(alignment: .leading, spacing: 28) {
            HStack(spacing: 14) {
                Button { app.doEverything(video) } label: {
                    Label(missing.isEmpty ? "Everything is made" : "Do everything", systemImage: "bolt.fill")
                        .font(.headline)
                        .padding(.horizontal, 6)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
                .disabled(missing.isEmpty)
                Text(missing.isEmpty ? "Summary, polished text, skills, playbook and text on screen are all here."
                                     : "Still to make: " + AppModel.list(missing) + ". Free, on this Mac.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            part("Summary", .summary) { SummaryBody(video: video) }
            part("Playbook and research", .playbook) { PlaybookBody(video: video) }
            part("Skills", .skills) { SkillsBody(video: video) }
            if app.canReadScreen(video) {
                part("On screen", .screen) { ScreenBody(video: video) }
            }
            part("Info", .info) { InfoBody(video: video) }
            part("Rebuild — the message for any AI", .rebuild) { RebuildBody(video: video) }
            part(isPolished ? "Transcript (polished)" : "Transcript", .transcript) {
                TranscriptBody(video: video, paragraphs: paragraphs, find: "", isPolished: isPolished, highlight: nil)
            }
        }
    }

    private func part<Content: View>(_ title: String, _ tab: DetailTab, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(title).font(.title2.bold())
                Spacer()
                Button("Open alone") { open(tab) }
                    .buttonStyle(.borderless)
                    .font(.callout)
            }
            content()
            Divider().padding(.top, 8)
        }
    }
}
