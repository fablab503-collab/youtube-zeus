import Foundation

/// A GitHub repository found in a video, with the facts checked through the GitHub API.
nonisolated struct RepoCheck: Codable, Hashable, Sendable, Identifiable {
    var owner: String
    var name: String
    var foundIn: String
    var exists: Bool
    var description: String?
    var stars: Int
    var forks: Int
    var watchers: Int
    var openIssues: Int
    var license: String?
    var archived: Bool
    var isFork: Bool
    var ownerType: String?
    var ownerSince: String?
    var ownerRepos: Int
    var ownerFollowers: Int
    var createdAt: String?
    var pushedAt: String?
    var homepage: String?
    var language: String?
    var topics: [String]
    var latestRelease: String?
    var advisories: Int
    var criticalAdvisories: Int
    var linkedBack: Bool
    var verdict: String
    var checkedAt: Date

    var id: String { fullName.lowercased() }
    var fullName: String { "\(owner)/\(name)" }
    var noteName: String { SecondBrainExporter.sanitize("\(owner)-\(name)", limit: 120) }
    var url: URL { URL(string: "https://github.com/\(owner)/\(name)") ?? URL(string: "https://github.com")! }

    var verdictLabel: String {
        switch verdict {
        case "active": "Active"
        case "new": "New repository"
        case "stale": "Not updated for 18+ months"
        case "archived": "Archived (read-only)"
        case "fork": "Fork of another repository"
        case "missing": "Not found (deleted, renamed or private)"
        case "unchecked": "Not checked yet (GitHub limit)"
        default: verdict
        }
    }

    var verdictSymbol: String {
        switch verdict {
        case "active": "checkmark.seal.fill"
        case "new": "sparkles"
        case "stale", "fork", "unchecked": "exclamationmark.circle"
        case "archived": "archivebox"
        default: "xmark.octagon"
        }
    }

    var needsCare: Bool { criticalAdvisories > 0 || verdict == "missing" || verdict == "new" }

    var pushedDay: String { String((pushedAt ?? "").prefix(10)) }
    var createdDay: String { String((createdAt ?? "").prefix(10)) }

    /// One line for notes and AI packs.
    var summaryLine: String {
        guard exists else { return verdictLabel }
        var parts = [verdictLabel, "\(stars.formatted()) stars", license ?? "no license"]
        if !pushedDay.isEmpty { parts.append("last push \(pushedDay)") }
        if let latestRelease { parts.append("release \(latestRelease.components(separatedBy: " · ").first ?? latestRelease)") }
        if advisories > 0 { parts.append("\(advisories) security advisor\(advisories == 1 ? "y" : "ies")\(criticalAdvisories > 0 ? " (\(criticalAdvisories) critical)" : "")") }
        if linkedBack { parts.append("links back to the video or its site") }
        return parts.joined(separator: " · ")
    }
}

nonisolated enum GitHubLinks {
    /// Paths on github.com that are not repositories.
    private static let reservedOwners: Set<String> = [
        "orgs", "sponsors", "topics", "features", "about", "marketplace", "settings", "login", "apps", "collections",
        "trending", "explore", "pricing", "security", "notifications", "issues", "pulls", "search", "site", "readme",
        "enterprise", "customer-stories", "team", "join", "contact", "events", "codespaces", "copilot", "users",
        "new", "account", "resources", "solutions", "signup", "watching", "stars", "dashboard", "discussions",
    ]

    /// github.com/<owner>/<repo> links in a text, in order, without duplicates (gists and raw files excluded).
    static func extract(from text: String, source: String) -> [(owner: String, name: String, source: String)] {
        let pattern = #"(?<![A-Za-z0-9.-])(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})"#
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return [] }
        let ns = text as NSString
        var seen = Set<String>()
        var result: [(owner: String, name: String, source: String)] = []
        for match in regex.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let owner = ns.substring(with: match.range(at: 1))
            var name = ns.substring(with: match.range(at: 2))
            name = name.trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?)-_"))
            if name.lowercased().hasSuffix(".git") { name = String(name.dropLast(4)) }
            guard !name.isEmpty, !reservedOwners.contains(owner.lowercased()) else { continue }
            let key = (owner + "/" + name).lowercased()
            guard seen.insert(key).inserted else { continue }
            result.append((owner, name, source))
        }
        return result
    }

    /// Repositories linked in the description, in the channel's own comments, or spelled out in the transcript.
    static func find(description: String, channel: String, comments: [VideoComment],
                     transcript: String) -> [(owner: String, name: String, source: String)] {
        var all = extract(from: description, source: "description")
        func add(_ more: [(owner: String, name: String, source: String)]) {
            let known = Set(all.map { ($0.owner + "/" + $0.name).lowercased() })
            all += more.filter { !known.contains(($0.owner + "/" + $0.name).lowercased()) }
        }
        let channelKey = normalized(channel)
        let own = comments.filter { !channelKey.isEmpty && normalized($0.author) == channelKey }
        add(extract(from: own.map(\.text).joined(separator: "\n"), source: "channel's comment"))
        add(extract(from: transcript, source: "transcript"))
        return Array(all.prefix(15))
    }

    private static func normalized(_ text: String) -> String {
        text.lowercased().filter { $0.isLetter || $0.isNumber }
    }

    // MARK: Checking

    private static var cacheURL: URL { AppFolders.support.appendingPathComponent("github-cache.json") }
    private static let cacheLock = NSLock()

    private static func cached(_ key: String) -> RepoCheck? {
        cacheLock.lock(); defer { cacheLock.unlock() }
        guard let data = try? Data(contentsOf: cacheURL),
              let cache = try? JSONDecoder.iso.decode([String: RepoCheck].self, from: data),
              let hit = cache[key.lowercased()], Date.now.timeIntervalSince(hit.checkedAt) < 3 * 86_400 else { return nil }
        return hit
    }

    private static func store(_ check: RepoCheck, alias: String) {
        cacheLock.lock(); defer { cacheLock.unlock() }
        var cache = (try? Data(contentsOf: cacheURL)).flatMap { try? JSONDecoder.iso.decode([String: RepoCheck].self, from: $0) } ?? [:]
        cache[check.id] = check
        cache[alias.lowercased()] = check
        if let data = try? JSONEncoder.iso.encode(cache) { try? data.write(to: cacheURL) }
    }

    /// Forgets cached checks so the next check asks GitHub again.
    static func clearCache() {
        cacheLock.lock(); defer { cacheLock.unlock() }
        try? FileManager.default.removeItem(at: cacheURL)
    }

    /// A GitHub token raises the limit from 60 to 5,000 requests an hour: Keychain ("github"),
    /// GITHUB_TOKEN, or the GitHub CLI's sign-in.
    private static let token: String? = {
        if let saved = KeychainStore.read("github"), !saved.isEmpty { return saved }
        if let env = ProcessInfo.processInfo.environment["GITHUB_TOKEN"], !env.isEmpty { return env }
        for path in ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"] where FileManager.default.isExecutableFile(atPath: path) {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: path)
            process.arguments = ["auth", "token"]
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            guard (try? process.run()) != nil else { continue }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            if process.terminationStatus == 0, !text.isEmpty { return text }
        }
        return nil
    }()

    static var hasToken: Bool { token != nil }

    private static func get(_ path: String) async -> (Int, Any?) {
        guard let url = URL(string: "https://api.github.com/" + path) else { return (0, nil) }
        var request = URLRequest(url: url)
        request.timeoutInterval = 20
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("2022-11-28", forHTTPHeaderField: "X-GitHub-Api-Version")
        request.setValue("YouTube-Zeus/2.2", forHTTPHeaderField: "User-Agent")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        guard let (data, response) = try? await URLSession.shared.data(for: request) else { return (0, nil) }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        return (status, try? JSONSerialization.jsonObject(with: data))
    }

    /// Checks a repository: does it exist, is it alive, archived, a fork, licensed, linked back to the video,
    /// how many security advisories it has.
    static func verify(owner: String, name: String, source: String, videoID: String, description: String) async -> RepoCheck {
        if let hit = cached(owner + "/" + name) {
            var copy = hit
            copy.foundIn = source
            if let homepage = copy.homepage, homepage.contains(videoID) { copy.linkedBack = true }
            return copy
        }
        var check = RepoCheck(owner: owner, name: name, foundIn: source, exists: false, description: nil, stars: 0, forks: 0,
                              watchers: 0, openIssues: 0, license: nil, archived: false, isFork: false, ownerType: nil,
                              ownerSince: nil, ownerRepos: 0, ownerFollowers: 0, createdAt: nil, pushedAt: nil,
                              homepage: nil, language: nil, topics: [], latestRelease: nil, advisories: 0,
                              criticalAdvisories: 0, linkedBack: false, verdict: "missing", checkedAt: .now)
        let (status, json) = await get("repos/\(owner)/\(name)")
        guard status == 200, let repo = json as? [String: Any] else {
            check.verdict = status == 404 ? "missing" : "unchecked"
            if status == 404 { store(check, alias: owner + "/" + name) }
            AppLog.write("GITHUB \(owner)/\(name) → HTTP \(status)")
            return check
        }
        // GitHub follows renamed repositories: keep the current name.
        if let full = repo["full_name"] as? String, let slash = full.firstIndex(of: "/") {
            check.owner = String(full[..<slash])
            check.name = String(full[full.index(after: slash)...])
        }
        check.exists = true
        check.description = repo["description"] as? String
        check.stars = repo["stargazers_count"] as? Int ?? 0
        check.forks = repo["forks_count"] as? Int ?? 0
        check.watchers = repo["subscribers_count"] as? Int ?? 0
        check.openIssues = repo["open_issues_count"] as? Int ?? 0
        check.license = ((repo["license"] as? [String: Any])?["spdx_id"] as? String).map { $0 == "NOASSERTION" ? "custom" : $0 }
        check.archived = repo["archived"] as? Bool ?? false
        check.isFork = repo["fork"] as? Bool ?? false
        check.ownerType = (repo["owner"] as? [String: Any])?["type"] as? String
        check.createdAt = repo["created_at"] as? String
        check.pushedAt = repo["pushed_at"] as? String
        check.homepage = (repo["homepage"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        check.language = repo["language"] as? String
        check.topics = repo["topics"] as? [String] ?? []

        let (ownerStatus, ownerJSON) = await get("users/\(check.owner)")
        if ownerStatus == 200, let user = ownerJSON as? [String: Any] {
            check.ownerSince = (user["created_at"] as? String).map { String($0.prefix(10)) }
            check.ownerRepos = user["public_repos"] as? Int ?? 0
            check.ownerFollowers = user["followers"] as? Int ?? 0
        }
        let (releaseStatus, release) = await get("repos/\(check.owner)/\(check.name)/releases/latest")
        if releaseStatus == 200, let release = release as? [String: Any] {
            check.latestRelease = [release["tag_name"] as? String, (release["published_at"] as? String).map { String($0.prefix(10)) }]
                .compactMap { $0 }.joined(separator: " · ")
        }
        let (advisoryStatus, advisories) = await get("repos/\(check.owner)/\(check.name)/security-advisories?per_page=100&state=published")
        if advisoryStatus == 200, let list = advisories as? [[String: Any]] {
            check.advisories = list.count
            check.criticalAdvisories = list.filter { ($0["severity"] as? String) == "critical" }.count
        }

        // Linked back: the repository's homepage is the video, or a site the video's description also links.
        if let homepage = check.homepage {
            let host = (URL(string: homepage)?.host ?? "").replacingOccurrences(of: "www.", with: "")
            check.linkedBack = homepage.contains(videoID) || (host.count > 3 && description.localizedCaseInsensitiveContains(host))
        }

        let iso = ISO8601DateFormatter()
        let pushed = check.pushedAt.flatMap { iso.date(from: $0) } ?? .distantPast
        let created = check.createdAt.flatMap { iso.date(from: $0) } ?? .distantPast
        if check.archived { check.verdict = "archived" }
        else if check.isFork { check.verdict = "fork" }
        else if Date.now.timeIntervalSince(pushed) > 548 * 86_400 { check.verdict = "stale" }
        else if Date.now.timeIntervalSince(created) < 30 * 86_400, check.stars < 20, !check.linkedBack { check.verdict = "new" }
        else { check.verdict = "active" }
        store(check, alias: owner + "/" + name)
        AppLog.write("GITHUB \(check.fullName) → \(check.verdict), \(check.stars) stars, \(check.advisories) advisories")
        return check
    }

    /// Finds and checks every repository of a video (one at a time, to stay polite with the API).
    static func check(videoID: String, description: String, channel: String, comments: [VideoComment],
                      transcript: String) async -> [RepoCheck] {
        var results: [RepoCheck] = []
        for found in find(description: description, channel: channel, comments: comments, transcript: transcript) {
            let check = await verify(owner: found.owner, name: found.name, source: found.source,
                                     videoID: videoID, description: description)
            if !results.contains(where: { $0.id == check.id }) { results.append(check) }
        }
        return results
    }

    /// The first lines of the README (raw file, not counted against the API limit).
    static func readmeExcerpt(owner: String, name: String, lines: Int = 40) async -> String? {
        for file in ["README.md", "readme.md", "README"] {
            guard let url = URL(string: "https://raw.githubusercontent.com/\(owner)/\(name)/HEAD/\(file)"),
                  let (data, response) = try? await URLSession.shared.data(from: url),
                  (response as? HTTPURLResponse)?.statusCode == 200 else { continue }
            let text = String(decoding: data, as: UTF8.self)
            return text.components(separatedBy: "\n").prefix(lines).joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return nil
    }

    // MARK: Notes (Sources/GitHub/<owner>-<name>.md)

    static let factsStart = "%% zeus:github:facts start %%"
    static let factsEnd = "%% zeus:github:facts end %%"

    struct Mention: Sendable {
        var note: String
        var title: String
        var source: String
    }

    static func factsBlock(_ check: RepoCheck, seenIn: [Mention]) -> String {
        let day = SecondBrainExporter.dayFormatter.string(from: check.checkedAt)
        func cell(_ text: String) -> String { text.replacingOccurrences(of: "|", with: "/").replacingOccurrences(of: "\n", with: " ") }
        var lines = [factsStart, "## Facts (checked \(day) through the GitHub API by YouTube Zeus)", "", "| | |", "|---|---|"]
        var owner = "\(check.ownerType?.lowercased() ?? "account") `\(check.owner)`"
        if let since = check.ownerSince {
            owner += ", since \(since.prefix(4)), \(check.ownerRepos) public repos, \(check.ownerFollowers.formatted()) followers"
        }
        lines.append("| Repository | \(check.url.absoluteString) (\(owner)) |")
        lines.append("| Open in YouTube Zeus | [\(check.fullName)](\(BrainLinks.zeus(repo: check.fullName))) |")
        if let description = check.description { lines.append("| Description | \(cell(description)) |") }
        lines.append("| Verdict | \(check.verdictLabel)\(check.linkedBack ? " · links back to the video or its site" : "") |")
        if check.exists {
            lines.append("| Created / last push | \(check.createdDay) / \(check.pushedDay) |")
            lines.append("| Stars / forks / watchers | \(check.stars.formatted()) / \(check.forks.formatted()) / \(check.watchers.formatted()) |")
            lines.append("| Open issues + PRs | \(check.openIssues.formatted()) |")
            lines.append("| License | \(check.license ?? "none declared") |")
            if let language = check.language { lines.append("| Main language | \(language) |") }
            if let release = check.latestRelease { lines.append("| Latest release | \(release) |") }
            if let homepage = check.homepage { lines.append("| Homepage | \(homepage) |") }
            if !check.topics.isEmpty { lines.append("| Topics | \(check.topics.joined(separator: ", ")) |") }
            lines.append("| Security advisories | "
                + (check.advisories == 0
                    ? "none published"
                    : "\(check.advisories) published (\(check.criticalAdvisories) critical): \(check.url.absoluteString)/security/advisories")
                + " |")
        }
        lines += ["", "Seen in:"]
        lines += seenIn.map { "- [[\($0.note)|\(cell($0.title))]] — found in the \($0.source)" }
        lines.append(factsEnd)
        return lines.joined(separator: "\n")
    }

    /// Creates the repository note, or refreshes only its facts block (everything else in the note is yours).
    /// Returns true when the file changed.
    @discardableResult
    static func writeNote(_ check: RepoCheck, seenIn: [Mention], folder: URL, create: Bool, readme: String?) -> Bool {
        guard check.exists else { return false }
        let file = folder.appendingPathComponent("\(check.noteName).md")
        let facts = factsBlock(check, seenIn: seenIn)
        if let text = try? String(contentsOf: file, encoding: .utf8) {
            guard let start = text.range(of: factsStart), let end = text.range(of: factsEnd),
                  start.lowerBound < end.upperBound else { return false }
            var updated = text
            updated.replaceSubrange(start.lowerBound..<end.upperBound, with: facts)
            guard updated != text else { return false }
            return (try? updated.write(to: file, atomically: true, encoding: .utf8)) != nil
        }
        guard create else { return false }
        let day = SecondBrainExporter.dayFormatter.string(from: .now)
        var lines = ["---", "date: \(day)", "type: source", "tags:", "  - source", "  - github"]
        lines += check.topics.prefix(5).map { SecondBrainExporter.tagSlug($0) }.filter { !$0.isEmpty }.map { "  - \($0)" }
        lines += ["ai-first: true", "title: \(SecondBrainExporter.yamlString(check.fullName))", "url: \(check.url.absoluteString)"]
        if let homepage = check.homepage { lines.append("homepage: \(homepage)") }
        lines += [
            "license: \(check.license ?? "none")", "verified: \(day)", "verdict: \(check.verdict)",
            "found-by: YouTube Zeus", "confidence: medium", "---", "",
            "# \(check.fullName)", "", "## For future agent", "",
            "\(check.description.map { $0.hasSuffix(".") ? $0 : $0 + "." } ?? "GitHub repository.") Linked in YouTube videos eaten by YouTube Zeus (see \"Seen in\"). The facts block is checked through the GitHub API and rewritten automatically; add your own notes below it, they are kept. The README excerpt is the project's own claims, not verified facts. Read the code and the security advisories before running anything from it.",
            "", facts, "", "## Notes", "", "- ", "",
        ]
        if let readme, !readme.isEmpty {
            lines += ["## README (first lines, \(day))", ""]
            lines += readme.components(separatedBy: "\n").map { $0.isEmpty ? ">" : "> \($0)" }
            lines.append("")
        }
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        return (try? lines.joined(separator: "\n").write(to: file, atomically: true, encoding: .utf8)) != nil
    }

    /// Markdown section for a video note.
    static func noteSection(_ repos: [RepoCheck]) -> [String] {
        guard !repos.isEmpty else { return [] }
        let day = SecondBrainExporter.dayFormatter.string(from: repos.map(\.checkedAt).max() ?? .now)
        var lines = ["## GitHub", "",
                     "Repositories linked in this video, checked through the GitHub API on \(day). A link in a video is not a security review: read the code before running it.",
                     ""]
        for repo in repos {
            let name = repo.exists ? "[[\(repo.noteName)|\(repo.fullName)]]" : "`\(repo.fullName)`"
            lines.append("- \(name) — \(repo.summaryLine) · found in the \(repo.foundIn) · \(repo.url.absoluteString)")
        }
        lines.append("")
        return lines
    }
}
