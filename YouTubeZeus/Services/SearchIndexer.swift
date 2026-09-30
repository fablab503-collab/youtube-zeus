import Foundation
import Observation
import SwiftData

/// Keeps the full-text search index (SearchIndex, SQLite FTS5) in step with the library: new, polished,
/// summarized or re-read items are indexed a moment after they change; at launch everything is checked once.
@Observable
final class SearchIndexer {
    private(set) var isIndexing = false
    private(set) var itemCount = 0
    private(set) var lastError: String?
    @ObservationIgnored private var pending: Set<String> = []
    @ObservationIgnored private var flushTask: Task<Void, Never>?
    @ObservationIgnored let index: SearchIndex?
    let context: ModelContext

    init(context: ModelContext) {
        self.context = context
        do {
            index = try SearchIndex()
        } catch {
            index = nil
            lastError = error.localizedDescription
            AppLog.write("SEARCH index unavailable: \(error.localizedDescription)")
        }
    }

    var isAvailable: Bool { index != nil }

    /// Indexes an item a moment after it changed (bulk eating stays cheap).
    func schedule(_ videoID: String) {
        guard index != nil else { return }
        pending.insert(videoID)
        flushTask?.cancel()
        flushTask = Task {
            try? await Task.sleep(for: .seconds(1.5))
            guard !Task.isCancelled else { return }
            await flush()
        }
    }

    private func flush() async {
        guard let index else { return }
        let ids = pending
        pending.removeAll()
        var documents: [SearchDocument] = []
        for id in ids {
            var descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.videoID == id })
            descriptor.fetchLimit = 1
            if let video = try? context.fetch(descriptor).first, video.status.hasText {
                documents.append(SearchDocument(video.snapshot))
            } else {
                index.remove(id)
            }
        }
        await write(documents, to: index)
    }

    private func write(_ documents: [SearchDocument], to index: SearchIndex) async {
        guard !documents.isEmpty else { return }
        isIndexing = true
        defer { isIndexing = false }
        let result: (changed: Int, error: String?) = await Task.detached(priority: .utility) {
            var changed = 0
            for document in documents {
                do {
                    if try index.update(document) { changed += 1 }
                } catch {
                    return (changed, error.localizedDescription)
                }
            }
            return (changed, nil)
        }.value
        lastError = result.error
        itemCount = index.counts.items
        if result.changed > 0 { AppLog.write("SEARCH indexed \(result.changed) item(s)") }
        if let error = result.error { AppLog.write("SEARCH error: \(error)") }
    }

    /// At launch: index what is missing or changed, forget what was deleted. Snapshots are built in small groups so
    /// the window stays responsive.
    func catchUp() async {
        guard let index else { return }
        let videos = ((try? context.fetch(FetchDescriptor<Video>())) ?? []).filter { $0.status.hasText }
        index.keepOnly(Set(videos.map(\.videoID)))
        let known = index.fingerprints()
        var batch: [SearchDocument] = []
        for video in videos {
            let document = SearchDocument(video.snapshot)
            if known[video.videoID] != document.fingerprint { batch.append(document) }
            if batch.count >= 12 {
                await write(batch, to: index)
                batch.removeAll()
                await Task.yield()
            }
        }
        await write(batch, to: index)
        itemCount = index.counts.items
    }

    func remove(_ videoID: String) {
        index?.remove(videoID)
    }

    /// Searches off the main thread.
    func search(_ text: String, limit: Int = 40, perItem: Int = 3) async -> [SearchHit] {
        guard let index else { return [] }
        return await Task.detached(priority: .userInitiated) {
            (try? index.search(text, limit: limit, perItem: perItem)) ?? []
        }.value
    }
}
