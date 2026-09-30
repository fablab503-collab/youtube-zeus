import AppKit
import Foundation

/// "Eat with Zeus" from the iPhone and iPad share sheet. A shortcut (made by Zeus, synced by iCloud) saves the shared
/// link as a small text file in iCloud Drive › Shortcuts › YouTube Zeus › Inbox; the Mac app watches that folder, eats
/// each link and moves the file to Inbox › Eaten. No server, no account beyond iCloud.
nonisolated enum PhoneInbox {
    static let shortcutName = "Eat with Zeus"

    /// iCloud Drive's Shortcuts folder on this Mac (what "Save File" writes to on iPhone without asking).
    static var shortcutsFolder: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents", isDirectory: true)
    }

    static var inbox: URL { shortcutsFolder.appendingPathComponent("YouTube Zeus/Inbox", isDirectory: true) }
    static var eaten: URL { inbox.appendingPathComponent("Eaten", isDirectory: true) }

    static var iCloudAvailable: Bool { FileManager.default.fileExists(atPath: shortcutsFolder.path) }

    /// Links waiting in the inbox (oldest first), with their files.
    static func pending() -> [(file: URL, links: [String])] {
        try? FileManager.default.createDirectory(at: inbox, withIntermediateDirectories: true)
        let files = ((try? FileManager.default.contentsOfDirectory(at: inbox, includingPropertiesForKeys: [.contentModificationDateKey],
                                                                  options: [.skipsHiddenFiles])) ?? [])
            .filter { !$0.hasDirectoryPath && ["txt", "url", "webloc", "text", ""].contains($0.pathExtension.lowercased()) }
            .sorted {
                let a = (try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                let b = (try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                return a < b
            }
        return files.compactMap { file in
            // iCloud may still be downloading it: try again at the next check.
            guard let data = try? Data(contentsOf: file), !data.isEmpty else { return nil }
            var text = String(decoding: data, as: UTF8.self)
            if file.pathExtension.lowercased() == "webloc",
               let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
               let url = plist["URL"] as? String { text = url }
            return (file, links(in: text))
        }
    }

    static func links(in text: String) -> [String] {
        guard let detector = try? NSDataDetector(types: NSTextCheckingResult.CheckingType.link.rawValue) else { return [] }
        let range = NSRange(text.startIndex..., in: text)
        var seen = Set<String>()
        return detector.matches(in: text, range: range).compactMap { $0.url?.absoluteString }.filter { seen.insert($0).inserted }
    }

    /// Moves a handled file out of the inbox (kept 30 days in Eaten, then removed).
    static func archive(_ file: URL) {
        try? FileManager.default.createDirectory(at: eaten, withIntermediateDirectories: true)
        let stamp = ISO8601DateFormatter().string(from: .now).replacingOccurrences(of: ":", with: "-")
        let target = eaten.appendingPathComponent("\(stamp) \(file.lastPathComponent)")
        if (try? FileManager.default.moveItem(at: file, to: target)) == nil { try? FileManager.default.removeItem(at: file) }
        let old = Date.now.addingTimeInterval(-30 * 86_400)
        for item in (try? FileManager.default.contentsOfDirectory(at: eaten, includingPropertiesForKeys: [.contentModificationDateKey])) ?? [] {
            if let date = try? item.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate, date < old {
                try? FileManager.default.removeItem(at: item)
            }
        }
    }

    // MARK: The shortcut

    /// The "Eat with Zeus" shortcut: in the share sheet for links, web pages and text; saves the links it receives in
    /// the inbox and shows a notification.
    static func shortcutPlist() -> [String: Any] {
        let urlsID = UUID().uuidString
        let textID = UUID().uuidString
        func token(_ attachment: [String: Any]) -> [String: Any] {
            ["Value": ["string": "\u{FFFC}", "attachmentsByRange": ["{0, 1}": attachment]], "WFSerializationType": "WFTextTokenString"]
        }
        let actions: [[String: Any]] = [
            ["WFWorkflowActionIdentifier": "is.workflow.actions.detect.link",
             "WFWorkflowActionParameters": [
                "UUID": urlsID,
                "WFInput": ["Value": ["Type": "ExtensionInput"], "WFSerializationType": "WFTextTokenAttachment"],
             ]],
            ["WFWorkflowActionIdentifier": "is.workflow.actions.gettext",
             "WFWorkflowActionParameters": [
                "UUID": textID,
                "WFTextActionText": token(["Type": "ActionOutput", "OutputUUID": urlsID, "OutputName": "URLs"]),
             ]],
            ["WFWorkflowActionIdentifier": "is.workflow.actions.documentpicker.save",
             "WFWorkflowActionParameters": [
                "WFInput": ["Value": ["Type": "ActionOutput", "OutputUUID": textID, "OutputName": "Text"],
                            "WFSerializationType": "WFTextTokenAttachment"],
                "WFAskWhereToSave": false,
                "WFFileDestinationPath": "YouTube Zeus/Inbox/",
                "WFSaveFileOverwrite": false,
             ]],
            ["WFWorkflowActionIdentifier": "is.workflow.actions.notification",
             "WFWorkflowActionParameters": [
                "WFNotificationActionTitle": "YouTube Zeus",
                "WFNotificationActionBody": "Sent to your Mac: Zeus eats it there.",
                "WFNotificationActionSound": false,
             ]],
        ]
        return [
            "WFWorkflowClientVersion": "2605.0.5",
            "WFWorkflowMinimumClientVersion": 900,
            "WFWorkflowMinimumClientVersionString": "900",
            "WFWorkflowIcon": ["WFWorkflowIconStartColor": 4_282_601_983, "WFWorkflowIconGlyphNumber": 59_446],
            "WFWorkflowTypes": ["ActionExtension"],
            "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFSafariWebPageContentItem", "WFStringContentItem",
                                                  "WFRichTextContentItem", "WFArticleContentItem"],
            "WFWorkflowHasShortcutInputVariables": true,
            "WFWorkflowImportQuestions": [],
            "WFQuickActionSurfaces": [],
            "WFWorkflowActions": actions,
        ]
    }

    /// Writes and signs the shortcut (Shortcuts' own `shortcuts sign`), ready to open. Returns the signed file.
    static func makeShortcut(in folder: URL) async throws -> URL {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let unsigned = folder.appendingPathComponent("unsigned.shortcut")
        let signed = folder.appendingPathComponent("\(shortcutName).shortcut")
        let data = try PropertyListSerialization.data(fromPropertyList: shortcutPlist(), format: .binary, options: 0)
        try data.write(to: unsigned, options: .atomic)
        try? FileManager.default.removeItem(at: signed)
        _ = try await ProcessRunner.check("/usr/bin/shortcuts", ["sign", "--mode", "anyone", "--input", unsigned.path, "--output", signed.path])
        try? FileManager.default.removeItem(at: unsigned)
        return signed
    }
}
