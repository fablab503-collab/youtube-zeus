import CoreGraphics
import Foundation
import ImageIO
import Vision

/// Reads the text shown in a video, on this Mac: frames are taken every few seconds with ffmpeg, frames that did not
/// change are skipped, and Apple's Vision framework reads the others. What is kept: slide titles (large text at the top),
/// terminal commands and code blocks, each with the moment it first appears. Free, private, no AI.
nonisolated struct ScreenReader: Sendable {
    let ffmpegPath: String
    var interval: Double = 2

    struct Line: Sendable {
        let text: String
        let box: CGRect      // normalized, origin bottom-left (Vision)
        let confidence: Float
    }

    func read(video: URL, workDir: URL, progress: @escaping @Sendable (String, Double?) -> Void) async throws -> [ScreenItem] {
        let frames = workDir.appendingPathComponent("frames", isDirectory: true)
        try? FileManager.default.removeItem(at: frames)
        try FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
        progress("Taking frames", nil)
        _ = try await ProcessRunner.check(ffmpegPath, [
            "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", video.path,
            "-an", "-vf", "fps=1/\(interval),scale='min(1920,iw)':-2",
            "-q:v", "3",
            frames.appendingPathComponent("%05d.jpg").path,
        ])
        let files = ((try? FileManager.default.contentsOfDirectory(at: frames, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "jpg" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        var collector = Collector()
        var previous: [UInt8]?
        var read = 0
        for (index, file) in files.enumerated() {
            try Task.checkCancellation()
            if index % 10 == 0 { progress("Reading the screen", Double(index) / Double(max(1, files.count))) }
            guard let image = Self.image(file) else { continue }
            let thumb = Self.fingerprint(image)
            if let previous, Self.difference(previous, thumb) < 3.0 { continue }
            previous = thumb
            let lines = try Self.recognize(image)
            read += 1
            collector.add(lines, at: Double(index) * interval)
        }
        AppLog.write("SCREEN \(files.count) frames, \(read) read, \(collector.items.count) items")
        return collector.items
    }

    // MARK: Frames

    static func image(_ url: URL) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }

    /// A 48×27 grayscale thumbnail: enough to see whether the screen changed.
    static func fingerprint(_ image: CGImage) -> [UInt8] {
        let width = 48, height = 27
        var pixels = [UInt8](repeating: 0, count: width * height)
        pixels.withUnsafeMutableBytes { buffer in
            guard let context = CGContext(data: buffer.baseAddress, width: width, height: height, bitsPerComponent: 8,
                                          bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
                                          bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return }
            context.interpolationQuality = .medium
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        }
        return pixels
    }

    /// Mean absolute difference (0–255) between two thumbnails.
    static func difference(_ a: [UInt8], _ b: [UInt8]) -> Double {
        guard a.count == b.count, !a.isEmpty else { return 255 }
        var total = 0
        for index in a.indices { total += abs(Int(a[index]) - Int(b[index])) }
        return Double(total) / Double(a.count)
    }

    static func recognize(_ image: CGImage) throws -> [Line] {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.automaticallyDetectsLanguage = true
        request.minimumTextHeight = 0.012
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        return (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first else { return nil }
            let text = candidate.string.trimmingCharacters(in: .whitespaces)
            guard text.count >= 2 else { return nil }
            return Line(text: text, box: observation.boundingBox, confidence: candidate.confidence)
        }
    }

    // MARK: What to keep

    static let commandWords: Set<String> = [
        "npm", "npx", "pnpm", "yarn", "bun", "bunx", "pip", "pip3", "pipx", "uv", "uvx", "brew", "git", "gh", "cd", "ls", "mkdir",
        "curl", "wget", "docker", "kubectl", "python", "python3", "node", "deno", "go", "cargo", "rustup", "swift", "xcodebuild",
        "claude", "codex", "gemini", "ollama", "export", "sudo", "chmod", "ssh", "scp", "make", "terraform", "aws", "gcloud",
        "az", "vercel", "supabase", "firebase", "n8n", "code", "cat", "echo", "touch", "rm", "cp", "mv", "source", "conda",
        "poetry", "flutter", "dotnet", "java", "mvn", "gradle", "php", "composer", "ruby", "gem", "bundle", "rails", "heroku",
        "netlify", "wrangler", "fly", "zeus", "yt-dlp", "ffmpeg", "tmux", "vim", "nano", "open", "defaults", "launchctl",
    ]

    /// "$ npm install x", "claude mcp add …", "git clone https://…"
    static func command(_ line: String) -> String? {
        var text = line.trimmingCharacters(in: .whitespaces)
        for prompt in ["$ ", "% ", "❯ ", "› ", "> ", "# ", "➜ ", "λ "] where text.hasPrefix(prompt) {
            text = String(text.dropFirst(prompt.count)).trimmingCharacters(in: .whitespaces)
        }
        let words = text.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        guard words.count >= 2, words.count <= 25, let first = words.first?.lowercased(), commandWords.contains(first),
              words.first == words.first?.lowercased() else { return nil }   // "Claude Sonnet 4.5", "Go ahead…" are not commands
        let second = words[1]
        // Words that are also English words or product names need something that looks like an argument.
        if ["code", "open", "make", "go", "source", "echo", "cat", "claude", "codex", "gemini", "zeus", "touch", "fly",
            "bundle", "gem", "java", "php", "ruby", "node", "python", "python3"].contains(first) {
            let looksLikeArgument = second.hasPrefix("-") || second.contains(where: { "./~=:\"'".contains($0) })
                || (second == second.lowercased() && second.count <= 12 && words.count <= 8 && !second.contains(where: \.isUppercase))
            guard looksLikeArgument, words.count <= 12 else { return nil }
        }
        // Prose that happens to start with a command word ("curl sails through while…"): a real command line is short,
        // or has an option, a path, a URL or an assignment early on.
        func argumentLike(_ word: String) -> Bool { word.hasPrefix("-") || word.contains(where: { "./~=:\"'$@".contains($0) }) }
        guard words.count <= 6 || words.prefix(4).dropFirst().contains(where: argumentLike) else { return nil }
        return text
    }

    static let codeSignals = ["{", "}", "(", ")", ";", " = ", "=>", "->", "::", "</", "/>", "\":", "==", "!=", "&&", "||", "[]", "()"]
    static let codeStarts = ["import ", "from ", "def ", "func ", "function ", "const ", "let ", "var ", "class ", "return ",
                             "if (", "if ", "for (", "for ", "while ", "#include", "print(", "console.log", "async ", "await ",
                             "public ", "private ", "struct ", "enum ", "export ", "SELECT ", "INSERT ", "<div", "<script",
                             "\"name\":", "- name:", "@", "//", "# "]

    static func codeScore(_ line: String) -> Int {
        var score = codeSignals.reduce(0) { $0 + (line.contains($1) ? 1 : 0) }
        if codeStarts.contains(where: { line.hasPrefix($0) }) { score += 2 }
        let symbols = line.filter { "{}()[];=<>:\"'/_.".contains($0) }.count
        if Double(symbols) / Double(max(1, line.count)) > 0.12 { score += 1 }
        return score
    }

    /// Keeps each thing once, at the moment it first appears.
    struct Collector {
        private(set) var items: [ScreenItem] = []
        private var seenTitles = Set<String>()
        private var seenCommands = Set<String>()
        private var lastCode: Set<String> = []

        /// Character trigrams in common (Jaccard): OCR noise barely changes it, different code does.
        static func similarity(_ a: String, _ b: String) -> Double {
            func grams(_ text: String) -> Set<String> {
                let chars = Array(text.lowercased().filter { !$0.isWhitespace })
                guard chars.count >= 3 else { return [String(chars)] }
                return Set((0..<(chars.count - 2)).map { String(chars[$0..<$0 + 3]) })
            }
            let x = grams(a), y = grams(b)
            let union = x.union(y).count
            return union == 0 ? 0 : Double(x.intersection(y).count) / Double(union)
        }

        static func normalize(_ text: String) -> String {
            text.lowercased().replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
        }

        mutating func add(_ lines: [Line], at seconds: Double) {
            guard !lines.isEmpty, items.count < 150 else { return }
            let heights = lines.map(\.box.height).sorted()
            let median = heights[heights.count / 2]
            // Top to bottom (Vision's origin is at the bottom).
            let ordered = lines.sorted { $0.box.maxY > $1.box.maxY }

            // Slide titles: on a frame that looks like a slide (few lines), the biggest text in the upper part.
            let isSlide = lines.count <= 14
            var titles = 0
            for line in ordered where isSlide && titles < 2 && line.box.height >= median * 1.6 && line.box.maxY > 0.5
                && line.confidence >= 0.5 {
                let text = line.text
                guard text.count >= 6, text.count <= 90, text.contains(where: \.isLetter), ScreenReader.command(text) == nil,
                      ScreenReader.codeScore(text) < 2, text.split(separator: " ").count >= 2 || text.count >= 10,
                      Set(text.lowercased().filter(\.isLetter)).count >= 4 else { continue }
                titles += 1
                if seenTitles.insert(Self.normalize(text)).inserted {
                    items.append(ScreenItem(start: seconds, kind: .title, text: text))
                }
            }

            // Commands.
            var commandLines = Set<String>()
            for line in ordered where line.confidence >= 0.4 {
                guard let command = ScreenReader.command(line.text) else { continue }
                commandLines.insert(line.text)
                if seenCommands.insert(Self.normalize(command)).inserted {
                    items.append(ScreenItem(start: seconds, kind: .command, text: command))
                }
            }

            // Code: several code-looking lines on the same frame make a block.
            let code = ordered.filter { !commandLines.contains($0.text) && $0.confidence >= 0.35 && ScreenReader.codeScore($0.text) >= 2 }
            if code.count >= 3 {
                let block = code.map(\.text).joined(separator: "\n")
                let set = Set(code.map { Self.normalize($0.text) })
                let fresh = set.subtracting(lastCode)
                let recent = items.indices.filter { items[$0].kind == .code }.suffix(6)
                if let same = recent.last(where: { Self.similarity(items[$0].text, block) >= 0.45 }) {
                    // The same code being typed or scrolled, or shown again: keep its first moment and the fuller text.
                    if block.count > items[same].text.count { items[same].text = block }
                    lastCode.formUnion(set)
                } else if Double(fresh.count) / Double(max(1, set.count)) >= 0.5 {
                    items.append(ScreenItem(start: seconds, kind: .code, text: block))
                    lastCode = set
                } else {
                    lastCode.formUnion(set)
                }
            }
        }
    }
}
