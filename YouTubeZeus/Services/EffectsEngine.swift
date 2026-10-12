import AppKit
import Foundation
import Observation

// Zeus Effects (3.8): a free, local-only version of Sirio Berati's open-source "Genjutsu" workflow (MIT).
// Keep how people move (depth, masks, face mesh), replace who they are with the user's reference photo, put the
// clip's original soundtrack back. The engine is Python (`effects/` in the repository, shipped inside the app as
// Contents/Resources/effects); its contract is effects/SPEC.md. This file runs it and follows its JSON lines.

// MARK: - Where things live

nonisolated enum EffectsPaths {
    /// The runtime folder (venv, weights, jobs): `ZEUS_EFFECTS_HOME`, else
    /// ~/Library/Application Support/YouTube Zeus/Effects (the engine follows the same rule).
    static var home: URL {
        if let custom = ProcessInfo.processInfo.environment["ZEUS_EFFECTS_HOME"], !custom.isEmpty {
            return URL(fileURLWithPath: (custom as NSString).expandingTildeInPath, isDirectory: true)
        }
        return AppFolders.support.appendingPathComponent("Effects", isDirectory: true)
    }

    static var python: URL { home.appendingPathComponent(".venv/bin/python") }
    static var jobs: URL { home.appendingPathComponent("Jobs", isDirectory: true) }
    static var log: URL { home.appendingPathComponent("effects.log") }
    /// "Save to Movies" copies results here.
    static var movies: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Movies/YouTube Zeus Effects", isDirectory: true)
    }

    /// The runtime Python exists (setup.sh has run).
    static var isSetUp: Bool { FileManager.default.isExecutableFile(atPath: python.path) }

    /// The engine folder (zeus_effects.py, setup.sh…): inside the app, else the repository's effects/ (development).
    static var engine: URL? {
        candidates.first { FileManager.default.fileExists(atPath: $0.appendingPathComponent("zeus_effects.py").path) }
    }

    static var script: URL? { engine?.appendingPathComponent("zeus_effects.py") }

    private static let sourceFile = #filePath

    static var candidates: [URL] {
        var list: [URL] = []
        if let resources = Bundle.main.resourceURL {
            list.append(resources.appendingPathComponent("effects", isDirectory: true))
        }
        // <repo>/YouTubeZeus/Services/EffectsEngine.swift → <repo>/effects
        let repo = URL(fileURLWithPath: sourceFile).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        list.append(repo.appendingPathComponent("effects", isDirectory: true))
        list.append(FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Developer/YouTubeZeus/effects", isDirectory: true))
        return list
    }

    /// `bash` arguments that install the main engine, or one of the optional generation engines
    /// (setup-engines.sh when the engine has it, else setup.sh with the same flag).
    static func setupCommand(_ setup: EffectsSetup) -> [String]? {
        guard let engine else { return nil }
        let main = engine.appendingPathComponent("setup.sh").path
        guard let flag = setup.flag else { return [main] }
        let engines = engine.appendingPathComponent("setup-engines.sh").path
        return [FileManager.default.fileExists(atPath: engines) ? engines : main, flag]
    }

    /// The environment of every engine run: Homebrew and uv on the PATH, the runtime folder, unbuffered output, and
    /// Python's bytecode cache outside the (signed) app bundle.
    static func environment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let user = FileManager.default.homeDirectoryForCurrentUser.path
        environment["PATH"] = ProcessRunner.searchPath + ":\(user)/.local/bin:\(user)/.cargo/bin"
            + (environment["PATH"].map { ":" + $0 } ?? "")
        environment["ZEUS_EFFECTS_HOME"] = home.path
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONPYCACHEPREFIX"] = home.appendingPathComponent("pycache", isDirectory: true).path
        environment["LANG"] = environment["LANG"] ?? "en_US.UTF-8"
        return environment
    }
}

// MARK: - Choices

nonisolated enum EffectsMode: String, CaseIterable, Identifiable, Sendable {
    case depth
    case depthMesh = "depth_mesh"
    case mesh
    case pose
    case depthPose = "depth_pose"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .depth: "Depth"
        case .depthMesh: "Depth + face mesh"
        case .mesh: "Face mesh"
        case .pose: "Pose"
        case .depthPose: "Depth + pose"
        }
    }

    var explanation: String {
        switch self {
        case .depth: "The people become coloured depth over the original background: the most freedom for a full replacement."
        case .depthMesh: "Depth plus a white face mesh on top: keeps lips, eyes and expressions."
        case .mesh: "The original picture with the face mesh on top: the lightest guide."
        case .pose: "Whole-body motion capture (body, hands, face): the coloured skeleton on black guides the model, so the new subject can have any shape. Also saves the movement as motion.json and motion.bvh."
        case .depthPose: "Grey depth of the people with the coloured body and hand skeleton on top, plus the saved movement (motion.json, motion.bvh)."
        }
    }

    static func label(_ raw: String) -> String { EffectsMode(rawValue: raw)?.label ?? raw }
}

nonisolated enum EffectsGenerator: String, CaseIterable, Identifiable, Sendable {
    case faceswap
    case vace

    var id: String { rawValue }

    var label: String {
        switch self {
        case .faceswap: "Face swap · fast"
        case .vace: "Full replace · slow (VACE)"
        }
    }

    var explanation: String {
        switch self {
        case .faceswap: "FaceFusion swaps the face only, on the original clip, at full quality. About 10 s per second of video."
        case .vace: "Wan2.1-VACE 1.3B redraws the whole person from the depth and the mask. About 10 minutes per second of video at 360p; still experimental (hands can smear)."
        }
    }
}

nonisolated enum EffectsSetup: Equatable, Sendable {
    /// The main engine (Python, depth, masks, face mesh).
    case main
    /// FaceFusion (face swap).
    case faceSwap
    /// Wan2.1-VACE-1.3B weights (full replacement, about 19 GB).
    case fullReplace

    var flag: String? {
        switch self {
        case .main: nil
        case .faceSwap: "--faceswap"
        case .fullReplace: "--vace"
        }
    }

    var label: String {
        switch self {
        case .main: "Setting up the Effects engine"
        case .faceSwap: "Adding the face swap engine"
        case .fullReplace: "Adding the full replace engine (about 19 GB)"
        }
    }
}

// MARK: - JSON lines

nonisolated enum EffectsJSON {
    static func any(_ text: String) -> Any? {
        guard !text.isEmpty else { return nil }
        return try? JSONSerialization.jsonObject(with: Data(text.utf8), options: [.fragmentsAllowed])
    }

    /// What a command printed as JSON: the whole output (it may be pretty-printed), else its last JSON line that is
    /// not a progress line. A final `{"done":true,"result":…}` is unwrapped to its result.
    static func output(_ stdout: String) -> Any? {
        var found = any(stdout.trimmingCharacters(in: .whitespacesAndNewlines))
        if found == nil {
            for line in stdout.split(whereSeparator: \.isNewline).reversed() {
                let text = line.trimmingCharacters(in: .whitespaces)
                guard text.hasPrefix("{") || text.hasPrefix("["), let value = any(text) else { continue }
                if let object = value as? [String: Any], object["stage"] != nil, object["done"] == nil, object["error"] == nil,
                   object.count <= 3 { continue }
                found = value
                break
            }
        }
        if let object = found as? [String: Any], bool(object["done"]) == true, let result = object["result"], !(result is NSNull) {
            return result
        }
        return found
    }

    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    static func number(_ value: Any?) -> Double? {
        if let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() { return number.doubleValue }
        return (value as? String).flatMap(Double.init)
    }

    static func string(_ value: Any?) -> String? {
        switch value {
        case let text as String: return text
        case let number as NSNumber where CFGetTypeID(number) != CFBooleanGetTypeID():
            let double = number.doubleValue
            return double == double.rounded() && abs(double) < 1e15 ? String(Int(double)) : String(format: "%.2f", double)
        default: return nil
        }
    }
}

/// One line of the engine's stdout (SPEC: progress lines, then `done` or `error`). Make it me (MAKE-IT-ME.md) adds a
/// first `plan` line with the step names and an `overall` 0–1 of the whole job on every progress line.
nonisolated enum EffectsEvent: Equatable, Sendable {
    case progress(stage: String, fraction: Double?, message: String, overall: Double?)
    case plan(steps: [String], job: String?)
    case done(job: String?)
    case failed(message: String, stage: String?)
    /// Anything that is not one of the JSON lines above (kept for the details log).
    case text(String)

    static func parse(_ line: String) -> EffectsEvent {
        guard line.hasPrefix("{"), let object = EffectsJSON.any(line) as? [String: Any] else { return .text(line) }
        if let error = object["error"], !(error is NSNull) {
            return .failed(message: EffectsJSON.string(error) ?? "The engine stopped.", stage: object["stage"] as? String)
        }
        if EffectsJSON.bool(object["done"]) == true {
            let result = object["result"] as? [String: Any]
            return .done(job: (object["job"] as? String) ?? (result?["job"] as? String) ?? (result?["dir"] as? String))
        }
        if let stage = object["stage"] as? String {
            if stage == "plan", let steps = object["steps"] as? [String] {
                return .plan(steps: steps, job: object["job"] as? String)
            }
            let fraction = EffectsJSON.number(object["progress"]).map { min(1, max(0, $0)) }
            let overall = EffectsJSON.number(object["overall"]).map { min(1, max(0, $0)) }
            return .progress(stage: stage, fraction: fraction, message: EffectsJSON.string(object["message"]) ?? "",
                             overall: overall)
        }
        return .text(line)
    }
}

// MARK: - Make it me

/// The `makeme` section of job.json (effects/MAKE-IT-ME.md): the one-button job's steps and result.
nonisolated struct EffectsMakeMe: Hashable, Sendable {
    nonisolated struct Step: Hashable, Sendable, Identifiable {
        var name: String
        var status: String       // pending · running · done · skipped · failed · cancelled
        var seconds: Double?
        var message: String?
        var id: String { name }
        var label: String { EffectsMakeMe.label(name) }
    }

    var status = ""              // running · done · failed · cancelled
    var plan: [String] = []
    var steps: [Step] = []
    var result: String?          // final.mp4 or final-voice.mp4
    var compare: String?
    var comment: String?
    var error: String?
    var swapJob: String?
    /// The options the job was started with (input, start, duration, self_video, ref_pack, refs, voice, quality,
    /// hat, hair, person…): what "Continue" passes again.
    var options: [String: String] = [:]
    var photos: [String] = []
    var hatFound: Bool?

    init?(_ object: [String: Any]?) {
        guard let object else { return nil }
        status = EffectsJSON.string(object["status"]) ?? ""
        plan = object["plan"] as? [String] ?? []
        let table = object["steps"] as? [String: Any] ?? [:]
        let names = plan.isEmpty ? table.keys.sorted() : plan
        steps = names.map { name in
            let item = table[name] as? [String: Any] ?? [:]
            return Step(name: name, status: EffectsJSON.string(item["status"]) ?? "pending",
                        seconds: EffectsJSON.number(item["seconds"]), message: EffectsJSON.string(item["message"]))
        }
        result = EffectsJSON.string(object["result"])
        compare = EffectsJSON.string(object["compare"])
        comment = EffectsJSON.string(object["comment"])
        error = EffectsJSON.string(object["error"])
        swapJob = EffectsJSON.string(object["swapjob"])
        let raw = object["options"] as? [String: Any] ?? [:]
        for (key, value) in raw {
            if let bool = EffectsJSON.bool(value) { options[key] = bool ? "true" : "false" }
            else if let text = EffectsJSON.string(value) { options[key] = text }
        }
        photos = raw["refs"] as? [String] ?? []
        if let hat = table["hat"] as? [String: Any], let status = EffectsJSON.string(hat["status"]) {
            hatFound = status == "done" ? true : (status == "skipped" ? false : nil)
        }
    }

    var isRunning: Bool { status == "running" }
    var isDone: Bool { status == "done" }
    var canContinue: Bool { status == "failed" || status == "cancelled" }
    var fromTheSwap: Bool { swapJob != nil }

    var statusLabel: String {
        switch status {
        case "running": "Make it me · running"
        case "done": "Make it me · done"
        case "failed": "Make it me · failed"
        case "cancelled": "Make it me · cancelled"
        default: "Make it me"
        }
    }

    static func label(_ step: String) -> String {
        switch step {
        case "plan": "Plan"
        case "prepare": "Prepare"
        case "refs": "References"
        case "approve": "Approve"
        case "faceswap": "Face swap"
        case "hat": "Hat"
        case "voice": "Voice"
        case "final": "Final"
        case "waiting": "Waiting"
        default: step.capitalized
        }
    }

    static func explanation(_ step: String) -> String {
        switch step {
        case "prepare": "Download or trim the clip, depth, people mask and face mesh."
        case "refs": "The reference frames (face pack, self-video or photos) and the voice sample."
        case "approve": "Recorded automatically: you consented and chose the clip."
        case "faceswap": "FaceFusion puts your face on, quality natural."
        case "hat": "A hat or cap found on the head is replaced with hair (VACE, about an hour)."
        case "voice": "Your voice says the same words with the same timing."
        case "final": "final.mp4, final-voice.mp4 and the side-by-side compare.mp4."
        default: ""
        }
    }
}

// MARK: - Doctor

/// `doctor --json`, read without assuming its exact keys: every flag becomes a check.
nonisolated struct EffectsDoctor: Equatable, Sendable {
    nonisolated struct Check: Identifiable, Equatable, Sendable {
        let name: String
        let ok: Bool
        let detail: String
        /// A value (version, memory, disk), not a yes/no.
        var info = false
        var id: String { name }
    }

    var checks: [Check] = []
    /// The main engine can prepare clips.
    var ready = false
    /// nil: the doctor did not say.
    var faceSwapReady: Bool?
    var fullReplaceReady: Bool?
    var raw = ""

    static let faceSwapWords = ["faceswap", "face_swap", "face swap", "facefusion"]
    static let fullReplaceWords = ["vace", "wan2"]
    /// Checks that do not stop the main engine (optional engines, GPU acceleration, warnings).
    static let optionalWords = faceSwapWords + fullReplaceWords + ["mps", "cuda", "optional", "warning"]

    static func parse(_ stdout: String) -> EffectsDoctor? {
        guard let object = EffectsJSON.output(stdout) as? [String: Any] else { return nil }
        var doctor = EffectsDoctor()
        if let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) {
            doctor.raw = String(decoding: data, as: UTF8.self)
        }
        visit(object, path: [], into: &doctor.checks)
        // Problems first, then the yes/no checks, then plain values.
        doctor.checks.sort { ($0.ok ? 1 : 0, $0.info ? 1 : 0, $0.name) < ($1.ok ? 1 : 0, $1.info ? 1 : 0, $1.name) }
        func matching(_ words: [String]) -> [Check] {
            doctor.checks.filter { check in words.contains { check.name.lowercased().contains($0) } }
        }
        let face = matching(faceSwapWords)
        let full = matching(fullReplaceWords)
        doctor.faceSwapReady = face.isEmpty ? nil : face.allSatisfy(\.ok)
        doctor.fullReplaceReady = full.isEmpty ? nil : full.allSatisfy(\.ok)
        if let ok = EffectsJSON.bool(object["ok"]) ?? EffectsJSON.bool(object["ready"]) {
            doctor.ready = ok
        } else {
            let optional = Set(matching(optionalWords).map(\.name))
            doctor.ready = doctor.checks.filter { !optional.contains($0.name) }.allSatisfy(\.ok)
        }
        return doctor
    }

    private static let flagKeys = ["ok", "ready", "present", "available", "exists", "installed", "found"]
    private static let detailKeys = ["detail", "version", "message", "error", "reason", "path", "hint"]

    private static func visit(_ value: Any, path: [String], into checks: inout [Check]) {
        let name = path.map { $0.replacingOccurrences(of: "_", with: " ") }.joined(separator: " › ")
        if let object = value as? [String: Any] {
            if !path.isEmpty, let flag = flagKeys.lazy.compactMap({ EffectsJSON.bool(object[$0]) }).first {
                let detail = detailKeys.lazy.compactMap { EffectsJSON.string(object[$0]) }.first ?? ""
                checks.append(Check(name: name, ok: flag, detail: detail))
                return
            }
            for key in object.keys.sorted() {
                guard let child = object[key], !["done", "job", "stage"].contains(key) else { continue }
                if path.isEmpty, ["ok", "ready"].contains(key), EffectsJSON.bool(child) != nil { continue }
                visit(child, path: path + [key], into: &checks)
            }
        } else if let flag = EffectsJSON.bool(value) {
            checks.append(Check(name: name, ok: flag, detail: ""))
        } else if value is NSNull {
            checks.append(Check(name: name, ok: false, detail: "missing"))
        } else if let list = value as? [Any] {
            checks.append(Check(name: name, ok: true, detail: list.compactMap { EffectsJSON.string($0) }.joined(separator: ", "), info: true))
        } else if let text = EffectsJSON.string(value) {
            let lower = name.lowercased()
            let bad = lower.contains("error") || lower.contains("missing")
            checks.append(Check(name: name, ok: !bad, detail: text, info: !bad))
        }
    }
}

// MARK: - Jobs

nonisolated struct EffectsGeneration: Hashable, Sendable {
    var engine: String
    var created: String
    var final: String?
}

/// One job folder (`Jobs/<id>/job.json` and the files SPEC lists).
nonisolated struct EffectsJob: Identifiable, Hashable, Sendable {
    var id: String
    var folder: URL
    var created: Date?
    var input = ""
    var start: Double = 0
    var duration: Double = 0
    var mode = ""
    var status = ""
    var approvedAt: String?
    var artifacts: [String: String] = [:]
    var generations: [EffectsGeneration] = []
    /// The one-button job (Make it me), when job.json has a `makeme` section.
    var makeMe: EffectsMakeMe?
    /// job.json's modification time: players and pictures reload when it changes.
    var stamp: Double = 0

    init(folder: URL, object: [String: Any], stamp: Double) {
        self.folder = folder
        self.stamp = stamp
        id = EffectsJSON.string(object["id"]) ?? folder.lastPathComponent
        created = Self.date(object["created"])
        input = EffectsJSON.string(object["input"]) ?? ""
        start = EffectsJSON.number(object["start"]) ?? 0
        duration = EffectsJSON.number(object["duration"]) ?? 0
        mode = EffectsJSON.string(object["mode"]) ?? ""
        status = EffectsJSON.string(object["status"]) ?? ""
        makeMe = EffectsMakeMe(object["makeme"] as? [String: Any])
        if let review = object["review"] as? [String: Any] {
            let hashes = review["hashes"] as? [String: Any] ?? [:]
            approvedAt = EffectsJSON.string(review["approved_at"]) ?? (hashes.isEmpty ? nil : "")
        }
        artifacts = (object["artifacts"] as? [String: Any])?.compactMapValues { EffectsJSON.string($0) } ?? [:]
        generations = ((object["generations"] as? [[String: Any]]) ?? []).map { item in
            EffectsGeneration(engine: EffectsJSON.string(item["engine"]) ?? "",
                              created: EffectsJSON.string(item["created"]) ?? EffectsJSON.string(item["finished"]) ?? "",
                              final: EffectsJSON.string(item["final"]) ?? EffectsJSON.string(item["output"]))
        }
    }

    static func load(_ folder: URL) -> EffectsJob? {
        let file = folder.appendingPathComponent("job.json")
        guard let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let modified = (try? FileManager.default.attributesOfItem(atPath: file.path)[.modificationDate] as? Date) ?? nil
        return EffectsJob(folder: folder, object: object, stamp: modified?.timeIntervalSince1970 ?? 0)
    }

    /// A file of the job (from `artifacts`, else by its usual name), when it exists.
    func file(_ name: String) -> URL? {
        let stem = (name as NSString).deletingPathExtension
        var paths: [String] = []
        for key in [name, stem, stem.replacingOccurrences(of: "-", with: "_")] {
            if let path = artifacts[key] { paths.append(path) }
        }
        paths.append(name)
        for path in paths {
            let url = path.hasPrefix("/") ? URL(fileURLWithPath: path) : folder.appendingPathComponent(path)
            if FileManager.default.fileExists(atPath: url.path) { return url }
        }
        return nil
    }

    var isApproved: Bool { approvedAt != nil }
    var isPrepared: Bool { file("control.mp4") != nil || file("preview.jpg") != nil }
    var final: URL? { file("final.mp4") }
    /// What to play and save: Make it me's result (final-voice.mp4 when the voice ran), else final.mp4.
    var resultVideo: URL? {
        if let name = makeMe?.result, let url = file(name) { return url }
        if makeMe != nil, let voice = file("final-voice.mp4") { return voice }
        return final
    }
    var compare: URL? { file("compare.mp4") }
    var swapResult: URL? { file("final-swap.mp4") }

    var youtubeID: String? {
        if case .video(let id) = YouTubeLink.parse(input), input.lowercased().hasPrefix("http") || input.count == 11 { return id }
        return nil
    }

    /// The source's name when it is not a video of the library.
    var inputName: String {
        if let id = youtubeID { return "YouTube video \(id)" }
        return input.isEmpty ? id : (input as NSString).lastPathComponent
    }

    var range: String { "\(start.timestamp)–\((start + duration).timestamp)" }

    var statusLabel: String {
        if let makeMe { return makeMe.statusLabel }
        if final != nil { return "Generated" }
        if isApproved { return "Approved" }
        return status.isEmpty ? (isPrepared ? "Prepared" : "Not ready") : status.replacingOccurrences(of: "_", with: " ").capitalized
    }

    static func date(_ value: Any?) -> Date? {
        if let seconds = EffectsJSON.number(value), seconds > 1_000_000_000 { return Date(timeIntervalSince1970: seconds) }
        guard let text = value as? String else { return nil }
        let full = ISO8601DateFormatter()
        full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = full.date(from: text) { return date }
        full.formatOptions = [.withInternetDateTime]
        if let date = full.date(from: text) { return date }
        // "2026-10-10T21:30:00" without a zone: local time.
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss"] {
            local.dateFormat = format
            if let date = local.date(from: text) { return date }
        }
        return nil
    }
}

nonisolated enum EffectsJobs {
    /// `list --json` in any of the shapes an engine may print: an array, `{"jobs": […]}`, or the final `done` line.
    static func parseList(_ stdout: String) -> [EffectsJob]? {
        guard let value = EffectsJSON.output(stdout) else { return nil }
        let items: [Any]
        if let array = value as? [Any] {
            items = array
        } else if let object = value as? [String: Any], let array = object["jobs"] as? [Any] {
            items = array
        } else {
            return nil
        }
        let jobs: [EffectsJob] = items.compactMap { item in
            if let path = item as? String { return EffectsJob.load(folder(path)) }
            guard let object = item as? [String: Any] else { return nil }
            let path = ["dir", "job_dir", "folder", "path", "job"].lazy.compactMap { object[$0] as? String }.first
            let place = path.map(folder) ?? EffectsPaths.jobs.appendingPathComponent(EffectsJSON.string(object["id"]) ?? "")
            return EffectsJob.load(place) ?? EffectsJob(folder: place, object: object, stamp: 0)
        }
        return sorted(jobs)
    }

    /// Reads Jobs/*/job.json directly (when the engine cannot run).
    static func scan() -> [EffectsJob] {
        let folders = (try? FileManager.default.contentsOfDirectory(at: EffectsPaths.jobs, includingPropertiesForKeys: nil)) ?? []
        return sorted(folders.compactMap(EffectsJob.load))
    }

    /// A job folder from a path the engine printed (absolute, relative to Jobs/, or its job.json).
    static func folder(_ path: String) -> URL {
        var url = path.hasPrefix("/") ? URL(fileURLWithPath: path) : EffectsPaths.jobs.appendingPathComponent(path)
        if url.lastPathComponent == "job.json" { url.deleteLastPathComponent() }
        return url.standardizedFileURL
    }

    static func sorted(_ jobs: [EffectsJob]) -> [EffectsJob] {
        jobs.sorted { ($0.created ?? .distantPast, $0.id) > ($1.created ?? .distantPast, $1.id) }
    }
}

// MARK: - Arguments

nonisolated enum EffectsArguments {
    static let maxDuration = 10.0

    static func prepare(input: String, start: Double, duration: Double, mode: EffectsMode, cookies: URL?) -> [String] {
        var arguments = ["prepare", "--input", input, "--start", number(max(0, start)),
                         "--duration", number(min(maxDuration, max(0.5, duration))), "--mode", mode.rawValue]
        if let cookies, input.lowercased().hasPrefix("http") { arguments += ["--cookies", cookies.path] }
        return arguments
    }

    static func generate(job: URL, engine: EffectsGenerator, references: [URL], facePack: URL? = nil, prompt: String,
                         consent: Bool, frames: Int, steps: Int, size: String, seed: Int?, enhance: Bool = false,
                         quality: String? = nil) -> [String] {
        var arguments = ["generate", "--job", job.path, "--engine", engine.rawValue]
        if engine == .faceswap && enhance { arguments.append("--enhance") }
        if engine == .faceswap, let quality { arguments += ["--quality", quality] }
        for reference in references { arguments += ["--ref", reference.path] }
        if let facePack { arguments += ["--ref-pack", facePack.path] }
        let text = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { arguments += ["--prompt", text] }
        if consent { arguments.append("--consent") }
        if engine == .vace { arguments += ["--frames", String(frames), "--steps", String(steps), "--size", size] }
        if let seed { arguments += ["--seed", String(seed)] }
        return arguments
    }

    /// `makeme` (effects/MAKE-IT-ME.md): the whole job in one go. `me` is a face pack folder, a self-video or photos.
    static func makeMe(input: String, start: Double, duration: Double, me: EffectsMe, voice: URL?, voiceOn: Bool,
                       quality: String, hat: Bool, hair: String, person: String, consent: Bool, job: URL?,
                       cookies: URL?) -> [String] {
        var arguments = ["makeme", "--input", input, "--start", number(max(0, start)),
                         "--duration", number(min(maxDuration, max(0.5, duration)))]
        switch me {
        case .facePack(let folder): arguments += ["--ref-pack", folder.path]
        case .selfVideo(let file): arguments += ["--self-video", file.path]
        case .photos(let files): for file in files { arguments += ["--ref", file.path] }
        }
        if let voice { arguments += ["--voice", voice.path] }
        if !voiceOn { arguments.append("--no-voice") }
        arguments += ["--quality", quality, "--hat", hat ? "remove" : "keep"]
        let hairText = hair.trimmingCharacters(in: .whitespacesAndNewlines)
        if !hairText.isEmpty { arguments += ["--hair", hairText] }
        let name = person.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { arguments += ["--person", name] }
        if consent { arguments.append("--consent") }
        if let job { arguments += ["--job", job.path] }
        if let cookies, input.lowercased().hasPrefix("http") { arguments += ["--cookies", cookies.path] }
        return arguments
    }

    /// The same job again, from what job.json `makeme.options` recorded (a failed or cancelled job continues where
    /// it stopped: finished steps are kept by the engine's hash markers).
    static func continueMakeMe(_ job: EffectsJob, cookies: URL?) -> [String]? {
        guard let record = job.makeMe, let input = record.options["input"], !input.isEmpty else { return nil }
        let me: EffectsMe
        if let pack = record.options["ref_pack"], !pack.isEmpty {
            me = .facePack(URL(fileURLWithPath: pack))
        } else if let video = record.options["self_video"], !video.isEmpty {
            me = .selfVideo(URL(fileURLWithPath: video))
        } else if !record.photos.isEmpty {
            me = .photos(record.photos.map { URL(fileURLWithPath: $0) })
        } else {
            return nil
        }
        let start = Double(record.options["start"] ?? "") ?? job.start
        let duration = Double(record.options["duration"] ?? "") ?? max(0.5, job.duration)
        let voice = record.options["voice"].flatMap { $0.isEmpty ? nil : URL(fileURLWithPath: $0) }
        return makeMe(input: input, start: start, duration: duration, me: me, voice: voice,
                      voiceOn: record.options["voice_on"] != "false", quality: record.options["quality"] ?? "natural",
                      hat: record.options["hat"] == "remove", hair: record.options["hair"] ?? "",
                      person: record.options["person"] ?? "", consent: record.options["consent"] == "true",
                      job: job.folder, cookies: cookies)
    }

    static func number(_ value: Double) -> String {
        value == value.rounded() ? String(Int(value)) : String(format: "%.2f", value)
    }
}

/// Who the person becomes, for Make it me.
nonisolated enum EffectsMe: Equatable, Sendable {
    case facePack(URL)
    case selfVideo(URL)
    case photos([URL])
}

nonisolated enum EffectsMeSource: String, CaseIterable, Identifiable, Sendable {
    case selfVideo = "self-video"
    case facePack = "face-pack"
    case photos

    var id: String { rawValue }

    var label: String {
        switch self {
        case .selfVideo: "Self-video"
        case .facePack: "Face pack"
        case .photos: "Photos"
        }
    }

    var explanation: String {
        switch self {
        case .selfVideo: "Film yourself 30–60 s, chest up, soft light, no hat: turn left and right, up and down, talk for 20 seconds, smile. Zeus keeps the sharpest frames per angle (eyes open) and takes your voice from it."
        case .facePack: "A .c101face pack from Cinema 101 or The Swap (Scan my face): several angles of your face."
        case .photos: "One to four clear photos of the face, front and sides."
        }
    }
}

nonisolated enum EffectsVoiceSource: String, CaseIterable, Identifiable, Sendable {
    case selfVideo = "self-video"
    case file
    case keep

    var id: String { rawValue }

    var label: String {
        switch self {
        case .selfVideo: "From the self-video"
        case .file: "Voice file"
        case .keep: "Keep the original voice"
        }
    }
}

// MARK: - Face packs

/// A face pack from Cinema 101's "Scan my face" (`<Name>.c101face`, effects/FACE-PACK.md), as the engine read it
/// (`facepack --pack <dir> --json`): the frames generate --ref-pack will use.
nonisolated struct EffectsFacePack: Equatable, Sendable {
    struct Pick: Equatable, Sendable, Identifiable {
        var file: URL
        var slot: String
        var why: String
        var id: String { file.path }
    }

    var folder: URL
    var name: String
    var person: String?
    var frames = 0
    var usable = 0
    var picked: [Pick] = []

    /// "12 usable frames of 14 · 5 used" (full replace uses the first 2).
    func summary(for engine: EffectsGenerator) -> String {
        let used = engine == .vace ? min(2, picked.count) : picked.count
        let total = frames > usable ? " of \(frames)" : ""
        return "\(usable) usable frame\(usable == 1 ? "" : "s")\(total) · \(used) used" + (person.map { " · \($0)" } ?? "")
    }

    static func parse(_ stdout: String, folder: URL) -> EffectsFacePack? {
        guard let object = EffectsJSON.output(stdout) as? [String: Any],
              let picks = object["picked"] as? [[String: Any]], !picks.isEmpty else { return nil }
        let path = (object["pack"] as? String).map { URL(fileURLWithPath: $0) } ?? folder
        var pack = EffectsFacePack(folder: path, name: EffectsJSON.string(object["name"]) ?? EffectsFacePacks.title(path),
                                   person: EffectsJSON.string(object["person"]))
        pack.frames = Int(EffectsJSON.number(object["frames"]) ?? 0)
        pack.usable = Int(EffectsJSON.number(object["usable"]) ?? 0)
        pack.picked = picks.compactMap { pick in
            guard let file = pick["path"] as? String else { return nil }
            return Pick(file: URL(fileURLWithPath: file), slot: EffectsJSON.string(pick["slot"]) ?? "",
                        why: EffectsJSON.string(pick["why"]) ?? "")
        }
        return pack.picked.isEmpty ? nil : pack
    }
}

/// Face packs on this Mac: AirDrop lands in Downloads, iCloud Drive syncs the Cinema 101 folder. Only the top two
/// levels of each place are listed (by extension, nothing is opened), never a deep scan.
nonisolated enum EffectsFacePacks {
    static let pathExtension = "c101face"

    static func isPack(_ url: URL) -> Bool { url.pathExtension.lowercased() == pathExtension }

    /// The pack's name without the extension ("Daniel 2026-10-11 16-00").
    static func title(_ url: URL) -> String { url.deletingPathExtension().lastPathComponent }

    static var places: [URL] {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let mobile = home.appendingPathComponent("Library/Mobile Documents", isDirectory: true)
        let drive = mobile.appendingPathComponent("com~apple~CloudDocs", isDirectory: true)
        var list = [home.appendingPathComponent("Downloads", isDirectory: true), drive,
                    drive.appendingPathComponent("Cinema 101/Face Packs", isDirectory: true)]
        // Cinema 101's own iCloud container, if it has one (iCloud~…cinema…/Documents).
        let containers = (try? FileManager.default.contentsOfDirectory(at: mobile, includingPropertiesForKeys: nil)) ?? []
        for container in containers where container.lastPathComponent.lowercased().contains("cinema") {
            let documents = container.appendingPathComponent("Documents", isDirectory: true)
            list += [documents, documents.appendingPathComponent("Face Packs", isDirectory: true)]
        }
        return list
    }

    static func discover(in places: [URL] = places, limit: Int = 20) -> [URL] {
        let fileManager = FileManager.default
        let keys: [URLResourceKey] = [.isDirectoryKey, .contentModificationDateKey]
        func children(_ folder: URL) -> [URL] {
            (try? fileManager.contentsOfDirectory(at: folder, includingPropertiesForKeys: keys,
                                                  options: [.skipsHiddenFiles, .skipsPackageDescendants])) ?? []
        }
        func isFolder(_ url: URL) -> Bool { (try? url.resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true }
        var found: [URL: Date] = [:]
        func add(_ url: URL) {
            guard isPack(url), isFolder(url) else { return }
            found[url.standardizedFileURL] = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
        }
        for place in places {
            for child in children(place) {
                if isPack(child) { add(child); continue }
                guard isFolder(child) else { continue }
                for grandchild in children(child) { add(grandchild) }
            }
        }
        return found.sorted { ($0.value, $1.key.path) > ($1.value, $0.key.path) }.prefix(limit).map(\.key)
    }

    /// Where a pack was found, for the menu ("Downloads", "iCloud Drive › Cinema 101").
    static func place(_ url: URL) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var parent = url.deletingLastPathComponent().path
        let drive = home + "/Library/Mobile Documents/com~apple~CloudDocs"
        if parent.hasPrefix(drive) {
            parent = "iCloud Drive" + parent.dropFirst(drive.count)
        } else if parent.hasPrefix(home + "/Library/Mobile Documents/") {
            parent = "iCloud Drive › Cinema 101"
        } else if parent.hasPrefix(home + "/") {
            parent = String(parent.dropFirst(home.count + 1))
        } else if parent.hasPrefix("/") {
            parent = String(parent.dropFirst())
        }
        return parent.replacingOccurrences(of: "/", with: " › ")
    }
}

// MARK: - Running the engine

/// A running engine command, shared between the reader threads and the main actor (Cancel).
nonisolated final class EffectsProcess: @unchecked Sendable {
    let process = Process()
    let out = Pipe()
    let err = Pipe()
    private let lock = NSLock()
    private var wasCancelled = false

    var cancelled: Bool { lock.withLock { wasCancelled } }

    /// Stops the command and everything it started (ffmpeg, FaceFusion…).
    func cancel() {
        lock.withLock { wasCancelled = true }
        guard process.isRunning else { return }
        ProcessTree.terminate(process.processIdentifier)
    }
}

nonisolated enum ProcessTree {
    /// SIGTERM to a process and all its descendants, SIGKILL to the ones still there 3 seconds later.
    static func terminate(_ pid: Int32) {
        var all: [Int32] = []
        collect(pid, into: &all)
        let members = all
        for member in members { kill(member, SIGTERM) }
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
            for member in members where kill(member, 0) == 0 { kill(member, SIGKILL) }
        }
    }

    private static func collect(_ pid: Int32, into list: inout [Int32]) {
        guard pid > 1, !list.contains(pid) else { return }
        list.append(pid)
        let pgrep = Process()
        pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        pgrep.arguments = ["-P", String(pid)]
        let pipe = Pipe()
        pgrep.standardOutput = pipe
        pgrep.standardError = FileHandle.nullDevice
        guard (try? pgrep.run()) != nil else { return }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        pgrep.waitUntilExit()
        for line in String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline) {
            if let child = Int32(line.trimmingCharacters(in: .whitespaces)) { collect(child, into: &list) }
        }
    }
}

nonisolated enum EffectsRunner {
    nonisolated enum Output: Sendable {
        case stdout(String)
        case stderr(String)
        case exit(Int32)
    }

    /// Starts a command; its stdout and stderr lines arrive in order, then its exit code.
    static func start(_ executable: URL, _ arguments: [String], directory: URL?, box: EffectsProcess) throws -> AsyncStream<Output> {
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw ProcessFailure.missingTool(executable.lastPathComponent)
        }
        let (stream, continuation) = AsyncStream.makeStream(of: Output.self, bufferingPolicy: .unbounded)
        let process = box.process
        process.executableURL = executable
        process.arguments = arguments
        process.environment = EffectsPaths.environment()
        if let directory { process.currentDirectoryURL = directory }
        process.standardOutput = box.out
        process.standardError = box.err
        process.standardInput = FileHandle.nullDevice
        let readers = DispatchGroup()
        readers.enter()
        readers.enter()
        process.terminationHandler = { finished in
            let status = finished.terminationStatus
            DispatchQueue.global().async {
                // A helper the engine started may keep a pipe open: do not wait for it forever.
                _ = readers.wait(timeout: .now() + 3)
                continuation.yield(.exit(status))
                continuation.finish()
            }
        }
        do {
            try process.run()
        } catch {
            readers.leave()
            readers.leave()
            continuation.finish()
            throw error
        }
        pump(box.out.fileHandleForReading, isError: false, readers: readers, continuation: continuation)
        pump(box.err.fileHandleForReading, isError: true, readers: readers, continuation: continuation)
        return stream
    }

    private static func pump(_ handle: FileHandle, isError: Bool, readers: DispatchGroup,
                             continuation: AsyncStream<Output>.Continuation) {
        DispatchQueue.global(qos: .userInitiated).async {
            var pending = Data()
            func emit(_ data: Data) {
                let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespaces)
                guard !text.isEmpty else { return }
                continuation.yield(isError ? .stderr(text) : .stdout(text))
            }
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                pending.append(chunk)
                // Lines end with \n, or \r for progress bars that redraw themselves.
                while let index = pending.firstIndex(where: { $0 == 10 || $0 == 13 }) {
                    emit(pending.subdata(in: pending.startIndex..<index))
                    pending = pending.subdata(in: (index + 1)..<pending.endIndex)
                }
                if pending.count > 4_000_000 { pending.removeAll() }
            }
            if !pending.isEmpty { emit(pending) }
            readers.leave()
        }
    }

    /// `zeus effects …`: the engine writes straight to the terminal (JSON lines as they come); returns its exit code.
    static func passThrough(_ executable: URL, _ arguments: [String], directory: URL?) async -> Int32 {
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            FileHandle.standardError.write(Data("zeus: \(executable.path) is missing\n".utf8))
            return 127
        }
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = EffectsPaths.environment()
        if let directory { process.currentDirectoryURL = directory }
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError
        process.standardInput = FileHandle.nullDevice
        return await withCheckedContinuation { continuation in
            process.terminationHandler = { continuation.resume(returning: $0.terminationStatus) }
            do {
                try process.run()
            } catch {
                process.terminationHandler = nil
                FileHandle.standardError.write(Data("zeus: \(error.localizedDescription)\n".utf8))
                continuation.resume(returning: 127)
            }
        }
    }
}

// MARK: - The Effects tab's state

/// Runs the engine for the Effects tab and keeps what it reports: stage, progress, message, then done or an error.
@Observable
final class EffectsEngine {
    enum Work: Equatable {
        case setup(EffectsSetup)
        case prepare
        case approve
        case generate
        case makeMe

        var label: String {
            switch self {
            case .setup(let setup): setup.label
            case .prepare: "Preparing the clip"
            case .approve: "Recording your approval"
            case .generate: "Generating on this Mac"
            case .makeMe: "Making it you"
            }
        }
    }

    /// Which form the tab shows for a new effect: the one-button job, or the step-by-step flow.
    enum Flow: String, CaseIterable, Identifiable {
        case makeMe
        case stepByStep

        var id: String { rawValue }
        var label: String { self == .makeMe ? "Make it me" : "Step by step" }
    }

    // Engine status (doctor)
    private(set) var doctor: EffectsDoctor?
    private(set) var doctorError: String?
    private(set) var isChecking = false

    // Jobs
    private(set) var jobs: [EffectsJob] = []
    /// nil: a new effect (the source form).
    var selectedJobID: String?

    // What runs now
    private(set) var work: Work?
    private(set) var stage = ""
    private(set) var fraction: Double?
    /// Make it me: 0–1 of the whole job, and the planned steps (from the engine's first line).
    private(set) var overall: Double?
    private(set) var planSteps: [String] = []
    private(set) var message = ""
    var lastError: String?
    /// Human output (stderr, setup scripts), last lines.
    private(set) var output: [String] = []
    @ObservationIgnored private var running: EffectsProcess?

    // Source
    var sourceVideoID: String?
    var sourceFile: URL?
    /// "75", "1:15", "1m15s".
    var startText = "0"
    var length: Double = 5
    var mode: EffectsMode = .depthMesh

    // Generate
    var referencePhoto: URL? { didSet { if referencePhoto != oldValue { consent = false } } }
    var secondPhoto: URL? { didSet { if secondPhoto != oldValue { consent = false } } }
    /// A Cinema 101 face pack (`--ref-pack`): with one, the photos are optional.
    var facePack: EffectsFacePack? { didSet { if facePack?.folder != oldValue?.folder { consent = false } } }
    /// The pack being read (`facepack --json`).
    private(set) var readingPack: URL?
    /// Packs found in Downloads and iCloud Drive, newest first.
    private(set) var foundPacks: [URL] = []
    var prompt = ""
    var consent = false
    var generator: EffectsGenerator = .faceswap
    var frames = 33
    var steps = 20
    var size = "360p"   // 480p fits 16 GB but takes ~2x longer and looked worse (effects/ENGINES.md)
    var seedText = ""
    var enhanceFace = false   // face swap: GFPGAN sharpening, ~20x slower (33 s vs 11 min for 3 s of 1080p)
    var faceQuality = "natural"   // face swap: natural (pixel boost 512 + real inside of the mouth) or fast

    // Make it me (effects/MAKE-IT-ME.md): one button
    var flow: Flow = .makeMe
    var meSource: EffectsMeSource = .selfVideo
    var selfVideo: URL? { didSet { if selfVideo != oldValue { makeMeConsent = false } } }
    var voiceSource: EffectsVoiceSource = .selfVideo
    var voiceFile: URL?
    var makeMeQuality = "natural"
    var hatRemove = false
    var hairText = ""
    var personName = ""
    var makeMeConsent = false

    /// Who the person becomes, from the form (nil when nothing is chosen yet).
    var makeMeWho: EffectsMe? {
        switch meSource {
        case .selfVideo: return selfVideo.map { .selfVideo($0) }
        case .facePack: return facePack.map { .facePack($0.folder) }
        case .photos:
            let photos = [referencePhoto, secondPhoto].compactMap { $0 }
            return photos.isEmpty ? nil : .photos(photos)
        }
    }

    var isBusy: Bool { work != nil }
    var isSetUp: Bool { EffectsPaths.isSetUp }
    var engineFolder: URL? { EffectsPaths.engine }
    var startSeconds: Double { BrainLinks.seconds(startText) ?? 0 }
    var selectedJob: EffectsJob? { selectedJobID.flatMap { id in jobs.first { $0.id == id } } }

    /// Whether the chosen generator is known to be missing (nil: the doctor did not say).
    func isInstalled(_ generator: EffectsGenerator) -> Bool? {
        generator == .faceswap ? doctor?.faceSwapReady : doctor?.fullReplaceReady
    }

    /// The first time the tab shows (the job list refreshes itself).
    func refreshIfNeeded() async {
        if doctor == nil, doctorError == nil, !isChecking, !isBusy { await refreshDoctor() }
    }

    func refreshDoctor() async {
        guard EffectsPaths.isSetUp, EffectsPaths.script != nil else {
            doctor = nil
            doctorError = nil
            return
        }
        isChecking = true
        defer { isChecking = false }
        let result = await collect(["doctor", "--json"])
        if let parsed = EffectsDoctor.parse(result.stdout) {
            doctor = parsed
            doctorError = nil
        } else {
            doctor = nil
            doctorError = result.error ?? "The engine did not answer the check."
        }
    }

    func refreshJobs() async {
        var found: [EffectsJob]?
        if EffectsPaths.isSetUp, EffectsPaths.script != nil {
            let result = await collect(["list", "--json"])
            if result.status == 0 { found = EffectsJobs.parseList(result.stdout) }
        }
        if let found {
            jobs = found
        } else {
            jobs = await Task.detached { EffectsJobs.scan() }.value
        }
    }

    /// Reads one job again from its folder (after approve or generate).
    func reload(_ folder: URL) {
        guard let job = EffectsJob.load(folder) else { return }
        if let index = jobs.firstIndex(where: { $0.id == job.id || $0.folder.standardizedFileURL == folder.standardizedFileURL }) {
            jobs[index] = job
        } else {
            jobs.insert(job, at: 0)
        }
        selectedJobID = job.id
    }

    // MARK: Commands

    @discardableResult
    func setUp(_ setup: EffectsSetup) async -> Bool {
        guard let command = EffectsPaths.setupCommand(setup) else {
            lastError = "The Effects engine is missing from this copy of the app."
            return false
        }
        let (ok, _) = await run(.setup(setup), URL(fileURLWithPath: "/bin/bash"), command)
        if ok { message = setup == .main ? "The Effects engine is ready." : "Done." }
        doctorError = nil
        await refreshDoctor()
        return ok
    }

    /// Prepares the source (≤ 10 s): download/trim, depth, masks, control video, face mesh. Returns the new job.
    func prepare(input: String) async -> EffectsJob? {
        guard let script = EffectsPaths.script else {
            lastError = "The Effects engine is missing from this copy of the app."
            return nil
        }
        let cookies = FileManager.default.fileExists(atPath: YouTubeAccount.cookieFile.path) ? YouTubeAccount.cookieFile : nil
        let arguments = EffectsArguments.prepare(input: input, start: startSeconds, duration: length, mode: mode, cookies: cookies)
        let (ok, path) = await run(.prepare, EffectsPaths.python, [script.path] + arguments)
        await refreshJobs()
        guard let path else { return nil }
        let folder = EffectsJobs.folder(path)
        let job = jobs.first { $0.folder.standardizedFileURL == folder } ?? EffectsJob.load(folder)
        if let job {
            if !jobs.contains(where: { $0.id == job.id }) { jobs.insert(job, at: 0) }
            selectedJobID = job.id
        }
        return ok ? job : nil
    }

    /// Records the review: SHA-256 of every prepared file (generation refuses if any of them changes).
    func approve(_ job: EffectsJob) async -> Bool {
        guard let script = EffectsPaths.script else { return false }
        let (ok, _) = await run(.approve, EffectsPaths.python, [script.path, "approve", "--job", job.folder.path])
        reload(job.folder)
        return ok
    }

    func generate(_ job: EffectsJob) async -> Bool {
        guard let script = EffectsPaths.script else { return false }
        guard consent else {
            lastError = "Tick “These photos show me or people who agreed” first."
            return false
        }
        let references = [referencePhoto, secondPhoto].compactMap { $0 }
        guard !references.isEmpty || facePack != nil else {
            lastError = "Choose a reference photo or a face pack first."
            return false
        }
        let seed = Int(seedText.trimmingCharacters(in: .whitespaces))
        let arguments = EffectsArguments.generate(job: job.folder, engine: generator, references: references,
                                                  facePack: facePack?.folder, prompt: prompt, consent: consent,
                                                  frames: frames, steps: steps, size: size, seed: seed, enhance: enhanceFace,
                                                  quality: faceQuality)
        let (ok, _) = await run(.generate, EffectsPaths.python, [script.path] + arguments)
        reload(job.folder)
        return ok
    }

    /// Make it me: the whole job (prepare, refs, approve, faceswap, hat, voice, final) with one command.
    func makeMe(input: String) async -> EffectsJob? {
        guard let script = EffectsPaths.script else {
            lastError = "The Effects engine is missing from this copy of the app."
            return nil
        }
        guard let who = makeMeWho else {
            lastError = "Say who you are: a self-video, a face pack or photos."
            return nil
        }
        guard makeMeConsent else {
            lastError = "Tick the consent box first: the clip, the face and the voice are yours or of people who agreed."
            return nil
        }
        let voiceOn: Bool
        var voice: URL?
        switch voiceSource {
        case .selfVideo: voiceOn = meSource == .selfVideo
        case .file:
            voiceOn = voiceFile != nil
            voice = voiceFile
        case .keep: voiceOn = false
        }
        let cookies = FileManager.default.fileExists(atPath: YouTubeAccount.cookieFile.path) ? YouTubeAccount.cookieFile : nil
        let arguments = EffectsArguments.makeMe(input: input, start: startSeconds, duration: length, me: who, voice: voice,
                                                voiceOn: voiceOn, quality: makeMeQuality, hat: hatRemove, hair: hairText,
                                                person: personName, consent: makeMeConsent, job: nil, cookies: cookies)
        let (ok, path) = await run(.makeMe, EffectsPaths.python, [script.path] + arguments)
        await refreshJobs()
        guard let path else { return nil }
        let folder = EffectsJobs.folder(path)
        let job = jobs.first { $0.folder.standardizedFileURL == folder } ?? EffectsJob.load(folder)
        if let job {
            if !jobs.contains(where: { $0.id == job.id }) { jobs.insert(job, at: 0) }
            selectedJobID = job.id
        }
        return ok ? job : nil
    }

    /// A failed or cancelled Make it me job again, in the same folder: the engine keeps the steps already done.
    func continueMakeMe(_ job: EffectsJob) async -> Bool {
        guard let script = EffectsPaths.script else { return false }
        let cookies = FileManager.default.fileExists(atPath: YouTubeAccount.cookieFile.path) ? YouTubeAccount.cookieFile : nil
        guard let arguments = EffectsArguments.continueMakeMe(job, cookies: cookies) else {
            lastError = "This job did not record what it was started with; start a new Make it me."
            return false
        }
        selectedJobID = job.id
        let (ok, _) = await run(.makeMe, EffectsPaths.python, [script.path] + arguments)
        reload(job.folder)
        return ok
    }

    func cancel() {
        running?.cancel()
    }

    /// Reads a face pack with the engine (validation, consent, the frames it will use) and keeps it for Generate.
    func useFacePack(_ folder: URL) async -> Bool {
        readingPack = folder
        defer { readingPack = nil }
        let result = await collect(["facepack", "--pack", folder.path, "--json"])
        guard result.status == 0, let pack = EffectsFacePack.parse(result.stdout, folder: folder) else {
            lastError = result.error ?? "\(folder.lastPathComponent) could not be read as a face pack."
            return false
        }
        facePack = pack
        return true
    }

    /// Lists the face packs in Downloads and iCloud Drive (top two levels).
    func findFacePacks() async {
        foundPacks = await Task.detached { EffectsFacePacks.discover() }.value
    }

    /// Copies the result (Make it me: final-voice.mp4 when the voice ran, else final.mp4) to
    /// ~/Movies/YouTube Zeus Effects/ (never over an earlier copy).
    func saveToMovies(_ job: EffectsJob) throws -> URL {
        guard let final = job.resultVideo else { throw CocoaError(.fileNoSuchFile) }
        let folder = EffectsPaths.movies
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let engine = job.makeMe != nil ? " (Make it me)" : (job.generations.last.map { " (\($0.engine))" } ?? "")
        let base = ("Zeus Effect " + job.id + engine).replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
        var target = folder.appendingPathComponent(base + ".mp4")
        var number = 2
        while FileManager.default.fileExists(atPath: target.path) {
            target = folder.appendingPathComponent("\(base) \(number).mp4")
            number += 1
        }
        try FileManager.default.copyItem(at: final, to: target)
        return target
    }

    // MARK: Running

    /// Runs one command at a time (the models share the Mac's memory) and follows its JSON lines.
    private func run(_ work: Work, _ executable: URL, _ arguments: [String]) async -> (ok: Bool, job: String?) {
        if let current = self.work {
            lastError = "Wait until “\(current.label)” is finished, or cancel it."
            return (false, nil)
        }
        self.work = work
        stage = ""
        fraction = nil
        overall = nil
        planSteps = []
        message = work.label + "…"
        lastError = nil
        output.removeAll()
        let box = EffectsProcess()
        running = box
        defer {
            self.work = nil
            running = nil
        }
        AppLog.write("EFFECTS \(work.label): \(arguments.dropFirst(executable.lastPathComponent == "bash" ? 0 : 1).joined(separator: " "))")
        let stream: AsyncStream<EffectsRunner.Output>
        do {
            stream = try EffectsRunner.start(executable, arguments, directory: EffectsPaths.engine, box: box)
        } catch {
            lastError = error.localizedDescription
            return (false, nil)
        }
        var job: String?
        var failure: String?
        var status: Int32 = -1
        var lastLine = ""
        for await item in stream {
            switch item {
            case .stdout(let line):
                switch EffectsEvent.parse(line) {
                case .progress(let newStage, let newFraction, let text, let newOverall):
                    if newStage != stage { fraction = nil }
                    stage = newStage
                    if let newFraction { fraction = newFraction }
                    if let newOverall { overall = newOverall }
                    if !text.isEmpty { message = text }
                case .plan(let steps, let path):
                    planSteps = steps
                    if let path { job = path }
                case .done(let path):
                    job = path ?? job
                    fraction = 1
                    overall = 1
                case .failed(let text, let failedStage):
                    failure = text
                    if let failedStage { stage = failedStage }
                case .text(let text):
                    append(text)
                    lastLine = text
                }
            case .stderr(let line):
                append(line)
                lastLine = line
            case .exit(let code):
                status = code
            }
        }
        if box.cancelled {
            message = "Cancelled."
            AppLog.write("EFFECTS cancelled: \(work.label)")
            return (false, job)
        }
        if status == 0, failure == nil {
            message = "Done."
            return (true, job)
        }
        lastError = failure ?? (lastLine.isEmpty ? "\(work.label) stopped (code \(status))." : lastLine)
        AppLog.write("EFFECTS failed: \(lastError ?? "")")
        return (false, job)
    }

    private func append(_ line: String) {
        output.append(line)
        if output.count > 400 { output.removeFirst(output.count - 400) }
    }

    /// A short command (doctor, list): its whole stdout.
    private func collect(_ arguments: [String]) async -> (status: Int32, stdout: String, error: String?) {
        guard let script = EffectsPaths.script else { return (1, "", "The Effects engine is missing from this copy of the app.") }
        let box = EffectsProcess()
        let stream: AsyncStream<EffectsRunner.Output>
        do {
            stream = try EffectsRunner.start(EffectsPaths.python, [script.path] + arguments, directory: EffectsPaths.engine, box: box)
        } catch {
            return (1, "", error.localizedDescription)
        }
        var lines: [String] = []
        var lastError = ""
        var failure: String?
        var status: Int32 = -1
        for await item in stream {
            switch item {
            case .stdout(let line):
                lines.append(line)
                if case .failed(let text, _) = EffectsEvent.parse(line) { failure = text }
            case .stderr(let line): lastError = line
            case .exit(let code): status = code
            }
        }
        let error = failure ?? (status == 0 ? nil : (lastError.isEmpty ? "The engine stopped (code \(status))." : lastError))
        return (status, lines.joined(separator: "\n"), error)
    }
}
