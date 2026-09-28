import Foundation
import Observation
import SwiftData

nonisolated enum CompileError: LocalizedError, Sendable {
    case noConsent
    case noTranscript
    case tooLong(Int)
    case budget(Int, Int)
    case nothingReusable

    var errorDescription: String? {
        switch self {
        case .noConsent: "This engine sends the transcript to OpenAI: allow it in Settings › Skills, or choose the local AI (free)."
        case .noTranscript: "Eat the video first: there is no transcript yet."
        case .tooLong(let chars): "This transcript is too long for one analysis (\(chars / 1000)k characters, limit 200k)."
        case .budget(let used, let limit): "Today's OpenAI budget is used (\(used) of \(limit) tokens). Raise it in Settings or try tomorrow."
        case .nothingReusable: "The model found nothing reusable enough to become a skill in this video."
        }
    }
}

/// Turns an eaten video into reviewed Agent Skills (the original YouTube Zeus feature).
/// Free by default: the local AI writes the draft; Codex or the OpenAI API only when chosen and allowed.
@Observable
final class SkillCompiler {
    let settings: AppSettings
    private(set) var compiling: Set<String> = []

    init(settings: AppSettings) { self.settings = settings }

    var usesLocal: Bool { settings.skillEngine == "local" }

    var localInstalled: Bool {
        FileManager.default.fileExists(atPath: "/Applications/Ollama.app") || ToolLocator.find("ollama", override: "") != nil
    }

    var hasKey: Bool { !(KeychainStore.read("openai") ?? "").isEmpty }

    var codexPath: String? { CodexLocator.path }

    var usesCodex: Bool { settings.skillEngine == "codex" && codexPath != nil }

    /// Ready to compile: the local AI is installed, or consent is given and a cloud engine is available.
    var isReady: Bool { usesLocal ? localInstalled : settings.openAIConsent && (usesCodex || hasKey) }

    var engineLabel: String {
        if usesLocal { return "the local AI (\(settings.polishModel))" }
        return usesCodex ? "Codex" + (settings.codexModel.isEmpty ? "" : " (\(settings.codexModel))") : settings.openAIModel
    }

    var notReadyMessage: String {
        if usesLocal { return "Install Ollama (free, ollama.com) to make skills on this Mac." }
        if !settings.openAIConsent { return "Allow sending transcripts to OpenAI in Settings › Codex skills." }
        if settings.skillEngine == "codex", codexPath == nil { return "The Codex CLI was not found. Install it (npm i -g @openai/codex) or use an API key." }
        return "Add your OpenAI API key in Settings › Codex skills."
    }

    // MARK: Compile

    func compile(_ video: Video, context: ModelContext) async throws -> [SkillDraft] {
        guard usesLocal || settings.openAIConsent else { throw CompileError.noConsent }
        var paragraphs = video.paragraphs
        guard !paragraphs.isEmpty else { throw CompileError.noTranscript }
        if usesLocal {
            // The local model reads about 10k tokens at once: long videos are analysed from the start.
            var size = 0
            paragraphs = Array(paragraphs.prefix { paragraph in
                size += paragraph.text.count + 40
                return size <= 36_000
            })
        }

        let input: [String: Any] = [
            "video": [
                "title": video.displayTitle,
                "channel": video.channelTitle,
                "url": video.url.absoluteString,
                "language": video.language,
                "transcript_source": video.source.rawValue,
            ],
            "paragraphs": paragraphs.map { ["id": $0.tag, "start": Int($0.start), "end": Int($0.end), "text": $0.text] },
        ]
        let userData = try JSONSerialization.data(withJSONObject: input, options: [.sortedKeys, .withoutEscapingSlashes])
        let user = String(decoding: userData, as: UTF8.self)
        guard user.count <= 200_000 else { throw CompileError.tooLong(user.count) }

        compiling.insert(video.videoID)
        defer { compiling.remove(video.videoID) }

        let system = AnalysisPrompt.system(skillLanguage: settings.skillLanguage)
        let json: Data
        let modelName: String
        if usesLocal {
            AppLog.write("SKILLS \(video.videoID): asking the local AI (\(settings.polishModel)), \(paragraphs.count) paragraphs")
            json = try await LocalLLM(model: settings.polishModel, context: 16_384)
                .structured(system: system, user: user, schema: AnalysisPrompt.schema)
            modelName = settings.polishModel + " (local)"
        } else if usesCodex, let codex = codexPath {
            AppLog.write("SKILLS \(video.videoID): asking Codex (\(codex))")
            json = try await CodexCLIClient(executable: codex, model: settings.codexModel)
                .structured(system: system, user: user, schema: AnalysisPrompt.schema)
            modelName = engineLabel
        } else {
            guard let key = KeychainStore.read("openai"), !key.isEmpty else { throw OpenAIError.noKey }
            let estimate = (user.count + 3_000) / 4 + 24_000
            let used = settings.tokensUsedToday()
            guard used + estimate <= settings.dailyTokenLimit else { throw CompileError.budget(used, settings.dailyTokenLimit) }
            let response = try await OpenAIClient(apiKey: key, model: settings.openAIModel)
                .structured(system: system, user: user, schemaName: "zeus_analysis", schema: AnalysisPrompt.schema)
            settings.addTokens(response.inputTokens + response.outputTokens)
            json = response.json
            modelName = settings.openAIModel
        }

        AppLog.write("SKILLS \(video.videoID): answer received (\(json.count) bytes)")
        let result: AnalysisResult
        do {
            result = try JSONDecoder().decode(AnalysisResult.self, from: json)
        } catch {
            throw OpenAIError.badJSON(error.localizedDescription)
        }
        let checked = Self.verify(result, paragraphs: paragraphs)
        guard !checked.capabilities.isEmpty else { throw CompileError.nothingReusable }

        var drafts: [SkillDraft] = []
        for capability in checked.capabilities.prefix(3) {
            let used = capability.referencedEvidence
            let evidence = checked.evidence.filter { used.contains($0.evidence_id) }
            let draft = SkillDraft(
                name: SkillRenderer.slug(capability.title),
                title: capability.title,
                capabilityData: try JSONEncoder().encode(capability),
                evidenceData: try JSONEncoder().encode(evidence),
                droppedEvidence: checked.dropped,
                model: modelName,
                video: video)
            context.insert(draft)
            drafts.append(draft)
        }
        try? context.save()
        return drafts
    }

    /// Keeps only evidence whose excerpt really appears in the cited paragraph (or a neighbour),
    /// then drops statements and citations that lost all their evidence.
    static func verify(_ result: AnalysisResult, paragraphs: [TranscriptParagraph]) -> (evidence: [EvidenceItem], capabilities: [Capability], dropped: Int) {
        let byTag = Dictionary(uniqueKeysWithValues: paragraphs.map { ($0.tag, $0) })
        var kept: [EvidenceItem] = []
        var rename: [String: String] = [:]
        var dropped = 0
        for (index, raw) in result.evidence.enumerated() {
            var item = raw
            var id = item.evidence_id.lowercased()
            if id.range(of: "^[a-z0-9][a-z0-9._-]{2,99}$", options: .regularExpression) == nil || rename.values.contains(id) {
                id = String(format: "ev-%03d", index + 1)
            }
            let needle = words(item.excerpt)
            guard needle.count >= 2, let paragraph = byTag[item.paragraph_id] else { dropped += 1; continue }
            let neighbours = [paragraph.id - 1, paragraph.id, paragraph.id + 1]
                .filter { paragraphs.indices.contains($0) }.map { paragraphs[$0] }
            if words(paragraph.text).contains(needle) {
                item.start_seconds = paragraph.start
                item.end_seconds = paragraph.end
            } else if words(neighbours.map(\.text).joined(separator: " ")).contains(needle) {
                item.start_seconds = neighbours.first?.start
                item.end_seconds = neighbours.last?.end
            } else {
                dropped += 1
                continue
            }
            rename[raw.evidence_id] = id
            item.evidence_id = id
            item.confidence = min(1, max(0, item.confidence))
            kept.append(item)
        }

        func fix(_ statements: [EvidenceBackedStatement]) -> [EvidenceBackedStatement] {
            statements.compactMap { statement in
                let ids = statement.evidence_ids.compactMap { rename[$0] }
                return ids.isEmpty ? nil : EvidenceBackedStatement(text: statement.text, evidence_ids: ids)
            }
        }
        let capabilities = result.capabilities.map { raw -> Capability in
            var capability = raw
            capability.steps = fix(raw.steps)
            capability.claims = fix(raw.claims)
            capability.recommendations = fix(raw.recommendations)
            capability.citations = raw.citations.compactMap { citation in
                rename[citation.evidence_id].map { CitationDefinition(evidence_id: $0, label: citation.label) }
            }
            return capability
        }
        return (kept, capabilities, dropped)
    }

    /// Lowercased letters and digits only, one space between words: robust to caption punctuation.
    nonisolated static func words(_ text: String) -> String {
        let folded = text.lowercased().replacingOccurrences(of: "’", with: "'")
        return folded.components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }.joined(separator: " ")
    }

    // MARK: Review

    func publishedMeta(for name: String) -> [String: Any]? {
        let url = settings.publishFolderURL.appendingPathComponent(name).appendingPathComponent(".zeus.json")
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    func existsWithoutZeus(_ name: String) -> Bool {
        let folder = settings.publishFolderURL.appendingPathComponent(name)
        return FileManager.default.fileExists(atPath: folder.path) && publishedMeta(for: name) == nil
    }

    func nextVersion(for draft: SkillDraft) -> (version: String, isUpdate: Bool, previousChanges: String?) {
        if draft.status == .published { return (draft.version, false, nil) }
        guard let meta = publishedMeta(for: draft.name), let version = meta["version"] as? String else {
            return ("1.0.0", false, nil)
        }
        let parts = version.split(separator: ".").compactMap { Int($0) }
        let next = parts.count == 3 ? "\(parts[0]).\(parts[1] + 1).0" : "1.1.0"
        let changes = try? String(contentsOf: settings.publishFolderURL
            .appendingPathComponent(draft.name).appendingPathComponent("references/changes.md"), encoding: .utf8)
        return (next, true, changes)
    }

    func files(for draft: SkillDraft) -> [String: String] {
        guard let capability = draft.capability else { return [:] }
        let (version, isUpdate, previous) = nextVersion(for: draft)
        let video = draft.video
        let source = SkillRenderer.Source(
            videoTitle: video?.displayTitle ?? "unknown video",
            videoURL: video?.url.absoluteString ?? "",
            channel: video?.channelTitle ?? "",
            model: draft.model)
        return SkillRenderer.render(name: draft.name, version: version, capability: capability, evidence: draft.evidence,
                                    source: source, isUpdate: isUpdate, previousChanges: previous,
                                    skillMDOverride: draft.skillMDOverride)
    }

    func issues(for draft: SkillDraft, files: [String: String]) -> [SkillIssue] {
        guard let capability = draft.capability else {
            return [SkillIssue(code: "UNREADABLE", message: "This draft could not be read.", path: "", line: nil, mandatory: true)]
        }
        return SkillValidator.validate(files: files, name: draft.name, evidence: draft.evidence, capability: capability,
                                       collision: draft.status != .published && existsWithoutZeus(draft.name),
                                       droppedEvidence: draft.droppedEvidence)
    }

    /// Writes the four files into the publish folder, keeping a copy of any previous version.
    func publish(_ draft: SkillDraft, context: ModelContext) throws {
        let files = files(for: draft)
        guard !issues(for: draft, files: files).contains(where: \.mandatory) else {
            throw OpenAIError.badJSON("fix the blocking issues first")
        }
        let (version, _, _) = nextVersion(for: draft)
        let fm = FileManager.default
        let folder = settings.publishFolderURL.appendingPathComponent(draft.name, isDirectory: true)
        if fm.fileExists(atPath: folder.path) {
            let stamp = ISO8601DateFormatter().string(from: .now).replacingOccurrences(of: ":", with: "-")
            let backup = AppFolders.skillHistory.appendingPathComponent(draft.name).appendingPathComponent(stamp)
            try fm.createDirectory(at: backup.deletingLastPathComponent(), withIntermediateDirectories: true)
            try fm.copyItem(at: folder, to: backup)
            try fm.removeItem(at: folder)
        }
        for (path, text) in files {
            let url = folder.appendingPathComponent(path)
            try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try text.write(to: url, atomically: true, encoding: .utf8)
        }
        let meta: [String: Any] = [
            "made_by": "YouTube Zeus 2.0",
            "version": version,
            "video_id": draft.video?.videoID ?? "",
            "video_url": draft.video?.url.absoluteString ?? "",
            "model": draft.model,
            "published_at": ISO8601DateFormatter().string(from: .now),
        ]
        let data = try JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        try data.write(to: folder.appendingPathComponent(".zeus.json"))
        draft.version = version
        draft.status = .published
        draft.publishedAt = .now
        draft.publishedPath = folder.path
        try? context.save()
    }
}
