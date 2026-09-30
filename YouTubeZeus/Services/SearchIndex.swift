import CryptoKit
import Foundation
import SQLite3

/// One match of a search: a paragraph (or summary line, on-screen text, title…) of an eaten item, with its moment.
nonisolated struct SearchHit: Codable, Hashable, Sendable, Identifiable {
    let videoID: String
    let title: String
    let channel: String
    let start: Double
    /// transcript | summary | point | screen | title | description
    let section: String
    /// The matching text with the matched words between « and ».
    let snippet: String
    let score: Double
    /// The whole row (paragraph, key point, on-screen text…).
    var text: String = ""

    var id: String { "\(videoID)|\(section)|\(Int(start))|\(snippet.hashValue)" }
}

/// What is indexed for one eaten item.
nonisolated struct SearchDocument: Sendable {
    struct Row: Sendable {
        let start: Double
        let section: String
        let text: String
    }

    let videoID: String
    let kind: String
    let title: String
    let channel: String
    let published: Date?
    let rows: [Row]

    /// Changes whenever something searchable changes (transcript, polish, summary, text on screen, names).
    var fingerprint: String {
        var hasher = SHA256()
        hasher.update(data: Data((title + "\u{1}" + channel).utf8))
        for row in rows { hasher.update(data: Data("\(row.section)\u{1}\(Int(row.start))\u{1}\(row.text)\u{2}".utf8)) }
        return hasher.finalize().prefix(12).map { String(format: "%02x", $0) }.joined()
    }

    init(videoID: String, kind: String, title: String, channel: String, published: Date?, rows: [Row]) {
        self.videoID = videoID
        self.kind = kind
        self.title = title
        self.channel = channel
        self.published = published
        self.rows = rows
    }

    /// Everything searchable in a snapshot: title line, summary, key points, transcript paragraphs (polished
    /// when available), text read on screen, names, and the description.
    init(_ video: VideoSnapshot) {
        var rows: [Row] = []
        let head = ([video.title, video.channelTitle] + video.tags.prefix(15) + (video.digest?.topics ?? [])
                    + video.entities.map(\.name)).joined(separator: " · ")
        rows.append(Row(start: 0, section: "title", text: head))
        if let digest = video.digest {
            if let lines = digest.summaryLines, !lines.isEmpty {
                rows += lines.map { Row(start: $0.seconds ?? 0, section: "summary", text: $0.text) }
            } else if !digest.summary.isEmpty {
                rows.append(Row(start: 0, section: "summary", text: digest.summary))
            }
            for (index, point) in digest.keyPoints.enumerated() {
                rows.append(Row(start: digest.keyPointTime(index) ?? 0, section: "point", text: point))
            }
        }
        rows += video.paragraphs.map { Row(start: $0.start, section: "transcript", text: $0.text) }
        rows += video.screen.map { Row(start: $0.start, section: "screen", text: $0.text) }
        let description = video.description.trimmingCharacters(in: .whitespacesAndNewlines)
        if !description.isEmpty { rows.append(Row(start: 0, section: "description", text: String(description.prefix(6_000)))) }
        self.init(videoID: video.videoID, kind: video.kind.rawValue, title: video.title, channel: video.channelTitle,
                  published: video.publishedAt, rows: rows)
    }
}

nonisolated enum SearchIndexError: LocalizedError, Sendable {
    case open(String)
    case sql(String)

    var errorDescription: String? {
        switch self {
        case .open(let why): "The search index could not be opened: \(why)"
        case .sql(let why): "Search index error: \(why)"
        }
    }
}

/// Full-text search over everything eaten, with SQLite FTS5 (built into macOS). One file,
/// `~/Library/Application Support/YouTube Zeus/Search.sqlite`, written by the app and read by `zeus search`,
/// Ask your brain and the MCP server. Rows are paragraphs, so every hit has its moment in the video.
nonisolated final class SearchIndex: @unchecked Sendable {
    static let fileName = "Search.sqlite"
    static var defaultURL: URL { AppFolders.support.appendingPathComponent(fileName) }
    /// Bumped when the schema or the tokenizer changes (the index is rebuilt).
    static let schemaVersion = 1

    private var db: OpaquePointer?
    private let lock = NSLock()
    let readOnly: Bool

    static var exists: Bool { FileManager.default.fileExists(atPath: defaultURL.path) }

    init(url: URL = SearchIndex.defaultURL, readOnly: Bool = false) throws {
        self.readOnly = readOnly
        // "Read-only" readers (zeus search, Ask, MCP) still open read-write so SQLite can share the app's WAL files;
        // they never create the file and never change the schema or the rows.
        let flags = SQLITE_OPEN_READWRITE | (readOnly ? 0 : SQLITE_OPEN_CREATE) | SQLITE_OPEN_FULLMUTEX
        guard sqlite3_open_v2(url.path, &db, flags, nil) == SQLITE_OK else {
            let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown"
            sqlite3_close(db)
            db = nil
            throw SearchIndexError.open(message)
        }
        sqlite3_busy_timeout(db, 5_000)
        if !readOnly { try prepareSchema() }
    }

    deinit { sqlite3_close(db) }

    private func prepareSchema() throws {
        try exec("PRAGMA journal_mode=WAL")
        try exec("PRAGMA synchronous=NORMAL")
        let version = (try? scalarInt("PRAGMA user_version")) ?? 0
        if version != Self.schemaVersion {
            try exec("DROP TABLE IF EXISTS docs")
            try exec("DROP TABLE IF EXISTS rows")
        }
        try exec("""
        CREATE TABLE IF NOT EXISTS docs(
          num INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT UNIQUE NOT NULL, kind TEXT, title TEXT, channel TEXT,
          published REAL, fingerprint TEXT, updated REAL)
        """)
        try exec("""
        CREATE VIRTUAL TABLE IF NOT EXISTS rows USING fts5(
          text, section UNINDEXED, start UNINDEXED, video_id UNINDEXED,
          tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3')
        """)
        try exec("PRAGMA user_version = \(Self.schemaVersion)")
    }

    // MARK: Writing

    func fingerprint(of videoID: String) -> String? {
        lock.withLock { try? query("SELECT fingerprint FROM docs WHERE video_id = ?", [videoID]) { $0.text(0) }.first }
    }

    /// Every indexed item and its fingerprint (to find what changed since the last launch).
    func fingerprints() -> [String: String] {
        lock.withLock {
            let pairs = (try? query("SELECT video_id, fingerprint FROM docs") { ($0.text(0), $0.text(1)) }) ?? []
            return Dictionary(pairs, uniquingKeysWith: { first, _ in first })
        }
    }

    /// Replaces an item's rows (skipped when nothing changed).
    @discardableResult
    func update(_ document: SearchDocument) throws -> Bool {
        let fingerprint = document.fingerprint
        return try lock.withLock {
            let existing = try query("SELECT num, fingerprint FROM docs WHERE video_id = ?", [document.videoID]) { ($0.int(0), $0.text(1)) }.first
            if existing?.1 == fingerprint { return false }
            try exec("BEGIN IMMEDIATE")
            do {
                let num: Int64
                if let existing {
                    num = existing.0
                    try run("DELETE FROM rows WHERE rowid BETWEEN ? AND ?", [num * 1_000_000, num * 1_000_000 + 999_999])
                    try run("UPDATE docs SET kind = ?, title = ?, channel = ?, published = ?, fingerprint = ?, updated = ? WHERE num = ?",
                            [document.kind, document.title, document.channel, document.published?.timeIntervalSince1970,
                             fingerprint, Date.now.timeIntervalSince1970, num])
                } else {
                    try run("INSERT INTO docs(video_id, kind, title, channel, published, fingerprint, updated) VALUES(?, ?, ?, ?, ?, ?, ?)",
                            [document.videoID, document.kind, document.title, document.channel,
                             document.published?.timeIntervalSince1970, fingerprint, Date.now.timeIntervalSince1970])
                    num = sqlite3_last_insert_rowid(db)
                }
                for (index, row) in document.rows.prefix(999_999).enumerated() where !row.text.isEmpty {
                    try run("INSERT INTO rows(rowid, text, section, start, video_id) VALUES(?, ?, ?, ?, ?)",
                            [num * 1_000_000 + Int64(index), row.text, row.section, row.start, document.videoID])
                }
                try exec("COMMIT")
            } catch {
                try? exec("ROLLBACK")
                throw error
            }
            return true
        }
    }

    func remove(_ videoID: String) {
        lock.withLock {
            guard let num = try? query("SELECT num FROM docs WHERE video_id = ?", [videoID]) { $0.int(0) }.first else { return }
            try? run("DELETE FROM rows WHERE rowid BETWEEN ? AND ?", [num * 1_000_000, num * 1_000_000 + 999_999])
            try? run("DELETE FROM docs WHERE num = ?", [num])
        }
    }

    /// Removes items that are no longer in the library.
    func keepOnly(_ videoIDs: Set<String>) {
        for id in fingerprints().keys where !videoIDs.contains(id) { remove(id) }
    }

    var counts: (items: Int, rows: Int) {
        lock.withLock {
            let items = (try? scalarInt("SELECT count(*) FROM docs")) ?? 0
            let rows = (try? scalarInt("SELECT count(*) FROM rows")) ?? 0
            return (items, rows)
        }
    }

    // MARK: Searching

    /// Ranked hits (BM25), at most `perItem` per item, items in order of their best hit.
    /// The query understands "exact phrases", word* prefixes, OR, NOT, and `a NEAR b`.
    func search(_ text: String, limit: Int = 30, perItem: Int = 3, sections: Set<String>? = nil) throws -> [SearchHit] {
        let match = Self.ftsQuery(text)
        guard !match.isEmpty else { return [] }
        let rows: [(String, Double, String, String, Double, String)] = try lock.withLock {
            try query("""
            SELECT video_id, start, section, snippet(rows, 0, '«', '»', '…', 18), bm25(rows), text
            FROM rows WHERE rows MATCH ? ORDER BY bm25(rows) LIMIT ?
            """, [match, Int64(max(200, limit * perItem * 6))]) {
                ($0.text(0), $0.double(1), $0.text(2), $0.text(3), $0.double(4), $0.text(5))
            }
        }
        let docs = try lock.withLock {
            try query("SELECT video_id, title, channel FROM docs") { ($0.text(0), ($0.text(1), $0.text(2))) }
        }
        let names = Dictionary(docs, uniquingKeysWith: { first, _ in first })
        var order: [String] = []
        var byItem: [String: [SearchHit]] = [:]
        for (id, start, section, snippet, score, text) in rows {
            if let sections, !sections.contains(section) { continue }
            let name = names[id] ?? ("", "")
            // Titles and summaries weigh a little more than one paragraph of transcript.
            let weight = section == "title" ? 1.6 : (section == "summary" || section == "point" ? 1.25 : 1)
            let hit = SearchHit(videoID: id, title: name.0, channel: name.1, start: start, section: section,
                                snippet: snippet, score: -score * weight, text: text)
            if byItem[id] == nil { order.append(id) }
            if (byItem[id]?.count ?? 0) < perItem { byItem[id, default: []].append(hit) }
        }
        // Items with several good hits rank a little higher.
        let ranked = order.sorted { a, b in
            func strength(_ id: String) -> Double {
                let hits = byItem[id] ?? []
                return (hits.map(\.score).max() ?? 0) + 0.15 * hits.dropFirst().map(\.score).reduce(0, +)
            }
            return strength(a) > strength(b)
        }
        return ranked.prefix(limit).flatMap { byItem[$0] ?? [] }
    }

    /// Plain words become an AND of quoted words ("claude" "hooks"); "quoted phrases", word*, OR, NOT, AND and
    /// `a NEAR b` (within 10 words) are understood. Everything else is quoted, so no input can break the query.
    static func ftsQuery(_ text: String) -> String {
        var tokens: [String] = []
        var current = ""
        var inQuote = false
        for character in text {
            if character == "\"" {
                if inQuote {
                    if !current.isEmpty { tokens.append("\u{1}" + current) }
                    current = ""
                    inQuote = false
                } else {
                    if !current.isEmpty { tokens.append(current) }
                    current = ""
                    inQuote = true
                }
            } else if !inQuote, character.isWhitespace || character == "," || character == "?" || character == "!" || character == ";" {
                if !current.isEmpty { tokens.append(current) }
                current = ""
            } else {
                current.append(character)
            }
        }
        if !current.isEmpty { tokens.append(inQuote ? "\u{1}" + current : current) }

        func quoted(_ word: String) -> String? {
            let isPhrase = word.hasPrefix("\u{1}")
            var body = isPhrase ? String(word.dropFirst()) : word
            var prefix = false
            if !isPhrase, body.hasSuffix("*") {
                prefix = true
                body = String(body.dropLast())
            }
            body = body.trimmingCharacters(in: CharacterSet(charactersIn: "()[]{}^:.'’\u{201C}\u{201D}"))
            guard body.rangeOfCharacter(from: .alphanumerics) != nil else { return nil }
            return "\"" + body.replacingOccurrences(of: "\"", with: "\"\"") + "\"" + (prefix ? "*" : "")
        }

        var parts: [String] = []
        var index = 0
        while index < tokens.count {
            let token = tokens[index]
            let upper = token.uppercased()
            if ["OR", "NOT", "AND"].contains(token), !parts.isEmpty, index + 1 < tokens.count {
                parts.append(token)
            } else if upper == "NEAR" || upper.hasPrefix("NEAR/"), let left = parts.last, !["OR", "NOT", "AND"].contains(left),
                      index + 1 < tokens.count, let right = quoted(tokens[index + 1]) {
                let distance = Int(upper.dropFirst(5)) ?? 10
                parts[parts.count - 1] = "NEAR(\(left) \(right), \(distance))"
                index += 1
            } else if let word = quoted(token) {
                parts.append(word)
            }
            index += 1
        }
        while let last = parts.last, ["OR", "NOT", "AND"].contains(last) { parts.removeLast() }
        return parts.joined(separator: " ")
    }

    /// For Ask your brain: any of the question's meaningful words, best paragraphs first.
    static func anyWordsQuery(_ words: [String]) -> String {
        words.map { "\"" + $0.replacingOccurrences(of: "\"", with: "") + "\"" }.joined(separator: " OR ")
    }

    // MARK: SQLite helpers

    struct Row {
        let statement: OpaquePointer?
        func text(_ column: Int32) -> String {
            sqlite3_column_text(statement, column).map { String(cString: $0) } ?? ""
        }
        func int(_ column: Int32) -> Int64 { sqlite3_column_int64(statement, column) }
        func double(_ column: Int32) -> Double { sqlite3_column_double(statement, column) }
    }

    private static let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    private func bind(_ statement: OpaquePointer?, _ values: [Any?]) {
        for (offset, value) in values.enumerated() {
            let index = Int32(offset + 1)
            switch value {
            case nil: sqlite3_bind_null(statement, index)
            case let value as String: sqlite3_bind_text(statement, index, value, -1, Self.transient)
            case let value as Int64: sqlite3_bind_int64(statement, index, value)
            case let value as Int: sqlite3_bind_int64(statement, index, Int64(value))
            case let value as Double: sqlite3_bind_double(statement, index, value)
            default: sqlite3_bind_null(statement, index)
            }
        }
    }

    private func exec(_ sql: String) throws {
        var error: UnsafeMutablePointer<CChar>?
        guard sqlite3_exec(db, sql, nil, nil, &error) == SQLITE_OK else {
            let message = error.map { String(cString: $0) } ?? "unknown"
            sqlite3_free(error)
            throw SearchIndexError.sql(message)
        }
    }

    private func run(_ sql: String, _ values: [Any?]) throws {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            throw SearchIndexError.sql(String(cString: sqlite3_errmsg(db)))
        }
        defer { sqlite3_finalize(statement) }
        bind(statement, values)
        let result = sqlite3_step(statement)
        guard result == SQLITE_DONE || result == SQLITE_ROW else {
            throw SearchIndexError.sql(String(cString: sqlite3_errmsg(db)))
        }
    }

    private func query<T>(_ sql: String, _ values: [Any?] = [], _ read: (Row) -> T) throws -> [T] {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else {
            throw SearchIndexError.sql(String(cString: sqlite3_errmsg(db)))
        }
        defer { sqlite3_finalize(statement) }
        bind(statement, values)
        var results: [T] = []
        while true {
            let step = sqlite3_step(statement)
            if step == SQLITE_ROW {
                results.append(read(Row(statement: statement)))
            } else if step == SQLITE_DONE {
                break
            } else {
                throw SearchIndexError.sql(String(cString: sqlite3_errmsg(db)))
            }
        }
        return results
    }

    private func scalarInt(_ sql: String) throws -> Int {
        Int(try query(sql) { $0.int(0) }.first ?? 0)
    }
}

extension SearchHit {
    /// The snippet with the matched words highlighted (for SwiftUI).
    nonisolated var markedParts: [(text: String, matched: Bool)] {
        var parts: [(text: String, matched: Bool)] = []
        var rest = Substring(snippet)
        while let open = rest.firstIndex(of: "«") {
            if open > rest.startIndex { parts.append((String(rest[..<open]), false)) }
            let afterOpen = rest.index(after: open)
            guard let close = rest[afterOpen...].firstIndex(of: "»") else {
                parts.append((String(rest[afterOpen...]), false))
                return parts
            }
            parts.append((String(rest[afterOpen..<close]), true))
            rest = rest[rest.index(after: close)...]
        }
        if !rest.isEmpty { parts.append((String(rest), false)) }
        return parts
    }

    nonisolated var plainSnippet: String { snippet.replacingOccurrences(of: "«", with: "").replacingOccurrences(of: "»", with: "") }

    nonisolated var sectionLabel: String {
        switch section {
        case "summary": "Summary"
        case "point": "Key point"
        case "screen": "On screen"
        case "title": "Title"
        case "description": "Description"
        default: "Transcript"
        }
    }
}
