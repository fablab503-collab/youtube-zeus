import SwiftData
import SwiftUI

struct SkillsListView: View {
    @Environment(AppModel.self) private var app
    @Query(sort: \SkillDraft.createdAt, order: .reverse) private var skills: [SkillDraft]

    var body: some View {
        @Bindable var app = app
        List(selection: $app.selectedSkillID) {
            ForEach([SkillStatus.draft, .published, .rejected], id: \.self) { status in
                let items = skills.filter { $0.status == status }
                if !items.isEmpty {
                    Section(status.label) {
                        ForEach(items) { skill in
                            SkillRow(skill: skill).tag(skill.id)
                                .contextMenu {
                                    if let path = skill.publishedPath { Button("Show in Finder") { app.reveal(path) } }
                                    Button("Delete Draft", role: .destructive) {
                                        if app.selectedSkillID == skill.id { app.selectedSkillID = nil }
                                        app.context.delete(skill)
                                        try? app.context.save()
                                    }
                                }
                        }
                    }
                }
            }
        }
        .overlay {
            if skills.isEmpty {
                EmptyState(symbol: "sparkles.rectangle.stack", title: "No skills yet",
                           message: "Open an eaten video and choose “Make Codex skills”. Drafts wait here for your review.")
            }
        }
        .navigationTitle("Codex skills")
        .navigationSubtitle(app.settings.publishFolder)
    }
}

struct SkillRow: View {
    let skill: SkillDraft

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundStyle(color)
                .frame(width: 30)
            VStack(alignment: .leading, spacing: 3) {
                Text(skill.title).font(.headline).lineLimit(2)
                Text(skill.name).font(.caption.monospaced()).foregroundStyle(.secondary)
                if let video = skill.video {
                    Text(video.displayTitle).font(.caption).foregroundStyle(.tertiary).lineLimit(1)
                }
            }
        }
        .padding(.vertical, 3)
    }

    private var icon: String {
        switch skill.status {
        case .draft: "doc.badge.clock.fill"
        case .published: "checkmark.seal.fill"
        case .rejected: "xmark.seal.fill"
        }
    }

    private var color: Color {
        switch skill.status {
        case .draft: .orange
        case .published: .green
        case .rejected: .secondary
        }
    }
}

struct SkillReviewView: View {
    @Environment(AppModel.self) private var app
    @Query private var matches: [SkillDraft]
    @State private var file = "SKILL.md"
    @State private var editing = false
    @State private var editText = ""
    @State private var nameText = ""

    init(skillID: UUID) {
        _matches = Query(filter: #Predicate<SkillDraft> { $0.id == skillID })
    }

    var body: some View {
        if let skill = matches.first {
            review(skill)
        } else {
            EmptyState(symbol: "questionmark", title: "Draft not found", message: "It may have been deleted.")
        }
    }

    private func review(_ skill: SkillDraft) -> some View {
        let files = app.compiler.files(for: skill)
        let issues = app.compiler.issues(for: skill, files: files)
        let blocking = issues.filter(\.mandatory)
        return ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                // Header
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Chip(text: skill.status.label, symbol: skill.status == .published ? "checkmark.seal.fill" : "doc.badge.clock",
                             tint: skill.status == .published ? .green : (skill.status == .rejected ? .secondary : .orange))
                        Chip(text: "v\(app.compiler.nextVersion(for: skill).version)", symbol: "number")
                        if let type = skill.capability?.artifact_type { Chip(text: type, symbol: "tag") }
                        Chip(text: skill.model, symbol: "cpu")
                    }
                    Text(skill.title).font(.title2.bold())
                    HStack {
                        Text("Name").foregroundStyle(.secondary)
                        TextField("skill-name", text: $nameText)
                            .font(.body.monospaced())
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: 320)
                            .disabled(skill.status == .published)
                            .onSubmit { rename(skill) }
                        if nameText != skill.name, skill.status != .published {
                            Button("Rename") { rename(skill) }.buttonStyle(.glass)
                        }
                    }
                    if let video = skill.video {
                        Button {
                            app.selection = .library
                            app.selectedVideoID = video.videoID
                        } label: {
                            Label("From “\(video.displayTitle)”", systemImage: "play.rectangle")
                        }
                        .buttonStyle(.link)
                    }
                    if let path = skill.publishedPath {
                        Button { app.reveal(path) } label: { Label(path, systemImage: "folder") }
                            .buttonStyle(.link)
                    }
                }

                // Checks
                VStack(alignment: .leading, spacing: 8) {
                    if issues.isEmpty {
                        Label("All checks passed: structure, safety, evidence.", systemImage: "checkmark.shield.fill")
                            .foregroundStyle(.green)
                    }
                    ForEach(issues) { issue in
                        Label {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(issue.message)
                                Text("\(issue.code) · \(issue.path)\(issue.line.map { " line \($0)" } ?? "")")
                                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                        } icon: {
                            Image(systemName: issue.mandatory ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                                .foregroundStyle(issue.mandatory ? .red : .orange)
                        }
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .glassEffect(.regular, in: .rect(cornerRadius: 18))

                // Actions
                if skill.status != .published {
                    GlassEffectContainer(spacing: 10) {
                        HStack(spacing: 10) {
                            Button {
                                do {
                                    try app.compiler.publish(skill, context: app.context)
                                    app.show("Published \(skill.name) to \(app.settings.publishFolder).")
                                } catch {
                                    app.show(error.localizedDescription, error: true)
                                }
                            } label: {
                                Label("Approve and publish", systemImage: "checkmark.seal")
                            }
                            .buttonStyle(.glassProminent)
                            .disabled(!blocking.isEmpty || editing)
                            .help(blocking.isEmpty ? "Write the skill to \(app.settings.publishFolder)" : "Fix the blocking issues first")

                            Button {
                                if editing {
                                    skill.skillMDOverride = editText
                                    try? app.context.save()
                                    editing = false
                                } else {
                                    editText = files["SKILL.md"] ?? ""
                                    file = "SKILL.md"
                                    editing = true
                                }
                            } label: {
                                Label(editing ? "Save edits" : "Edit SKILL.md", systemImage: editing ? "checkmark" : "pencil")
                            }
                            .buttonStyle(.glass)

                            if skill.skillMDOverride != nil, !editing {
                                Button("Undo edits") {
                                    skill.skillMDOverride = nil
                                    try? app.context.save()
                                }
                                .buttonStyle(.glass)
                            }

                            if skill.status == .draft {
                                Button(role: .destructive) {
                                    skill.status = .rejected
                                    try? app.context.save()
                                } label: {
                                    Label("Reject", systemImage: "xmark")
                                }
                                .buttonStyle(.glass)
                            } else {
                                Button("Back to review") {
                                    skill.status = .draft
                                    try? app.context.save()
                                }
                                .buttonStyle(.glass)
                            }
                        }
                    }
                }

                // Files
                Picker("File", selection: $file) {
                    ForEach(SkillRenderer.bundlePaths, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .disabled(editing)

                Group {
                    if editing {
                        TextEditor(text: $editText)
                            .font(.system(.callout, design: .monospaced))
                            .frame(minHeight: 480)
                            .scrollContentBackground(.hidden)
                    } else {
                        Text(files[file] ?? "")
                            .font(.system(.callout, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(16)
                .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 14))
            }
            .padding(28)
            .frame(maxWidth: 900, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle(skill.name)
        .onAppear { nameText = skill.name }
    }

    private func rename(_ skill: SkillDraft) {
        let slug = SkillRenderer.slug(nameText)
        skill.name = slug
        nameText = slug
        if var text = skill.skillMDOverride,
           let range = text.range(of: #"(?m)^name: .*$"#, options: .regularExpression) {
            text.replaceSubrange(range, with: "name: \(SkillRenderer.jsonString(slug))")
            skill.skillMDOverride = text
        }
        try? app.context.save()
    }
}
