import AppKit
import Foundation

/// The two-way link between YouTube Zeus and the Second Brain.
///
/// - Zeus → vault: open the exact note (video, channel index, collection index, repository, master index) in
///   Obsidian with `obsidian://open?path=…`, or in the default Markdown app when Obsidian is not installed.
/// - Vault → Zeus: every note carries `youtubezeus://open?…` links that bring the same page up in the app.
nonisolated enum BrainLinks {
    // MARK: Vault → Zeus (links written into notes)

    static let scheme = "youtubezeus"

    static func zeus(video id: String, tab: String? = nil, t seconds: Double? = nil) -> String {
        "youtubezeus://open?video=\(encode(id))" + (tab.map { "&tab=\(encode($0))" } ?? "")
            + (seconds.map { "&t=\(Int($0.rounded(.down)))" } ?? "")
    }
    static func zeus(collection id: String) -> String { "youtubezeus://open?collection=\(encode(id))" }
    static func zeus(channel id: String) -> String { "youtubezeus://open?channel=\(encode(id))" }
    static func zeus(repo fullName: String) -> String { "youtubezeus://open?repo=\(encode(fullName))" }
    static func zeus(topic: String) -> String { "youtubezeus://open?topic=\(encode(topic))" }
    static func zeus(view: String) -> String { "youtubezeus://open?view=\(encode(view))" }
    static func zeusAsk(_ question: String) -> String { "youtubezeus://ask?q=\(encode(question))" }
    static func zeusEat(_ link: String) -> String { "youtubezeus://eat?url=\(encode(link))" }
    static func zeusPack(collection id: String) -> String { "youtubezeus://pack?collection=\(encode(id))" }
    static func zeus(entity name: String) -> String { "youtubezeus://open?entity=\(encode(name))" }
    static func zeusSearch(_ query: String) -> String { "youtubezeus://search?q=\(encode(query))" }
    static func zeusPodcast(feed: String, latest: Int? = nil) -> String {
        "youtubezeus://podcast?feed=\(encode(feed))" + (latest.map { "&latest=\($0)" } ?? "")
    }
    static func zeusEatFile(_ path: String) -> String { "youtubezeus://eat?file=\(encode(path))" }
    static func zeusScreen(video id: String) -> String { "youtubezeus://screen?video=\(encode(id))" }
    static func zeusDigest(week: String) -> String { "youtubezeus://digest?week=\(encode(week))" }

    /// Markdown link to a Zeus page, for notes.
    static func markdown(_ title: String, _ link: String) -> String { "[\(title)](\(link))" }

    // MARK: Zeus → vault (open a note)

    /// `obsidian://open?path=<absolute path>`: Obsidian finds the vault that contains the file.
    static func obsidianURL(for file: URL) -> URL? {
        URL(string: "obsidian://open?path=" + encode(file.path))
    }

    @MainActor static var obsidianInstalled: Bool {
        guard let probe = URL(string: "obsidian://open") else { return false }
        return NSWorkspace.shared.urlForApplication(toOpen: probe) != nil
    }

    @MainActor static var openLabel: String { obsidianInstalled ? "Open in Obsidian" : "Open Note" }

    /// Opens a note at its exact page. Returns false when the file does not exist (yet).
    @MainActor @discardableResult
    static func open(_ file: URL) -> Bool {
        guard FileManager.default.fileExists(atPath: file.path) else { return false }
        if obsidianInstalled, let url = obsidianURL(for: file) {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.open(file)
        }
        return true
    }

    /// Percent-encodes everything except unreserved characters and "/" (safe in paths and query values).
    static func encode(_ text: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~/")
        return text.addingPercentEncoding(withAllowedCharacters: allowed) ?? text
    }

    // MARK: Parsing (youtubezeus://…)

    enum Target: Equatable, Sendable {
        case eat(String)
        case eatFile(String)
        case video(String, tab: String?, t: Double?)
        case collection(String)
        case channel(String)
        case repo(String)
        case topic(String)
        case view(String)
        case ask(String)
        case pack(String)
        case playlists(String)
        case podcast(String, latest: Int?)
        case search(String)
        case entity(String)
        case screen(String)
        case digest(String?)
    }

    static func parse(_ url: URL) -> Target? {
        guard url.scheme == scheme else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first { $0.name == name }?.value.flatMap { $0.isEmpty ? nil : $0 }
        }
        switch url.host {
        case "eat":
            if let file = value("file") { return .eatFile(file) }
            return value("url").map { .eat($0) }
        case "ask": return value("q").map { .ask($0) }
        case "pack": return value("collection").map { .pack($0) }
        case "playlists": return value("channel").map { .playlists($0) }
        case "podcast": return (value("feed") ?? value("url")).map { .podcast($0, latest: value("latest").flatMap { Int($0) }) }
        case "search": return value("q").map { .search($0) }
        case "screen": return value("video").map { .screen($0) }
        case "digest": return .digest(value("week"))
        case "open", "show":
            if let id = value("video") { return .video(id, tab: value("tab"), t: value("t").flatMap(Self.seconds)) }
            if let name = value("entity") { return .entity(name) }
            if let id = value("collection") { return .collection(id) }
            if let id = value("channel") { return .channel(id) }
            if let repo = value("repo") { return .repo(repo) }
            if let topic = value("topic") { return .topic(topic) }
            if let view = value("view") { return .view(view) }
            return .view("library")
        default: return nil
        }
    }

    /// "754", "754s", "12:34", "1:02:03", "12m34s" → seconds.
    static func seconds(_ text: String) -> Double? {
        let value = text.trimmingCharacters(in: .whitespaces).lowercased()
        if let plain = Double(value.hasSuffix("s") ? String(value.dropLast()) : value) { return max(0, plain) }
        if value.contains(":") {
            var total = 0.0
            for part in value.split(separator: ":") {
                guard let number = Double(part) else { return nil }
                total = total * 60 + number
            }
            return total
        }
        var total = 0.0, number = ""
        for character in value {
            if character.isNumber { number.append(character); continue }
            guard let amount = Double(number) else { return nil }
            switch character {
            case "h": total += amount * 3600
            case "m": total += amount * 60
            case "s": total += amount
            default: return nil
            }
            number = ""
        }
        return number.isEmpty ? total : nil
    }
}
