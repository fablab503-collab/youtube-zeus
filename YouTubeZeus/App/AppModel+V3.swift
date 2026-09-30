import AppKit
import Foundation
import SwiftData

/// YouTube Zeus 3.0: your own files, podcasts, text on screen, people/tools/companies, weekly digest, iPhone inbox.
extension AppModel {
    /// Background work of 3.0, started once after launch: names for items that have none yet, the notes of people,
    /// tools and companies, the weekly digest, and the iPhone inbox (checked every 20 seconds).
    func startV3() {
        engine.onEaten = { [weak self] id in self?.itemEaten(id) }
        engine.onNames = { [weak self] _ in self?.scheduleEntityNotes() }
        scheduleEntityNotes(delay: 2)
        // Names found with an older, looser prompt are found again once.
        if UserDefaults.standard.integer(forKey: "entityPromptVersion") < 3 {
            for video in (try? context.fetch(FetchDescriptor<Video>())) ?? [] where video.entitiesAt != nil {
                video.entitiesAt = nil
            }
            try? context.save()
            UserDefaults.standard.set(3, forKey: "entityPromptVersion")
        }
        if settings.entitiesEnabled {
            let doneRaw = EatStatus.done.rawValue
            let waiting = ((try? context.fetch(FetchDescriptor<Video>(predicate: #Predicate { $0.statusRaw == doneRaw },
                                                                     sortBy: [SortDescriptor(\.addedAt, order: .reverse)]))) ?? [])
                .filter { $0.entitiesAt == nil }
            for video in waiting { engine.scheduleEntities(video.videoID) }
            if !waiting.isEmpty { AppLog.write("NAMES \(waiting.count) items wait for their people, tools and companies") }
        }
        v3Loop?.cancel()
        v3Loop = Task {
            var tick = 0
            while !Task.isCancelled {
                if settings.phoneInboxEnabled { await checkPhoneInbox() }
                if tick % 90 == 0 { await writeDigestsIfDue() }   // every 30 minutes
                tick += 1
                try? await Task.sleep(for: .seconds(20))
            }
        }
    }

    /// Something was just eaten: your own videos get their screen read when that is on.
    func itemEaten(_ id: String) {
        guard let video = engine.video(id) else { return }
        if video.kind == .file, settings.readScreenOfFiles, let file = video.mediaURL, MediaFiles.isVideo(file) {
            readScreen(video, quiet: true)
        }
    }

    // MARK: Your own files

    /// Eats your own audio or video files (drop, Choose File…, zeus eat <path>, youtubezeus://eat?file=…).
    func eatFiles(_ urls: [URL]) async {
        var added: [String] = []
        var skipped: [String] = []
        for url in urls {
            let file = url.standardizedFileURL
            guard MediaFiles.isMedia(file) else {
                skipped.append(file.lastPathComponent)
                continue
            }
            guard FileManager.default.fileExists(atPath: file.path) else {
                show(MediaFileError.missing(file.path).localizedDescription, error: true)
                continue
            }
            let id: String
            do {
                id = try await Task.detached { try MediaFiles.identity(file) }.value
            } catch {
                show("Could not read \(file.lastPathComponent): \(error.localizedDescription)", error: true)
                continue
            }
            if let existing = engine.video(id) {
                // The same recording, maybe moved: remember where it is now.
                existing.mediaURLString = file.absoluteString
                if existing.status == .failed { added.append(id) } else { selectedVideoID = id }
                continue
            }
            let video = Video(videoID: id, title: file.deletingPathExtension().lastPathComponent, channelTitle: "My files",
                              channelID: "files", publishedAt: MediaFiles.creationDate(file), status: .discovered)
            video.kind = .file
            video.mediaURLString = file.absoluteString
            video.thumbnailURLString = nil
            context.insert(video)
            added.append(id)
        }
        try? context.save()
        if !added.isEmpty {
            engine.enqueue(added.map { (id: $0, title: "", channel: "", channelID: "") }, force: true)
            if selection != .library { selection = .library }
            selectedVideoID = added.first
            show(added.count == 1 ? "Eating your file on this Mac." : "Eating \(added.count) files on this Mac.")
        }
        if !skipped.isEmpty {
            show(MediaFileError.notMedia(skipped.joined(separator: ", ")).localizedDescription, error: true)
        }
    }

    func chooseFiles() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [.audiovisualContent]
        panel.message = "Choose recordings, lectures, calls or voice memos: Zeus listens to them on this Mac."
        guard panel.runModal() == .OK else { return }
        let urls = panel.urls
        Task { await eatFiles(urls) }
    }

    // MARK: Podcasts

    /// Follows a podcast (RSS feed or Apple Podcasts link); new episodes are eaten automatically.
    func followPodcast(_ link: String, latest: Int? = nil) async {
        isResolvingLink = true
        defer { isResolvingLink = false }
        do {
            let feed = try await PodcastFeed.resolve(link)
            let podcast = try await PodcastFeed.fetch(feed)
            let channel = watcher.channel(podcast.id) ?? {
                let created = Channel(channelID: podcast.id, title: podcast.title, handle: podcast.author, avatarURLString: podcast.image)
                context.insert(created)
                return created
            }()
            let isNew = channel.feedURLString == nil
            channel.kindRaw = "podcast"
            channel.title = podcast.title.isEmpty ? feed.host ?? "Podcast" : podcast.title
            channel.handle = podcast.author
            channel.avatarURLString = podcast.image ?? channel.avatarURLString
            channel.feedURLString = feed.absoluteString
            channel.websiteURLString = podcast.link
            var known = Set(channel.knownVideoIDs)
            podcast.episodes.forEach { known.insert($0.id(feed: feed)) }
            channel.knownVideoIDs = Array(known)
            channel.lastCheckedAt = .now
            try? context.save()
            selection = .podcast(podcast.id)
            AppLog.write("PODCAST following \(channel.title) (\(podcast.episodes.count) episodes) \(feed.absoluteString)")
            if let latest {
                eatEpisodes(Array(podcast.episodes.prefix(max(0, latest))), channel: channel, feed: feed, language: podcast.language)
            } else if isNew {
                show("Following \(channel.title). New episodes will be eaten automatically.")
                if !podcast.episodes.isEmpty {
                    podcastOffer = PodcastOffer(channelID: podcast.id, title: channel.title, feed: feed,
                                                episodes: Array(podcast.episodes.prefix(50)), language: podcast.language)
                }
            } else {
                show("Already following \(channel.title).")
            }
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    /// Adds episodes to the library and eats them (newest first).
    func eatEpisodes(_ episodes: [PodcastEpisode], channel: Channel, feed: URL, language: String?) {
        let ids = engine.addEpisodes(episodes, channel: channel, feed: feed, language: language, fromWatch: false, autoEat: true)
        if !ids.isEmpty { show("Eating \(ids.count) episode\(ids.count == 1 ? "" : "s") of \(channel.title).") }
    }

    /// "Eat latest" of a followed podcast.
    func eatLatestEpisodes(_ channel: Channel, count: Int) async {
        guard let feedText = channel.feedURLString, let feed = URL(string: feedText) else { return }
        do {
            let podcast = try await PodcastFeed.fetch(feed)
            eatEpisodes(Array(podcast.episodes.prefix(count)), channel: channel, feed: feed, language: podcast.language)
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    // MARK: Text on screen, people and tools, digests (filled in below as 3.0 grows)

    /// Reads the text on screen of a video (slide titles, code, commands) with Vision, on this Mac.
    func readScreen(_ video: Video, quiet: Bool = false) {
        engine.scheduleScreen(video.videoID, force: !quiet)
        if !quiet { show("Reading the screen of “\(video.displayTitle)” on this Mac…") }
    }

    /// An item changed: its people, tools and companies are refreshed with its names (onNames).
    func entityChanged(_ id: String) {}

    // MARK: People, tools and companies

    /// Rebuilds the registry and the notes a few seconds after the last change.
    func scheduleEntityNotes(delay: Double = 8) {
        entityRebuildTask?.cancel()
        entityRebuildTask = Task {
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled else { return }
            await rebuildEntityNotes()
        }
    }

    func rebuildEntityNotes() async {
        let videos = ((try? context.fetch(FetchDescriptor<Video>())) ?? []).filter { $0.status.hasText && $0.entitiesData != nil }
        let inputs = videos.map { video in
            EntityInput(videoID: video.videoID, title: video.displayTitle, noteName: video.noteName, channel: video.channelTitle,
                        published: video.publishedAt, kind: video.kind, entities: video.entities)
        }
        let sources = settings.secondBrainURL.deletingLastPathComponent()
        let youtube = settings.secondBrainURL
        let threshold = settings.entityNoteThreshold
        let reachable = settings.secondBrainEnabled && exporter.folderReachable
        let result = await Task.detached(priority: .utility) {
            EntityNotes.rebuild(inputs, sources: sources, youtube: youtube, threshold: threshold, vaultReachable: reachable)
        }.value
        entityVersion += 1
        if result.written > 0 || !result.newlyLinked.isEmpty {
            AppLog.write("NAMES \(result.records) names, \(result.notes) with a note, \(result.written) note(s) written")
        }
        // Items that name someone who just got a note: their note now links to it.
        guard !result.newlyLinked.isEmpty, reachable else { return }
        for video in videos where video.entities.contains(where: { result.newlyLinked.contains(EntityRegistry.key($0.name)) }) {
            exporter.export(video)
        }
        try? context.save()
    }

    // MARK: Weekly digest

    /// Writes this week's digest on the chosen day and hour, and last week's when it was missed (the Mac was off).
    func writeDigestsIfDue() async {
        guard settings.digestEnabled, settings.secondBrainEnabled, exporter.folderReachable else { return }
        let now = Date.now
        let calendar = Calendar.current
        let thisWeek = WeeklyDigest.key(for: now)
        var due: [String] = []
        if let last = WeeklyDigest.previous(thisWeek), !digestExists(last) { due.append(last) }
        let weekday = calendar.component(.weekday, from: now)
        if weekday == settings.digestWeekday, calendar.component(.hour, from: now) >= settings.digestHour, !digestExists(thisWeek) {
            due.append(thisWeek)
        }
        for key in due { await writeDigest(key, quiet: true) }
    }

    func digestExists(_ key: String) -> Bool {
        if FileManager.default.fileExists(atPath: WeeklyDigest.fileURL(key, folder: settings.digestFolder).path) { return true }
        return (UserDefaults.standard.stringArray(forKey: "digestsSkipped") ?? []).contains(key)
    }

    /// Writes (or rewrites) the digest of a week.
    @discardableResult
    func writeDigest(_ key: String, quiet: Bool = false) async -> URL? {
        let videos = (try? context.fetch(FetchDescriptor<Video>())) ?? []
        guard let made = await DigestMaker.make(key: key, videos: videos, settings: settings, useAI: true) else {
            // Nothing eaten that week: remember it, so the check does not try again.
            var skipped = UserDefaults.standard.stringArray(forKey: "digestsSkipped") ?? []
            if !skipped.contains(key) { skipped.append(key) }
            UserDefaults.standard.set(Array(skipped.suffix(60)), forKey: "digestsSkipped")
            if !quiet { show("Nothing was eaten that week.", error: true) }
            return nil
        }
        let file = WeeklyDigest.fileURL(key, folder: settings.digestFolder)
        do {
            try FileManager.default.createDirectory(at: settings.digestFolder, withIntermediateDirectories: true)
            try made.markdown.write(to: file, atomically: true, encoding: .utf8)
            digestVersion += 1
            AppLog.write("DIGEST \(key): \(made.items) items")
            exporter.scheduleIndexes(in: context)
            if quiet, settings.notifyWhenEaten { Notifier.post(title: "Your week in YouTube Zeus", body: WeeklyDigest.title(key)) }
            if !quiet { show("Digest of \(WeeklyDigest.title(key).lowercased()) written (\(made.items) items).") }
            return file
        } catch {
            if !quiet { show("Could not write the digest: \(error.localizedDescription)", error: true) }
            return nil
        }
    }

    /// youtubezeus://digest?week=… : shows the digest (writes it first when it does not exist yet).
    func openDigest(week: String?) async {
        let key = week ?? WeeklyDigest.key(for: .now)
        selection = .digests
        if !FileManager.default.fileExists(atPath: WeeklyDigest.fileURL(key, folder: settings.digestFolder).path) {
            await writeDigest(key)
        }
        selectedDigest = key
    }

    func digestKeys() -> [String] {
        let files = (try? FileManager.default.contentsOfDirectory(at: settings.digestFolder, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "md" && $0.lastPathComponent.contains("-W") }
            .map { $0.deletingPathExtension().lastPathComponent }.sorted(by: >)
    }

    // MARK: iPhone and iPad inbox

    func checkPhoneInbox() async {
        guard PhoneInbox.iCloudAvailable else { return }
        let pending = await Task.detached(priority: .utility) { PhoneInbox.pending() }.value
        for (file, links) in pending {
            if links.isEmpty {
                AppLog.write("PHONE no link in \(file.lastPathComponent)")
            }
            for link in links {
                AppLog.write("PHONE eat \(link)")
                await eat(link)
                phoneLinksEaten += 1
            }
            PhoneInbox.archive(file)
        }
    }

    /// Makes the "Eat with Zeus" shortcut and opens it in Shortcuts ("Add Shortcut"); iCloud brings it to the iPhone
    /// and iPad, where it appears in the share sheet.
    func installPhoneShortcut() {
        Task {
            do {
                let file = try await PhoneInbox.makeShortcut(in: AppFolders.support.appendingPathComponent("Shortcut", isDirectory: true))
                try? FileManager.default.createDirectory(at: PhoneInbox.inbox, withIntermediateDirectories: true)
                NSWorkspace.shared.open(file)
                show("Shortcuts opens “\(PhoneInbox.shortcutName)”: click Add Shortcut. It syncs to your iPhone and iPad through iCloud.")
            } catch {
                show("Could not make the shortcut: \(error.localizedDescription)", error: true)
            }
        }
    }

    /// Plays an item from a moment: YouTube in the browser; podcasts and your files in Zeus.
    func playMoment(_ video: Video, at seconds: Double) {
        switch video.kind {
        case .youtube:
            NSWorkspace.shared.open(seconds > 0 ? video.url(at: seconds) : video.url)
        case .podcast, .file:
            player.play(video, at: seconds)
        }
    }
}
