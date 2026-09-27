import Foundation

nonisolated struct ProcessOutput: Sendable {
    let status: Int32
    let stdout: Data
    let stderr: String

    var stdoutString: String { String(decoding: stdout, as: UTF8.self) }
}

nonisolated enum ProcessFailure: LocalizedError, Sendable {
    case missingTool(String)
    case failed(tool: String, status: Int32, message: String)
    case cancelled

    var errorDescription: String? {
        switch self {
        case .missingTool(let tool):
            "\(tool) is not installed. Install it with Homebrew (brew install \(tool)) or set its path in Settings › Tools."
        case .failed(let tool, let status, let message):
            "\(tool) stopped with code \(status): \(message)"
        case .cancelled:
            "Cancelled"
        }
    }
}

/// Holds everything a running process shares between threads.
nonisolated final class ProcessBox: @unchecked Sendable {
    let process = Process()
    let outPipe = Pipe()
    let errPipe = Pipe()
    private let lock = NSLock()
    private var out = Data()
    private var err = Data()
    private var pending = Data()
    private(set) var cancelled = false
    private var resumed = false
    private var outEOF = false
    private var errEOF = false
    private(set) var terminated = false

    /// Marks a pipe as finished; returns true when the process has ended and both pipes are drained.
    func markEOF(stdout: Bool) -> Bool {
        lock.withLock {
            if stdout { outEOF = true } else { errEOF = true }
            return terminated && outEOF && errEOF
        }
    }

    func markTerminated() -> Bool {
        lock.withLock {
            terminated = true
            return outEOF && errEOF
        }
    }

    func appendOut(_ data: Data, splitLines: Bool) -> [String] {
        lock.withLock {
            out.append(data)
            return splitLines ? extractLines(data) : []
        }
    }

    func appendErr(_ data: Data) -> [String] {
        lock.withLock {
            err.append(data)
            return extractLines(data)
        }
    }

    private func extractLines(_ data: Data) -> [String] {
        pending.append(data)
        var lines: [String] = []
        while let index = pending.firstIndex(where: { $0 == 10 || $0 == 13 }) {
            let line = pending[pending.startIndex..<index]
            pending = Data(pending[(index + 1)...])
            let text = String(decoding: line, as: UTF8.self).trimmingCharacters(in: .whitespaces)
            if !text.isEmpty { lines.append(text) }
        }
        if pending.count > 64_000 { pending.removeAll() }
        return lines
    }

    func snapshot() -> (Data, Data) { lock.withLock { (out, err) } }

    /// Returns true only the first time, so a continuation is resumed once.
    func claimResume() -> Bool {
        lock.withLock {
            if resumed { return false }
            resumed = true
            return true
        }
    }

    func cancel() {
        lock.withLock { cancelled = true }
        if process.isRunning { process.terminate() }
    }
}

nonisolated enum ProcessRunner {
    static let searchPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    /// Runs a command without blocking, collecting stdout and stderr.
    /// `onLine` receives progress lines (stderr always, stdout when `streamStdout` is true).
    static func run(
        _ executable: String,
        _ arguments: [String],
        currentDirectory: URL? = nil,
        streamStdout: Bool = false,
        onLine: (@Sendable (String) -> Void)? = nil
    ) async throws -> ProcessOutput {
        guard FileManager.default.isExecutableFile(atPath: executable) else {
            throw ProcessFailure.missingTool((executable as NSString).lastPathComponent)
        }
        try Task.checkCancellation()

        let box = ProcessBox()
        let process = box.process
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        if let currentDirectory { process.currentDirectoryURL = currentDirectory }
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = searchPath + ":" + (environment["PATH"] ?? "")
        environment["PYTHONUNBUFFERED"] = "1"
        environment["LANG"] = environment["LANG"] ?? "en_US.UTF-8"
        process.environment = environment
        process.standardOutput = box.outPipe
        process.standardError = box.errPipe
        process.standardInput = FileHandle.nullDevice

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<ProcessOutput, Error>) in
                // Resumes once: when the process has ended and both pipes reached EOF, or shortly after
                // the end if a child process keeps a pipe open (for example a helper started by the tool).
                let finish: @Sendable () -> Void = {
                    guard box.claimResume() else { return }
                    box.outPipe.fileHandleForReading.readabilityHandler = nil
                    box.errPipe.fileHandleForReading.readabilityHandler = nil
                    let (out, err) = box.snapshot()
                    if box.cancelled {
                        continuation.resume(throwing: ProcessFailure.cancelled)
                    } else {
                        continuation.resume(returning: ProcessOutput(
                            status: box.process.terminationStatus,
                            stdout: out,
                            stderr: String(decoding: err, as: UTF8.self)))
                    }
                }
                box.outPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    if data.isEmpty {
                        handle.readabilityHandler = nil
                        if box.markEOF(stdout: true) { finish() }
                        return
                    }
                    let lines = box.appendOut(data, splitLines: streamStdout && onLine != nil)
                    if let onLine { lines.forEach(onLine) }
                }
                box.errPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    if data.isEmpty {
                        handle.readabilityHandler = nil
                        if box.markEOF(stdout: false) { finish() }
                        return
                    }
                    let lines = box.appendErr(data)
                    if let onLine { lines.forEach(onLine) }
                }
                process.terminationHandler = { _ in
                    if box.markTerminated() {
                        finish()
                    } else {
                        DispatchQueue.global().asyncAfter(deadline: .now() + 1.5) { finish() }
                    }
                }
                do {
                    try process.run()
                } catch {
                    if box.claimResume() { continuation.resume(throwing: error) }
                }
            }
        } onCancel: {
            box.cancel()
        }
    }

    /// Runs a command and throws a readable error when it fails.
    static func check(
        _ executable: String,
        _ arguments: [String],
        currentDirectory: URL? = nil,
        streamStdout: Bool = false,
        onLine: (@Sendable (String) -> Void)? = nil
    ) async throws -> ProcessOutput {
        let output = try await run(executable, arguments, currentDirectory: currentDirectory,
                                   streamStdout: streamStdout, onLine: onLine)
        guard output.status == 0 else {
            throw ProcessFailure.failed(
                tool: (executable as NSString).lastPathComponent,
                status: output.status,
                message: Self.lastMeaningfulLine(output.stderr))
        }
        return output
    }

    static func lastMeaningfulLine(_ text: String) -> String {
        let lines = text.split(whereSeparator: \.isNewline).map(String.init)
        if let error = lines.last(where: { $0.contains("ERROR") || $0.lowercased().contains("error") }) {
            return String(error.prefix(400))
        }
        return String((lines.last ?? "no details").prefix(400))
    }
}

/// Finds the command-line tools the app relies on.
nonisolated enum ToolLocator {
    static func find(_ name: String, override: String) -> String? {
        let expanded = (override as NSString).expandingTildeInPath
        if !expanded.isEmpty, FileManager.default.isExecutableFile(atPath: expanded) { return expanded }
        for dir in ProcessRunner.searchPath.split(separator: ":") {
            let path = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: path) { return path }
        }
        return nil
    }
}

/// A small log for troubleshooting: ~/Library/Application Support/YouTube Zeus/zeus.log
nonisolated enum AppLog {
    private static let lock = NSLock()

    static func write(_ message: String) {
        let line = ISO8601DateFormatter().string(from: .now) + "  " + message.replacingOccurrences(of: "\n", with: " ") + "\n"
        let url = AppFolders.support.appendingPathComponent("zeus.log")
        lock.withLock {
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(Data(line.utf8))
                try? handle.close()
            } else {
                try? Data(line.utf8).write(to: url)
            }
            if let size = try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int, size > 2_000_000 {
                let old = url.deletingLastPathComponent().appendingPathComponent("zeus.old.log")
                try? FileManager.default.removeItem(at: old)
                try? FileManager.default.moveItem(at: url, to: old)
            }
        }
    }
}
