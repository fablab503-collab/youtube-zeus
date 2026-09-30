import Foundation

nonisolated enum WhisperError: LocalizedError, Sendable {
    case badOutput
    case download(String)

    var errorDescription: String? {
        switch self {
        case .badOutput: "Whisper finished but its transcript could not be read."
        case .download(let why): "The download failed: \(why)"
        }
    }
}

nonisolated enum AppFolders {
    static var support: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let url = base.appendingPathComponent("YouTube Zeus", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    static var models: URL { sub("Models") }
    static var work: URL { sub("Work") }
    /// Podcast artwork and frames of your own videos (shown in the app and copied next to notes).
    static var thumbnails: URL { sub("Thumbnails") }
    static var skillHistory: URL { sub("Skill History") }

    private static func sub(_ name: String) -> URL {
        let url = support.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

/// Which speech recognizer listens. Measured on an M2 Pro (20-minute talk, human captions as reference, 28 Sept 2026):
/// whisper.cpp as in 2.x 66.8 s / 7.9 % word errors; whisper.cpp without text context 53.4 s / 7.3 %;
/// MLX Whisper without text context 39.6 s / 7.2 %; WhisperKit (Neural Engine) 101 s. With text context both
/// greedy decoders fell into repetition loops (36–40 % errors), so Zeus never conditions on the previous text.
nonisolated enum SpeechEngine: String, CaseIterable, Identifiable, Sendable {
    case auto
    case mlx
    case whisperCpp = "whisper.cpp"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .auto: "Automatic — MLX when available, else whisper.cpp"
        case .mlx: "MLX Whisper (fastest on Apple silicon)"
        case .whisperCpp: "whisper.cpp"
        }
    }
}

/// MLX Whisper (large-v3-turbo) through `uvx`: nothing to install by hand beyond uv; the package and the model
/// (1.6 GB) are downloaded once into uv's and Hugging Face's caches.
nonisolated struct MLXWhisper: Sendable {
    static let package = "mlx-whisper==0.4.3"
    static let model = "mlx-community/whisper-large-v3-turbo"
    let uvx: String

    static var executable: String? { ToolLocator.find("uvx", override: "") }

    func transcribe(wav: URL, workDir: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> TranscriptResult {
        let out = workDir.appendingPathComponent("mlx", isDirectory: true)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        _ = try await ProcessRunner.check(uvx, [
            "--python", "3.12", "--from", Self.package, "mlx_whisper", wav.path,
            "--model", Self.model,
            "--condition-on-previous-text", "False",
            "--output-format", "json",
            "--output-dir", out.path,
            "--verbose", "False",
        ]) { line in
            if line.contains("frames/s") || line.contains("%|"), let percent = YTDLP.percent(in: line) { progress(percent) }
        }
        let json = out.appendingPathComponent(wav.deletingPathExtension().lastPathComponent + ".json")
        return try WhisperTranscriber.parse(Data(contentsOf: json))
    }
}

/// Local speech-to-text, used when a video has no captions, for podcasts without a transcript and for your files.
nonisolated struct WhisperTranscriber: Sendable {
    let whisperPath: String
    let ffmpegPath: String
    let model: WhisperModel
    var engine: SpeechEngine = .auto

    var modelURL: URL { AppFolders.models.appendingPathComponent(model.fileName) }
    var hasModel: Bool {
        let size = (try? FileManager.default.attributesOfItem(atPath: modelURL.path)[.size] as? Int) ?? 0
        return size > 10_000_000
    }

    /// The engine that will really listen (MLX needs uv; whisper.cpp is always the fallback). "Automatic" also looks at
    /// the memory the Mac has free: MLX's model (1.6 GB) crawled when the Mac was swapping (a 2.5-minute file took
    /// 5.5 minutes on 28 Sept, whisper.cpp 18 s), so a busy Mac gets the smaller whisper.cpp model.
    var resolvedEngine: SpeechEngine {
        switch engine {
        case .whisperCpp: return .whisperCpp
        case .mlx: return MLXWhisper.executable != nil ? .mlx : .whisperCpp
        case .auto:
            guard MLXWhisper.executable != nil else { return .whisperCpp }
            return SystemMemory.availableGB >= 5 ? .mlx : .whisperCpp
        }
    }

    var engineLabel: String { resolvedEngine == .mlx ? "MLX Whisper" : "whisper.cpp" }

    func ensureModel(progress: @escaping @Sendable (Double) -> Void) async throws {
        guard !hasModel else { return }
        let downloader = FileDownloader(destination: modelURL, progress: progress)
        _ = try await downloader.download(from: model.downloadURL)
    }

    func transcribe(audio: URL, workDir: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> TranscriptResult {
        try FileManager.default.createDirectory(at: workDir, withIntermediateDirectories: true)
        let wav = workDir.appendingPathComponent("audio16k.wav")
        _ = try await ProcessRunner.check(ffmpegPath, [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", audio.path, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav.path,
        ])
        if resolvedEngine == .mlx, let uvx = MLXWhisper.executable {
            // 16 kHz mono 16-bit: 32,000 bytes a second. MLX normally needs a tenth of the audio's length; when the Mac is
            // short of memory or the GPU is busy it can crawl, so after a generous limit whisper.cpp takes over.
            let size = (try? FileManager.default.attributesOfItem(atPath: wav.path)[.size] as? Int) ?? 0
            let limit = 240 + Double(size) / 32_000 * 0.6
            do {
                return try await Self.withTimeout(seconds: limit) {
                    try await MLXWhisper(uvx: uvx).transcribe(wav: wav, workDir: workDir, progress: progress)
                }
            } catch {
                if error is CancellationError, Task.isCancelled { throw error }
                AppLog.write("WHISPER MLX \(error is TimeoutError ? "too slow (\(Int(limit)) s)" : "failed: \(error.localizedDescription)"), using whisper.cpp")
            }
        }
        if !hasModel { try await ensureModel { _ in } }
        let prefix = workDir.appendingPathComponent("whisper")
        let threads = max(4, ProcessInfo.processInfo.activeProcessorCount - 2)
        _ = try await ProcessRunner.check(whisperPath, [
            "-m", modelURL.path,
            "-f", wav.path,
            "-l", "auto",
            "-t", String(threads),
            "-mc", "0",          // no text context: faster, and no repetition loops
            "-oj",
            "-of", prefix.path,
            "-pp",
        ]) { line in
            if line.contains("progress"), let percent = YTDLP.percent(in: line) { progress(percent) }
        }
        let jsonURL = prefix.appendingPathExtension("json")
        let data = try Data(contentsOf: jsonURL)
        return try Self.parse(data)
    }

    struct TimeoutError: Error {}

    /// Runs `work`, cancelling it (and its process) after `seconds`.
    static func withTimeout<T: Sendable>(seconds: Double, _ work: @escaping @Sendable () async throws -> T) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask { try await work() }
            group.addTask {
                try await Task.sleep(for: .seconds(seconds))
                throw TimeoutError()
            }
            guard let result = try await group.next() else { throw TimeoutError() }
            group.cancelAll()
            return result
        }
    }

    static func parse(_ data: Data) throws -> TranscriptResult {
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw WhisperError.badOutput
        }
        var segments: [TranscriptSegment] = []
        if let items = json["transcription"] as? [[String: Any]] {
            for item in items {
                let offsets = item["offsets"] as? [String: Any] ?? [:]
                let start = (offsets["from"] as? Double ?? 0) / 1000
                let end = (offsets["to"] as? Double ?? 0) / 1000
                let text = (item["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if keep(text) { segments.append(TranscriptSegment(start: start, end: end, text: text)) }
            }
        } else if let items = json["segments"] as? [[String: Any]] {
            for item in items {
                let text = (item["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                if keep(text) {
                    segments.append(TranscriptSegment(start: item["start"] as? Double ?? 0,
                                                      end: item["end"] as? Double ?? 0, text: text))
                }
            }
        }
        guard !segments.isEmpty else { throw YTDLPError.emptyTranscript }
        let result = json["result"] as? [String: Any]
        let language = result?["language"] as? String ?? json["language"] as? String ?? ""
        return TranscriptResult(segments: segments, source: .whisper, language: language)
    }

    private static func keep(_ text: String) -> Bool {
        !text.isEmpty && text != "[BLANK_AUDIO]" && text != "[ Silence ]" && text != "(silence)"
    }
}

/// Downloads a large file with progress (used for the Whisper model).
nonisolated final class FileDownloader: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    private let destination: URL
    private let progress: @Sendable (Double) -> Void
    private let lock = NSLock()
    private var continuation: CheckedContinuation<URL, Error>?

    init(destination: URL, progress: @escaping @Sendable (Double) -> Void) {
        self.destination = destination
        self.progress = progress
    }

    func download(from url: URL) async throws -> URL {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<URL, Error>) in
                lock.withLock { self.continuation = continuation }
                let session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
                session.downloadTask(with: url).resume()
                session.finishTasksAndInvalidate()
            }
        } onCancel: {
            self.finish(.failure(CancellationError()))
        }
    }

    private func finish(_ result: Result<URL, Error>) {
        let pending: CheckedContinuation<URL, Error>? = lock.withLock {
            let value = continuation
            continuation = nil
            return value
        }
        pending?.resume(with: result)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        if let http = downloadTask.response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            finish(.failure(WhisperError.download("HTTP \(http.statusCode)")))
            return
        }
        do {
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            try FileManager.default.moveItem(at: location, to: destination)
            finish(.success(destination))
        } catch {
            finish(.failure(error))
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard totalBytesExpectedToWrite > 0 else { return }
        progress(Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error { finish(.failure(WhisperError.download(error.localizedDescription))) }
    }
}

/// Memory the Mac can hand out right now (free, inactive and purgeable pages).
nonisolated enum SystemMemory {
    static var availableGB: Double {
        var stats = vm_statistics64()
        var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &stats) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                host_statistics64(mach_host_self(), HOST_VM_INFO64, $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { return 8 }
        let pages = Double(stats.free_count) + Double(stats.inactive_count) + Double(stats.purgeable_count)
        return pages * Double(getpagesize()) / 1_073_741_824
    }
}
