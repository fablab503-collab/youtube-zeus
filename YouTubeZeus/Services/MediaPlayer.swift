import AVFoundation
import AVKit
import Foundation
import Observation
import SwiftUI

/// Plays podcast episodes and your own files from a moment (youtubezeus://open?video=…&t=…, timestamps in notes).
@Observable
final class MediaPlayer {
    private(set) var videoID: String?
    private(set) var title = ""
    private(set) var subtitle = ""
    private(set) var isVideo = false
    private(set) var isPlaying = false
    private(set) var current: Double = 0
    private(set) var duration: Double = 0
    @ObservationIgnored private(set) var player: AVPlayer?
    @ObservationIgnored private var observer: Any?

    var isActive: Bool { videoID != nil }

    func play(_ video: Video, at seconds: Double) {
        guard let url = video.mediaURL else { return }
        if videoID != video.videoID || player == nil {
            stop()
            let item = AVPlayerItem(url: url)
            let player = AVPlayer(playerItem: item)
            self.player = player
            videoID = video.videoID
            title = video.displayTitle
            subtitle = video.channelTitle
            isVideo = MediaFiles.isVideo(url)
            duration = video.duration
            observer = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] time in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.current = time.seconds
                    self.isPlaying = (self.player?.rate ?? 0) > 0
                    if let length = self.player?.currentItem?.duration.seconds, length.isFinite, length > 0 { self.duration = length }
                }
            }
        }
        player?.seek(to: CMTime(seconds: max(0, seconds), preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
        player?.play()
        isPlaying = true
    }

    func toggle() {
        guard let player else { return }
        if player.rate > 0 { player.pause() } else { player.play() }
        isPlaying = player.rate > 0
    }

    func skip(_ delta: Double) {
        guard let player else { return }
        let target = max(0, player.currentTime().seconds + delta)
        player.seek(to: CMTime(seconds: target, preferredTimescale: 600))
    }

    func stop() {
        if let observer { player?.removeTimeObserver(observer) }
        observer = nil
        player?.pause()
        player = nil
        videoID = nil
        isPlaying = false
        current = 0
    }
}

/// The small player at the bottom of the window while an episode or a file plays.
struct PlayerBar: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        let player = app.player
        if player.isActive {
            HStack(spacing: 12) {
                if player.isVideo, let avPlayer = player.player {
                    VideoPlayer(player: avPlayer)
                        .frame(width: 176, height: 99)
                        .clipShape(.rect(cornerRadius: 8))
                }
                Button { player.skip(-15) } label: { Image(systemName: "gobackward.15") }
                    .buttonStyle(.borderless)
                Button { player.toggle() } label: {
                    Image(systemName: player.isPlaying ? "pause.fill" : "play.fill").font(.title3)
                }
                .buttonStyle(.borderless)
                Button { player.skip(30) } label: { Image(systemName: "goforward.30") }
                    .buttonStyle(.borderless)
                VStack(alignment: .leading, spacing: 2) {
                    Text(player.title).font(.callout.weight(.semibold)).lineLimit(1)
                    Text("\(player.current.timestamp) / \(player.duration.timestamp) · \(player.subtitle)")
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 8)
                if let id = player.videoID {
                    Button { app.open(video: id, at: player.current) } label: { Image(systemName: "text.quote") }
                        .buttonStyle(.borderless)
                        .help("Show this moment in the transcript")
                }
                Button { player.stop() } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                    .buttonStyle(.borderless)
                    .help("Stop")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .glassEffect(.regular, in: .rect(cornerRadius: 18))
            .padding(.horizontal, 16)
            .padding(.bottom, 12)
            .frame(maxWidth: 720)
            .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }
}
