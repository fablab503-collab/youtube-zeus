import CryptoKit
import Foundation

/// Links from X, Instagram, Facebook, Threads and Spotify (3.8).
///
/// - X, Instagram, Facebook, Threads: a post becomes an item of kind `.social` (id `web-<hash>`). yt-dlp reads it
///   (with the sign-ins made in Zeus's web window); when the post has a video or audio, Whisper listens to it, and the
///   words of the post are kept too. Posts with only text keep their text.
/// - Spotify: an episode or a show is looked up in Apple's free podcast directory by its name and eaten from the
///   public RSS feed (Spotify itself never gives the audio). Spotify-only shows and music cannot be eaten.
nonisolated enum SocialSite: String, Sendable, CaseIterable {
    case x, instagram, facebook, threads, spotify
    // 4.3: more sites, and any other page yt-dlp knows (about 1,800 sites) as `.web`.
    case telegram, tiktok, linkedin, reddit, bluesky, vimeo, twitch, soundcloud, dailymotion, rumble, kick, pinterest
    case web

    var name: String {
        switch self {
        case .x: "X"
        case .instagram: "Instagram"
        case .facebook: "Facebook"
        case .threads: "Threads"
        case .spotify: "Spotify"
        case .telegram: "Telegram"
        case .tiktok: "TikTok"
        case .linkedin: "LinkedIn"
        case .reddit: "Reddit"
        case .bluesky: "Bluesky"
        case .vimeo: "Vimeo"
        case .twitch: "Twitch"
        case .soundcloud: "SoundCloud"
        case .dailymotion: "Dailymotion"
        case .rumble: "Rumble"
        case .kick: "Kick"
        case .pinterest: "Pinterest"
        case .web: "the web"
        }
    }

    /// What one item is called ("X post", "Telegram message"…).
    var itemName: String {
        switch self {
        case .spotify: "Spotify episode"
        case .telegram: "Telegram message"
        case .vimeo, .dailymotion, .rumble, .tiktok: "\(name) video"
        case .twitch, .kick: "\(name) stream"
        case .soundcloud: "SoundCloud track"
        case .web: "Web page"
        default: "\(name) post"
        }
    }

    var symbol: String {
        switch self {
        case .x: "text.bubble.fill"
        case .instagram: "camera.fill"
        case .facebook: "person.2.fill"
        case .threads: "at"
        case .spotify, .soundcloud: "waveform"
        case .telegram: "paperplane.fill"
        case .tiktok: "music.note"
        case .linkedin: "briefcase.fill"
        case .reddit: "bubble.left.and.bubble.right.fill"
        case .bluesky: "cloud.fill"
        case .vimeo, .dailymotion, .rumble: "play.rectangle.fill"
        case .twitch, .kick: "dot.radiowaves.left.and.right"
        case .pinterest: "pin.fill"
        case .web: "globe"
        }
    }

    /// The page to sign in from (inside Zeus), for posts that need an account.
    var signInURL: URL? {
        switch self {
        case .x: URL(string: "https://x.com/login")
        case .instagram: URL(string: "https://www.instagram.com/accounts/login/")
        case .facebook: URL(string: "https://www.facebook.com/login")
        case .threads: URL(string: "https://www.threads.com/login")
        case .tiktok: URL(string: "https://www.tiktok.com/login")
        case .linkedin: URL(string: "https://www.linkedin.com/login")
        case .reddit: URL(string: "https://www.reddit.com/login")
        case .vimeo: URL(string: "https://vimeo.com/log_in")
        default: nil
        }
    }

    /// The cookie domains kept for yt-dlp after a sign-in.
    var cookieDomains: [String] {
        switch self {
        case .x: ["x.com", "twitter.com"]
        case .instagram: ["instagram.com"]
        case .facebook: ["facebook.com"]
        case .threads: ["threads.net", "threads.com", "instagram.com"]
        case .tiktok: ["tiktok.com"]
        case .linkedin: ["linkedin.com"]
        case .reddit: ["reddit.com"]
        case .vimeo: ["vimeo.com"]
        default: []
        }
    }

    /// The site of a link: a known one, `.web` for any other http(s) page, nil for what is not a link at all.
    static func of(_ text: String) -> SocialSite? {
        guard let url = SocialLinks.url(text), let host = url.host?.lowercased(), host.contains(".") else { return nil }
        func on(_ domains: String...) -> Bool { domains.contains { host == $0 || host.hasSuffix("." + $0) } }
        if on("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com") { return .x }
        if on("instagram.com", "instagr.am") { return .instagram }
        if on("facebook.com", "fb.watch", "fb.com") { return .facebook }
        if on("threads.net", "threads.com") { return .threads }
        if on("open.spotify.com", "spotify.link", "spotify.com") { return .spotify }
        if on("t.me", "telegram.me", "telegram.org") { return .telegram }
        if on("tiktok.com") { return .tiktok }
        if on("linkedin.com", "lnkd.in") { return .linkedin }
        if on("reddit.com", "redd.it") { return .reddit }
        if on("bsky.app") { return .bluesky }
        if on("vimeo.com") { return .vimeo }
        if on("twitch.tv") { return .twitch }
        if on("soundcloud.com") { return .soundcloud }
        if on("dailymotion.com", "dai.ly") { return .dailymotion }
        if on("rumble.com") { return .rumble }
        if on("kick.com") { return .kick }
        if on("pinterest.com", "pin.it") { return .pinterest }
        return .web
    }

    /// The sites listed by name (the Eat bar takes any other link too).
    static var named: [SocialSite] { allCases.filter { $0 != .web } }
}

nonisolated enum SocialLinks {
    static func url(_ text: String) -> URL? {
        var value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, !value.contains(" ") else { return nil }
        if !value.lowercased().hasPrefix("http") { value = "https://" + value }
        guard let url = URL(string: value), url.host != nil else { return nil }
        return url
    }

    /// The post's address without tracking parameters (`?s=46`, `igsh=`…); Facebook keeps the ones that name the post.
    static func canonical(_ text: String) -> URL? {
        guard let url = url(text), var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        let keep: Set<String> = ["v", "story_fbid", "id", "fbid"]
        parts.queryItems = parts.queryItems?.filter { keep.contains($0.name) }
        if parts.queryItems?.isEmpty == true { parts.queryItems = nil }
        parts.fragment = nil
        if let host = parts.host?.lowercased() {
            // Same post, one address.
            if host == "twitter.com" || host == "mobile.twitter.com" || host == "fxtwitter.com" || host == "vxtwitter.com" { parts.host = "x.com" }
            if host == "m.facebook.com" || host == "web.facebook.com" { parts.host = "www.facebook.com" }
            if host == "threads.net" || host == "www.threads.net" { parts.host = "www.threads.com" }
        }
        if parts.path.count > 1, parts.path.hasSuffix("/") { parts.path.removeLast() }
        return parts.url
    }

    /// The library id of a post: `web-` and a short hash of its address.
    static func id(for text: String) -> String? {
        guard let url = canonical(text) else { return nil }
        let hash = SHA256.hash(data: Data(url.absoluteString.lowercased().utf8)).prefix(6).map { String(format: "%02x", $0) }.joined()
        return "web-" + hash
    }

    /// The author of a post from its address, when the address names it: X, Threads, TikTok (`@user`), Telegram
    /// (`t.me/<channel>/<id>` → `@channel`), Reddit (`r/<subreddit>`), Bluesky (`/profile/<handle>`).
    static func handle(in url: URL) -> String? {
        let parts = url.pathComponents.filter { $0 != "/" }
        guard let first = parts.first else { return nil }
        switch SocialSite.of(url.absoluteString) {
        case .reddit:
            return first == "r" && parts.count > 1 ? "r/" + parts[1] : nil
        case .bluesky:
            return first == "profile" && parts.count > 1 ? "@" + parts[1] : nil
        case .telegram:
            let channel = first == "s" && parts.count > 1 ? parts[1] : first
            return ["joinchat", "addstickers", "c", "share"].contains(channel) ? nil : "@" + channel
        case .x, .threads, .tiktok, .instagram:
            guard !["i", "p", "reel", "reels", "tv", "stories", "video", "share", "t"].contains(first) else { return nil }
            return first.hasPrefix("@") ? first : "@" + first
        case .vimeo, .soundcloud, .twitch, .kick, .rumble, .dailymotion:
            return Int(first) == nil && !["video", "embed", "c", "v"].contains(first) ? first : nil
        default:
            return nil
        }
    }

    // MARK: The page itself (when yt-dlp cannot read the post)

    struct PageMeta: Sendable {
        var title = ""
        var description = ""
        var image: String?
        var video: String?
        var audio: String?
        var siteName = ""
    }

    /// Open Graph tags of a public page (title, text, picture, video).
    static func pageMeta(_ url: URL) async -> PageMeta? {
        var request = URLRequest(url: url, timeoutInterval: 20)
        // Social sites serve their tags to link-preview bots, not always to browsers.
        request.setValue("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)", forHTTPHeaderField: "User-Agent")
        request.setValue("en-US,en;q=0.8", forHTTPHeaderField: "Accept-Language")
        guard let (data, _) = try? await URLSession.shared.data(for: request) else { return nil }
        let html = String(decoding: data.prefix(1_500_000), as: UTF8.self)
        var meta = PageMeta()
        meta.title = tag(html, "og:title") ?? tag(html, "twitter:title") ?? ""
        meta.description = tag(html, "og:description") ?? tag(html, "twitter:description") ?? tag(html, "description") ?? ""
        meta.image = tag(html, "og:image") ?? tag(html, "twitter:image")
        meta.video = tag(html, "og:video:secure_url") ?? tag(html, "og:video:url") ?? tag(html, "og:video")
        meta.audio = tag(html, "og:audio")
        meta.siteName = tag(html, "og:site_name") ?? ""
        guard !meta.title.isEmpty || !meta.description.isEmpty else { return nil }
        return meta
    }

    /// `<meta property|name="key" content="…">` in either order, HTML entities decoded.
    static func tag(_ html: String, _ key: String) -> String? {
        let escaped = NSRegularExpression.escapedPattern(for: key)
        let patterns = [
            "<meta[^>]+(?:property|name)=[\"']\(escaped)[\"'][^>]*content=[\"']([^\"']*)[\"']",
            "<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)=[\"']\(escaped)[\"']",
        ]
        for pattern in patterns {
            guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
                  let match = regex.firstMatch(in: html, range: NSRange(html.startIndex..., in: html)),
                  let range = Range(match.range(at: 1), in: html) else { continue }
            let value = decodeEntities(String(html[range])).trimmingCharacters(in: .whitespacesAndNewlines)
            if !value.isEmpty { return value }
        }
        return nil
    }

    static func decodeEntities(_ text: String) -> String {
        var value = text
        let named = ["&amp;": "&", "&quot;": "\"", "&#39;": "'", "&#x27;": "'", "&apos;": "'", "&lt;": "<", "&gt;": ">", "&nbsp;": " "]
        for (entity, character) in named { value = value.replacingOccurrences(of: entity, with: character) }
        // Numeric entities (&#8217; &#x1F600;)
        if let regex = try? NSRegularExpression(pattern: "&#(x?)([0-9a-fA-F]+);") {
            let matches = regex.matches(in: value, range: NSRange(value.startIndex..., in: value)).reversed()
            for match in matches {
                guard let whole = Range(match.range, in: value), let hexRange = Range(match.range(at: 1), in: value),
                      let numberRange = Range(match.range(at: 2), in: value) else { continue }
                let isHex = !value[hexRange].isEmpty
                guard let code = UInt32(value[numberRange], radix: isHex ? 16 : 10), let scalar = Unicode.Scalar(code) else { continue }
                value.replaceSubrange(whole, with: String(Character(scalar)))
            }
        }
        return value
    }

    // MARK: Spotify → the public podcast feed

    struct SpotifyItem: Sendable {
        var isShow: Bool
        var episodeTitle: String
        var showName: String
    }

    /// Reads a Spotify episode or show page: the episode's title and the show's name.
    static func spotify(_ url: URL) async throws -> SpotifyItem {
        let path = url.path.lowercased()
        if path.contains("/track/") || path.contains("/album/") || path.contains("/playlist/") || path.contains("/artist/") {
            throw SocialError.spotifyMusic
        }
        guard let meta = await pageMeta(url) else { throw SocialError.unreadable("Spotify") }
        if path.contains("/show/") {
            return SpotifyItem(isShow: true, episodeTitle: "", showName: meta.title)
        }
        // "Listen to this episode from <Show> on Spotify. …"
        var show = ""
        if let range = meta.description.range(of: #"(?i)episode from (.+?) on Spotify"#, options: .regularExpression) {
            show = String(meta.description[range])
                .replacingOccurrences(of: #"(?i)^episode from "#, with: "", options: .regularExpression)
                .replacingOccurrences(of: #"(?i) on Spotify$"#, with: "", options: .regularExpression)
        }
        // Newer pages: "<Episode> · <Show>" in the title.
        var title = meta.title
        if show.isEmpty, let dot = meta.title.range(of: " · ", options: .backwards) {
            show = String(meta.title[dot.upperBound...])
            title = String(meta.title[..<dot.lowerBound])
        }
        guard !show.isEmpty else { throw SocialError.unreadable("Spotify") }
        return SpotifyItem(isShow: false, episodeTitle: title, showName: show)
    }

    /// The public RSS feed of a show, found by its name in Apple's free podcast directory.
    static func feed(forShow name: String) async throws -> URL {
        var parts = URLComponents(string: "https://itunes.apple.com/search")!
        parts.queryItems = [.init(name: "media", value: "podcast"), .init(name: "entity", value: "podcast"),
                            .init(name: "limit", value: "10"), .init(name: "term", value: name)]
        let (data, _) = try await URLSession.shared.data(from: parts.url!)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let results = json?["results"] as? [[String: Any]] ?? []
        let wanted = simplified(name)
        let best = results.first { simplified($0["collectionName"] as? String ?? "") == wanted }
            ?? results.first { simplified($0["collectionName"] as? String ?? "").contains(wanted) || wanted.contains(simplified($0["collectionName"] as? String ?? "")) }
        guard let feed = (best?["feedUrl"] as? String).flatMap(URL.init(string:)) else {
            throw SocialError.spotifyOnly(name)
        }
        return feed
    }

    /// The episode of a feed whose title matches (the closest one).
    static func episode(_ title: String, in episodes: [PodcastEpisode]) -> PodcastEpisode? {
        let wanted = simplified(title)
        guard !wanted.isEmpty else { return nil }
        if let exact = episodes.first(where: { simplified($0.title) == wanted }) { return exact }
        if let contained = episodes.first(where: { simplified($0.title).contains(wanted) || wanted.contains(simplified($0.title)) }) { return contained }
        // Most words in common.
        let words = Set(wanted.split(separator: " "))
        let scored = episodes.map { episode in (episode, Set(simplified(episode.title).split(separator: " ")).intersection(words).count) }
        guard let top = scored.max(by: { $0.1 < $1.1 }), Double(top.1) >= Double(words.count) * 0.6 else { return nil }
        return top.0
    }

    static func simplified(_ text: String) -> String {
        text.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: nil)
            .components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }.joined(separator: " ")
    }
}

nonisolated enum SocialError: LocalizedError {
    case spotifyMusic
    case spotifyOnly(String)
    case unreadable(String)
    case nothing(String)
    /// 4.5: a video or audio post without words (music, a clip) and without text.
    case noSpeech(String)

    var errorDescription: String? {
        switch self {
        case .spotifyMusic: "Spotify music has no words to take: Zeus eats Spotify podcasts (episodes and shows)."
        case .spotifyOnly(let show): "“\(show)” has no public feed: it is only on Spotify, which never gives the audio."
        case .unreadable(let site): "\(site) did not show this post. If it is private or needs an account, sign in to \(site) in Settings › Accounts."
        case .nothing(let site): "This \(site) post has no text and no sound to take."
        case .noSpeech(let site): "This \(site) post is a video or a sound without speech (music, a clip) and has no text: nothing to write down."
        }
    }
}
