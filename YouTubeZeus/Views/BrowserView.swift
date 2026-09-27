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
    private var observations: [NSKeyValueObservation] = []

    init() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
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
                ToolbarItemGroup(placement: .navigation) {
                    Button { browser.webView.goBack() } label: { Image(systemName: "chevron.left") }
                        .disabled(!browser.canGoBack)
                    Button { browser.webView.goForward() } label: { Image(systemName: "chevron.right") }
                        .disabled(!browser.canGoForward)
                    Button { browser.webView.reload() } label: {
                        Image(systemName: browser.isLoading ? "xmark" : "arrow.clockwise")
                    }
                }
                ToolbarItem(placement: .principal) {
                    TextField("youtube.com", text: $address)
                        .textFieldStyle(.roundedBorder)
                        .frame(minWidth: 260, idealWidth: 420)
                        .onSubmit(go)
                }
                ToolbarItem {
                    Button { browser.open(URL(string: "https://www.youtube.com/")!) } label: { Image(systemName: "house") }
                        .help("YouTube home")
                }
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
