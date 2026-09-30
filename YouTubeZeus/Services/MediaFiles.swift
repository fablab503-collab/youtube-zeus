import AVFoundation
import AppKit
import CryptoKit
import Foundation
import UniformTypeIdentifiers

nonisolated enum MediaFileError: LocalizedError, Sendable {
    case missing(String)
    case notMedia(String)

    var errorDescription: String? {
        switch self {
        case .missing(let path): "The file is not there any more: \(path). Drop it on Zeus again from its new place."
        case .notMedia(let name): "\(name) is not an audio or video file."
        }
    }
}

/// Your own audio and video files: identity, duration and a picture for the library and the note.
nonisolated enum MediaFiles {
    static let extensions: Set<String> = ["mp4", "m4v", "mov", "mkv", "webm", "avi", "mp3", "m4a", "aac", "wav", "aiff", "aif",
                                          "caf", "flac", "ogg", "opus", "mpg", "mpeg", "3gp"]

    static func isMedia(_ url: URL) -> Bool {
        if extensions.contains(url.pathExtension.lowercased()) { return true }
        guard let type = UTType(filenameExtension: url.pathExtension) else { return false }
        return type.conforms(to: .audiovisualContent)
    }

    static func isVideo(_ url: URL) -> Bool {
        if let type = UTType(filenameExtension: url.pathExtension), type.conforms(to: .movie) || type.conforms(to: .video) { return true }
        return ["mp4", "m4v", "mov", "mkv", "webm", "avi", "mpg", "mpeg", "3gp"].contains(url.pathExtension.lowercased())
    }

    /// "file-<hash>" from the size and the first and last megabyte: the same file keeps its identity when it is
    /// renamed or moved, and two different files never share one.
    static func identity(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        let size = try handle.seekToEnd()
        var hasher = SHA256()
        hasher.update(data: Data("\(size)".utf8))
        try handle.seek(toOffset: 0)
        hasher.update(data: try handle.read(upToCount: 1_048_576) ?? Data())
        if size > 2_097_152 {
            try handle.seek(toOffset: size - 1_048_576)
            hasher.update(data: try handle.read(upToCount: 1_048_576) ?? Data())
        }
        return "file-" + hasher.finalize().prefix(6).map { String(format: "%02x", $0) }.joined()
    }

    static func duration(_ url: URL) async -> Double {
        let asset = AVURLAsset(url: url)
        guard let time = try? await asset.load(.duration), time.isNumeric else { return 0 }
        return max(0, time.seconds)
    }

    static func creationDate(_ url: URL) -> Date? {
        let values = try? url.resourceValues(forKeys: [.creationDateKey, .contentModificationDateKey])
        return values?.creationDate ?? values?.contentModificationDate
    }

    /// A frame at 10 % of a video, saved as the item's picture (Application Support/Thumbnails/<id>.jpg).
    static func makeThumbnail(for url: URL, id: String) async -> URL? {
        let target = AppFolders.thumbnails.appendingPathComponent("\(id).jpg")
        if FileManager.default.fileExists(atPath: target.path) { return target }
        let asset = AVURLAsset(url: url)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 960, height: 540)
        let length = (try? await asset.load(.duration))?.seconds ?? 0
        let time = CMTime(seconds: length > 0 ? length * 0.1 : 1, preferredTimescale: 600)
        guard let image = try? await generator.image(at: time).image else { return nil }
        return saveJPEG(image, to: target) ? target : nil
    }

    static func saveJPEG(_ image: CGImage, to url: URL, quality: Double = 0.8) -> Bool {
        let rep = NSBitmapImageRep(cgImage: image)
        guard let data = rep.representation(using: .jpeg, properties: [.compressionFactor: quality]) else { return false }
        return (try? data.write(to: url, options: .atomic)) != nil
    }

    /// Podcast artwork, kept as the item's picture for the note.
    static func saveArtwork(from url: URL, id: String) async {
        let target = AppFolders.thumbnails.appendingPathComponent("\(id).jpg")
        guard !FileManager.default.fileExists(atPath: target.path),
              let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200, data.count > 1_000,
              let image = NSImage(data: data), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else { return }
        // Artwork can be 3000 px square: keep a small copy.
        let side = 600.0
        let scale = min(1, side / Double(max(cg.width, cg.height)))
        let size = CGSize(width: Double(cg.width) * scale, height: Double(cg.height) * scale)
        guard let context = CGContext(data: nil, width: Int(size.width), height: Int(size.height), bitsPerComponent: 8,
                                      bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return }
        context.interpolationQuality = .high
        context.draw(cg, in: CGRect(origin: .zero, size: size))
        if let small = context.makeImage() { _ = saveJPEG(small, to: target) }
    }
}
