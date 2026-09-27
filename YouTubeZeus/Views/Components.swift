import SwiftUI

extension Color {
    static let zeus = Color(red: 0.45, green: 0.33, blue: 1.0)
    static let zeusGold = Color(red: 1.0, green: 0.78, blue: 0.22)
}

extension EatStatus {
    var color: Color {
        switch self {
        case .discovered: .blue
        case .queued: .secondary
        case .fetching: .zeus
        case .transcribing: .orange
        case .summarizing: .pink
        case .polishing: .mint
        case .waiting: .teal
        case .done: .green
        case .failed: .red
        }
    }
}

struct StatusBadge: View {
    let status: EatStatus
    var compact = false

    var body: some View {
        Label(status.label, systemImage: status.symbol)
            .labelStyle(.titleAndIcon)
            .font(compact ? .caption2.weight(.semibold) : .caption.weight(.semibold))
            .foregroundStyle(status.color)
            .padding(.horizontal, compact ? 6 : 8)
            .padding(.vertical, compact ? 2 : 3)
            .background(status.color.opacity(0.13), in: .capsule)
            .symbolEffect(.pulse, isActive: status.isBusy)
    }
}

struct Chip: View {
    let text: String
    var symbol: String?
    var tint: Color = .secondary

    var body: some View {
        HStack(spacing: 4) {
            if let symbol { Image(systemName: symbol) }
            Text(text)
        }
        .font(.caption.weight(.medium))
        .foregroundStyle(tint)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(tint.opacity(0.12), in: .capsule)
    }
}

struct Thumbnail: View {
    let url: URL?
    var width: CGFloat = 112
    var radius: CGFloat = 10

    var body: some View {
        AsyncImage(url: url, transaction: Transaction(animation: .easeOut(duration: 0.2))) { phase in
            switch phase {
            case .success(let image):
                image.resizable().aspectRatio(contentMode: .fill)
            default:
                ZStack {
                    LinearGradient(colors: [.zeus.opacity(0.35), .zeus.opacity(0.12)], startPoint: .topLeading, endPoint: .bottomTrailing)
                    Image(systemName: "play.rectangle.fill").font(.title2).foregroundStyle(.white.opacity(0.7))
                }
            }
        }
        .frame(width: width, height: width * 9 / 16)
        .clipShape(.rect(cornerRadius: radius))
        .overlay { RoundedRectangle(cornerRadius: radius).strokeBorder(.white.opacity(0.08)) }
    }
}

struct Avatar: View {
    let url: URL?
    let title: String
    var size: CGFloat = 28

    var body: some View {
        AsyncImage(url: url) { phase in
            if case .success(let image) = phase {
                image.resizable().aspectRatio(contentMode: .fill)
            } else {
                ZStack {
                    Circle().fill(Color.zeus.gradient)
                    Text(String(title.prefix(1)).uppercased())
                        .font(.system(size: size * 0.45, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                }
            }
        }
        .frame(width: size, height: size)
        .clipShape(.circle)
    }
}

struct EmptyState: View {
    let symbol: String
    let title: String
    let message: String

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: symbol)
        } description: {
            Text(message)
        }
    }
}

struct ToastView: View {
    let toast: Toast

    var body: some View {
        Label(toast.text, systemImage: toast.isError ? "exclamationmark.triangle.fill" : "bolt.fill")
            .font(.callout.weight(.medium))
            .foregroundStyle(toast.isError ? Color.red : Color.primary)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .glassEffect(.regular, in: .capsule)
            .padding(.top, 14)
            .transition(.move(edge: .top).combined(with: .opacity))
    }
}

extension Date {
    var shortDay: String { formatted(date: .abbreviated, time: .omitted) }
    var relative: String { formatted(.relative(presentation: .named)) }
}
