import SwiftData
import SwiftUI

/// The Trash (3.4): videos taken out of the library. Restore them, or delete them for good.
struct TrashView: View {
    @Environment(AppModel.self) private var app
    @Query(filter: #Predicate<Video> { $0.statusRaw == "trashed" }, sort: \Video.addedAt, order: .reverse)
    private var trashed: [Video]
    @State private var selection: Set<String> = []
    @State private var confirmEmpty = false

    private var selected: [Video] { trashed.filter { selection.contains($0.videoID) } }

    var body: some View {
        List(selection: $selection) {
            ForEach(trashed) { video in
                VideoRow(video: video)
                    .tag(video.videoID)
                    .contextMenu {
                        Button("Restore") { app.restore([video]) }
                        Button("Delete Forever", role: .destructive) { app.delete(video) }
                    }
            }
        }
        .overlay {
            if trashed.isEmpty {
                EmptyState(symbol: "trash", title: "The Trash is empty",
                           message: "Videos you delete wait here: restore them, or empty the Trash to remove them for good. Their notes in the Second Brain stay.")
            }
        }
        .navigationTitle("Trash")
        .navigationSubtitle("\(trashed.count) video\(trashed.count == 1 ? "" : "s")")
        .toolbar {
            ToolbarItemGroup {
                if !selection.isEmpty {
                    Button { app.restore(selected); selection.removeAll() } label: {
                        Label("Restore \(selection.count)", systemImage: "arrow.uturn.backward")
                    }
                }
                Button(role: .destructive) { confirmEmpty = true } label: { Label("Empty Trash", systemImage: "trash.slash") }
                    .disabled(trashed.isEmpty)
            }
        }
        .confirmationDialog("Delete the \(trashed.count) videos in the Trash for good?", isPresented: $confirmEmpty) {
            Button("Empty Trash", role: .destructive) { app.emptyTrash(); selection.removeAll() }
        } message: {
            Text("Their transcripts, summaries and playbooks leave YouTube Zeus. The notes already in the Second Brain stay.")
        }
        .onChange(of: selection) { _, ids in
            if ids.count == 1, let id = ids.first { app.selectedVideoID = id }
        }
    }
}
