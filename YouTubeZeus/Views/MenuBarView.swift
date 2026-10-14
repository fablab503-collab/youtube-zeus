import SwiftData
import SwiftUI

struct MenuBarView: View {
    @Environment(AppModel.self) private var app
    @Environment(\.openWindow) private var openWindow
    @Query(sort: \Video.eatenAt, order: .reverse) private var videos: [Video]

    var body: some View {
        let busy = videos.filter { $0.status.isBusy || $0.status == .queued }
        let recent = videos.filter { $0.status == .done }.prefix(5)

        Button("Open YouTube Zeus") { open() }
            .keyboardShortcut("o")
        Button("Eat Link from Clipboard") { app.eatClipboard() }
            .disabled(app.clipboardLink == nil)
        Button(app.watcher.isChecking ? "Checking channels…" : "Check Channels Now") {
            Task { await app.watcher.checkAll() }
        }
        .disabled(app.watcher.isChecking)

        // 4.5: one item at a time — the one being eaten, and how many wait.
        let eating = busy.first { $0.status == .fetching || $0.status == .transcribing } ?? busy.first
        if let eating {
            Divider()
            Button("Eating: \(eating.displayTitle.prefix(48)) — \(app.engine.state(for: eating.videoID)?.step ?? eating.status.label)") {
                app.selection = .now
                app.selectedVideoID = eating.videoID
                open()
            }
            if busy.count > 1 { Text("\(busy.count - 1) more waiting") }
        }
        if !recent.isEmpty {
            Divider()
            Text("Recently eaten")
            ForEach(Array(recent)) { video in
                Button(String(video.displayTitle.prefix(56))) {
                    app.selection = .now
                    app.initialTab = DetailTab.everything.rawValue
                    app.selectedVideoID = video.videoID
                    open()
                }
            }
        }
        Divider()
        if let last = app.watcher.lastRun {
            Text("Channels checked \(last.relative)")
        }
        SettingsLink { Text("Settings…") }
            .keyboardShortcut(",")
        Button("Quit YouTube Zeus") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }

    private func open() {
        openWindow(id: "main")
        NSApp.activate()
    }
}
