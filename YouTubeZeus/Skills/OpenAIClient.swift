import Foundation
import Security

/// The OpenAI key lives only in the macOS Keychain.
nonisolated enum KeychainStore {
    private static let service = "com.danielmadac.youtubezeus"

    static func read(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    static func save(_ value: String, account: String) -> Bool {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        guard !value.isEmpty else { return true }
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }
}

nonisolated enum OpenAIError: LocalizedError, Sendable {
    case noKey
    case http(Int, String)
    case incomplete(String)
    case refusal(String)
    case badJSON(String)

    var errorDescription: String? {
        switch self {
        case .noKey: "Add your OpenAI API key in Settings › Codex skills."
        case .http(let code, let message): "OpenAI answered \(code): \(message)"
        case .incomplete(let reason): "OpenAI stopped early (\(reason)). Try a larger output limit or a shorter video."
        case .refusal(let text): "The model refused: \(text)"
        case .badJSON(let why): "The model's answer did not match the skill format (\(why))."
        }
    }
}

nonisolated struct OpenAIResponse: Sendable {
    let json: Data
    let inputTokens: Int
    let outputTokens: Int
}

/// Minimal client for the OpenAI Responses API with strict structured output.
nonisolated struct OpenAIClient: Sendable {
    let apiKey: String
    let model: String

    func structured(system: String, user: String, schemaName: String, schema: [String: Any],
                    maxOutputTokens: Int = 24_000) async throws -> OpenAIResponse {
        var body: [String: Any] = [
            "model": model,
            "input": [
                ["role": "system", "content": [["type": "input_text", "text": system]]],
                ["role": "user", "content": [["type": "input_text", "text": user]]],
            ],
            "text": ["format": ["type": "json_schema", "name": schemaName, "strict": true, "schema": schema]],
            "max_output_tokens": maxOutputTokens,
            "store": false,
        ]
        if model.hasPrefix("gpt-5") || model.hasPrefix("o") {
            body["reasoning"] = ["effort": "low"]
        }
        var request = URLRequest(url: URL(string: "https://api.openai.com/v1/responses")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 600
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw OpenAIError.http(status, String(decoding: data.prefix(300), as: UTF8.self))
        }
        guard status == 200 else {
            let message = (json["error"] as? [String: Any])?["message"] as? String ?? "unknown error"
            throw OpenAIError.http(status, message)
        }
        let usage = json["usage"] as? [String: Any] ?? [:]
        let inputTokens = usage["input_tokens"] as? Int ?? 0
        let outputTokens = usage["output_tokens"] as? Int ?? 0
        if (json["status"] as? String) == "incomplete" {
            let reason = (json["incomplete_details"] as? [String: Any])?["reason"] as? String ?? "incomplete"
            throw OpenAIError.incomplete(reason)
        }
        for item in json["output"] as? [[String: Any]] ?? [] where (item["type"] as? String) == "message" {
            for content in item["content"] as? [[String: Any]] ?? [] {
                if (content["type"] as? String) == "refusal" {
                    throw OpenAIError.refusal(content["refusal"] as? String ?? "")
                }
                if (content["type"] as? String) == "output_text", let text = content["text"] as? String {
                    return OpenAIResponse(json: Data(text.utf8), inputTokens: inputTokens, outputTokens: outputTokens)
                }
            }
        }
        throw OpenAIError.badJSON("no text in the answer")
    }
}

/// Uses the Codex CLI and its ChatGPT sign-in instead of an API key.
/// Runs `codex exec` read-only in an empty folder with a strict output schema.
nonisolated struct CodexCLIClient: Sendable {
    let executable: String
    let model: String

    func structured(system: String, user: String, schema: [String: Any]) async throws -> Data {
        let dir = AppFolders.work.appendingPathComponent("codex-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let schemaURL = dir.appendingPathComponent("schema.json")
        try JSONSerialization.data(withJSONObject: schema, options: [.sortedKeys]).write(to: schemaURL)
        let answerURL = dir.appendingPathComponent("answer.json")
        let prompt = system + """


        Do not run any commands and do not read any files: everything you need is below.

        Untrusted source data (JSON):
        """ + "\n" + user
        var arguments = ["exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                         "-C", dir.path, "--output-schema", schemaURL.path, "-o", answerURL.path,
                         "-c", "model_reasoning_effort=\"low\"", "-c", "mcp_servers={}"]
        if !model.isEmpty { arguments += ["-m", model] }
        arguments.append(prompt)
        let output = try await ProcessRunner.run(executable, arguments, currentDirectory: dir)
        guard let data = try? Data(contentsOf: answerURL), !data.isEmpty else {
            let detail = ProcessRunner.lastMeaningfulLine(output.stderr.isEmpty ? output.stdoutString : output.stderr)
            if detail.lowercased().contains("login") || detail.lowercased().contains("auth") {
                throw OpenAIError.http(401, "Codex is not signed in. Run `codex login` in Terminal once.")
            }
            throw OpenAIError.badJSON("Codex gave no answer: \(detail)")
        }
        // The last message may wrap the JSON in a code fence.
        let text = String(decoding: data, as: UTF8.self)
        if let start = text.firstIndex(of: "{"), let end = text.lastIndex(of: "}") {
            return Data(text[start...end].utf8)
        }
        return data
    }
}

nonisolated enum CodexLocator {
    /// The Codex CLI bundled with the ChatGPT / Codex apps is kept up to date by the app, so it comes first.
    static var path: String? {
        let bundled = [
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "/Applications/Codex.app/Contents/Resources/codex",
        ]
        if let path = bundled.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) { return path }
        return ToolLocator.find("codex", override: "")
    }
}
