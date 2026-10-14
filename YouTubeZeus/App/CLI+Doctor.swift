import Foundation

/// 4.5: `zeus doctor <link|id>` — why a YouTube video cannot be eaten: yt-dlp's version, the sign-in in use, and the
/// video read in several ways (with and without the sign-in, with other YouTube clients). Reads only, downloads nothing.
/// Another site's link: what yt-dlp sees and whether the sound can be taken (downloaded to a temporary folder).
extension ZeusCLI {
    static func doctor(_ link: String, settings: AppSettings) async -> Int32 {
        if YouTubeLink.parse(link) == nil, link.lowercased().hasPrefix("http") {
            return await postDoctor(link, settings: settings)
        }
        let id: String
        if let parsed = YouTubeLink.parse(link), case .video(let found) = parsed { id = found } else { id = link }
        guard let path = ToolLocator.find("yt-dlp", override: settings.ytdlpPath) else {
            print("yt-dlp is not installed (brew install yt-dlp).")
            return 1
        }
        let version = (try? await ProcessRunner.run(path, ["--version"]))?.stdoutString
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "?"
        print("yt-dlp \(version) at \(path)")
        let deno = ToolLocator.find("deno", override: "") ?? "not found"
        print("JavaScript runtime for YouTube (deno): \(deno)")
        let cookies = settings.cookieArguments
        print("YouTube sign-in used by Zeus: " + (cookies.isEmpty ? "none" : cookies.joined(separator: " ")))
        print("")
        for attempt in YTDLP.attempts(cookies: cookies) {
            let arguments = ["-J", "--skip-download", "--no-warnings", "--no-playlist"] + attempt.arguments
                + ["--", YTDLP.watchURL(id)]
            let started = Date.now
            guard let output = try? await ProcessRunner.run(path, arguments) else {
                print("✗ \(attempt.name): could not run yt-dlp")
                continue
            }
            let seconds = String(format: "%.0f s", Date.now.timeIntervalSince(started))
            if output.status == 0,
               let json = try? JSONSerialization.jsonObject(with: output.stdout) as? [String: Any] {
                let title = json["title"] as? String ?? "?"
                let subtitles = (json["subtitles"] as? [String: Any] ?? [:]).count
                let auto = (json["automatic_captions"] as? [String: Any] ?? [:]).count
                let formats = (json["formats"] as? [Any] ?? []).count
                print("✓ \(attempt.name) (\(seconds)): “\(title)” — \(formats) formats, captions \(subtitles), auto-captions \(auto)")
            } else {
                print("✗ \(attempt.name) (\(seconds)): \(ProcessRunner.lastMeaningfulLine(output.stderr))")
            }
        }
        return 0
    }

    /// A post or a video on another site: what yt-dlp sees, then the sound taken the way the app takes it (in a folder
    /// deleted after) and what ffmpeg reads in it.
    static func postDoctor(_ link: String, settings: AppSettings) async -> Int32 {
        guard let ytdlp = settings.makeSocialYTDLP() else {
            print("yt-dlp is not installed (brew install yt-dlp).")
            return 1
        }
        print("Sign-in used for posts: " + (settings.socialCookieArguments.isEmpty ? "none" : "yes (Zeus's web window or your browser)"))
        do {
            let json = try await ytdlp.pageInfo(url: link)
            let formats = json["formats"] as? [[String: Any]] ?? []
            print("✓ read by yt-dlp (\(json["extractor_key"] as? String ?? "?")): “\(json["title"] as? String ?? "?")” — "
                  + "\(Int(json["duration"] as? Double ?? 0)) s, \(formats.count) formats, ext \(json["ext"] as? String ?? "?")")
            for format in formats.suffix(8) {
                print("    \(format["format_id"] as? String ?? "?") · \(format["protocol"] as? String ?? "?") · audio \(format["acodec"] as? String ?? "?") · video \(format["vcodec"] as? String ?? "?")")
            }
        } catch {
            print("✗ yt-dlp could not read it: \(error.localizedDescription)")
            return 1
        }
        let work = FileManager.default.temporaryDirectory.appendingPathComponent("zeus-doctor-" + UUID().uuidString.prefix(6), isDirectory: true)
        defer { try? FileManager.default.removeItem(at: work) }
        do {
            let audio = try await ytdlp.downloadAudio(url: link, workDir: work) { _ in }
            let size = (try? FileManager.default.attributesOfItem(atPath: audio.path)[.size] as? Int) ?? 0
            print("✓ sound taken: \(audio.lastPathComponent), \(size / 1024) KB")
            if let ffmpeg = ToolLocator.find("ffmpeg", override: settings.ffmpegPath),
               let probe = try? await ProcessRunner.run(ffmpeg, ["-hide_banner", "-i", audio.path]) {
                let streams = probe.stderr.split(whereSeparator: \.isNewline).filter { $0.contains("Stream #") || $0.contains("Invalid") || $0.contains("Error") }
                print(streams.isEmpty ? "  ffmpeg finds no stream in it" : streams.prefix(4).map { "  ffmpeg: " + $0.trimmingCharacters(in: .whitespaces) }.joined(separator: "\n"))
            }
        } catch {
            print("✗ the sound could not be taken: \(error.localizedDescription)")
        }
        return 0
    }
}
