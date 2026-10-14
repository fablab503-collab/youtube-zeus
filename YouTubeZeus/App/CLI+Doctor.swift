import Foundation

/// 4.5: `zeus doctor <link|id>` — why a YouTube video cannot be eaten: yt-dlp's version, the sign-in in use, and the
/// video read in several ways (with and without the sign-in, with other YouTube clients). Reads only, downloads nothing.
extension ZeusCLI {
    static func doctor(_ link: String, settings: AppSettings) async -> Int32 {
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
}
