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

    nonisolated static var cookieFile: URL { AppFolders.support.appendingPathComponent("youtube-cookies.txt") }

    static let signInURL = URL(string: "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F")!

    /// Reads the web view's cookies and rewrites cookies.txt.
    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        let cookies = await WKWebsiteDataStore.default().httpCookieStore.allCookies()
        let relevant = cookies.filter { cookie in
            let domain = cookie.domain.lowercased()
            return domain.hasSuffix("youtube.com") || domain.hasSuffix("google.com")
        }
        isSignedIn = relevant.contains { cookie in
            cookie.domain.lowercased().hasSuffix("youtube.com")
                && ["SAPISID", "__Secure-3PSID", "LOGIN_INFO", "SID"].contains(cookie.name)
        }
        lastRefresh = .now
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

    /// Removes the YouTube and Google sign-in from Zeus (not from Safari or other browsers).
    func signOut() async {
        let store = WKWebsiteDataStore.default()
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
