import Foundation
import Observation
import WebKit

/// Signing in to YouTube inside Zeus. The sign-in lives in the app's web view; its cookies are
/// written to a private cookies.txt so yt-dlp can read members-only, age-restricted and account
/// lists (subscriptions, Watch Later, Liked videos).
@Observable
final class YouTubeAccount {
    private(set) var isSignedIn = false
    private(set) var lastRefresh: Date?
    private var refreshing = false

    /// The cookies file of the YouTube page in front (3.9: one per page).
    nonisolated static var cookieFile: URL { YouTubePages.cookieFile(for: YouTubePages.activeIDNow) }
    /// Told whether the page in front is signed in, after each check.
    @ObservationIgnored var onChecked: ((Bool) -> Void)?
    /// X, Instagram, Facebook and Threads sign-ins made in Zeus's web window (3.8), for yt-dlp. Keep private.
    nonisolated static var socialCookieFile: URL { AppFolders.support.appendingPathComponent("social-cookies.txt") }
    /// The sites signed in to inside Zeus.
    private(set) var socialSignedIn: Set<SocialSite> = []

    static let signInURL = URL(string: "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F")!

    /// Reads the web view's cookies and rewrites cookies.txt.
    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        // Posts' sign-ins live in the app's default web data; YouTube's in the page in front.
        writeSocialCookies(await WKWebsiteDataStore.default().httpCookieStore.allCookies())
        let cookies = await YouTubePages.activeStore.httpCookieStore.allCookies()
        let relevant = cookies.filter { cookie in
            let domain = cookie.domain.lowercased()
            return domain.hasSuffix("youtube.com") || domain.hasSuffix("google.com")
        }
        isSignedIn = relevant.contains { cookie in
            cookie.domain.lowercased().hasSuffix("youtube.com")
                && ["SAPISID", "__Secure-3PSID", "LOGIN_INFO", "SID"].contains(cookie.name)
        }
        lastRefresh = .now
        onChecked?(isSignedIn)
        let file = Self.cookieFile
        guard isSignedIn else {
            try? FileManager.default.removeItem(at: file)
            return
        }
        var lines = ["# Netscape HTTP Cookie File", "# Written by YouTube Zeus from its YouTube sign-in. Keep private.", ""]
        for cookie in relevant {
            let domain = cookie.domain
            let includeSubdomains = domain.hasPrefix(".") ? "TRUE" : "FALSE"
            let expiry = Int(cookie.expiresDate?.timeIntervalSince1970 ?? 0)
            let value = cookie.value.replacingOccurrences(of: "\n", with: "")
            lines.append([domain, includeSubdomains, cookie.path, cookie.isSecure ? "TRUE" : "FALSE",
                          String(expiry), cookie.name, value].joined(separator: "\t"))
        }
        let text = lines.joined(separator: "\n") + "\n"
        try? text.write(to: file, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }

    /// Session cookies that mean "signed in" on each site.
    private static let sessionCookies: [SocialSite: Set<String>] = [
        .x: ["auth_token"], .instagram: ["sessionid"], .facebook: ["c_user", "xs"], .threads: ["sessionid"],
        .tiktok: ["sessionid"], .linkedin: ["li_at"], .reddit: ["reddit_session", "token_v2"], .vimeo: ["vimeo"],
    ]

    private func writeSocialCookies(_ cookies: [HTTPCookie]) {
        var signedIn: Set<SocialSite> = []
        var kept: [HTTPCookie] = []
        for site in SocialSite.allCases where !site.cookieDomains.isEmpty {
            let mine = cookies.filter { cookie in
                let domain = cookie.domain.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "."))
                return site.cookieDomains.contains { domain == $0 || domain.hasSuffix("." + $0) }
            }
            if mine.contains(where: { Self.sessionCookies[site]?.contains($0.name) == true }) {
                signedIn.insert(site)
                kept += mine
            }
        }
        socialSignedIn = signedIn
        let file = Self.socialCookieFile
        guard !kept.isEmpty else {
            try? FileManager.default.removeItem(at: file)
            return
        }
        var seen = Set<String>()
        var lines = ["# Netscape HTTP Cookie File", "# Written by YouTube Zeus from its X / Instagram / Facebook / Threads sign-ins. Keep private.", ""]
        for cookie in kept where seen.insert(cookie.domain + "|" + cookie.path + "|" + cookie.name).inserted {
            let domain = cookie.domain
            let expiry = Int(cookie.expiresDate?.timeIntervalSince1970 ?? 0)
            lines.append([domain, domain.hasPrefix(".") ? "TRUE" : "FALSE", cookie.path, cookie.isSecure ? "TRUE" : "FALSE",
                          String(expiry), cookie.name, cookie.value.replacingOccurrences(of: "\n", with: "")].joined(separator: "\t"))
        }
        try? (lines.joined(separator: "\n") + "\n").write(to: file, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
    }

    /// Signs out of one site inside Zeus.
    func signOut(of site: SocialSite) async {
        let store = WKWebsiteDataStore.default()
        let records = await store.dataRecords(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes())
        // Threads signs in through Instagram: signing out of Threads leaves Instagram alone.
        let domains = site == .threads ? ["threads.net", "threads.com"] : site.cookieDomains
        let targets = records.filter { domains.contains($0.displayName.lowercased()) }
        await store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), for: targets)
        await refresh()
    }

    /// Removes the YouTube and Google sign-in from Zeus (not from Safari or other browsers).
    func signOut() async {
        let store = YouTubePages.activeStore
        let records = await store.dataRecords(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes())
        let targets = records.filter { record in
            let name = record.displayName.lowercased()
            return name.contains("youtube") || name.contains("google")
        }
        await store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), for: targets)
        try? FileManager.default.removeItem(at: Self.cookieFile)
        isSignedIn = false
    }
}
