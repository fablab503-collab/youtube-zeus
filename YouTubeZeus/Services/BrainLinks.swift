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

    static func zeus(video id: String, tab: String? = nil) -> String {
        "youtubezeus://open?video=\(encode(id))" + (tab.map { "&tab=\(encode($0))" } ?? "")
    }
    static func zeus(collection id: String) -> String { "youtubezeus://open?collection=\(encode(id))" }
    static func zeus(channel id: String) -> String { "youtubezeus://open?channel=\(encode(id))" }
    static func zeus(repo fullName: String) -> String { "youtubezeus://open?repo=\(encode(fullName))" }
    static func zeus(topic: String) -> String { "youtubezeus://open?topic=\(encode(topic))" }
    static func zeus(view: String) -> String { "youtubezeus://open?view=\(encode(view))" }
    static func zeusAsk(_ question: String) -> String { "youtubezeus://ask?q=\(encode(question))" }
    static func zeusEat(_ link: String) -> String { "youtubezeus://eat?url=\(encode(link))" }

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
        case video(String, tab: String?)
        case collection(String)
        case channel(String)
        case repo(String)
        case topic(String)
        case view(String)
        case ask(String)
    }

    static func parse(_ url: URL) -> Target? {
        guard url.scheme == scheme else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first { $0.name == name }?.value.flatMap { $0.isEmpty ? nil : $0 }
        }
        switch url.host {
        case "eat": return value("url").map { .eat($0) }
        case "ask": return value("q").map { .ask($0) }
        case "open", "show":
            if let id = value("video") { return .video(id, tab: value("tab")) }
            if let id = value("collection") { return .collection(id) }
            if let id = value("channel") { return .channel(id) }
            if let repo = value("repo") { return .repo(repo) }
            if let topic = value("topic") { return .topic(topic) }
            if let view = value("view") { return .view(view) }
            return .view("library")
        default: return nil
        }
    }
}
