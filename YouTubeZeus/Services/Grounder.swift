import Foundation
import NaturalLanguage

/// Finds the moment of a video behind a sentence: a key point, a summary sentence, the quote of an Ask answer.
/// A small BM25 ranking of the video's own paragraphs, on this Mac, without AI, so it is instant, free and the same
/// every time. When the best paragraph shares too little with the sentence (a general statement, another language),
/// no moment is given rather than a wrong one.
nonisolated enum Grounder {
    static let stopWords: Set<String> = [
        // English
        "the", "and", "for", "are", "but", "not", "you", "your", "with", "that", "this", "these", "those", "from", "they",
        "them", "their", "there", "then", "than", "have", "has", "had", "was", "were", "been", "being", "will", "would",
        "can", "could", "should", "may", "might", "must", "about", "into", "onto", "over", "under", "also", "just", "very",
        "more", "most", "much", "many", "some", "such", "what", "when", "where", "which", "who", "whom", "whose", "why",
        "how", "all", "any", "each", "every", "both", "its", "it's", "our", "out", "own", "same", "other", "only",
        "like", "get", "gets", "got", "use", "used", "using", "make", "makes", "made", "one", "two", "video", "speaker",
        "explains", "explain", "discusses", "discuss", "shows", "show", "talks", "talk", "says", "said", "mentions",
        "describes", "highlights", "emphasizes", "demonstrates", "notes", "points", "way", "ways", "thing", "things",
        "really", "actually", "going", "gonna", "want", "need", "know", "let", "lets", "okay", "yeah", "right", "so",
        // French
        "les", "des", "une", "est", "que", "qui", "dans", "pour", "par", "sur", "avec", "pas", "plus", "son", "ses",
        "aux", "ces", "cette", "mais", "comme", "tout", "tous", "elle", "ils", "nous", "vous", "leur", "leurs", "donc",
        "aussi", "fait", "faire", "peut", "sont", "ont", "été", "être", "avoir", "vidéo", "explique", "montre",
        // Italian, Spanish, German, Romanian (the most frequent ones)
        "che", "per", "con", "non", "una", "del", "della", "dei", "gli", "sono", "come", "anche", "los", "las", "del",
        "por", "para", "con", "una", "que", "más", "como", "der", "die", "das", "und", "ist", "nicht", "mit", "den",
        "ein", "eine", "auch", "sich", "și", "din", "care", "pentru", "este", "sunt", "sau", "mai", "cum",
    ]

    /// Lowercased, accents removed, stop words dropped, simple stemming (plural "s", then the first 6 letters).
    static func terms(_ text: String) -> [String] {
        let folded = text.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: nil)
            .replacingOccurrences(of: "’", with: "'")
        var result: [String] = []
        for raw in folded.components(separatedBy: CharacterSet.alphanumerics.inverted) where !raw.isEmpty {
            let isNumber = raw.allSatisfy(\.isNumber)
            guard isNumber || raw.count >= 3, !stopWords.contains(raw) else { continue }
            var word = raw
            if word.count > 4, word.hasSuffix("s"), !word.hasSuffix("ss") { word.removeLast() }
            result.append(String(word.prefix(6)))
        }
        return result
    }

    struct Index: Sendable {
        let starts: [Double]
        let counts: [[String: Int]]
        let lengths: [Int]
        let average: Double
        let documentFrequency: [String: Int]

        var count: Int { starts.count }

        func idf(_ term: String) -> Double {
            let df = Double(documentFrequency[term] ?? 0)
            return log(1 + (Double(count) - df + 0.5) / (df + 0.5))
        }
    }

    static func index(_ paragraphs: [TranscriptParagraph]) -> Index {
        var counts: [[String: Int]] = []
        var lengths: [Int] = []
        var df: [String: Int] = [:]
        for paragraph in paragraphs {
            let words = terms(paragraph.text)
            var tf: [String: Int] = [:]
            for word in words { tf[word, default: 0] += 1 }
            for word in tf.keys { df[word, default: 0] += 1 }
            counts.append(tf)
            lengths.append(max(1, words.count))
        }
        let average = lengths.isEmpty ? 1 : Double(lengths.reduce(0, +)) / Double(lengths.count)
        return Index(starts: paragraphs.map(\.start), counts: counts, lengths: lengths, average: average, documentFrequency: df)
    }

    struct Match: Sendable {
        let paragraph: Int
        let seconds: Double
        /// Share of the sentence's weight (rare words weigh more) found in the paragraph and the next one.
        let confidence: Double
        let matchedTerms: Int
    }

    /// The best paragraph for a sentence, looking at each paragraph with the one after it (an idea often spans two).
    static func best(for sentence: String, in index: Index) -> Match? {
        guard index.count > 0 else { return nil }
        let wanted = Set(terms(sentence)).filter { index.documentFrequency[$0] != nil }
        guard !wanted.isEmpty else { return nil }
        let possible = wanted.reduce(0) { $0 + index.idf($1) }
        let k1 = 1.2, b = 0.75
        var bestScore = -1.0
        var bestIndex = 0
        for i in 0..<index.count {
            var score = 0.0
            for term in wanted {
                let tf = Double(index.counts[i][term] ?? 0) + 0.6 * Double(i + 1 < index.count ? index.counts[i + 1][term] ?? 0 : 0)
                guard tf > 0 else { continue }
                let length = Double(index.lengths[i])
                score += index.idf(term) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / index.average))
            }
            if score > bestScore {
                bestScore = score
                bestIndex = i
            }
        }
        guard bestScore > 0 else { return nil }
        let found = wanted.filter { term in
            index.counts[bestIndex][term] != nil || (bestIndex + 1 < index.count && index.counts[bestIndex + 1][term] != nil)
        }
        // The moment is the first of the two paragraphs that holds a word of the sentence.
        let start = found.contains { index.counts[bestIndex][$0] != nil } ? bestIndex : min(bestIndex + 1, index.count - 1)
        let matched = found.reduce(0) { $0 + index.idf($1) }
        return Match(paragraph: start, seconds: index.starts[start], confidence: possible > 0 ? matched / possible : 0,
                     matchedTerms: found.count)
    }

    /// Strict enough to leave a sentence without a moment rather than point to the wrong one.
    static func accept(_ match: Match, sentenceTerms: Int) -> Bool {
        if match.matchedTerms >= 3 { return match.confidence >= 0.34 }
        if match.matchedTerms == 2 { return match.confidence >= 0.5 }
        return sentenceTerms <= 2 && match.confidence >= 0.8
    }

    static func time(for sentence: String, in index: Index) -> Double? {
        guard let match = best(for: sentence, in: index) else { return nil }
        let count = Set(terms(sentence)).filter { index.documentFrequency[$0] != nil }.count
        return accept(match, sentenceTerms: count) ? match.seconds : nil
    }

    static func sentences(_ text: String) -> [String] {
        let tokenizer = NLTokenizer(unit: .sentence)
        tokenizer.string = text
        var result: [String] = []
        tokenizer.enumerateTokens(in: text.startIndex..<text.endIndex) { range, _ in
            let sentence = text[range].trimmingCharacters(in: .whitespacesAndNewlines)
            if !sentence.isEmpty { result.append(sentence) }
            return true
        }
        return result.isEmpty ? [text] : result
    }

    /// Adds the moments to a summary: one per key point and one per summary sentence (nil when unsure).
    static func ground(_ digest: VideoDigest, paragraphs: [TranscriptParagraph]) -> VideoDigest {
        var grounded = digest
        let built = Self.index(paragraphs)
        grounded.keyPointTimes = digest.keyPoints.map { time(for: $0, in: built) }
        grounded.summaryLines = sentences(digest.summary).map { TimedLine(text: $0, seconds: time(for: $0, in: built)) }
        return grounded
    }

    /// The paragraph that holds a quote (Ask your brain sources): the model's time is corrected when the quote is found
    /// elsewhere in the same video.
    static func locate(quote: String, in paragraphs: [TranscriptParagraph]) -> Double? {
        let built = Self.index(paragraphs)
        guard let match = best(for: quote, in: built) else { return nil }
        return match.confidence >= 0.5 && match.matchedTerms >= 2 ? match.seconds : nil
    }
}
