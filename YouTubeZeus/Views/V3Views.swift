import SwiftData
import SwiftUI

// MARK: - People, tools and companies

/// Every name found in the library, most named first (the registry written after each item's names are found).
struct EntitiesListView: View {
    @Environment(AppModel.self) private var app
    @State private var query = ""
    @State private var kind: EntityKind?
    @State private var records: [EntityRecord] = []

    var body: some View {
        @Bindable var app = app
        let shown = records.filter { record in
            (kind == nil || record.kind == kind) &&
                (query.isEmpty || record.name.localizedCaseInsensitiveContains(query)
                    || record.aliases.contains { $0.localizedCaseInsensitiveContains(query) })
        }
        List(selection: $app.selectedEntity) {
            ForEach(EntityKind.allCases) { group in
                let items = shown.filter { $0.kind == group }
                if !items.isEmpty {
                    Section("\(group.plural) (\(items.count))") {
                        ForEach(items.prefix(300), id: \.name) { record in
                            HStack {
                                Image(systemName: record.kind.symbol).foregroundStyle(.secondary).frame(width: 18)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(record.name).lineLimit(1)
                                    if let about = record.about {
                                        Text(about).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                    }
                                }
                                Spacer()
                                Text("\(record.mentions.count)").foregroundStyle(.secondary).monospacedDigit()
                                if record.note != nil {
                                    Image(systemName: "brain.head.profile").font(.caption).foregroundStyle(.tertiary)
                                }
                            }
                            .tag(record.name)
                        }
                    }
                }
            }
        }
        .overlay {
            if records.isEmpty {
                EmptyState(symbol: "person.2.fill", title: "No names yet",
                           message: "After each summary, the local AI lists the people, tools and companies an item names. Items eaten before 3.0 are read in the background.")
            }
        }
        .searchable(text: $query, placement: .toolbar, prompt: "Find a name")
        .toolbar {
            ToolbarItem {
                Picker("Kind", selection: $kind) {
                    Text("All").tag(EntityKind?.none)
                    ForEach(EntityKind.allCases) { Text($0.plural).tag(EntityKind?.some($0)) }
                }
                .pickerStyle(.menu)
            }
            ToolbarItem {
                Button {
                    app.openInBrain(app.settings.secondBrainURL.appendingPathComponent(EntityNotes.indexName + ".md"))
                } label: {
                    Label("Index note", systemImage: "list.bullet.rectangle")
                }
                .help(BrainLinks.openLabel + ": People, tools and companies")
            }
        }
        .navigationTitle("People & tools")
        .navigationSubtitle("\(records.count) names · \(app.engine.entityQueue.count + (app.engine.entityCurrent == nil ? 0 : 1)) items waiting")
        .task(id: app.entityVersion) {
            records = await Task.detached { Array(EntityRegistry.load().values) }.value
                .sorted { $0.mentions.count == $1.mentions.count ? $0.name < $1.name : $0.mentions.count > $1.mentions.count }
        }
    }
}

struct EntityDetailView: View {
    @Environment(AppModel.self) private var app
    let name: String
    @State private var record: EntityRecord?

    var body: some View {
        ScrollView {
            if let record {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(spacing: 14) {
                        Image(systemName: record.kind.symbol)
                            .font(.title)
                            .foregroundStyle(.tint)
                            .frame(width: 56, height: 56)
                            .glassEffect(.regular.tint(.zeus.opacity(0.2)), in: .circle)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(record.name).font(.title2.bold())
                            Text("\(record.kind.label) · named in \(record.mentions.count) item\(record.mentions.count == 1 ? "" : "s")")
                                .foregroundStyle(.secondary)
                            if !record.aliases.isEmpty {
                                Text("Also written: " + record.aliases.prefix(6).joined(separator: ", ")).font(.caption).foregroundStyle(.tertiary)
                            }
                        }
                    }
                    if let about = record.about {
                        Text(about).font(.body).foregroundStyle(.secondary)
                    }
                    HStack {
                        if let file = EntityNotes.noteFile(record, sources: app.settings.secondBrainURL.deletingLastPathComponent()) {
                            Button { app.openInBrain(file) } label: { Label(BrainLinks.openLabel, systemImage: "brain.head.profile") }
                                .buttonStyle(.glass)
                        } else {
                            Text("A note is written once \(app.settings.entityNoteThreshold) items name it.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Button {
                            app.librarySearch = "\"\(record.name)\""
                            app.selection = .library
                        } label: { Label("Search the library", systemImage: "magnifyingglass") }
                            .buttonStyle(.glass)
                    }
                    Text("Seen in").font(.headline)
                    ForEach(record.mentions, id: \.videoID) { mention in
                        VStack(alignment: .leading, spacing: 6) {
                            Button { app.open(video: mention.videoID) } label: {
                                Text(mention.title).font(.body.weight(.medium)).multilineTextAlignment(.leading)
                            }
                            .buttonStyle(.plain)
                            if let context = mention.context?.components(separatedBy: " — ").dropFirst().first {
                                Text(context).font(.caption).foregroundStyle(.secondary)
                            }
                            if !mention.times.isEmpty {
                                HStack(spacing: 6) {
                                    ForEach(mention.times, id: \.self) { seconds in
                                        Button { app.open(video: mention.videoID, at: seconds) } label: {
                                            Text(seconds.timestamp).font(.caption.monospacedDigit().weight(.semibold))
                                        }
                                        .buttonStyle(.plain)
                                        .foregroundStyle(.tint)
                                        .padding(.horizontal, 6).padding(.vertical, 2)
                                        .background(Color.zeus.opacity(0.1), in: .capsule)
                                    }
                                }
                            }
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .glassEffect(.regular, in: .rect(cornerRadius: 14))
                    }
                }
                .padding(28)
                .frame(maxWidth: 820, alignment: .leading)
                .frame(maxWidth: .infinity)
            } else {
                EmptyState(symbol: "questionmark", title: name, message: "Not in the library any more.")
            }
        }
        .navigationTitle(name)
        .task(id: "\(name)-\(app.entityVersion)") {
            let wanted = name
            record = await Task.detached { EntityRegistry.find(wanted, in: EntityRegistry.load()) }.value
        }
    }
}

// MARK: - Weekly digests

struct DigestsListView: View {
    @Environment(AppModel.self) private var app
    @State private var keys: [String] = []
    @State private var writing = false

    var body: some View {
        @Bindable var app = app
        List(selection: $app.selectedDigest) {
            Section {
                Button {
                    writing = true
                    Task {
                        let key = WeeklyDigest.key(for: .now)
                        if await app.writeDigest(key) != nil { app.selectedDigest = key }
                        writing = false
                    }
                } label: {
                    Label(writing ? "Writing… (the local AI picks the ideas)" : "Write this week's digest now", systemImage: "square.and.pencil")
                }
                .disabled(writing)
                Text("Every \(weekdayName) at \(app.settings.digestHour):00, Zeus writes the week's digest in Sources/YouTube/Digests (Settings › Extras).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Digests") {
                ForEach(keys, id: \.self) { key in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(WeeklyDigest.title(key))
                        Text(key).font(.caption).foregroundStyle(.secondary)
                    }
                    .tag(key)
                }
            }
        }
        .overlay {
            if keys.isEmpty, !writing {
                EmptyState(symbol: "calendar", title: "No digest yet", message: "The first one is written this \(weekdayName) evening.")
            }
        }
        .navigationTitle("Weekly digests")
        .task(id: app.digestVersion) { keys = app.digestKeys() }
    }

    private var weekdayName: String {
        let names = Calendar.current.weekdaySymbols
        return names[max(0, min(names.count - 1, app.settings.digestWeekday - 1))]
    }
}

struct DigestDetailView: View {
    @Environment(AppModel.self) private var app
    let week: String
    @State private var text = ""

    var body: some View {
        let file = WeeklyDigest.fileURL(week, folder: app.settings.digestFolder)
        ScrollView {
            MarkdownNote(text: text)
                .padding(28)
                .frame(maxWidth: 820, alignment: .leading)
                .frame(maxWidth: .infinity)
        }
        .environment(\.openURL, OpenURLAction { url in
            guard url.scheme == BrainLinks.scheme else { return .systemAction }
            app.handle(url)
            return .handled
        })
        .navigationTitle(WeeklyDigest.title(week))
        .toolbar {
            ToolbarItemGroup {
                Button { app.openInBrain(file) } label: { Label(BrainLinks.openLabel, systemImage: "brain.head.profile") }
                Button {
                    Task {
                        await app.writeDigest(week)
                        text = (try? String(contentsOf: file, encoding: .utf8)) ?? text
                    }
                } label: { Label("Write again", systemImage: "arrow.clockwise") }
                    .help("Write this digest again (new items, new ideas)")
            }
        }
        .task(id: "\(week)-\(app.digestVersion)") {
            text = (try? String(contentsOf: file, encoding: .utf8)) ?? "This digest has not been written yet."
        }
    }
}

/// A light Markdown view for notes written by Zeus: headings, bullet lists, links; wiki links show their label.
struct MarkdownNote: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                switch block.kind {
                case 1: Text(inline(block.text)).font(.title.bold()).padding(.bottom, 4)
                case 2: Text(inline(block.text)).font(.title3.bold()).padding(.top, 10)
                case 3: Text(inline(block.text)).font(.headline).padding(.top, 6)
                case 4:
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("•").foregroundStyle(.tint)
                        Text(inline(block.text)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                default: Text(inline(block.text)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private var blocks: [(kind: Int, text: String)] {
        var lines = text.components(separatedBy: "\n")
        if lines.first == "---", let end = lines.dropFirst().firstIndex(of: "---") { lines = Array(lines[(end + 1)...]) }
        return lines.compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, !trimmed.hasPrefix("%%") else { return nil }
            if trimmed.hasPrefix("### ") { return (3, String(trimmed.dropFirst(4))) }
            if trimmed.hasPrefix("## ") { return (2, String(trimmed.dropFirst(3))) }
            if trimmed.hasPrefix("# ") { return (1, String(trimmed.dropFirst(2))) }
            if trimmed.hasPrefix("- ") { return (4, String(trimmed.dropFirst(2))) }
            return (0, trimmed)
        }
    }

    private func inline(_ text: String) -> AttributedString {
        let plain = text
            .replacingOccurrences(of: #"\[\[([^\]|]+)\|([^\]]+)\]\]"#, with: "$2", options: .regularExpression)
            .replacingOccurrences(of: #"\[\[([^\]]+)\]\]"#, with: "$1", options: .regularExpression)
        return (try? AttributedString(markdown: plain, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(plain)
    }
}
