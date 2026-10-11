import Foundation
import Observation
import WebKit

/// YouTube pages (3.9): up to 20 YouTube accounts side by side in Zeus's YouTube window ("Personal", "Work",
/// "Third channel"…). Each page has its own web data (its own Google sign-in) and its own cookies file for yt-dlp;
/// the page in front is the one Zeus uses for your lists, members-only and age-restricted videos.
/// The first page is the window as it was before 3.9 (the app's default web data), so nobody is signed out.
nonisolated struct YouTubePage: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var name: String
    /// Last known state of its sign-in (checked whenever the page is in front).
    var signedIn: Bool = false

    static let mainID = "default"
    var isMain: Bool { id == Self.mainID }
}

@Observable
final class YouTubePages {
    static let maxPages = 20
    nonisolated static let activeKey = "youtubeActivePage"
    private static let listKey = "youtubePages"

    private(set) var pages: [YouTubePage]
    private(set) var activeID: String

    init() {
        let defaults = UserDefaults.standard
        var list = (defaults.data(forKey: Self.listKey)).flatMap { try? JSONDecoder().decode([YouTubePage].self, from: $0) } ?? []
        if !list.contains(where: \.isMain) { list.insert(YouTubePage(id: YouTubePage.mainID, name: "Page 1"), at: 0) }
        pages = list
        let active = defaults.string(forKey: Self.activeKey) ?? YouTubePage.mainID
        activeID = list.contains { $0.id == active } ? active : YouTubePage.mainID
    }

    var active: YouTubePage { pages.first { $0.id == activeID } ?? pages[0] }
    var canAdd: Bool { pages.count < Self.maxPages }

    /// The web data of a page: the app's default one for the first page, its own store for the others.
    static func store(for id: String) -> WKWebsiteDataStore {
        guard id != YouTubePage.mainID, let uuid = UUID(uuidString: id) else { return .default() }
        return WKWebsiteDataStore(forIdentifier: uuid)
    }

    static var activeStore: WKWebsiteDataStore { store(for: activeIDNow) }

    /// The page in front, read straight from the settings (usable off the main actor, by yt-dlp's arguments).
    nonisolated static var activeIDNow: String { UserDefaults.standard.string(forKey: activeKey) ?? YouTubePage.mainID }

    /// yt-dlp's cookies file of a page (the first page keeps the name it always had).
    nonisolated static func cookieFile(for id: String) -> URL {
        AppFolders.support.appendingPathComponent(id == YouTubePage.mainID ? "youtube-cookies.txt" : "youtube-cookies-\(id).txt")
    }

    @discardableResult
    func add(named name: String? = nil) -> YouTubePage? {
        guard canAdd else { return nil }
        let page = YouTubePage(id: UUID().uuidString, name: name?.trimmingCharacters(in: .whitespaces).nilIfEmpty ?? "Page \(pages.count + 1)")
        pages.append(page)
        save()
        return page
    }

    func rename(_ id: String, to name: String) {
        guard let index = pages.firstIndex(where: { $0.id == id }) else { return }
        let clean = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return }
        pages[index].name = String(clean.prefix(40))
        save()
    }

    func select(_ id: String) {
        guard pages.contains(where: { $0.id == id }) else { return }
        activeID = id
        UserDefaults.standard.set(id, forKey: Self.activeKey)
    }

    func setSignedIn(_ signedIn: Bool) {
        guard let index = pages.firstIndex(where: { $0.id == activeID }), pages[index].signedIn != signedIn else { return }
        pages[index].signedIn = signedIn
        save()
    }

    /// Removes a page with its sign-in and its cookies (the first page cannot be removed).
    func remove(_ id: String) async {
        guard id != YouTubePage.mainID, let uuid = UUID(uuidString: id) else { return }
        if activeID == id { select(YouTubePage.mainID) }
        pages.removeAll { $0.id == id }
        save()
        try? FileManager.default.removeItem(at: Self.cookieFile(for: id))
        try? await WKWebsiteDataStore.remove(forIdentifier: uuid)
    }

    /// A page by its name ("Work") or its number ("2").
    func find(_ text: String) -> YouTubePage? {
        let value = text.trimmingCharacters(in: .whitespaces)
        if let number = Int(value), number >= 1, number <= pages.count { return pages[number - 1] }
        return pages.first { $0.name.localizedCaseInsensitiveCompare(value) == .orderedSame }
            ?? pages.first { $0.id == value }
    }

    private func save() {
        if let data = try? JSONEncoder().encode(pages) { UserDefaults.standard.set(data, forKey: Self.listKey) }
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
