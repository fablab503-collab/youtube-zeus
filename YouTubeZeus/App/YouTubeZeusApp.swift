import SwiftData
import SwiftUI

struct YouTubeZeusApp: App {
    @State private var model: AppModel
    private let container: ModelContainer

    init() {
        let container = AppModel.makeContainer()
        self.container = container
        _model = State(initialValue: AppModel(context: container.mainContext))
    }

    var body: some Scene {
        Window("YouTube Zeus", id: "main") {
            ContentView()
                .environment(model)
                .environment(model.settings)
                .frame(minWidth: 1000, minHeight: 640)
                .task { model.start() }
        }
        .modelContainer(container)
        .handlesExternalEvents(matching: ["*"])
        .defaultSize(width: 1320, height: 860)
        .commands { ZeusCommands(model: model) }

        Settings {
            SettingsView()
                .environment(model)
                .environment(model.settings)
                .modelContainer(container)
        }

        MenuBarExtra(isInserted: Binding(get: { model.settings.keepInMenuBar },
                                         set: { model.settings.keepInMenuBar = $0 })) {
            MenuBarView()
                .environment(model)
                .modelContainer(container)
                .task { model.start() }
        } label: {
            Label("YouTube Zeus", systemImage: model.engine.activeCount > 0 ? "bolt.circle.fill" : "bolt.fill")
        }
    }
}

struct ZeusCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(after: .newItem) {
            Button("Eat a Link…") { model.focusEatBar += 1 }
                .keyboardShortcut("l", modifiers: [.command])
            Button("Eat Link from Clipboard") { model.eatClipboard() }
                .keyboardShortcut("v", modifiers: [.command, .shift])
            Divider()
            Button("Check Channels Now") { Task { await model.watcher.checkAll() } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
        }
        CommandGroup(replacing: .help) {
            Link("yt-dlp supported sites", destination: URL(string: "https://github.com/yt-dlp/yt-dlp")!)
        }
    }
}
