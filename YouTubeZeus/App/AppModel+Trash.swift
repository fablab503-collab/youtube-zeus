import Foundation
import SwiftData

/// The Trash (3.4): Delete moves videos out of the library; Restore brings them back; Empty Trash deletes for good.
/// The notes already written in the Second Brain are never touched.
extension AppModel {
    func trash(_ videos: [Video]) {
        let live = videos.filter { $0.isAlive && $0.status != .trashed }
        guard !live.isEmpty else { return }
        for video in live {
            let id = video.videoID
            engine.forget(id)
            video.trashedFromRaw = video.statusRaw
            video.trashedAt = .now
            video.status = .trashed
            indexer.remove(id)
            if selectedVideoID == id { selectedVideoID = nil }
        }
        try? context.save()
        AppLog.write("TRASH \(live.count) moved to the Trash")
        show(live.count == 1 ? "“\(live[0].displayTitle)” is in the Trash." : "\(live.count) videos are in the Trash.")
    }

    func trash(_ video: Video) { trash([video]) }

    /// Favorites (3.5): stars or un-stars videos (all starred when one of them is not).
    func toggleFavorite(_ videos: [Video]) {
        let live = videos.filter(\.isAlive)
        guard !live.isEmpty else { return }
        let star = live.contains { !$0.isFavorite }
        for video in live { video.isFavorite = star }
        try? context.save()
        show(star ? (live.count == 1 ? "“\(live[0].displayTitle)” is in your Favorites." : "\(live.count) videos added to Favorites.")
                  : (live.count == 1 ? "Removed from Favorites." : "\(live.count) videos removed from Favorites."))
    }

    func toggleFavorite(_ video: Video) { toggleFavorite([video]) }

    /// Everything in the library goes to the Trash (a clean start that can be undone).
    func trashLibrary() {
        let all = ((try? context.fetch(FetchDescriptor<Video>())) ?? []).filter { $0.status != .trashed }
        trash(all)
    }

    func restore(_ videos: [Video]) {
        for video in videos where video.isAlive && video.status == .trashed {
            var status = EatStatus(rawValue: video.trashedFromRaw ?? "") ?? .done
            // What was being eaten when it was trashed starts again from the line (or waits for "Eat now").
            if status.isBusy || status == .queued { status = video.transcriptText.isEmpty ? .failed : .done }
            video.status = status
            if status == .failed { video.statusDetail = "Restored from the Trash: eat it again" }
            video.trashedAt = nil
            video.trashedFromRaw = nil
            indexer.schedule(video.videoID)
        }
        try? context.save()
        show(videos.count == 1 ? "Restored." : "\(videos.count) videos restored.")
    }

    func emptyTrash() {
        let trashedRaw = EatStatus.trashed.rawValue
        let items = (try? context.fetch(FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == trashedRaw }))) ?? []
        for video in items { delete(video) }
        AppLog.write("TRASH emptied (\(items.count))")
        show("The Trash is empty.")
    }
}
