import Foundation

/// The languages YouTube can translate captions into (3.7): every two-letter language your Mac knows, plus the codes
/// YouTube spells its own way. YouTube accepts about 160 of them; the others answer "no captions in that language".
nonisolated enum TranslationLanguages {
    struct Language: Identifiable, Hashable, Sendable {
        let code: String
        let name: String
        var id: String { code }
    }

    /// YouTube's spellings where they differ from ISO codes.
    static let youtubeCode: [String: String] = ["he": "iw", "jv": "jw", "zh": "zh-Hans", "nb": "no", "fil": "fil"]

    static let all: [Language] = {
        let english = Locale(identifier: "en")
        var codes = Set(Locale.LanguageCode.isoLanguageCodes.map(\.identifier).filter { $0.count == 2 })
        codes.formUnion(["zh-Hans", "zh-Hant", "fil", "haw", "ceb", "hmn", "kri", "lus", "mni-Mtei", "nso", "pt-PT", "ckb", "doi", "gom", "ilo", "mai"])
        codes.subtract(["zh", "he", "jv", "nb"])
        codes.formUnion(["iw", "jw", "no"])
        return codes.compactMap { code -> Language? in
            let lookup = ["iw": "he", "jw": "jv", "no": "nb"][code] ?? code
            guard let name = english.localizedString(forIdentifier: lookup), !name.isEmpty else { return nil }
            return Language(code: code, name: name)
        }
        .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }()

    static func name(_ code: String) -> String {
        all.first { $0.code == code }?.name ?? Locale(identifier: "en").localizedString(forIdentifier: code) ?? code
    }

    /// The language of this Mac first (then English, French, Italian, Spanish…), for the short list.
    static var suggested: [Language] {
        let mine = Locale.preferredLanguages.map { String($0.split(separator: "-").first ?? "") }
        var codes: [String] = []
        for code in mine + ["en", "fr", "it", "es", "ro", "de", "pt", "zh-Hans", "ja", "ar"] where !codes.contains(code) {
            if all.contains(where: { $0.code == code }) { codes.append(code) }
        }
        return codes.prefix(10).compactMap { code in all.first { $0.code == code } }
    }
}
