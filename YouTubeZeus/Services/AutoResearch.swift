import Foundation

/// 4.5: the research Zeus can do itself, for free, on this Mac — Homebrew (version, description, install, what is
/// installed here), npm and PyPI (latest version), the GitHub API (activity, release, advisories, open issues).
/// Saved as "Zeus found" facts: the question stays open until an agent or Daniel confirms it.
nonisolated enum AutoResearch {
    struct Fact: Sendable {
        var text: String
        var sources: [String]
        var by: String
    }

    enum Manager: Sendable { case brew, cask, npm, pip }

    /// Checks the open tool and repository questions; returns how many facts were saved.
    @discardableResult
    static func run(_ video: VideoSnapshot, playbook: Playbook?) async -> Int {
        let brief = Research.brief(video, playbook: playbook)
        var saved = 0
        for question in brief.open where !Task.isCancelled {
            guard let fact = await check(question, video: video, playbook: playbook) else { continue }
            let finding = ResearchFinding(question: question.question, answer: fact.text, sources: fact.sources,
                                          by: fact.by, key: question.key, partial: true)
            if (try? Research.add(finding, to: video.videoID, title: video.title)) != nil { saved += 1 }
        }
        return saved
    }

    static func check(_ question: ResearchQuestion, video: VideoSnapshot, playbook: Playbook?) async -> Fact? {
        switch question.topic {
        case "tool":
            let subject = question.subject.lowercased()
            let tool = playbook?.tools.first { $0.name.lowercased() == subject }
            if let install = tool?.install, let found = package(from: install) {
                switch found.manager {
                case .brew, .cask:
                    if let fact = await homebrew(found.name, cask: found.manager == .cask) { return fact }
                case .npm:
                    if let fact = await npm(found.name) { return fact }
                case .pip:
                    if let fact = await pypi(found.name) { return fact }
                }
            }
            // No install command: Homebrew by the tool's name (a formula, then an app).
            let name = brewName(question.subject)
            if !name.isEmpty {
                if let fact = await homebrew(name, cask: false) { return fact }
                if let fact = await homebrew(name, cask: true) { return fact }
            }
            // A repository with the tool's name, linked in the video.
            if let repo = video.repos.first(where: { $0.exists && $0.name.lowercased() == subject }) {
                return github(repo)
            }
            return nil
        case "repo":
            guard let repo = video.repos.first(where: { $0.exists && $0.fullName.lowercased() == question.subject.lowercased() }) else {
                return nil
            }
            var fact = github(repo)
            let issues = await GitHubLinks.topIssues(owner: repo.owner, name: repo.name)
            if !issues.isEmpty {
                let list = issues.map { "#\($0.number) “\($0.title)” (\($0.comments) comments)" }.joined(separator: "; ")
                fact.text += " Most discussed open issues: \(list)."
                fact.sources.append("https://github.com/\(repo.fullName)/issues")
            }
            return fact
        default:
            return nil
        }
    }

    // MARK: Install commands

    /// `brew install --cask foo`, `npm i -g foo`, `npx foo@latest`, `pip install foo`, `pipx install foo`,
    /// `uv tool install foo`, `uvx foo` → the manager and the package (versions and options removed).
    static func package(from command: String) -> (manager: Manager, name: String)? {
        let words = command.replacingOccurrences(of: "`", with: " ")
            .split(whereSeparator: { $0.isWhitespace || $0 == ";" || $0 == "&" })
            .map(String.init)
        func firstName(after index: Int) -> String? {
            guard index + 1 < words.count else { return nil }
            return words[(index + 1)...].first { !$0.hasPrefix("-") }
        }
        for (index, word) in words.enumerated() {
            let next = index + 1 < words.count ? words[index + 1] : ""
            switch word {
            case "brew" where next == "install":
                let rest = words.dropFirst(index + 2)
                let cask = rest.contains("--cask")
                if let name = firstName(after: index + 1).flatMap(clean), !name.contains("/") { return (cask ? Manager.cask : Manager.brew, name) }
            case "npm" where next == "install" || next == "i":
                if let name = firstName(after: index + 1).flatMap(clean) { return (Manager.npm, name) }
            case "npx", "bunx":
                if let name = firstName(after: index).flatMap(clean) { return (Manager.npm, name) }
            case "pip" where next == "install", "pip3" where next == "install", "pipx" where next == "install":
                if let name = firstName(after: index + 1).flatMap(clean) { return (Manager.pip, name) }
            case "uv" where next == "tool" && index + 2 < words.count && words[index + 2] == "install":
                if let name = firstName(after: index + 2).flatMap(clean) { return (Manager.pip, name) }
            case "uvx":
                if let name = firstName(after: index).flatMap(clean) { return (Manager.pip, name) }
            default:
                continue
            }
        }
        return nil
    }

    /// A package name without its version (`foo@1.2`, `foo==1.2`, `foo>=1`, `foo[extra]`), or nil when it is not one.
    static func clean(_ word: String) -> String? {
        var name = word.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        if let range = name.range(of: #"(==|>=|<=|~=|\[).*$"#, options: .regularExpression) { name.removeSubrange(range) }
        // npm: keep a scope (@scope/name), drop a version (name@1.2 or @scope/name@latest).
        if let at = name.lastIndex(of: "@"), at != name.startIndex { name = String(name[..<at]) }
        guard !name.isEmpty, name.range(of: #"^@?[A-Za-z0-9][A-Za-z0-9._+\-/]*$"#, options: .regularExpression) != nil else { return nil }
        return name
    }

    /// "Ollama" → "ollama", "Claude Code" → "claude-code"; nothing for long names.
    static func brewName(_ name: String) -> String {
        let words = name.lowercased().split(whereSeparator: \.isWhitespace)
        guard !words.isEmpty, words.count <= 2 else { return "" }
        let joined = words.joined(separator: "-")
        return joined.range(of: #"^[a-z0-9][a-z0-9+._\-]*$"#, options: .regularExpression) != nil ? joined : ""
    }

    // MARK: Sources

    static func homebrew(_ name: String, cask: Bool) async -> Fact? {
        guard let brew = ToolLocator.find("brew", override: ""),
              let output = try? await ProcessRunner.run(brew, ["info", "--json=v2", cask ? "--cask" : "--formula", name]),
              output.status == 0,
              let root = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any] else { return nil }
        if !cask, let formula = (root["formulae"] as? [[String: Any]])?.first {
            let version = ((formula["versions"] as? [String: Any])?["stable"] as? String) ?? "?"
            let mine = ((formula["installed"] as? [[String: Any]])?.last?["version"] as? String)
            var text = "Homebrew has `\(name)` \(version)"
            if let description = formula["desc"] as? String, !description.isEmpty { text += " — \(description)" }
            if let license = formula["license"] as? String, !license.isEmpty { text += " (licence \(license))" }
            text += ". Install: `brew install \(name)`."
            text += installedLine(mine, latest: version, upgrade: "brew upgrade \(name)")
            if (formula["deprecated"] as? Bool) == true {
                text += " Deprecated" + ((formula["deprecation_reason"] as? String).map { ": \($0)" } ?? "") + "."
            }
            var sources = ["https://formulae.brew.sh/formula/\(name)"]
            if let homepage = formula["homepage"] as? String { sources.insert(homepage, at: 0) }
            return Fact(text: text, sources: sources, by: "Zeus · Homebrew")
        }
        if cask, let app = (root["casks"] as? [[String: Any]])?.first {
            let version = (app["version"] as? String) ?? "?"
            let mine = app["installed"] as? String
            let title = (app["name"] as? [String])?.first ?? name
            var text = "Homebrew has the app \(title) (`\(name)`) \(version)"
            if let description = app["desc"] as? String, !description.isEmpty { text += " — \(description)" }
            text += ". Install: `brew install --cask \(name)`."
            text += installedLine(mine, latest: version, upgrade: "brew upgrade --cask \(name)")
            if (app["deprecated"] as? Bool) == true { text += " Deprecated in Homebrew." }
            var sources = ["https://formulae.brew.sh/cask/\(name)"]
            if let homepage = app["homepage"] as? String { sources.insert(homepage, at: 0) }
            return Fact(text: text, sources: sources, by: "Zeus · Homebrew")
        }
        return nil
    }

    private static func installedLine(_ mine: String?, latest: String, upgrade: String) -> String {
        guard let mine, !mine.isEmpty else { return " Not installed on this Mac." }
        return mine == latest ? " Installed on this Mac (\(mine), the latest)." : " On this Mac: \(mine) (`\(upgrade)` for \(latest))."
    }

    static func npm(_ name: String) async -> Fact? {
        guard let url = URL(string: "https://registry.npmjs.org/\(name)/latest"), let info = await json(url),
              let version = info["version"] as? String else { return nil }
        var text = "npm has `\(name)` \(version)"
        if let description = info["description"] as? String, !description.isEmpty { text += " — \(description)" }
        if let license = info["license"] as? String, !license.isEmpty { text += " (licence \(license))" }
        text += ". Install: `npm install -g \(name)` (Node first: `brew install node`)."
        if let node = (info["engines"] as? [String: Any])?["node"] as? String { text += " Needs Node \(node)." }
        return Fact(text: text, sources: ["https://www.npmjs.com/package/\(name)"], by: "Zeus · npm")
    }

    static func pypi(_ name: String) async -> Fact? {
        guard let url = URL(string: "https://pypi.org/pypi/\(name)/json"), let root = await json(url),
              let info = root["info"] as? [String: Any], let version = info["version"] as? String else { return nil }
        var text = "PyPI has `\(name)` \(version)"
        if let summary = info["summary"] as? String, !summary.isEmpty { text += " — \(summary)" }
        text += "."
        if let python = info["requires_python"] as? String, !python.isEmpty { text += " Needs Python \(python)." }
        text += " Install: `uv tool install \(name)` (or `pipx install \(name)`)."
        return Fact(text: text, sources: ["https://pypi.org/project/\(name)/"], by: "Zeus · PyPI")
    }

    static func github(_ repo: RepoCheck) -> Fact {
        var parts: [String] = ["\(repo.stars.formatted()) stars"]
        if let pushed = repo.pushedAt { parts.append("last push \(pushed.prefix(10))") }
        if let release = repo.latestRelease, !release.isEmpty { parts.append("latest release \(release)") }
        if let license = repo.license, !license.isEmpty { parts.append(license) }
        parts.append("\(repo.openIssues) open issues and pull requests")
        parts.append(repo.advisories == 0 ? "no security advisories"
                     : "\(repo.advisories) security advisor\(repo.advisories == 1 ? "y" : "ies") (\(repo.criticalAdvisories) critical)")
        var text = "GitHub (\(repo.fullName)): " + parts.joined(separator: ", ") + "."
        if repo.archived { text += " ARCHIVED: no longer maintained." }
        if repo.isFork { text += " It is a fork." }
        text += " Zeus did not read the code."
        var sources = [repo.url.absoluteString]
        if repo.advisories > 0 { sources.append(repo.url.absoluteString + "/security/advisories") }
        return Fact(text: text, sources: sources, by: "Zeus · GitHub API")
    }

    private static func json(_ url: URL) async -> [String: Any]? {
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.setValue("YouTube-Zeus", forHTTPHeaderField: "User-Agent")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}
