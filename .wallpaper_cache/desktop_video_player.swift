
import Cocoa
import AVFoundation

class WallpaperAppDelegate: NSObject, NSApplicationDelegate {
    var windows: [NSWindow] = []
    var players: [AVQueuePlayer] = []
    var loopers: [AVPlayerLooper] = []
    let mediaURL: URL
    let isMuted: Bool
    let fillScreen: Bool
    let isGIF: Bool

    init(mediaURL: URL, isMuted: Bool, fillScreen: Bool) {
        self.mediaURL = mediaURL
        self.isMuted = isMuted
        self.fillScreen = fillScreen
        self.isGIF = mediaURL.pathExtension.lowercased() == "gif"
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupWindows()
        // Rebuild windows automatically if displays are connected/disconnected or resolution changes
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screensDidChange),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
    }

    @objc func screensDidChange() {
        for player in players {
            player.pause()
        }
        for window in windows {
            window.orderOut(nil)
        }
        windows.removeAll()
        players.removeAll()
        loopers.removeAll()
        setupWindows()
    }

    func setupWindows() {
        for screen in NSScreen.screens {
            let frame = screen.frame
            let window = NSWindow(
                contentRect: frame,
                styleMask: [.borderless],
                backing: .buffered,
                defer: false
            )

            // Place the window at the Desktop level (behind desktop icons)
            let desktopLevel = Int(CGWindowLevelForKey(.desktopWindow))
            window.level = NSWindow.Level(rawValue: desktopLevel)
            window.collectionBehavior = [
                .canJoinAllSpaces,
                .stationary,
                .ignoresCycle,
                .fullScreenAuxiliary
            ]
            window.ignoresMouseEvents = true
            window.isOpaque = true
            window.hasShadow = false
            window.backgroundColor = .black

            if isGIF, let gifImage = NSImage(contentsOf: mediaURL) {
                let containerView = NSView(frame: NSRect(origin: .zero, size: frame.size))
                containerView.wantsLayer = true
                containerView.layer?.masksToBounds = true

                let imageView = NSImageView(frame: containerView.bounds)
                imageView.image = gifImage
                imageView.animates = true
                imageView.canDrawSubviewsIntoLayer = true

                if fillScreen && gifImage.size.width > 0 && gifImage.size.height > 0 {
                    // Aspect-fill calculation so the GIF covers the entire monitor cleanly
                    let screenRatio = frame.width / frame.height
                    let imageRatio = gifImage.size.width / gifImage.size.height
                    var drawWidth = frame.width
                    var drawHeight = frame.height
                    if screenRatio > imageRatio {
                        drawHeight = frame.width / imageRatio
                    } else {
                        drawWidth = frame.height * imageRatio
                    }
                    let offsetX = (frame.width - drawWidth) / 2.0
                    let offsetY = (frame.height - drawHeight) / 2.0
                    imageView.frame = NSRect(x: offsetX, y: offsetY, width: drawWidth, height: drawHeight)
                    imageView.imageScaling = .scaleAxesIndependently
                } else {
                    imageView.frame = containerView.bounds
                    imageView.autoresizingMask = [.width, .height]
                    imageView.imageScaling = .scaleProportionallyUpOrDown
                }

                containerView.addSubview(imageView)
                window.contentView = containerView
                window.orderFrontRegardless()
                windows.append(window)
            } else {
                let asset = AVURLAsset(url: mediaURL)
                let view = NSView(frame: NSRect(origin: .zero, size: frame.size))
                view.wantsLayer = true

                let playerItem = AVPlayerItem(asset: asset)
                let queuePlayer = AVQueuePlayer(playerItem: playerItem)
                queuePlayer.isMuted = isMuted
                queuePlayer.preventsDisplaySleepDuringVideoPlayback = false

                let looper = AVPlayerLooper(player: queuePlayer, templateItem: playerItem)

                let playerLayer = AVPlayerLayer(player: queuePlayer)
                playerLayer.frame = view.bounds
                playerLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
                playerLayer.videoGravity = fillScreen ? .resizeAspectFill : .resizeAspect

                view.layer?.addSublayer(playerLayer)
                window.contentView = view
                window.orderFrontRegardless()

                queuePlayer.play()

                windows.append(window)
                players.append(queuePlayer)
                loopers.append(looper)
            }
        }
    }
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Usage: desktop_video_player <media_path> [--unmute] [--fit]\n", stderr)
    exit(1)
}

let mediaPath = args[1]
let isMuted = !args.contains("--unmute")
let fillScreen = !args.contains("--fit")

let url = URL(fileURLWithPath: mediaPath)
guard FileManager.default.fileExists(atPath: url.path) else {
    fputs("Error: File not found at \(mediaPath)\n", stderr)
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory) // Hide Dock icon and menu bar
let delegate = WallpaperAppDelegate(mediaURL: url, isMuted: isMuted, fillScreen: fillScreen)
app.delegate = delegate
app.run()
