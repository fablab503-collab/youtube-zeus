import Foundation
import Observation

// "Do jobs from The Swap" (effects/MAKE-IT-ME.md): The Swap on the iPhone writes a `<stamp>-<name>.swapjob` folder into a
// shared place (iCloud Drive › The Swap › Jobs, or the NAS); this Mac does it with `zeus_effects.py watch`, which the app
// runs as a Process for as long as it is open. No network service: the engine only watches folders. Each job becomes
// an ordinary Zeus job (Jobs/<name>), so it shows in the Effects jobs list like the others.

// MARK: - Folders

nonisolated enum SwapFolders {
    static var iCloud: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs/The Swap/Jobs", isDirectory: true)
    }

    static var nas: URL { URL(fileURLWithPath: "/Volumes/Volume1/The Swap Jobs", isDirectory: true) }

    /// The folders the engine watches by default (the same rule as zfx/swapjobs.py: the ones that exist).
    static var candidates: [URL] { [iCloud, nas] }
    static var existing: [URL] { candidates.filter { FileManager.default.fileExists(atPath: $0.path) } }

    /// "iCloud Drive › The Swap › Jobs", "Volume1 › The Swap Jobs".
    static func place(_ url: URL) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var path = url.path
        let drive = home + "/Library/Mobile Documents/com~apple~CloudDocs"
        if path.hasPrefix(drive) {
            path = "iCloud Drive" + path.dropFirst(drive.count)
        } else if path.hasPrefix("/Volumes/") {
            path = String(path.dropFirst("/Volumes/".count))
        } else if path.hasPrefix(home + "/") {
            path = String(path.dropFirst(home.count + 1))
        }
        return path.split(separator: "/").joined(separator: " › ")
    }
}

/// One line the watcher printed about a job.
nonisolated struct SwapJobEvent: Equatable, Sendable, Identifiable {
    var folder: String
    var zeusJob: String?
    var status: String          // picked · waiting · running · done · error · cancelled
    var step: String?
    var progress: Double?
    var message: String
    var when: Date = .now

    var id: String { folder }
    var name: String {
        let last = (folder as NSString).lastPathComponent
        return last.hasSuffix(".swapjob") ? String(last.dropLast(".swapjob".count)) : last
    }
    var isFinished: Bool { ["done", "error", "cancelled"].contains(status) }
}

// MARK: - The watcher process

/// Runs `zeus_effects.py watch` while the app is open and keeps what it reports (the folders, the job being done).
@Observable
final class SwapWatcher {
    private(set) var isRunning = false
    private(set) var folders: [URL] = []
    /// The job the Mac is doing now (nil between jobs).
    private(set) var current: SwapJobEvent?
    /// The last jobs finished since launch, newest first.
    private(set) var recent: [SwapJobEvent] = []
    private(set) var lastError: String?
    private(set) var output: [String] = []
    private(set) var startedAt: Date?
    /// Called when a job is picked up or finished (the Effects jobs list refreshes).
    var onJob: ((SwapJobEvent) -> Void)?
    @ObservationIgnored private var box: EffectsProcess?
    @ObservationIgnored private var task: Task<Void, Never>?

    var isBusy: Bool { current != nil && !(current?.isFinished ?? true) }

    /// Starts the watcher when the engine is set up and a folder exists; `cleanDays` > 0 removes finished jobs older
    /// than that many days (Settings).
    func start(cleanDays: Int = 0) {
        guard !isRunning else { return }
        lastError = nil
        guard EffectsPaths.isSetUp, let script = EffectsPaths.script else {
            lastError = "The Effects engine is not set up yet (Effects › Set up)."
            return
        }
        folders = SwapFolders.existing
        guard !folders.isEmpty else {
            lastError = "No shared folder yet: iCloud Drive › The Swap › Jobs appears once The Swap has written a job."
            return
        }
        var arguments = ["watch"]
        for folder in folders { arguments += ["--folder", folder.path] }
        if cleanDays > 0 { arguments += ["--clean-days", String(cleanDays)] }
        let box = EffectsProcess()
        self.box = box
        isRunning = true
        startedAt = .now
        AppLog.write("SWAP watching " + folders.map(\.path).joined(separator: ", "))
        task = Task { [weak self] in
            await self?.run(script: script, arguments: arguments, box: box)
        }
    }

    func stop() {
        box?.cancel()
        task?.cancel()
    }

    func restart(cleanDays: Int) {
        stop()
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(400))
            self?.start(cleanDays: cleanDays)
        }
    }

    private func run(script: URL, arguments: [String], box: EffectsProcess) async {
        defer {
            isRunning = false
            self.box = nil
            if var job = current, !job.isFinished {
                job.status = "error"
                job.message = "The watcher stopped."
                current = nil
                recent.insert(job, at: 0)
            }
        }
        let stream: AsyncStream<EffectsRunner.Output>
        do {
            stream = try EffectsRunner.start(EffectsPaths.python, [script.path] + arguments, directory: EffectsPaths.engine, box: box)
        } catch {
            lastError = error.localizedDescription
            return
        }
        var lastLine = ""
        for await item in stream {
            switch item {
            case .stdout(let line): handle(line)
            case .stderr(let line):
                append(line)
                lastLine = line
            case .exit(let code):
                if !box.cancelled, code != 0 {
                    lastError = lastLine.isEmpty ? "The watcher stopped (code \(code))." : lastLine
                    AppLog.write("SWAP watcher stopped: \(lastError ?? "")")
                }
            }
        }
    }

    private func handle(_ line: String) {
        guard line.hasPrefix("{"), let object = EffectsJSON.any(line) as? [String: Any] else {
            append(line)
            return
        }
        if let error = object["error"], !(error is NSNull) {
            lastError = EffectsJSON.string(error)
            return
        }
        switch object["stage"] as? String {
        case "watch":
            if let list = object["folders"] as? [String] { folders = list.map { URL(fileURLWithPath: $0, isDirectory: true) } }
            if let message = EffectsJSON.string(object["message"]) { append(message) }
        case "job":
            guard let folder = object["job"] as? String else { return }
            let event = SwapJobEvent(folder: folder, zeusJob: object["zeus_job"] as? String,
                                     status: EffectsJSON.string(object["status"]) ?? "running",
                                     step: EffectsJSON.string(object["step"]),
                                     progress: EffectsJSON.number(object["progress"]),
                                     message: EffectsJSON.string(object["message"]) ?? "")
            if event.isFinished {
                current = nil
                recent.insert(event, at: 0)
                if recent.count > 20 { recent.removeLast(recent.count - 20) }
                append("\(event.name): \(event.status) — \(event.message)")
                onJob?(event)
            } else {
                let picked = current?.folder != event.folder
                current = event
                if picked {
                    append("\(event.name): picked up")
                    onJob?(event)
                }
            }
        case "clean":
            if let message = EffectsJSON.string(object["message"]), let job = object["job"] as? String {
                append("\((job as NSString).lastPathComponent): \(message)")
            }
        default:
            break
        }
    }

    private func append(_ line: String) {
        guard !line.isEmpty else { return }
        output.append(line)
        if output.count > 200 { output.removeFirst(output.count - 200) }
    }
}
