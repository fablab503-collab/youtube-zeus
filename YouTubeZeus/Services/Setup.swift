import AppKit
import Foundation
import Observation

// MARK: - This Mac

/// What the setup assistant reads about this Mac to choose the local AI model.
nonisolated struct MacHardware: Sendable, Equatable {
    var name: String
    var chip: String
    var memoryGB: Int
    var gpuCores: Int?
    var freeDiskGB: Int
    var appleSilicon: Bool

    var summary: String {
        var parts = [chip, "\(memoryGB) GB memory"]
        if let gpuCores { parts.append("\(gpuCores)-core GPU") }
        parts.append("\(freeDiskGB) GB free")
        return parts.joined(separator: " · ")
    }

    static func current() async -> MacHardware {
        let arm = isAppleSilicon
        let chip = sysctlString("machdep.cpu.brand_string") ?? (arm ? "Apple silicon" : "Intel")
        let memory = Int((Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824).rounded())
        var gpu: Int?
        if let output = try? await ProcessRunner.run("/usr/sbin/ioreg", ["-rd1", "-c", "AGXAccelerator"]) {
            let text = output.stdoutString
            if let range = text.range(of: "\"gpu-core-count\" = ") {
                gpu = Int(text[range.upperBound...].prefix { $0.isNumber })
            }
        }
        var name = "Mac"
        if let output = try? await ProcessRunner.run("/usr/sbin/system_profiler", ["SPHardwareDataType", "-json"]),
           let json = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any],
           let item = (json["SPHardwareDataType"] as? [[String: Any]])?.first,
           let machine = item["machine_name"] as? String, !machine.isEmpty {
            name = machine
        }
        return MacHardware(name: name, chip: chip, memoryGB: memory, gpuCores: gpu, freeDiskGB: freeDiskGB(), appleSilicon: arm)
    }

    static func freeDiskGB() -> Int {
        let home = URL(fileURLWithPath: NSHomeDirectory())
        guard let bytes = (try? home.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]))?
            .volumeAvailableCapacityForImportantUsage else { return 0 }
        return Int(bytes / 1_000_000_000)
    }

    private static var isAppleSilicon: Bool {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        return sysctlbyname("hw.optional.arm64", &value, &size, nil, 0) == 0 && value == 1
    }

    private static func sysctlString(_ name: String) -> String? {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 1 else { return nil }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctlbyname(name, &buffer, &size, nil, 0) == 0 else { return nil }
        let text = String(decoding: buffer.prefix { $0 != 0 }, as: UTF8.self).trimmingCharacters(in: .whitespaces)
        return text.isEmpty ? nil : text
    }
}

// MARK: - Local AI models

/// A model the local AI can run, checked against Ollama's library (the first candidate tag that exists wins).
nonisolated struct LocalModelOption: Identifiable, Sendable, Equatable {
    var id: String { key }
    let key: String
    let name: String
    let candidates: [String]
    let rank: Int
    let note: String
    var tag: String
    /// Download size in GB (from Ollama's library when it answered, else an estimate).
    var sizeGB: Double
    /// The weights read for every word, in GB: the whole model, except mixture-of-experts models.
    let activeGB: Double?
    /// nil: not checked (offline); false: not in Ollama's library.
    var available: Bool?

    init(_ key: String, _ name: String, _ candidates: [String], gb: Double, active: Double? = nil, rank: Int, note: String) {
        self.key = key
        self.name = name
        self.candidates = candidates
        self.rank = rank
        self.note = note
        activeGB = active
        tag = candidates[0]
        sizeGB = gb
    }

    var sizeText: String { sizeGB < 10 ? String(format: "%.1f GB", sizeGB) : "\(Int(sizeGB.rounded())) GB" }
}

nonisolated enum ModelFit: Sendable {
    /// Runs well next to everything else on this Mac.
    case comfortable
    /// Runs, but leaves little memory for the rest.
    case heavy
    /// Needs more memory or disk space than this Mac has.
    case tooBig
}

nonisolated enum ModelCatalog {
    /// Instruct / hybrid models that answer in JSON with thinking turned off, strongest first.
    static let options: [LocalModelOption] = [
        .init("qwen3-30b", "Qwen3 30B (MoE)", ["qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3:30b"], gb: 18.6, active: 2, rank: 90,
              note: "The strongest here, and fast for its size: only 3B of its 30B parameters work at a time."),
        .init("qwen3-32b", "Qwen3 32B", ["qwen3:32b"], gb: 20.2, rank: 85,
              note: "Very strong, slower than the 30B MoE."),
        .init("gemma3-27b", "Gemma 3 27B", ["gemma3:27b"], gb: 17.4, rank: 80,
              note: "Very good in French, Italian, German and other European languages."),
        .init("qwen3-14b", "Qwen3 14B", ["qwen3:14b"], gb: 9.3, rank: 70,
              note: "Strong summaries, names and answers."),
        .init("gemma3-12b", "Gemma 3 12B", ["gemma3:12b"], gb: 8.1, rank: 65,
              note: "Good quality, good in European languages."),
        .init("qwen3-8b", "Qwen3 8B", ["qwen3:8b"], gb: 5.2, rank: 55,
              note: "A good balance for Macs with 16 GB."),
        .init("qwen3-4b", "Qwen3 4B Instruct", ["qwen3:4b-instruct", "qwen3:4b-instruct-2507-q4_K_M", "qwen3:4b"], gb: 2.5, rank: 45,
              note: "Light and fast. Zeus's tested default."),
        .init("gemma3-4b", "Gemma 3 4B", ["gemma3:4b"], gb: 3.3, rank: 40,
              note: "Light, good in European languages."),
        .init("qwen3-1.7b", "Qwen3 1.7B", ["qwen3:1.7b"], gb: 1.4, rank: 20,
              note: "Basic, for Macs with little memory."),
    ]

    /// Memory decides what fits (Apple silicon shares it between the CPU and the GPU); the GPU decides the speed.
    /// Models that can look at pictures (the frames of playbooks), strongest first.
    static let visionOptions: [LocalModelOption] = [
        .init("qwen3-vl-30b", "Qwen3-VL 30B (MoE)", ["qwen3-vl:30b-a3b-instruct", "qwen3-vl:30b"], gb: 19.6, active: 2, rank: 90,
              note: "The best at reading screens, and fast for its size."),
        .init("qwen3-vl-8b", "Qwen3-VL 8B", ["qwen3-vl:8b-instruct", "qwen3-vl:8b"], gb: 6.1, rank: 75,
              note: "Reads screens, buttons and code very well."),
        .init("qwen2.5vl-7b", "Qwen2.5-VL 7B", ["qwen2.5vl:7b"], gb: 6.0, rank: 65,
              note: "Good at screens and documents."),
        .init("gemma3-12b-vision", "Gemma 3 12B", ["gemma3:12b"], gb: 8.1, rank: 60,
              note: "Sees pictures and writes well (also a text model)."),
        .init("qwen3-vl-4b", "Qwen3-VL 4B", ["qwen3-vl:4b-instruct", "qwen3-vl:4b"], gb: 3.3, rank: 55,
              note: "Light, and reads screens well."),
        .init("qwen2.5vl-3b", "Qwen2.5-VL 3B", ["qwen2.5vl:3b"], gb: 3.2, rank: 45,
              note: "Light."),
        .init("gemma3-4b-vision", "Gemma 3 4B", ["gemma3:4b"], gb: 3.3, rank: 40,
              note: "Light (also a text model)."),
        .init("qwen3-vl-2b", "Qwen3-VL 2B", ["qwen3-vl:2b-instruct", "qwen3-vl:2b"], gb: 1.9, rank: 25,
              note: "Basic, for Macs with little memory."),
    ]

    /// Whether a model also looks at pictures (Gemma 3 from 4B, the VL models, LLaVA…).
    static func canSee(_ tag: String) -> Bool {
        let name = tag.lowercased()
        if name.hasPrefix("gemma3") { return !name.contains(":1b") && !name.contains("270m") }
        return name.contains("-vl") || name.contains("vl:") || name.hasPrefix("llava") || name.contains("vision")
            || name.hasPrefix("minicpm-v") || name.hasPrefix("mistral-small3")
    }

    /// The first tag of an option that Ollama's library has (the first one when the library does not answer).
    static func firstAvailable(_ option: LocalModelOption) async -> String {
        for tag in option.candidates {
            switch await registry(tag) {
            case .found: return tag
            case .missing: continue
            case .unknown: return tag
            }
        }
        return option.candidates[0]
    }

    static func fit(_ option: LocalModelOption, on mac: MacHardware) -> ModelFit {
        let memory = Double(mac.memoryGB)
        if option.sizeGB + 2 > Double(mac.freeDiskGB) || option.sizeGB > memory * 0.65 { return .tooBig }
        if slow(option, on: mac) { return .heavy }
        return option.sizeGB <= memory * 0.4 ? .comfortable : .heavy
    }

    /// A rough speed in words per second: every word reads the active weights once, and Apple silicon reads about
    /// 13 GB/s of memory per GPU core (M2: 10 cores, 100 GB/s; M2 Pro: 19 cores, 200 GB/s; M2 Max: 38 cores, 400 GB/s).
    static func wordsPerSecond(_ option: LocalModelOption, on mac: MacHardware) -> Double? {
        guard mac.appleSilicon, let cores = mac.gpuCores, cores > 0 else { return nil }
        return 0.6 * Double(cores) * 13 / (option.activeGB ?? option.sizeGB)
    }

    /// Below 6 words per second, summaries of long videos take too long.
    static func slow(_ option: LocalModelOption, on mac: MacHardware) -> Bool {
        (wordsPerSecond(option, on: mac) ?? 99) < 6
    }

    /// The strongest model that runs comfortably, else the lightest one that runs at all.
    static func recommended(_ options: [LocalModelOption], on mac: MacHardware) -> LocalModelOption? {
        let usable = options.filter { $0.available != false }
        return usable.filter { fit($0, on: mac) == .comfortable }.max { $0.rank < $1.rank }
            ?? usable.filter { fit($0, on: mac) != .tooBig }.min { $0.sizeGB < $1.sizeGB }
    }

    enum RegistryAnswer: Sendable { case found(Double), missing, unknown }

    /// Asks Ollama's library whether each model exists, and its real download size.
    static func resolve(_ options: [LocalModelOption]) async -> [LocalModelOption] {
        await withTaskGroup(of: (Int, LocalModelOption).self) { group in
            for (index, option) in options.enumerated() {
                group.addTask {
                    var resolved = option
                    for tag in option.candidates {
                        switch await registry(tag) {
                        case .found(let size):
                            resolved.tag = tag
                            resolved.sizeGB = size
                            resolved.available = true
                            return (index, resolved)
                        case .missing:
                            continue
                        case .unknown:
                            return (index, resolved)
                        }
                    }
                    resolved.available = false
                    return (index, resolved)
                }
            }
            var result = options
            for await (index, option) in group { result[index] = option }
            return result
        }
    }

    static func registry(_ tag: String) async -> RegistryAnswer {
        let parts = tag.split(separator: ":", maxSplits: 1).map(String.init)
        let name = parts[0].contains("/") ? parts[0] : "library/\(parts[0])"
        let version = parts.count > 1 ? parts[1] : "latest"
        guard let url = URL(string: "https://registry.ollama.ai/v2/\(name)/manifests/\(version)") else { return .unknown }
        var request = URLRequest(url: url)
        request.timeoutInterval = 8
        request.setValue("application/vnd.docker.distribution.manifest.v2+json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else { return .unknown }
        if http.statusCode == 404 { return .missing }
        guard http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return .unknown }
        let bytes = ((json["layers"] as? [[String: Any]]) ?? []).compactMap { ($0["size"] as? NSNumber)?.doubleValue }.reduce(0, +)
        return bytes > 0 ? .found(bytes / 1_000_000_000) : .unknown
    }

    /// A model is on disk when Ollama keeps its manifest (default model folder), even with Ollama stopped.
    static func isDownloaded(_ tag: String) -> Bool {
        let parts = tag.split(separator: ":", maxSplits: 1).map(String.init)
        let name = parts[0].contains("/") ? parts[0] : "library/\(parts[0])"
        let version = parts.count > 1 ? parts[1] : "latest"
        let base = ProcessInfo.processInfo.environment["OLLAMA_MODELS"].map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".ollama/models")
        let manifest = base.appendingPathComponent("manifests/registry.ollama.ai/\(name)/\(version)")
        return FileManager.default.fileExists(atPath: manifest.path)
    }
}

// MARK: - Setup steps

nonisolated enum SetupItem: String, CaseIterable, Identifiable, Sendable {
    case commandLineTools, homebrew, youtube, ffmpeg, whisper, ollama, model, speech

    var id: String { rawValue }

    var title: String {
        switch self {
        case .commandLineTools: "Apple's command-line tools"
        case .homebrew: "Homebrew"
        case .youtube: "yt-dlp and Deno"
        case .ffmpeg: "ffmpeg"
        case .whisper: "whisper.cpp"
        case .ollama: "Ollama"
        case .model: "Local AI model"
        case .speech: "Speech model"
        }
    }

    var purpose: String {
        switch self {
        case .commandLineTools: "Needed by Homebrew. Installed by Apple's own installer."
        case .homebrew: "The free package manager that installs and updates the tools below."
        case .youtube: "Reads YouTube: captions, video details, playlists and channels. Zeus needs it to eat videos."
        case .ffmpeg: "Audio for listening, frames for the text on screen, and your own video files."
        case .whisper: "Listens to videos and podcasts that have no captions."
        case .ollama: "Runs the free local AI on this Mac: polishing, summaries, names, digests and Ask."
        case .model: "The brain of the local AI, chosen for this Mac's memory and disk."
        case .speech: "Whisper's model, for videos without captions. Otherwise it downloads the first time it is needed."
        }
    }

    var symbol: String {
        switch self {
        case .commandLineTools: "hammer"
        case .homebrew: "mug"
        case .youtube: "play.rectangle"
        case .ffmpeg: "film.stack"
        case .whisper: "waveform"
        case .ollama: "cpu"
        case .model: "brain"
        case .speech: "ear"
        }
    }

    var formulas: [String] {
        switch self {
        case .youtube: ["yt-dlp", "deno"]
        case .ffmpeg: ["ffmpeg"]
        case .whisper: ["whisper-cpp"]
        case .ollama: ["ollama"]
        default: []
        }
    }

    /// The order "Set up everything" follows.
    static let installOrder: [SetupItem] = [.commandLineTools, .homebrew, .youtube, .ffmpeg, .whisper, .ollama, .model, .speech]
}

enum SetupState: Equatable {
    case checking
    case ready(String)
    case missing(String)
    case working(String, Double?)
    /// Waiting for the user in an installer window.
    case waiting(String)
    case failed(String)

    var isReady: Bool { if case .ready = self { true } else { false } }
    /// The text shown with the state ("" while checking).
    var detail: String {
        switch self {
        case .checking: ""
        case .ready(let text), .missing(let text), .working(let text, _), .waiting(let text), .failed(let text): text
        }
    }
    var isBusy: Bool {
        switch self {
        case .working, .waiting, .checking: true
        default: false
        }
    }
}

nonisolated enum SetupError: LocalizedError, Sendable {
    case needsHomebrew
    case noPackage
    case timedOut(String)
    case command(String)

    var errorDescription: String? {
        switch self {
        case .needsHomebrew: "Homebrew is needed first."
        case .noPackage: "Homebrew's installer was not found on GitHub. Try \"Use Terminal instead\"."
        case .timedOut(let what): "\(what) did not finish in time. Try again."
        case .command(let message): message
        }
    }
}

// MARK: - The assistant

/// Checks and installs everything Zeus uses on a new Mac: Homebrew (Apple's installer asks for the password once),
/// yt-dlp, Deno, ffmpeg, whisper.cpp, Ollama and the local AI model that suits this Mac. Shown at the first launch,
/// from Settings › Tools, `zeus setup` and `youtubezeus://setup`.
@Observable
final class SetupAssistant {
    private(set) var states: [SetupItem: SetupState] = [:]
    private(set) var log: [String] = []
    private(set) var hardware: MacHardware?
    private(set) var models: [LocalModelOption] = ModelCatalog.options
    private(set) var installedModels: [String] = []
    private(set) var running = false
    private(set) var checked = false
    /// Reading this Mac and Ollama's library (the page waits for it before installing).
    private(set) var refreshing = false
    /// The Ollama tag the model step downloads and Zeus then uses.
    var chosenModel: String
    /// The vision model of playbooks (3.2): optional, downloaded the first time a playbook is made.
    private(set) var visionModels: [LocalModelOption] = ModelCatalog.visionOptions
    private(set) var visionState: SetupState = .checking
    @ObservationIgnored var onNote: ((String) -> Void)?
    /// The command line prints download progress (every 10 %).
    @ObservationIgnored var onProgress: ((String) -> Void)?
    @ObservationIgnored private var printedStep: [SetupItem: Int] = [:]
    @ObservationIgnored private var runTask: Task<Void, Never>?
    let settings: AppSettings
    private let ollama = OllamaClient()

    init(settings: AppSettings) {
        self.settings = settings
        chosenModel = settings.polishModel
    }

    func state(_ item: SetupItem) -> SetupState { states[item] ?? .checking }

    var brewPath: String? {
        ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"].first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    /// Zeus cannot eat YouTube videos without yt-dlp.
    var essentialsMissing: Bool { checked && !state(.youtube).isReady }

    /// Everything Zeus uses is there (the speech model downloads by itself when first needed).
    var everythingReady: Bool {
        let needed: [SetupItem] = [.youtube, .ffmpeg, .whisper, .ollama, .model]
        return needed.allSatisfy { state($0).isReady }
    }

    var recommended: LocalModelOption? { hardware.flatMap { ModelCatalog.recommended(models, on: $0) } }

    func option(for tag: String) -> LocalModelOption? { models.first { $0.tag == tag || $0.candidates.contains(tag) } }

    func displayName(_ tag: String) -> String { option(for: tag)?.name ?? tag }

    func isInstalled(_ tag: String) -> Bool {
        installedModels.contains(tag) || installedModels.contains(tag + ":latest") || ModelCatalog.isDownloaded(tag)
    }

    // MARK: Checking

    /// At launch: only what is on this Mac, no network.
    func quickCheck() async {
        await checkTools()
        await checkModel()
        checked = true
    }

    /// The setup page and `zeus setup`: also reads the hardware and asks Ollama's library for the models.
    func refresh() async {
        refreshing = true
        defer { refreshing = false }
        await checkTools()
        hardware = await MacHardware.current()
        if models.allSatisfy({ $0.available == nil }) {
            models = await ModelCatalog.resolve(models)
        }
        await checkModel(choosing: true)
        if visionModels.allSatisfy({ $0.available == nil }) {
            visionModels = await ModelCatalog.resolve(visionModels)
        }
        checkVision()
        checked = true
    }

    /// The model that reads the frames of playbooks: the one chosen in Settings, else the best one for this Mac.
    var visionChoice: LocalModelOption? {
        let setting = settings.visionModel
        if let option = visionModels.first(where: { $0.key == setting || $0.candidates.contains(setting) }) { return option }
        guard setting.isEmpty, let hardware else { return nil }
        return ModelCatalog.recommended(visionModels, on: hardware)
    }

    /// The text model already sees pictures (Gemma 3): nothing more to download.
    var visionUsesTextModel: Bool { settings.visionModel.isEmpty && ModelCatalog.canSee(settings.polishModel) }

    func checkVision() {
        if !settings.playbookVision {
            visionState = .ready("Off (Settings › Local AI)")
        } else if visionUsesTextModel {
            visionState = .ready("\(displayName(settings.polishModel)) also reads the frames")
        } else if let option = visionChoice {
            visionState = isInstalled(option.tag)
                ? .ready(option.name)
                : .missing("\(option.name) (\(option.sizeText)) downloads the first time you make a playbook")
        } else {
            visionState = .missing("No vision model fits this Mac: playbooks use the transcript and the text on screen")
        }
    }

    func downloadVision() {
        guard !running, let option = visionChoice else { return }
        runTask = Task {
            running = true
            defer { running = false }
            visionState = .working("Downloading \(option.name)…", 0)
            note("$ ollama pull \(option.tag)")
            do {
                try await ollama.ensureRunning()
                try await ollama.pull(option.tag) { value in
                    Task { @MainActor in self.visionState = .working("Downloading \(option.name)…", value) }
                }
                note("✓ \(option.name) is ready for playbooks")
                await checkModel()
                checkVision()
            } catch {
                visionState = .failed(error.localizedDescription)
                note("✗ \(option.name): \(error.localizedDescription)")
            }
        }
    }

    func checkTools() async {
        if let brew = brewPath {
            update(.commandLineTools, .ready("Installed"))
            update(.homebrew, .ready(brew))
        } else {
            update(.commandLineTools, Self.commandLineToolsInstalled ? .ready("Installed") : .missing("Not installed"))
            update(.homebrew, .missing("Not installed"))
        }
        let ytdlp = ToolLocator.find("yt-dlp", override: settings.ytdlpPath)
        let deno = ToolLocator.find("deno", override: "")
        if let ytdlp, deno != nil {
            update(.youtube, .ready(ytdlp))
        } else {
            var missing: [String] = []
            if ytdlp == nil { missing.append("yt-dlp") }
            if deno == nil { missing.append("Deno") }
            update(.youtube, .missing(missing.joined(separator: " and ") + " missing"))
        }
        update(.ffmpeg, ToolLocator.find("ffmpeg", override: settings.ffmpegPath).map { SetupState.ready($0) } ?? .missing("Not installed"))
        update(.whisper, ToolLocator.find("whisper-cli", override: settings.whisperPath).map { SetupState.ready($0) } ?? .missing("Not installed"))
        if !Self.ollamaInstalled {
            update(.ollama, .missing("Not installed"))
        } else {
            let serving = await ollama.isRunning()
            update(.ollama, .ready(serving ? "Running" : "Installed (starts when needed)"))
        }
        let speech = WhisperTranscriber(whisperPath: "", ffmpegPath: "", model: whisperModel)
        update(.speech, speech.hasModel ? .ready(whisperModel.label) : .missing("Not downloaded yet"))
    }

    /// `choosing`: on a Mac without the model Zeus uses, propose the one recommended for this Mac.
    func checkModel(choosing: Bool = false) async {
        let serving = await ollama.isRunning()
        installedModels = serving ? await ollama.installedModels() : []
        if choosing, !isInstalled(chosenModel), !UserDefaults.standard.bool(forKey: "setupModelChosen"),
           let recommended {
            chosenModel = recommended.tag
        }
        guard Self.ollamaInstalled else {
            update(.model, .missing("Comes after Ollama"))
            return
        }
        if isInstalled(chosenModel) {
            update(.model, .ready(displayName(chosenModel) + (settings.polishModel == chosenModel ? "" : " (not in use yet)")))
        } else {
            update(.model, .missing("\(displayName(chosenModel)) is not downloaded"))
        }
    }

    static var commandLineToolsInstalled: Bool {
        FileManager.default.fileExists(atPath: "/Library/Developer/CommandLineTools/usr/bin/git")
    }

    static var ollamaInstalled: Bool {
        FileManager.default.fileExists(atPath: "/Applications/Ollama.app") || ToolLocator.find("ollama", override: "") != nil
    }

    private var whisperModel: WhisperModel { WhisperModel(rawValue: settings.whisperModel) ?? .turbo }

    // MARK: Installing

    /// Installs everything missing, in order. Homebrew's own installer asks for the Mac password once.
    func setUpEverything() {
        guard !running else { return }
        runTask = Task { await runAll() }
    }

    /// Installs one item (from its card).
    func start(_ item: SetupItem) {
        guard !running else { return }
        runTask = Task {
            running = true
            await install(item)
            running = false
        }
    }

    func stop() {
        runTask?.cancel()
        runTask = nil
        running = false
        Task { await checkTools(); await checkModel() }
    }

    /// `withHomebrew: false` (the command line) skips the steps that open installer windows.
    func runAll(withHomebrew: Bool = true) async {
        running = true
        defer { running = false }
        note("Setting up YouTube Zeus on \(hardware?.name ?? "this Mac")")
        var skipped = Set<SetupItem>()
        for item in SetupItem.installOrder {
            if Task.isCancelled { break }
            if state(item).isReady || skipped.contains(item) { continue }
            if !withHomebrew, item == .commandLineTools || item == .homebrew {
                note("Homebrew is missing: open YouTube Zeus (its setup page installs it with Apple's installer), or run Homebrew's script from brew.sh in Terminal.")
                break
            }
            let ok = await install(item)
            // Every later step installs through Homebrew, and the model needs Ollama.
            if !ok, item == .commandLineTools || item == .homebrew { break }
            if !ok, item == .ollama { skipped.insert(.model) }
        }
        await checkTools()
        await checkModel()
        note(everythingReady ? "✓ Everything is ready." : "Some steps still need attention (see above).")
        AppLog.write("SETUP " + SetupItem.installOrder.map { "\($0.rawValue)=\(state($0).isReady ? "ok" : "missing")" }.joined(separator: " "))
    }

    @discardableResult
    func install(_ item: SetupItem) async -> Bool {
        do {
            switch item {
            case .commandLineTools: try await installCommandLineTools()
            case .homebrew: try await installHomebrew()
            case .youtube, .ffmpeg, .whisper: try await brewInstall(item)
            case .ollama: try await installOllama()
            case .model: try await downloadModel()
            case .speech: try await downloadSpeechModel()
            }
            await checkTools()
            if item == .ollama || item == .model { await checkModel() }
            return state(item).isReady
        } catch is CancellationError {
            set(item, .missing("Stopped"))
            return false
        } catch {
            set(item, .failed(error.localizedDescription))
            note("✗ \(item.title): \(error.localizedDescription)")
            AppLog.write("SETUP \(item.rawValue) failed: \(error.localizedDescription)")
            return false
        }
    }

    private func installCommandLineTools() async throws {
        if brewPath != nil || Self.commandLineToolsInstalled { return }
        note("Opening Apple's installer for the command-line tools")
        set(.commandLineTools, .waiting("Apple's installer is open: click Install. It takes about 5 to 15 minutes."))
        _ = try? await ProcessRunner.run("/usr/bin/xcode-select", ["--install"])
        try await waitUntil(minutes: 45, what: SetupItem.commandLineTools.title) { Self.commandLineToolsInstalled }
        note("✓ Command-line tools installed")
    }

    private func installHomebrew() async throws {
        if brewPath != nil { return }
        if !Self.commandLineToolsInstalled { try await installCommandLineTools() }
        set(.homebrew, .working("Downloading Homebrew's official installer…", 0))
        note("Downloading Homebrew's installer from github.com/Homebrew/brew")
        let package = try await Self.latestHomebrewPackage()
        let destination = AppFolders.work.appendingPathComponent(package.lastPathComponent)
        let file = try await FileDownloader(destination: destination) { value in
            Task { @MainActor in self.set(.homebrew, .working("Downloading Homebrew's installer…", value)) }
        }.download(from: package)
        note("Opening \(file.lastPathComponent): click Continue, then Install, and type your Mac password")
        set(.homebrew, .waiting("Homebrew's installer is open: click Continue, then Install, and type your Mac password."))
        NSWorkspace.shared.open(file)
        try await waitUntil(minutes: 30, what: "Homebrew's installer") { self.brewPath != nil }
        try? FileManager.default.removeItem(at: file)
        note("✓ Homebrew installed (\(brewPath ?? ""))")
    }

    /// The other way: Homebrew's own script in Terminal (it asks for the password there).
    func installHomebrewInTerminal() {
        let file = AppFolders.work.appendingPathComponent("Install Homebrew.command")
        let script = """
        #!/bin/bash
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        echo
        echo "Done. Go back to YouTube Zeus: it continues by itself."

        """
        do {
            try script.write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        } catch {
            set(.homebrew, .failed(error.localizedDescription))
            return
        }
        NSWorkspace.shared.open(file)
        note("Homebrew's script is running in Terminal: it asks for your Mac password there")
        set(.homebrew, .waiting("Finish Homebrew's installer in Terminal (it asks for your Mac password)."))
        runTask = Task {
            running = true
            defer { running = false }
            do {
                try await waitUntil(minutes: 30, what: "Homebrew's installer") { self.brewPath != nil }
                note("✓ Homebrew installed (\(brewPath ?? ""))")
            } catch {
                set(.homebrew, .failed(error.localizedDescription))
            }
            await checkTools()
        }
    }

    private func brewInstall(_ item: SetupItem) async throws {
        guard let brew = brewPath else { throw SetupError.needsHomebrew }
        let formulas = item.formulas
        set(item, .working("Installing with Homebrew…", nil))
        note("$ brew install \(formulas.joined(separator: " "))")
        try await Self.runBrew(brew, ["install"] + formulas) { line in
            Task { @MainActor in self.brewLine(line, item: item) }
        }
        note("✓ \(item.title) installed")
    }

    private func installOllama() async throws {
        if !Self.ollamaInstalled { try await brewInstall(.ollama) }
        if await ollama.isRunning() { return }
        set(.ollama, .working("Starting the local AI…", nil))
        if let brew = brewPath,
           FileManager.default.isExecutableFile(atPath: (brew as NSString).deletingLastPathComponent + "/ollama") {
            note("$ brew services start ollama")
            var started = false
            do {
                try await Self.runBrew(brew, ["services", "start", "ollama"]) { line in
                    Task { @MainActor in self.note(line) }
                }
                started = true
            } catch {
                note("brew services: \(error.localizedDescription)")
            }
            // The service needs a moment before it answers (the "could not connect" error right after starting).
            if started {
                for _ in 0..<40 {
                    if await ollama.isRunning() { break }
                    try await Task.sleep(for: .milliseconds(500))
                }
            }
        }
        if !(await ollama.isRunning()) { try await ollama.ensureRunning() }
        note("✓ Ollama is running")
    }

    private func downloadModel() async throws {
        let serving = await ollama.isRunning()
        if !Self.ollamaInstalled || !serving { try await installOllama() }
        let tag = chosenModel
        let name = displayName(tag)
        set(.model, .working("Downloading \(name)…", 0))
        note("$ ollama pull \(tag)")
        try await ollama.pull(tag) { value in
            Task { @MainActor in self.set(.model, .working("Downloading \(name)…", value)) }
        }
        settings.polishModel = tag
        UserDefaults.standard.set(true, forKey: "setupModelChosen")
        note("✓ \(name) is ready: Zeus uses it for polishing, summaries, names, digests and Ask")
        AppLog.write("SETUP model \(tag) ready")
    }

    /// Switches Zeus to a model that is already downloaded.
    func useChosenModel() {
        settings.polishModel = chosenModel
        UserDefaults.standard.set(true, forKey: "setupModelChosen")
        note("Zeus now uses \(displayName(chosenModel))")
        Task { await checkModel() }
    }

    private func downloadSpeechModel() async throws {
        let model = whisperModel
        set(.speech, .working("Downloading \(model.label)…", 0))
        note("Downloading Whisper's \(model.rawValue) model")
        try await WhisperTranscriber(whisperPath: "", ffmpegPath: "", model: model).ensureModel { value in
            Task { @MainActor in self.set(.speech, .working("Downloading \(model.label)…", value)) }
        }
        note("✓ Speech model ready")
    }

    // MARK: Helpers

    private func waitUntil(minutes: Double, what: String, _ done: () -> Bool) async throws {
        let deadline = Date.now.addingTimeInterval(minutes * 60)
        while Date.now < deadline {
            if done() { return }
            try await Task.sleep(for: .seconds(2))
        }
        throw SetupError.timedOut(what)
    }

    private func set(_ item: SetupItem, _ state: SetupState) {
        states[item] = state
        if let onProgress, case .working(let text, let value?) = state {
            let step = Int(value * 10)
            if printedStep[item] != step {
                printedStep[item] = step
                onProgress("\(text) \(Int(value * 100)) %")
            }
        }
    }

    /// A check keeps the last error on screen until the item is really there.
    private func update(_ item: SetupItem, _ state: SetupState) {
        if case .failed = self.state(item), case .missing = state { return }
        set(item, state)
    }

    func note(_ line: String) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        log.append(trimmed)
        if log.count > 600 { log.removeFirst(log.count - 600) }
        onNote?(trimmed)
    }

    /// Homebrew's progress lines: the card shows the latest one, the details keep them all.
    private func brewLine(_ line: String, item: SetupItem) {
        let text = line.replacingOccurrences(of: "==> ", with: "").trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty, !text.hasPrefix("#") else { return }
        note(line)
        if case .working = state(item) { set(item, .working(String(text.prefix(120)), nil)) }
    }

    /// Runs brew without questions (`yes` answers "Do you want to proceed?").
    nonisolated static func runBrew(_ brew: String, _ arguments: [String],
                                    onLine: @escaping @Sendable (String) -> Void) async throws {
        let script = #"yes | NONINTERACTIVE=1 HOMEBREW_NO_ENV_HINTS=1 HOMEBREW_NO_INSTALL_CLEANUP=1 "$0" "$@""#
        do {
            _ = try await ProcessRunner.check("/bin/bash", ["-c", script, brew] + arguments, streamStdout: true, onLine: onLine)
        } catch ProcessFailure.failed(_, _, let message) {
            throw SetupError.command("brew \(arguments.joined(separator: " ")) failed: \(message)")
        }
    }

    /// The .pkg of Homebrew's latest release on GitHub (Apple's installer asks for the password).
    nonisolated static func latestHomebrewPackage() async throws -> URL {
        var request = URLRequest(url: URL(string: "https://api.github.com/repos/Homebrew/brew/releases/latest")!)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 20
        let (data, _) = try await URLSession.shared.data(for: request)
        guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let assets = json["assets"] as? [[String: Any]],
              let link = assets.compactMap({ $0["browser_download_url"] as? String }).first(where: { $0.hasSuffix(".pkg") }),
              let url = URL(string: link) else { throw SetupError.noPackage }
        return url
    }
}
