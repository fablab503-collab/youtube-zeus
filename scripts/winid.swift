import CoreGraphics
import Foundation
let owner = CommandLine.arguments.dropFirst().first ?? "YouTube Zeus"
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
for w in list where (w[kCGWindowOwnerName as String] as? String) == owner && (w[kCGWindowLayer as String] as? Int) == 0 {
    print(w[kCGWindowNumber as String] as? Int ?? 0)
}
