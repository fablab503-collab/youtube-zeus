import Foundation

/// One-click connection of `zeus mcp` to the AI apps on this Mac. JSON and TOML configs are backed up before any change
/// (`<file>.zeus-backup`), and only the "youtube-zeus" entry is added or replaced.
nonisolated enum MCPConnect {
    static let serverName = "youtube-zeus"

    enum Client: String, CaseIterable, Identifiable, Sendable {
        case claudeDesktop, claudeCode, codex, cursor, lmStudio, gemini

        var id: String { rawValue }

        var label: String {
            switch self {
            case .claudeDesktop: "Claude (desktop app)"
            case .claudeCode: "Claude Code"
            case .codex: "Codex"
            case .cursor: "Cursor"
            case .lmStudio: "LM Studio"
            case .gemini: "Gemini CLI"
            }
        }

        var note: String {
            switch self {
            case .claudeDesktop: "Takes effect the next time the Claude app starts."
            case .claudeCode: "Added with `claude mcp add --scope user`."
            case .codex: "Added to ~/.codex/config.toml."
            case .cursor: "Added to ~/.cursor/mcp.json."
            case .lmStudio: "Added to ~/.lmstudio/mcp.json."
            case .gemini: "Added to ~/.gemini/settings.json."
            }
        }
    }

    /// The app binary itself, so no PATH is needed.
    static var command: String {
        Bundle.main.executablePath ?? "/Applications/YouTube Zeus.app/Contents/MacOS/YouTube Zeus"
    }

    static let arguments = ["--cli", "mcp"]

    private static var home: URL { FileManager.default.homeDirectoryForCurrentUser }

    static func configFile(_ client: Client) -> URL? {
        switch client {
        case .claudeDesktop: home.appendingPathComponent("Library/Application Support/Claude/claude_desktop_config.json")
        case .claudeCode: nil
        case .codex: home.appendingPathComponent(".codex/config.toml")
        case .cursor: home.appendingPathComponent(".cursor/mcp.json")
        case .lmStudio: home.appendingPathComponent(".lmstudio/mcp.json")
        case .gemini: home.appendingPathComponent(".gemini/settings.json")
        }
    }

    static var claudeCLI: String? { ToolLocator.find("claude", override: "") }

    static func isInstalled(_ client: Client) -> Bool {
        switch client {
        case .claudeDesktop: FileManager.default.fileExists(atPath: "/Applications/Claude.app")
        case .claudeCode: claudeCLI != nil
        default: configFile(client).map { FileManager.default.fileExists(atPath: $0.deletingLastPathComponent().path) } ?? false
        }
    }

    static func isConnected(_ client: Client) -> Bool {
        if client == .claudeCode {
            let file = home.appendingPathComponent(".claude.json")
            guard let data = try? Data(contentsOf: file),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
            return (json["mcpServers"] as? [String: Any])?[serverName] != nil
        }
        guard let file = configFile(client), let text = try? String(contentsOf: file, encoding: .utf8) else { return false }
        return text.contains(serverName)
    }

    static func connect(_ client: Client) async throws {
        switch client {
        case .claudeCode:
            guard let claude = claudeCLI else { throw ProcessFailure.missingTool("claude") }
            _ = try? await ProcessRunner.run(claude, ["mcp", "remove", "--scope", "user", serverName])
            _ = try await ProcessRunner.check(claude, ["mcp", "add", "--scope", "user", serverName, "--", command] + arguments)
        case .codex:
            guard let file = configFile(client) else { return }
            var text = (try? String(contentsOf: file, encoding: .utf8)) ?? ""
            backup(file)
            // Replace an older entry (the table and its keys, up to the next table).
            if let range = text.range(of: #"(?ms)^\[mcp_servers\.youtube-zeus\]\n.*?(?=^\[|\z)"#, options: .regularExpression) {
                text.removeSubrange(range)
            }
            if !text.isEmpty, !text.hasSuffix("\n") { text += "\n" }
            text += "\n[mcp_servers.\(serverName)]\ncommand = \"\(command)\"\nargs = [\"--cli\", \"mcp\"]\n"
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try text.write(to: file, atomically: true, encoding: .utf8)
        default:
            guard let file = configFile(client) else { return }
            var json: [String: Any] = [:]
            if let data = try? Data(contentsOf: file), !data.isEmpty {
                guard let existing = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    throw CocoaError(.fileReadCorruptFile)
                }
                json = existing
                backup(file)
            }
            var servers = json["mcpServers"] as? [String: Any] ?? [:]
            servers[serverName] = ["command": command, "args": arguments]
            json["mcpServers"] = servers
            let data = try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: file, options: .atomic)
        }
    }

    private static func backup(_ file: URL) {
        guard FileManager.default.fileExists(atPath: file.path) else { return }
        let copy = file.deletingLastPathComponent().appendingPathComponent(file.lastPathComponent + ".zeus-backup")
        try? FileManager.default.removeItem(at: copy)
        try? FileManager.default.copyItem(at: file, to: copy)
    }

    /// The JSON to paste in any other MCP client.
    static var snippet: String {
        """
        {
          "mcpServers": {
            "\(serverName)": {
              "command": "\(command)",
              "args": ["--cli", "mcp"]
            }
          }
        }
        """
    }
}
