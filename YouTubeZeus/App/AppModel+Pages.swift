import Foundation
import SwiftUI

/// YouTube pages (3.9): switch, add, rename and remove YouTube accounts in Zeus's YouTube window.
extension AppModel {
    /// Puts a page in front: the YouTube window shows its account and yt-dlp uses its sign-in.
    func switchYouTubePage(_ id: String) {
        guard id != youtubePages.activeID else { return }
        youtubePages.select(id)
        resetBrowser()
        Task { await account.refresh() }
        AppLog.write("YOUTUBE PAGE \(youtubePages.active.name)")
        show("YouTube page: \(youtubePages.active.name)" + (youtubePages.active.signedIn ? "" : " (sign in on the right)"))
    }

    @discardableResult
    func addYouTubePage(named name: String? = nil) -> YouTubePage? {
        guard let page = youtubePages.add(named: name) else {
            show("You have \(YouTubePages.maxPages) YouTube pages, the most Zeus keeps.", error: true)
            return nil
        }
        switchYouTubePage(page.id)
        return page
    }

    func removeYouTubePage(_ id: String) {
        let wasActive = youtubePages.activeID == id
        let name = youtubePages.pages.first { $0.id == id }?.name ?? "the page"
        Task {
            await youtubePages.remove(id)
            if wasActive { resetBrowser() }
            await account.refresh()
            show("Removed \(name) and its YouTube sign-in from Zeus.")
        }
    }
}

/// The page switcher of the YouTube window's toolbar.
struct YouTubePageMenu: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let pages = app.youtubePages
        Menu {
            ForEach(Array(pages.pages.enumerated()), id: \.element.id) { index, page in
                Button {
                    app.switchYouTubePage(page.id)
                } label: {
                    if page.id == pages.activeID {
                        Label("\(index + 1). \(page.name)", systemImage: "checkmark")
                    } else {
                        Text("\(index + 1). \(page.name)" + (page.signedIn ? "" : "  (not signed in)"))
                    }
                }
            }
            Divider()
            Button("New Page") { app.addYouTubePage() }.disabled(!pages.canAdd)
            Button("Rename, Remove…") { app.selection = .browser }
        } label: {
            Label(pages.active.name, systemImage: "person.crop.rectangle.stack")
        }
        .help("YouTube pages: one YouTube account per page")
    }
}

/// The pages in the account panel: switch, rename, remove, add.
struct YouTubePagesSection: View {
    @Environment(AppModel.self) private var app
    @State private var renaming: YouTubePage?
    @State private var newName = ""
    @State private var removing: YouTubePage?

    var body: some View {
        let pages = app.youtubePages
        Section {
            ForEach(Array(pages.pages.enumerated()), id: \.element.id) { index, page in
                HStack(spacing: 10) {
                    Image(systemName: page.id == pages.activeID ? "largecircle.fill.circle" : "circle")
                        .foregroundStyle(page.id == pages.activeID ? Color.accentColor : Color.secondary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(index + 1). \(page.name)").fontWeight(page.id == pages.activeID ? .semibold : .regular)
                        Text(page.signedIn ? "Signed in" : "Not signed in").font(.caption).foregroundStyle(page.signedIn ? Color.green : Color.secondary)
                    }
                    Spacer()
                    Button { newName = page.name; renaming = page } label: { Image(systemName: "pencil") }
                        .buttonStyle(.borderless).help("Rename")
                }
                .contentShape(.rect)
                .onTapGesture { app.switchYouTubePage(page.id) }
                .contextMenu {
                    Button("Use This Page") { app.switchYouTubePage(page.id) }
                    Button("Rename…") { newName = page.name; renaming = page }
                    if !page.isMain {
                        Divider()
                        Button("Remove Page…", role: .destructive) { removing = page }
                    }
                }
            }
            Button {
                app.addYouTubePage()
            } label: {
                Label("New page", systemImage: "plus.circle.fill")
            }
            .disabled(!pages.canAdd)
        } header: {
            Text("YouTube pages")
        } footer: {
            Text("One YouTube account per page (up to \(YouTubePages.maxPages)): Personal, Work, a second channel… The page in front is the one Zeus uses for your lists and for members-only videos. The library stays one.")
        }
        .alert("Rename the page", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Name", text: $newName)
            Button("Rename") { if let page = renaming { app.youtubePages.rename(page.id, to: newName) }; renaming = nil }
            Button("Cancel", role: .cancel) { renaming = nil }
        } message: {
            Text("For example: Personal YouTube, Work YouTube, Third channel.")
        }
        .confirmationDialog("Remove “\(removing?.name ?? "")”?", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
            Button("Remove the page and its sign-in", role: .destructive) {
                if let page = removing { app.removeYouTubePage(page.id) }
                removing = nil
            }
        } message: {
            Text("Zeus forgets this page's YouTube sign-in. Your Google account itself is not touched, and the library stays as it is.")
        }
    }
}
