import Foundation

nonisolated enum WhisperError: LocalizedError, Sendable {
    case badOutput
    case download(String)

    var errorDescription: String? {
        switch self {
        case .badOutput: "Whisper finished but its transcript could not be read."
        case .download(let why): "The Whisper model could not be downloaded: \(why)"
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
    static var skillHistory: URL { sub("Skill History") }

    private static func sub(_ name: String) -> URL {
        let url = support.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

/// Local speech-to-text with whisper.cpp (whisper-cli), used when a video has no captions.
nonisolated struct WhisperTranscriber: Sendable {
    let whisperPath: String
    let ffmpegPath: String
    let model: WhisperModel

    var modelURL: URL { AppFolders.models.appendingPathComponent(model.fileName) }
    var hasModel: Bool {
        let size = (try? FileManager.default.attributesOfItem(atPath: modelURL.path)[.size] as? Int) ?? 0
        return size > 10_000_000
    }

    func ensureModel(progress: @escaping @Sendable (Double) -> Void) async throws {
        guard !hasModel else { return }
        let downloader = FileDownloader(destination: modelURL, progress: progress)
        _ = try await downloader.download(from: model.downloadURL)
    }

    func transcribe(audio: URL, workDir: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> TranscriptResult {
        let wav = workDir.appendingPathComponent("audio16k.wav")
        _ = try await ProcessRunner.check(ffmpegPath, [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", audio.path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav.path,
        ])
        let prefix = workDir.appendingPathComponent("whisper")
        let threads = max(4, ProcessInfo.processInfo.activeProcessorCount - 2)
        _ = try await ProcessRunner.check(whisperPath, [
            "-m", modelURL.path,
            "-f", wav.path,
            "-l", "auto",
            "-t", String(threads),
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
        let language = result?["language"] as? String ?? ""
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
