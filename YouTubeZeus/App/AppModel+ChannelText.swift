import AppKit
import Foundation
import SwiftData

/// "All channel" (3.6): every video of a YouTube channel eaten for its words only (captions first, Whisper when there
/// are none; no polishing, no summary), each one kept as its own text file in the channel's folder, plus one file with
/// all of them from the first video to the newest. Both are rewritten every time a video of the channel comes in.
///
/// Folder: `<Second Brain>/<Channel>/Text files/` (or `~/Documents/YouTube Zeus/<Channel>/Text files/` when the
/// Second Brain is off): `000 - <Channel> - ALL VIDEOS.txt`, then `001 - <date> - <title>.txt`, oldest first.
extension AppModel {
    nonisolated static let textChannelsKey = "allChannelTextChannels"

    func keepsTextFiles(_ channelTitle: String) -> Bool { textChannelNames.contains(channelTitle) }

    func setKeepsTextFiles(_ channelTitle: String, _ on: Bool) {
        guard !channelTitle.isEmpty else { return }
        if on { textChannelNames.insert(channelTitle) } else { textChannelNames.remove(channelTitle) }
        UserDefaults.standard.set(textChannelNames.sorted(), forKey: Self.textChannelsKey)
        if on { scheduleChannelText(channelTitle, delay: .zero) }
    }

    /// The whole channel, for its words: lists every video, eats the missing ones (text only) and keeps the files.
    func eatAllChannelText(name: String, url: URL, again: Bool = false) async {
        guard let ytdlp = settings.makeYTDLP() else {
            show(EatError.missing("yt-dlp").localizedDescription, error: true)
            return
        }
        let base = url.absoluteString.hasSuffix("/") ? String(url.absoluteString.dropLast()) : url.absoluteString
        let listURL = base.hasSuffix("/videos") ? base : base + "/videos"
        isResolvingLink = true
        defer { isResolvingLink = false }
        do {
            let listing = try await ytdlp.flatList(url: listURL)
            guard !listing.entries.isEmpty else {
                show("No videos found on this channel.", error: true)
                return
            }
            let title = name.isEmpty ? listing.title : name
            setKeepsTextFiles(title, true)
            if !listing.title.isEmpty, listing.title != title { setKeepsTextFiles(listing.title, true) }
            let entries = listing.entries.map { (id: $0.id, title: $0.title, channel: title, channelID: $0.channelID) }
            // `again`: the ones eaten for their words only are eaten again (e.g. after a better caption choice).
            let redo = again ? Set(entries.compactMap { engine.video($0.id) }.filter { $0.textOnly && $0.status == .done }.map(\.videoID)) : []
            if !redo.isEmpty { engine.enqueue(entries.filter { redo.contains($0.id) }, force: true) }
            let videos = engine.enqueue(entries)
            // Only the words: what was not eaten yet gets no polishing and no summary (thousands of minutes otherwise).
            for video in videos where video.status != .done && video.digestData == nil && video.polishedData == nil {
                video.textOnly = true
            }
            try? context.save()
            let missing = videos.filter { $0.status != .done }.count + redo.count
            AppLog.write("ALL CHANNEL \(title): \(listing.entries.count) videos, \(missing) to eat")
            show(missing == 0 ? "All \(listing.entries.count) videos of \(title) are in: their text files are ready."
                              : "Getting the words of \(missing) videos of \(title). Each one becomes a text file, plus one file with all of them.")
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    /// Stops getting the text of a channel: the videos still waiting leave the line (they had nothing yet, so they
    /// are simply forgotten; “Get all the text” lists them again). What is already in stays, files included.
    func stopChannelText(_ channelTitle: String) {
        let doneRaw = EatStatus.done.rawValue
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.channelTitle == channelTitle && $0.statusRaw != doneRaw })
        let waiting = ((try? context.fetch(descriptor)) ?? []).filter {
            $0.textOnly && $0.transcriptText.isEmpty && ($0.status == .queued || $0.status.isBusy || $0.status == .failed)
        }
        for video in waiting {
            engine.forget(video.videoID)
            context.delete(video)
        }
        try? context.save()
        AppLog.write("ALL CHANNEL \(channelTitle): stopped, \(waiting.count) waiting videos left the line")
        show(waiting.isEmpty ? "Nothing of \(channelTitle) was waiting." : "Stopped: \(waiting.count) videos of \(channelTitle) left the line. The text files already made stay.")
    }

    /// Where the text files of a channel live.
    func channelTextFolder(_ channelTitle: String) -> URL {
        let folder = SecondBrainExporter.channelFolderName(channelTitle)
        let base: URL
        if settings.secondBrainEnabled, exporter.folderReachable {
            base = settings.secondBrainURL.appendingPathComponent(folder, isDirectory: true)
        } else {
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            base = documents.appendingPathComponent("YouTube Zeus", isDirectory: true).appendingPathComponent(folder, isDirectory: true)
        }
        return base.appendingPathComponent("Text files", isDirectory: true)
    }

    func channelAllTextURL(_ channelTitle: String) -> URL {
        channelTextFolder(channelTitle).appendingPathComponent("000 - \(SecondBrainExporter.channelFolderName(channelTitle)) - ALL VIDEOS.txt")
    }

    /// Called when an item changes: its channel's files are rewritten a few seconds later (once for a burst).
    func channelTextItemChanged(_ id: String) {
        guard !textChannelNames.isEmpty, let video = engine.video(id), video.isAlive,
              keepsTextFiles(video.channelTitle) else { return }
        scheduleChannelText(video.channelTitle)
    }

    func scheduleChannelText(_ channelTitle: String, delay: Duration = .seconds(4)) {
        textRebuilds[channelTitle]?.cancel()
        textRebuilds[channelTitle] = Task {
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled else { return }
            writeChannelText(channelTitle)
            textRebuilds[channelTitle] = nil
        }
    }

    /// The videos of a channel that have their words, oldest first.
    func channelTextVideos(_ channelTitle: String) -> [Video] {
        let doneRaw = EatStatus.done.rawValue
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.channelTitle == channelTitle && $0.statusRaw == doneRaw })
        let videos = ((try? context.fetch(descriptor)) ?? []).filter { $0.kind == .youtube && !$0.transcriptText.isEmpty }
        return videos.sorted { a, b in
            switch (a.publishedAt, b.publishedAt) {
            case let (x?, y?) where x != y: return x < y
            case (.some, nil): return true
            case (nil, .some): return false
            default: return a.addedAt < b.addedAt
            }
        }
    }

    /// Rewrites the folder: one file per video, numbered from the first upload, and the file with all of them.
    @discardableResult
    func writeChannelText(_ channelTitle: String) -> Int {
        let videos = channelTextVideos(channelTitle)
        guard !videos.isEmpty else { return 0 }
        let folder = channelTextFolder(channelTitle)
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        } catch {
            AppLog.write("ALL CHANNEL \(channelTitle): cannot create \(folder.path): \(error.localizedDescription)")
            return 0
        }
        let digits = max(3, String(videos.count).count)
        var written = Set<String>()
        let allName = channelAllTextURL(channelTitle).lastPathComponent
        var all = """
        \(channelTitle) — every video, from the first to the newest
        \(videos.count) videos · written by YouTube Zeus on \(Date.now.formatted(date: .abbreviated, time: .shortened))
        One file per video sits next to this one (same numbers).


        """
        for (index, video) in videos.enumerated() {
            let number = String(format: "%0\(digits)d", index + 1)
            let day = video.publishedAt.map { Self.textDay.string(from: $0) } ?? "undated"
            let title = SecondBrainExporter.sanitize(video.displayTitle, limit: 90)
            let name = "\(number) - \(day) - \(title).txt"
            let header = "\(video.displayTitle)\n\(channelTitle) · \(day) · \(video.url.absoluteString)\n"
            let text = header + "\n" + video.transcriptText + "\n"
            let file = folder.appendingPathComponent(name)
            if (try? String(contentsOf: file, encoding: .utf8)) != text {
                try? text.write(to: file, atomically: true, encoding: .utf8)
            }
            written.insert(name)
            all += "==================== \(number) · \(day) · \(video.displayTitle) ====================\n"
            all += video.url.absoluteString + "\n\n" + video.transcriptText + "\n\n\n"
        }
        try? all.write(to: channelAllTextURL(channelTitle), atomically: true, encoding: .utf8)
        written.insert(allName)
        // Numbers move when an older video comes in later: files with an old number go.
        for name in (try? fm.contentsOfDirectory(atPath: folder.path)) ?? []
        where name.hasSuffix(".txt") && !written.contains(name) && name.first?.isNumber == true && name.contains(" - ") {
            try? fm.removeItem(at: folder.appendingPathComponent(name))
        }
        AppLog.write("ALL CHANNEL \(channelTitle): \(videos.count) text files")
        return videos.count
    }

    func openChannelTextFolder(_ channelTitle: String) {
        if writeChannelText(channelTitle) == 0 {
            show("No text yet for \(channelTitle): press “All channel” first.", error: true)
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([channelAllTextURL(channelTitle)])
    }

    func openChannelAllText(_ channelTitle: String) {
        guard writeChannelText(channelTitle) > 0 else {
            show("No text yet for \(channelTitle): press “All channel” first.", error: true)
            return
        }
        NSWorkspace.shared.open(channelAllTextURL(channelTitle))
    }

    /// The channel page of a channel known only by its videos.
    func channelURL(forTitle channelTitle: String) -> URL? {
        let descriptor = FetchDescriptor<Video>(predicate: #Predicate { $0.channelTitle == channelTitle })
        guard let id = ((try? context.fetch(descriptor)) ?? []).first(where: { !$0.channelID.isEmpty })?.channelID else { return nil }
        return URL(string: "https://www.youtube.com/channel/\(id)")
    }

    static let textDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()
}
