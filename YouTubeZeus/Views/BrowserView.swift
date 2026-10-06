import SwiftData
import SwiftUI
import WebKit

/// The YouTube web view: browse, sign in, and eat what you are watching.
@Observable
final class BrowserModel {
    let webView: WKWebView
    var url: URL?
    var title = ""
    var canGoBack = false
    var canGoForward = false
    var isLoading = false
    var bouclierOn = BouclierShield.isOn
    private var observations: [NSKeyValueObservation] = []

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        // Bouclier (Daniel's ad blocker): YouTube ads, ad banners and ad servers blocked in this browser.
        if BouclierShield.isOn { BouclierShield.install(in: configuration.userContentController) }
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        let web = WKWebView(frame: .zero, configuration: configuration)
        // A Safari user agent, so Google's sign-in page accepts the web view.
        web.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        web.allowsBackForwardNavigationGestures = true
        web.isInspectable = false
        webView = web
        observations = [
            web.observe(\.url, options: [.new]) { [weak self] web, _ in
                MainActor.assumeIsolated { self?.url = web.url }
            },
            web.observe(\.title, options: [.new]) { [weak self] web, _ in
                MainActor.assumeIsolated { self?.title = web.title ?? "" }
            },
            web.observe(\.canGoBack, options: [.new]) { [weak self] web, _ in
                MainActor.assumeIsolated { self?.canGoBack = web.canGoBack }
            },
            web.observe(\.canGoForward, options: [.new]) { [weak self] web, _ in
                MainActor.assumeIsolated { self?.canGoForward = web.canGoForward }
            },
            web.observe(\.isLoading, options: [.new]) { [weak self] web, _ in
                MainActor.assumeIsolated { self?.isLoading = web.isLoading }
            },
        ]
        web.load(URLRequest(url: URL(string: "https://www.youtube.com/")!))
    }

    func open(_ url: URL) { webView.load(URLRequest(url: url)) }

    /// Turns Bouclier on or off in this browser and reloads the page.
    func setBouclier(_ on: Bool) {
        BouclierShield.isOn = on
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        controller.removeAllContentRuleLists()
        if on { BouclierShield.install(in: controller) }
        bouclierOn = on
        webView.reload()
    }
}

private struct WebViewHost: NSViewRepresentable {
    let webView: WKWebView
    func makeNSView(context: Context) -> WKWebView { webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

struct BrowserScreen: View {
    @Environment(AppModel.self) private var app
    @State private var address = ""

    var body: some View {
        let browser = app.browser
        let link = browser.url.flatMap { YouTubeLink.parse($0.absoluteString) }
        WebViewHost(webView: browser.webView)
            .ignoresSafeArea(edges: .bottom)
            .overlay(alignment: .bottomTrailing) {
                if link != nil {
                    Button {
                        if let url = browser.url { Task { await app.eat(url.absoluteString) } }
                    } label: {
                        Label(eatLabel(link), systemImage: "fork.knife")
                            .font(.headline)
                            .padding(.horizontal, 6)
                    }
                    .buttonStyle(.glassProminent)
                    .controlSize(.large)
                    .padding(22)
                    .transition(.scale.combined(with: .opacity))
                }
            }
            .animation(.spring(duration: 0.3), value: link)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Button { browser.webView.goBack() } label: { Image(systemName: "chevron.left") }
                            .disabled(!browser.canGoBack)
                            .help("Back")
                        Button { browser.webView.goForward() } label: { Image(systemName: "chevron.right") }
                            .disabled(!browser.canGoForward)
                            .help("Forward")
                        Button { if browser.isLoading { browser.webView.stopLoading() } else { _ = browser.webView.reload() } } label: {
                            Image(systemName: browser.isLoading ? "xmark" : "arrow.clockwise")
                        }
                        .help(browser.isLoading ? "Stop" : "Reload")
                        TextField("Search YouTube or type a link", text: $address)
                            .textFieldStyle(.roundedBorder)
                            .frame(minWidth: 240, idealWidth: 420)
                            .onSubmit(go)
                    }
                    .buttonStyle(.borderless)
                }
                ToolbarItem {
                    Button { browser.open(URL(string: "https://www.youtube.com/")!) } label: { Image(systemName: "house") }
                        .help("YouTube home")
                }
                ToolbarItem { BouclierToggle(browser: browser) }
            }
            .onChange(of: browser.url) { _, url in
                address = url?.absoluteString ?? ""
                Task { await app.account.refresh() }
            }
            .onAppear { address = browser.url?.absoluteString ?? "" }
            .navigationTitle(browser.title.isEmpty ? "YouTube" : browser.title)
    }

    private func eatLabel(_ link: YouTubeLink?) -> String {
        switch link {
        case .video: "Eat this video"
        case .playlist: "Eat this playlist"
        case .channel: "Follow & eat this channel"
        case nil: "Eat"
        }
    }

    private func go() {
        var text = address.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        if !text.contains(".") {
            text = "https://www.youtube.com/results?search_query=" + (text.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? text)
        } else if !text.lowercased().hasPrefix("http") {
            text = "https://" + text
        }
        if let url = URL(string: text) { app.browser.open(url) }
    }
}

/// The content column next to the browser: account status and account lists.
struct AccountPanel: View {
    @Environment(AppModel.self) private var app
    @Environment(AppSettings.self) private var settings
    @State private var working = ""

    var body: some View {
        @Bindable var settings = settings
        List {
            Section {
                HStack(spacing: 12) {
                    Image(systemName: app.account.isSignedIn ? "person.crop.circle.fill.badge.checkmark" : "person.crop.circle.badge.questionmark")
                        .font(.largeTitle)
                        .foregroundStyle(app.account.isSignedIn ? .green : .secondary)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(app.account.isSignedIn ? "Signed in to YouTube" : "Not signed in").font(.headline)
                        Text(app.account.isSignedIn
                             ? "Zeus uses this sign-in for members-only and age-restricted videos and for your lists."
                             : "Sign in on the right, in Zeus's own YouTube window. Your password goes to Google only.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 6)
                if app.account.isSignedIn {
                    Button("Sign out of YouTube in Zeus", role: .destructive) {
                        Task { await app.account.signOut() }
                    }
                } else {
                    Button {
                        app.browser.open(YouTubeAccount.signInURL)
                    } label: {
                        Label("Sign in to YouTube", systemImage: "person.badge.key.fill")
                    }
                    .buttonStyle(.glassProminent)
                }
            }

            Section("Your YouTube") {
                accountButton("Import my subscriptions", symbol: "person.2.fill",
                              detail: "Follow every channel you are subscribed to") {
                    await app.importAccountSubscriptions()
                }
                accountButton("Eat my Watch Later", symbol: VideoListKind.watchLater.symbol,
                              detail: "Every video saved for later, as one collection") {
                    await app.eatList(url: "https://www.youtube.com/playlist?list=WL", kind: .watchLater, title: "Watch Later")
                }
                accountButton("Eat my Liked videos", symbol: VideoListKind.liked.symbol,
                              detail: "Every video you liked, as one collection") {
                    await app.eatList(url: "https://www.youtube.com/playlist?list=LL", kind: .liked, title: "Liked videos")
                }
                if !working.isEmpty {
                    HStack { ProgressView().controlSize(.small); Text(working).foregroundStyle(.secondary) }
                }
            }
            .disabled(!app.account.isSignedIn && settings.cookieSource == "zeus")

            Section("Sign-in used by yt-dlp") {
                Picker("Cookies from", selection: $settings.cookieSource) {
                    Text("Zeus's YouTube window").tag("zeus")
                    Text("Safari").tag("safari")
                    Text("Chrome").tag("chrome")
                    Text("Firefox").tag("firefox")
                    Text("Brave").tag("brave")
                    Text("Edge").tag("edge")
                    Text("None").tag("none")
                }
                Text("Safari needs Full Disk Access for YouTube Zeus; Chrome asks for its Keychain password once.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .navigationTitle("YouTube")
        .task { await app.account.refresh() }
    }

    private func accountButton(_ title: String, symbol: String, detail: String, action: @escaping () async -> Void) -> some View {
        Button {
            working = title + "…"
            Task {
                await action()
                working = ""
            }
        } label: {
            HStack(spacing: 12) {
                Image(systemName: symbol).font(.title3).foregroundStyle(.tint).frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.body.weight(.medium))
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .disabled(!working.isEmpty)
    }
}

// MARK: - The small YouTube browser of the Eat bar (3.3)

/// The video open in the YouTube browser: its clean link, title and thumbnail.
struct BrowserVideo: Equatable {
    let id: String
    let title: String

    var url: String { "https://www.youtube.com/watch?v=\(id)" }
    var thumbnail: URL? { URL(string: "https://i.ytimg.com/vi/\(id)/mqdefault.jpg") }

    init?(_ browser: BrowserModel) {
        guard let address = browser.url?.absoluteString, case .video(let id) = YouTubeLink.parse(address) else { return nil }
        self.id = id
        var title = browser.title
        if title.hasSuffix(" - YouTube") { title = String(title.dropLast(10)) }
        self.title = title.isEmpty ? "YouTube video" : title
    }

    static func copy(_ link: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(link, forType: .string)
    }
}

extension AppModel {
    /// The small YouTube browser; when the YouTube page of the sidebar is showing, that one is used.
    func openMiniBrowser() {
        if selection == .browser { return }
        _ = browser
        showMiniBrowser = true
    }
}

struct MiniBrowserSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var address = ""
    @State private var copied = false

    var body: some View {
        let browser = app.browser
        let video = BrowserVideo(browser)
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Button { browser.webView.goBack() } label: { Image(systemName: "chevron.left") }
                    .disabled(!browser.canGoBack).help("Back")
                Button { browser.webView.goForward() } label: { Image(systemName: "chevron.right") }
                    .disabled(!browser.canGoForward).help("Forward")
                Button { if browser.isLoading { browser.webView.stopLoading() } else { _ = browser.webView.reload() } } label: {
                    Image(systemName: browser.isLoading ? "xmark" : "arrow.clockwise")
                }
                Button { browser.open(URL(string: "https://www.youtube.com/")!) } label: { Image(systemName: "house") }
                    .help("YouTube home")
                TextField("Search YouTube or type a link", text: $address)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(go)
                BouclierToggle(browser: browser)
                Button("Done") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
            .buttonStyle(.borderless)
            .padding(10)
            WebViewHost(webView: browser.webView)
            HStack(spacing: 10) {
                if let video {
                    AsyncImage(url: video.thumbnail) { image in
                        image.resizable().aspectRatio(16 / 9, contentMode: .fill)
                    } placeholder: { Color.secondary.opacity(0.15) }
                        .frame(width: 80, height: 45)
                        .clipShape(.rect(cornerRadius: 8))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(video.title).font(.callout.weight(.semibold)).lineLimit(1)
                        Text(video.url).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    Spacer(minLength: 8)
                    Button {
                        BrowserVideo.copy(video.url)
                        copied = true
                    } label: {
                        Label(copied ? "Copied" : "Copy URL", systemImage: copied ? "checkmark" : "link")
                    }
                    .buttonStyle(.glass)
                    Button {
                        app.eatBarFill = video.url
                        dismiss()
                    } label: { Label("Put in the Eat bar", systemImage: "arrow.down.to.line") }
                        .buttonStyle(.glass)
                    Button {
                        let link = video.url
                        dismiss()
                        Task { await app.eat(link) }
                    } label: { Label("Eat now", systemImage: "fork.knife") }
                        .buttonStyle(.glassProminent)
                } else {
                    Image(systemName: "hand.point.up.left").foregroundStyle(.secondary)
                    Text("Sign in if you like, then open a video: its link appears here to copy or eat.")
                        .font(.callout).foregroundStyle(.secondary)
                    Spacer()
                }
            }
            .padding(12)
            .animation(.default, value: video?.id)
        }
        .frame(minWidth: 860, idealWidth: 1000, minHeight: 600, idealHeight: 720)
        .onAppear { address = browser.url?.absoluteString ?? "" }
        .onChange(of: browser.url) { _, url in
            address = url?.absoluteString ?? ""
            copied = false
            Task { await app.account.refresh() }
        }
    }

    private func go() {
        var text = address.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        if !text.contains(".") {
            text = "https://www.youtube.com/results?search_query=" + (text.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? text)
        } else if !text.lowercased().hasPrefix("http") {
            text = "https://" + text
        }
        if let url = URL(string: text) { app.browser.open(url) }
    }
}


// MARK: - Bouclier in the YouTube browser (3.3)

/// Bouclier's YouTube protection (github.com/fablab503-collab/fablab-tools, bouclier/, MIT), built into the browser:
/// its YouTube script (strips ads from the player data, skips server-inserted ads, disarms the anti-adblock penalty),
/// its element hiding for YouTube, and a content rule list that blocks ad and tracking servers.
enum BouclierShield {
    static let version = "1.3"

    /// On by default; the switch in the YouTube browser turns it off.
    static var isOn: Bool {
        get { UserDefaults.standard.object(forKey: "bouclierInBrowser") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "bouclierInBrowser") }
    }
    private static let ruleListID = "bouclier-youtube-1"

    /// Ad and tracking servers YouTube pages call (WebKit content rules).
    private static let blockedHosts = [
        "doubleclick.net", "googlesyndication.com", "googleadservices.com", "googletagservices.com",
        "googletagmanager.com", "google-analytics.com", "adservice.google.com", "pagead2.googlesyndication.com",
        "imasdk.googleapis.com", "ads.youtube.com", "adsense.com", "2mdn.net", "scorecardresearch.com",
    ]
    /// YouTube's own ad and ad-tracking addresses.
    private static let blockedPaths = [
        #"^https?://[^/]*youtube\.com/pagead/"#, #"^https?://[^/]*youtube\.com/api/stats/ads"#,
        #"^https?://[^/]*youtube\.com/ptracking"#, #"^https?://[^/]*youtube\.com/get_midroll_"#,
    ]

    static func install(in controller: WKUserContentController) {
        if let url = Bundle.main.url(forResource: "youtube-main", withExtension: "js"),
           let source = try? String(contentsOf: url, encoding: .utf8) {
            controller.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: false,
                                                  in: .page))
        }
        if let css = hidingCSS() {
            let script = """
            (function(){var s=document.createElement('style');s.id='bouclier-hide';s.textContent=\(jsString(css));\
            (document.head||document.documentElement).appendChild(s);})();
            """
            controller.addUserScript(WKUserScript(source: script, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
        let rules = rulesJSON()
        Task { @MainActor in
            do {
                if let list = try await WKContentRuleListStore.default()?.compileContentRuleList(
                    forIdentifier: ruleListID, encodedContentRuleList: rules) {
                    controller.add(list)
                }
            } catch {
                AppLog.write("BOUCLIER rules not compiled: \(error.localizedDescription)")
            }
        }
    }

    /// Bouclier's YouTube element hiding as CSS ("#player-ads" is kept: YouTube uses it to detect blockers).
    static func hidingCSS() -> String? {
        guard let url = Bundle.main.url(forResource: "builtin-youtube", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let specific = json["specific"] as? [String: [String]] else { return nil }
        let exceptions = Set(((json["exceptions"] as? [String: [String]]) ?? [:]).values.flatMap { $0 })
        let selectors = specific.values.flatMap { $0 }.filter { !exceptions.contains($0) }
        guard !selectors.isEmpty else { return nil }
        return selectors.joined(separator: ",\n") + " { display: none !important; }"
    }

    static func rulesJSON() -> String {
        var rules: [[String: Any]] = []
        for host in blockedHosts {
            // WebKit's rule patterns have no groups: "any host ending with" is written [^/]*.
            let pattern = "^https?://[^/]*" + host.replacingOccurrences(of: ".", with: "\\.") + "[:/]"
            rules.append(["trigger": ["url-filter": pattern], "action": ["type": "block"]])
        }
        for path in blockedPaths {
            rules.append(["trigger": ["url-filter": path], "action": ["type": "block"]])
        }
        let data = (try? JSONSerialization.data(withJSONObject: rules)) ?? Data("[]".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    private static func jsString(_ text: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [text])) ?? Data("[\"\"]".utf8)
        return String(String(decoding: data, as: UTF8.self).dropFirst().dropLast())
    }
}


/// The Bouclier switch shown in the YouTube browsers.
struct BouclierToggle: View {
    let browser: BrowserModel

    var body: some View {
        Button { browser.setBouclier(!browser.bouclierOn) } label: {
            Label(browser.bouclierOn ? "Bouclier on" : "Bouclier off",
                  systemImage: browser.bouclierOn ? "checkmark.shield.fill" : "shield.slash")
                .font(.caption.weight(.semibold))
                .foregroundStyle(browser.bouclierOn ? Color.green : Color.secondary)
        }
        .buttonStyle(.borderless)
        .help(browser.bouclierOn
              ? "Bouclier \(BouclierShield.version) blocks YouTube ads, ad banners and ad servers here. Click to turn it off."
              : "Bouclier is off in this browser. Click to block YouTube ads again.")
    }
}
