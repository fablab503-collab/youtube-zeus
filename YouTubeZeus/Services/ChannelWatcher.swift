import Foundation
import Observation
import SwiftData

nonisolated enum WatchError: LocalizedError, Sendable {
    case notAChannel
    case missingTool

    var errorDescription: String? {
        switch self {
        case .notAChannel: "Zeus could not find a channel behind that link."
        case .missingTool: "yt-dlp was not found. Install it with Homebrew or set its path in Settings › Tools."
        }
    }
}

/// Follows channels through their public feeds and eats new uploads.
@Observable
final class ChannelWatcher {
    private(set) var isChecking = false
    private(set) var lastRun: Date?
    private var loop: Task<Void, Never>?

    let context: ModelContext
    let settings: AppSettings
    let engine: EatEngine
    let exporter: SecondBrainExporter

    init(context: ModelContext, settings: AppSettings, engine: EatEngine, exporter: SecondBrainExporter) {
        self.context = context
        self.settings = settings
        self.engine = engine
        self.exporter = exporter
    }

    func start() {
        guard loop == nil else { return }
        loop = Task {
            try? await Task.sleep(for: .seconds(5))
            while !Task.isCancelled {
                await checkAll()
                try? await Task.sleep(for: .seconds(max(5, settings.pollMinutes) * 60))
            }
        }
    }

    func checkAll() async {
        guard !isChecking else { return }
        isChecking = true
        defer {
            isChecking = false
            lastRun = .now
        }
        let channels = (try? context.fetch(FetchDescriptor<Channel>(sortBy: [SortDescriptor(\.title)]))) ?? []
        for channel in channels {
            _ = await check(channel)
        }
        engine.retryWaiting()
        engine.resumePending()
        exporter.retryPending(in: context)
    }

    /// Looks at the channel's feed; new uploads since following are added (and eaten when auto-eat is on).
    @discardableResult
    func check(_ channel: Channel) async -> Int {
        do {
            let feed = try await ChannelFeedReader.fetch(channelID: channel.channelID)
            if channel.title.isEmpty || channel.title == channel.channelID, !feed.title.isEmpty { channel.title = feed.title }
            var known = Set(channel.knownVideoIDs)
            var fresh: [FeedEntry] = []
            for entry in feed.entries where !known.contains(entry.videoID) {
                known.insert(entry.videoID)
                let isNew = (entry.published ?? .distantPast) > channel.addedAt.addingTimeInterval(-3600)
                guard isNew, engine.video(entry.videoID) == nil else { continue }
                if entry.isShort, settings.skipShorts { continue }
                fresh.append(entry)
            }
            channel.knownVideoIDs = Array(known).suffix(500).map { $0 }
            channel.lastCheckedAt = .now
            channel.lastError = nil
            for entry in fresh {
                if channel.autoEat {
                    let videos = engine.enqueue([(id: entry.videoID, title: entry.title, channel: channel.title, channelID: channel.channelID)], fromWatch: true)
                    videos.forEach { $0.publishedAt = entry.published; $0.isShort = entry.isShort }
                } else {
                    let video = Video(videoID: entry.videoID, title: entry.title, channelTitle: channel.title,
                                      channelID: channel.channelID, publishedAt: entry.published,
                                      status: .discovered, fromChannelWatch: true)
                    video.isShort = entry.isShort
                    context.insert(video)
                }
            }
            try? context.save()
            return fresh.count
        } catch {
            channel.lastError = error.localizedDescription
            channel.lastCheckedAt = .now
            try? context.save()
            return 0
        }
    }

    // MARK: Following

    func channel(_ id: String) -> Channel? {
        var descriptor = FetchDescriptor<Channel>(predicate: #Predicate { $0.channelID == id })
        descriptor.fetchLimit = 1
        return try? context.fetch(descriptor).first
    }

    /// Follows a channel link (@handle, /channel/UC…, /c/…, /user/…). Existing uploads are remembered, not eaten.
    func follow(url: URL) async throws -> (channel: Channel, latest: [PlaylistEntry]) {
        guard let ytdlp = settings.makeYTDLP() else { throw WatchError.missingTool }
        let listing = try await ytdlp.flatList(url: url.absoluteString.hasSuffix("/videos")
                                                                      ? url.absoluteString : url.absoluteString + "/videos",
                                                                      limit: 25)
        guard listing.channelID.hasPrefix("UC") else { throw WatchError.notAChannel }
        let followed = self.channel(listing.channelID) ?? {
            let created = Channel(channelID: listing.channelID, title: listing.title,
                                  handle: listing.handle, avatarURLString: listing.avatar)
            context.insert(created)
            return created
        }()
        followed.title = listing.title.replacingOccurrences(of: " - Videos", with: "")
        followed.handle = listing.handle
        if let avatar = listing.avatar { followed.avatarURLString = avatar }
        var known = Set(followed.knownVideoIDs)
        listing.entries.forEach { known.insert($0.id) }
        if let feed = try? await ChannelFeedReader.fetch(channelID: listing.channelID) {
            feed.entries.forEach { known.insert($0.videoID) }
        }
        followed.knownVideoIDs = Array(known)
        followed.lastCheckedAt = .now
        try? context.save()
        return (followed, listing.entries)
    }

    /// Google Takeout › YouTube and YouTube Music › subscriptions › subscriptions.csv
    func importSubscriptions(from file: URL) async throws -> Int {
        let text = try String(contentsOf: file, encoding: .utf8)
        var count = 0
        for row in SubscriptionsCSV.parse(text) where channel(row.channelID) == nil {
            let channel = Channel(channelID: row.channelID, title: row.title)
            context.insert(channel)
            if let feed = try? await ChannelFeedReader.fetch(channelID: row.channelID) {
                channel.knownVideoIDs = feed.entries.map(\.videoID)
                channel.lastCheckedAt = .now
            }
            count += 1
        }
        try? context.save()
        return count
    }

    /// Follows many channels at once (subscriptions). Existing uploads are remembered, not eaten.
    func follow(entries: [ChannelEntry]) async -> Int {
        var count = 0
        for entry in entries where channel(entry.channelID) == nil {
            let channel = Channel(channelID: entry.channelID, title: entry.title)
            if entry.url.contains("/@"), let handle = entry.url.split(separator: "/").last(where: { $0.hasPrefix("@") }) {
                channel.handle = String(handle)
            }
            context.insert(channel)
            if let feed = try? await ChannelFeedReader.fetch(channelID: entry.channelID) {
                channel.knownVideoIDs = feed.entries.map(\.videoID)
                channel.lastCheckedAt = .now
            }
            count += 1
        }
        try? context.save()
        return count
    }

    func unfollow(_ channel: Channel) {
        context.delete(channel)
        try? context.save()
    }
}
