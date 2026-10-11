import AppKit
import Foundation
import SwiftData
import SwiftUI
import WebKit

/// X, Instagram, Facebook, Threads and Spotify links (3.8).
extension AppModel {
    func eatSocial(_ link: String, site: SocialSite) async {
        if site == .spotify {
            await eatSpotify(link)
            return
        }
        guard let id = engine.addPost(link) else {
            show("That \(site.name) link could not be read.", error: true)
            return
        }
        if selection == .skills || selection == .trash { selection = .library }
        selectedVideoID = id
        let signedIn = account.socialSignedIn.contains(site)
        show("Eating the \(site.name) post." + (signedIn || site == .x ? "" : " If it needs an account, sign in to \(site.name) in Settings › Accounts."))
    }

    /// A Spotify episode or show: found by name in Apple's free podcast directory, eaten from its public feed.
    func eatSpotify(_ link: String) async {
        guard let url = SocialLinks.url(link) else { return }
        isResolvingLink = true
        defer { isResolvingLink = false }
        do {
            let item = try await SocialLinks.spotify(url)
            let feed = try await SocialLinks.feed(forShow: item.showName)
            if item.isShow {
                isResolvingLink = false
                await followPodcast(feed.absoluteString)
                return
            }
            let podcast = try await PodcastFeed.fetch(feed)
            guard let episode = SocialLinks.episode(item.episodeTitle, in: podcast.episodes) else {
                show("“\(item.episodeTitle)” is not in the public feed of \(item.showName) (maybe Spotify-only, or too old for the feed).", error: true)
                return
            }
            // The show is kept (without eating its new episodes) so the episode has its folder and index.
            let channel = watcher.channel(podcast.id) ?? {
                let created = Channel(channelID: podcast.id, title: podcast.title, handle: podcast.author, avatarURLString: podcast.image)
                created.kindRaw = "podcast"
                created.feedURLString = feed.absoluteString
                created.websiteURLString = podcast.link
                created.autoEat = false
                created.knownVideoIDs = podcast.episodes.map { $0.id(feed: feed) }
                created.lastCheckedAt = .now
                context.insert(created)
                return created
            }()
            try? context.save()
            AppLog.write("SPOTIFY \(item.showName) › \(episode.title) → \(feed.absoluteString)")
            eatEpisodes([episode], channel: channel, feed: feed, language: podcast.language)
            selectedVideoID = episode.id(feed: feed)
        } catch {
            show(error.localizedDescription, error: true)
        }
    }
}

/// Settings › Accounts: sign in to X, Instagram, Facebook or Threads inside Zeus (for private posts and stories).
/// The sign-in stays in Zeus's own web window; only yt-dlp on this Mac reads it.
struct SocialAccountsView: View {
    @Environment(AppModel.self) private var app
    @State private var signingIn: SocialSite?

    var body: some View {
        Form {
            Section {
                ForEach(SocialSite.allCases.filter { $0.signInURL != nil }, id: \.self) { site in
                    HStack {
                        Label(site.name, systemImage: site.symbol)
                        Spacer()
                        if app.account.socialSignedIn.contains(site) {
                            Text("Signed in").foregroundStyle(.green)
                            Button("Sign Out") { Task { await app.account.signOut(of: site) } }
                        } else {
                            Button("Sign In…") { signingIn = site }
                        }
                    }
                }
            } header: {
                Text("Posts")
            } footer: {
                Text("Public posts work without an account. Sign in for private accounts, stories and posts a site only shows to members. The sign-in stays inside YouTube Zeus on this Mac.")
            }
            Section {
                Label("Spotify podcasts are eaten from their public feed: no account needed. Music and Spotify-only shows cannot be eaten.", systemImage: SocialSite.spotify.symbol)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Spotify")
            }
        }
        .formStyle(.grouped)
        .task { await app.account.refresh() }
        .sheet(item: $signingIn) { site in
            SocialSignInSheet(site: site)
        }
    }
}

extension SocialSite: Identifiable {
    nonisolated var id: String { rawValue }
}

struct SocialSignInSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let site: SocialSite

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Sign in to \(site.name)", systemImage: site.symbol).font(.headline)
                Spacer()
                Button("Done") {
                    Task {
                        await app.account.refresh()
                        dismiss()
                        if app.account.socialSignedIn.contains(site) { app.show("Signed in to \(site.name) inside YouTube Zeus.") }
                    }
                }
                .keyboardShortcut(.defaultAction)
            }
            .padding(12)
            Divider()
            if let url = site.signInURL {
                SignInWebView(url: url)
            }
        }
        .frame(minWidth: 520, idealWidth: 560, minHeight: 640, idealHeight: 720)
    }
}

/// A plain web view on the app's own data store (the same sign-ins as Zeus's YouTube window).
struct SignInWebView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        view.load(URLRequest(url: url))
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {}
}
