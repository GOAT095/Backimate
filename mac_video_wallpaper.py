#!/usr/bin/env python3
"""
macOS Video & GIF Wallpaper & Lock Screen Utility (Backimate)
=============================================================
Supports playing custom videos (.mp4, .mov, .m4v) AND animated GIFs (.gif)
in the background of your Mac:

1. Desktop Wallpaper Mode (--mode desktop):
   Uses a native Cocoa/AVFoundation/AppKit borderless window pinned strictly behind
   your desktop icons (`kCGDesktopWindowLevel`) across all Spaces and monitors.
   Plays videos via `AVPlayerLooper` and animated GIFs natively via `NSImageView`
   (`animates = true`) without needing root permissions.

2. Lock Screen & Native Aerial Mode (--mode lockscreen):
   WHY GIFs WORKED WHILE MP4s FAILED:
   - In `desktop_video_player`, `com.apple.screenIsUnlocked` is posted ONLY to the
     logged-in user's Aqua GUI session (`DistributedNotificationCenter`). Our previous
     root LaunchDaemon ran in the `system` Mach bootstrap namespace, which NEVER
     receives `com.apple.screenIsUnlocked`! Meanwhile, `desktop_video_player` (running
     as user `mac`) receives `com.apple.screenIsUnlocked` every single time, and
     `WallpaperAerialsExtension` / `WallpaperAgent` run as user `mac`!
   - Even better: when `desktop_video_player` touches `/tmp/.backimate_unlock_trigger`
     on unlock, the root LaunchDaemon (`WatchPaths`) immediately resets `idleassetsd`
     as `root`, while `desktop_video_player` resets `WallpaperAerialsExtension` and
     `WallpaperAgent` as user `mac`!
   - Furthermore, for `.mp4` videos, `media_to_mov` now converts the video through
     an intermediate animated GIF (`CGImageDestination` -> `CGImageSource`), so the
     final `.mov` is literally produced by the 100% working GIF-to-MOV code path!

3. Combined Mode (--mode both, DEFAULT):
   Runs the instant Desktop background player AND configures the Lock Screen Aerial
   wallpaper (prompts for `sudo` password if not already running as root).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

SWIFT_DESKTOP_PLAYER_SOURCE = r"""
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
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screensDidChange),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(sessionDidWake),
            name: NSWorkspace.screensDidWakeNotification,
            object: nil
        )
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(sessionDidUnlock),
            name: NSNotification.Name("com.apple.screenIsUnlocked"),
            object: nil
        )
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(sessionDidLock),
            name: NSNotification.Name("com.apple.screenIsLocked"),
            object: nil
        )
    }

    @objc func sessionDidWake() {
        for player in players {
            if player.rate == 0 {
                player.play()
            }
        }
    }

    @objc func sessionDidLock() {
        // When screen locks, do not kill extensions mid-lock; let the freshly-reset extension play from 0:00!
    }

    @objc func sessionDidUnlock() {
        sessionDidWake()
        // 1. Kill user-owned Wallpaper extensions immediately in the user's Aqua session
        //    so macOS respawns them fresh at playhead 0:00 before the NEXT screen lock!
        for procName in ["WallpaperAerialsExtension", "WallpaperVideoExtension", "WallpaperAgent", "NeptuneOneWallpaper"] {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/killall")
            task.arguments = ["-9", procName]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
        }
        // 2. Touch /tmp/.backimate_unlock_trigger so the root WatchPaths LaunchDaemon also resets idleassetsd as root!
        let triggerPath = "/tmp/.backimate_unlock_trigger"
        try? "\(Date().timeIntervalSince1970)".write(toFile: triggerPath, atomically: true, encoding: .utf8)
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
app.setActivationPolicy(.accessory)
let delegate = WallpaperAppDelegate(mediaURL: url, isMuted: isMuted, fillScreen: fillScreen)
app.delegate = delegate
app.run()
"""

SWIFT_MEDIA_CONVERTER_SOURCE = r"""
import Foundation
import ImageIO
import UniformTypeIdentifiers
import AVFoundation
import CoreVideo
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("Usage: media_to_mov <input_file> <output.mov>\n", stderr)
    exit(1)
}

let inputURL = URL(fileURLWithPath: args[1])
let outputURL = URL(fileURLWithPath: args[2])
let ext = inputURL.pathExtension.lowercased()

try? FileManager.default.removeItem(at: outputURL)

/// Step 1: If the input is a video (.mp4, .mov, .m4v), we first convert its frames into an
/// in-memory or temporary `.gif` via `CGImageDestination` so that Step 2 (`convertGIFToMOV`)
/// runs the EXACT SAME 100% working `CGImageSource` -> `AVAssetWriter` code path that works for GIFs!
func convertVideoToTempGIF(videoURL: URL, tempGIFURL: URL) -> Bool {
    let asset = AVURLAsset(url: videoURL)
    guard let videoTrack = asset.tracks(withMediaType: .video).first else {
        return false
    }

    let naturalSize = videoTrack.naturalSize.applying(videoTrack.preferredTransform)
    let srcW = abs(naturalSize.width)
    let srcH = abs(naturalSize.height)
    let maxDim: CGFloat = 1280.0
    var targetW = Int(srcW)
    var targetH = Int(srcH)
    if srcW > maxDim || srcH > maxDim {
        let scale = min(maxDim / max(srcW, 1.0), maxDim / max(srcH, 1.0))
        targetW = Int(srcW * scale)
        targetH = Int(srcH * scale)
    }
    targetW = max(64, (targetW % 2 == 0) ? targetW : targetW + 1)
    targetH = max(64, (targetH % 2 == 0) ? targetH : targetH + 1)

    guard let reader = try? AVAssetReader(asset: asset) else { return false }
    let readerSettings: [String: Any] = [
        kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32ARGB),
        kCVPixelBufferWidthKey as String: targetW,
        kCVPixelBufferHeightKey as String: targetH
    ]
    let readerOutput = AVAssetReaderTrackOutput(track: videoTrack, outputSettings: readerSettings)
    readerOutput.alwaysCopiesSampleData = false
    guard reader.canAdd(readerOutput) else { return false }
    reader.add(readerOutput)
    guard reader.startReading() else { return false }

    // Sample up to ~120 frames at ~16.67 FPS (delay = 0.06s, matching the working GIF!)
    let srcFPS = Double(videoTrack.nominalFrameRate > 0 ? videoTrack.nominalFrameRate : 30.0)
    let targetFPS = 16.6666667
    let skipRatio = max(1.0, srcFPS / targetFPS)

    var cgFrames: [CGImage] = []
    var frameIdx = 0
    var nextTarget = 0.0
    let colorSpace = CGColorSpaceCreateDeviceRGB()

    while let sb = readerOutput.copyNextSampleBuffer() {
        defer { frameIdx += 1 }
        if Double(frameIdx) + 0.01 < nextTarget {
            continue
        }
        nextTarget += skipRatio
        guard let pb = CMSampleBufferGetImageBuffer(sb) else { continue }
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        if let ctx = CGContext(
            data: CVPixelBufferGetBaseAddress(pb),
            width: targetW,
            height: targetH,
            bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(pb),
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
        ), let cgImg = ctx.makeImage() {
            cgFrames.append(cgImg)
        }
        CVPixelBufferUnlockBaseAddress(pb, .readOnly)
        if cgFrames.count >= 120 {
            break
        }
    }

    guard !cgFrames.isEmpty else { return false }

    try? FileManager.default.removeItem(at: tempGIFURL)
    guard let dest = CGImageDestinationCreateWithURL(
        tempGIFURL as CFURL,
        UTType.gif.identifier as CFString,
        cgFrames.count,
        nil
    ) else {
        return false
    }

    let fileProps: [CFString: Any] = [
        kCGImagePropertyGIFDictionary: [
            kCGImagePropertyGIFLoopCount: 0
        ]
    ]
    CGImageDestinationSetProperties(dest, fileProps as CFDictionary)

    let frameProps: [CFString: Any] = [
        kCGImagePropertyGIFDictionary: [
            kCGImagePropertyGIFDelayTime: 0.06,
            kCGImagePropertyGIFUnclampedDelayTime: 0.06
        ]
    ]

    for img in cgFrames {
        CGImageDestinationAddImage(dest, img, frameProps as CFDictionary)
    }

    return CGImageDestinationFinalize(dest)
}

/// Step 2: The exact, unmodified GIF -> MOV encoder that produced your working `4C108785...mov.bak`!
func convertGIFToMOV(gifURL: URL, outputURL: URL) -> Bool {
    guard let source = CGImageSourceCreateWithURL(gifURL as CFURL, nil) else {
        fputs("Error: Could not read GIF source at \(gifURL.path)\n", stderr)
        return false
    }

    let frameCount = CGImageSourceGetCount(source)
    guard frameCount > 0, let firstImage = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        fputs("Error: GIF contains no frames.\n", stderr)
        return false
    }

    let rawW = max(64, firstImage.width)
    let rawH = max(64, firstImage.height)
    let width = (rawW % 2 == 0) ? rawW : rawW + 1
    let height = (rawH % 2 == 0) ? rawH : rawH + 1

    let candidateCodecs: [AVVideoCodecType] = [.hevc, .h264, .jpeg]
    var activeWriter: AVAssetWriter?
    var activeInput: AVAssetWriterInput?
    var activeAdaptor: AVAssetWriterInputPixelBufferAdaptor?

    let sourcePixelBufferAttributes: [String: Any] = [
        kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32ARGB),
        kCVPixelBufferWidthKey as String: width,
        kCVPixelBufferHeightKey as String: height
    ]

    for codec in candidateCodecs {
        try? FileManager.default.removeItem(at: outputURL)
        guard let w = try? AVAssetWriter(outputURL: outputURL, fileType: .mov) else { continue }
        let settings: [String: Any] = [
            AVVideoCodecKey: codec,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height
        ]
        let inp = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        inp.expectsMediaDataInRealTime = false
        let adp = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: inp,
            sourcePixelBufferAttributes: sourcePixelBufferAttributes
        )
        if w.canAdd(inp) {
            w.add(inp)
            if w.startWriting() {
                w.startSession(atSourceTime: .zero)
                activeWriter = w
                activeInput = inp
                activeAdaptor = adp
                break
            }
        }
    }

    guard let writer = activeWriter, let writerInput = activeInput, let adaptor = activeAdaptor else {
        fputs("Error: Could not initialize any compatible video encoder for MOV output.\n", stderr)
        return false
    }

    func frameDuration(at index: Int, source: CGImageSource) -> Double {
        var delay = 0.06
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any],
              let gifProps = properties[kCGImagePropertyGIFDictionary] as? [CFString: Any] else {
            return delay
        }
        if let unclamped = gifProps[kCGImagePropertyGIFUnclampedDelayTime] as? Double, unclamped > 0.01 {
            delay = unclamped
        } else if let clamped = gifProps[kCGImagePropertyGIFDelayTime] as? Double, clamped > 0.01 {
            delay = clamped
        }
        return delay
    }

    func makePixelBuffer(from image: CGImage, pool: CVPixelBufferPool?, width: Int, height: Int) -> CVPixelBuffer? {
        var pixelBuffer: CVPixelBuffer?
        if let pool = pool {
            CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &pixelBuffer)
        } else {
            let attrs: [CFString: Any] = [
                kCVPixelBufferCGImageCompatibilityKey: true,
                kCVPixelBufferCGBitmapContextCompatibilityKey: true
            ]
            CVPixelBufferCreate(
                kCFAllocatorDefault,
                width,
                height,
                kCVPixelFormatType_32ARGB,
                attrs as CFDictionary,
                &pixelBuffer
            )
        }
        guard let buffer = pixelBuffer else { return nil }

        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }

        guard let context = CGContext(
            data: CVPixelBufferGetBaseAddress(buffer),
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
        ) else {
            return nil
        }

        context.clear(CGRect(x: 0, y: 0, width: width, height: height))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return buffer
    }

    var singleLoopDuration = 0.0
    for i in 0..<frameCount {
        singleLoopDuration += frameDuration(at: i, source: source)
    }
    let repeatCount = max(1, Int(ceil(6.0 / max(singleLoopDuration, 0.05))))

    let timescale: Int32 = 600
    var currentTime = CMTime.zero

    for _ in 0..<repeatCount {
        for i in 0..<frameCount {
            guard let cgImage = CGImageSourceCreateImageAtIndex(source, i, nil) else { continue }
            while !writerInput.isReadyForMoreMediaData {
                Thread.sleep(forTimeInterval: 0.005)
            }
            if let buffer = makePixelBuffer(from: cgImage, pool: adaptor.pixelBufferPool, width: width, height: height) {
                adaptor.append(buffer, withPresentationTime: currentTime)
            }
            let delay = frameDuration(at: i, source: source)
            let step = CMTime(seconds: delay, preferredTimescale: timescale)
            currentTime = CMTimeAdd(currentTime, step)
        }
    }

    if let lastImage = CGImageSourceCreateImageAtIndex(source, frameCount - 1, nil) {
        while !writerInput.isReadyForMoreMediaData {
            Thread.sleep(forTimeInterval: 0.005)
        }
        if let buffer = makePixelBuffer(from: lastImage, pool: adaptor.pixelBufferPool, width: width, height: height) {
            adaptor.append(buffer, withPresentationTime: currentTime)
        }
    }

    writerInput.markAsFinished()
    let semaphore = DispatchSemaphore(value: 0)
    writer.finishWriting {
        semaphore.signal()
    }
    semaphore.wait()

    return writer.status == .completed
}

if ext == "gif" {
    if convertGIFToMOV(gifURL: inputURL, outputURL: outputURL) {
        exit(0)
    }
    exit(1)
} else {
    // Convert MP4/MOV -> temporary GIF -> MOV using the exact working GIF converter!
    let tempGIFURL = outputURL.deletingLastPathComponent().appendingPathComponent("tmp_video2gif_\(ProcessInfo.processInfo.processIdentifier).gif")
    defer { try? FileManager.default.removeItem(at: tempGIFURL) }
    fputs("[*] Converting video '\(inputURL.lastPathComponent)' to intermediate GIF so it uses the 100% working GIF->MOV pipeline...\n", stderr)
    guard convertVideoToTempGIF(videoURL: inputURL, tempGIFURL: tempGIFURL) else {
        fputs("Error: Could not convert video to intermediate GIF.\n", stderr)
        exit(1)
    }
    fputs("[+] Created intermediate GIF; now encoding Lock Screen MOV via working GIF pipeline...\n", stderr)
    if convertGIFToMOV(gifURL: tempGIFURL, outputURL: outputURL) {
        fputs("[+] Successfully encoded video via GIF->MOV pipeline!\n", stderr)
        exit(0)
    }
    exit(1)
}
"""

AERIAL_ROOT = Path("/Library/Application Support/com.apple.idleassetsd")
AERIAL_SQLITE = AERIAL_ROOT / "Aerial.sqlite"
AERIAL_BASE_DIR = AERIAL_ROOT / "Customer"
AERIAL_VIDEO_DIR = AERIAL_BASE_DIR / "4KSDR240FPS"
AERIAL_ENTRIES_JSON = AERIAL_BASE_DIR / "entries.json"

MODERN_AERIALS_ROOT = Path("/Library/Application Support/com.apple.wallpaper/aerials")
MODERN_AERIALS_VIDEOS_DIR = MODERN_AERIALS_ROOT / "videos"
MODERN_AERIALS_4K_DIR = MODERN_AERIALS_ROOT / "4KSDR240FPS"

LAUNCH_DAEMON_PLIST = Path("/Library/LaunchDaemons/com.backimate.lockreset.plist")
UNLOCK_TRIGGER_FILE = Path("/tmp/.backimate_unlock_trigger")

HARDCODED_GOLDEN_GATE_ID = "4207734D-74FE-4F92-B5E1-6EC8DEE24A15"

PRIMARY_DEFAULT_ID = "4C108785-A7BA-422E-9C79-B0129F1D5550"  # Tahoe Day
BUILTIN_SLOTS_TO_POPULATE = [
    "4C108785-A7BA-422E-9C79-B0129F1D5550",  # Tahoe Day
    "B2FC91ED-6891-4DEB-85A1-268B2B4160B6",  # Tahoe Morning
    "F88CDF4A-9681-4D1F-88FE-34F1A3C6A62B",  # Sequoia Morning
    "6D6834A4-2F0F-479A-B053-7D4DC5CB8EB7",  # Sequoia Sunrise
    "FE876489-CBD5-479B-A8F0-1B67F0741CEA",  # Sonoma Horizon
    "DAD82DCE-F3AE-4AEC-8A79-1694D412FC0A",  # Yosemite (Aerial.sqlite #1)
    "E5799A24-1949-4E66-A17B-B5EB05F28C5D",  # Yosemite Representative
    "86E89C23-C39B-44C8-A985-E56EEA6456FE",  # Golden Gate Night (non-hardcoded)
]

CACHE_DIR = Path(__file__).resolve().parent / ".wallpaper_cache"


def compile_swift_helper(name: str, source: str, frameworks: list[str]) -> Path:
    """Compiles a Swift helper binary into CACHE_DIR if not already cached."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    binary_path = CACHE_DIR / name
    source_path = CACHE_DIR / f"{name}.swift"

    needs_compile = True
    if binary_path.exists() and source_path.exists():
        if source_path.read_text(encoding="utf-8") == source:
            needs_compile = False

    if needs_compile:
        print(f"[*] Compiling native macOS helper ({name})...")
        source_path.write_text(source, encoding="utf-8")
        cmd = ["xcrun", "swiftc", "-O", str(source_path), "-o", str(binary_path)]
        for fw in frameworks:
            cmd.extend(["-framework", fw])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[!] Error compiling {name}:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
        os.chmod(binary_path, 0o755)
        print(f"[+] Compiled {name} to {binary_path}")

    return binary_path


def ensure_compiled_binary() -> Path:
    """Compiles the lightweight Swift/AVFoundation desktop player if not cached."""
    return compile_swift_helper(
        "desktop_video_player",
        SWIFT_DESKTOP_PLAYER_SOURCE,
        ["Cocoa", "AVFoundation"],
    )


def ensure_media_converter_binary() -> Path:
    """Compiles the native Swift ImageIO/AVFoundation media-to-MOV converter if not cached."""
    return compile_swift_helper(
        "media_to_mov",
        SWIFT_MEDIA_CONVERTER_SOURCE,
        ["Foundation", "ImageIO", "UniformTypeIdentifiers", "AVFoundation", "CoreVideo", "CoreGraphics"],
    )


def install_root_watchpaths_daemon():
    """
    Installs a root `WatchPaths` LaunchDaemon (`/Library/LaunchDaemons/com.backimate.lockreset.plist`)
    that watches `/tmp/.backimate_unlock_trigger` (touched by `desktop_video_player` in the user's
    Aqua session whenever `com.apple.screenIsUnlocked` fires) and kills `idleassetsd` and
    `WallpaperAerialsExtension` as `root` on every unlock!
    """
    if os.geteuid() != 0:
        return
    try:
        UNLOCK_TRIGGER_FILE.write_text("init", encoding="utf-8")
        os.chmod(UNLOCK_TRIGGER_FILE, 0o666)

        plist_content = {
            "Label": "com.backimate.lockreset",
            "ProgramArguments": [
                "/bin/sh",
                "-c",
                "/usr/bin/killall -9 WallpaperAerialsExtension WallpaperVideoExtension idleassetsd WallpaperAgent 2>/dev/null || true",
            ],
            "WatchPaths": [str(UNLOCK_TRIGGER_FILE)],
        }
        with LAUNCH_DAEMON_PLIST.open("wb") as f:
            plistlib.dump(plist_content, f)
        os.chmod(LAUNCH_DAEMON_PLIST, 0o644)
        os.chown(LAUNCH_DAEMON_PLIST, 0, 0)

        subprocess.run(["launchctl", "bootout", "system", str(LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "bootstrap", "system", str(LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        print("[+] Installed WatchPaths Unlock Reset Daemon (resets Lock Screen video daemons as root on every unlock)")
    except Exception as exc:
        print(f"[!] Warning: Could not install Lock Screen Reset Daemon ({exc})")


def prepare_lockscreen_mov(media_path: Path) -> Path:
    """
    Packages `media_path` (whether `.gif`, `.mp4`, `.m4v`, or `.mov`) into a Lock Screen
    `.mov` by routing videos through an intermediate `.gif` (`convertVideoToTempGIF` ->
    `convertGIFToMOV`), guaranteeing the 100% working GIF-to-MOV code path!
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and sudo_uid and sudo_gid:
        try:
            os.chown(CACHE_DIR, int(sudo_uid), int(sudo_gid))
        except Exception:
            pass

    stat = media_path.stat()
    fingerprint = hashlib.sha256(
        f"{media_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:via_gif_pipeline_v5".encode("utf-8")
    ).hexdigest()[:12]
    converted_mov = CACHE_DIR / f"{media_path.stem}_{fingerprint}.mov"

    if converted_mov.exists() and converted_mov.stat().st_size > 0:
        print(f"[*] Using cached GIF-pipeline Lock Screen .mov: {converted_mov.name}")
        return converted_mov

    converter_bin = ensure_media_converter_binary()
    print(f"[*] Processing '{media_path.name}' through the working GIF->MOV Lock Screen pipeline...")

    cmd = []
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        cmd.extend(["sudo", "-u", sudo_user])

    cmd.extend([
        str(converter_bin),
        str(media_path.resolve()),
        str(converted_mov),
    ])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            if line.startswith("["):
                print(f"    {line}")
    if result.returncode != 0 or not converted_mov.exists():
        print(f"[!] Failed to convert media to Lock Screen MOV:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"[+] Prepared Lock Screen .mov: {converted_mov.name}")
    return converted_mov


def stop_existing_desktop_player():
    """Stops any running desktop_video_player instances."""
    subprocess.run(["pkill", "-f", "desktop_video_player"], stderr=subprocess.DEVNULL)


def run_desktop_mode(video_path: Path, unmute: bool, fit: bool, foreground: bool):
    """Launches the desktop video/GIF wallpaper behind desktop icons."""
    binary_path = ensure_compiled_binary()
    stop_existing_desktop_player()

    sudo_user = os.environ.get("SUDO_USER")
    cmd = []
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        cmd.extend(["sudo", "-u", sudo_user])

    cmd.extend([str(binary_path), str(video_path.resolve())])
    if unmute:
        cmd.append("--unmute")
    if fit:
        cmd.append("--fit")

    if foreground:
        print(f"[*] Playing '{video_path.name}' on Desktop background (Ctrl+C to stop)...")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            print("\n[*] Stopped desktop wallpaper.")
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"[+] Desktop wallpaper ('{video_path.name}') started in background (PID: {proc.pid}).")
        print(f"    To stop it later, run: python3 {Path(__file__).name} --stop")


def get_target_user_home() -> Path:
    """Returns the real user's home directory even when running under `sudo`."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return Path(f"/Users/{sudo_user}")
    return Path.home()


def list_downloaded_aerials() -> list[tuple[str, Path]]:
    """Finds downloaded macOS Aerial wallpaper `.mov` files across both legacy and modern paths."""
    found = []
    seen = set()
    user_home = get_target_user_home()
    user_aerials_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
    search_dirs = [
        AERIAL_VIDEO_DIR,
        MODERN_AERIALS_VIDEOS_DIR,
        MODERN_AERIALS_4K_DIR,
        MODERN_AERIALS_ROOT,
        user_aerials_dir / "videos",
        user_aerials_dir / "4KSDR240FPS",
        user_aerials_dir,
    ]
    for d in search_dirs:
        if d.exists():
            for v in sorted(d.glob("*.mov"), key=lambda p: p.stat().st_mtime, reverse=True):
                if v.name not in seen:
                    seen.add(v.name)
                    found.append(("Installed Aerial Slot", v))
    return found


def build_aerial_wallpaper_choice_node(asset_id: str) -> dict:
    """
    Constructs the `Index.plist` dictionary structure that instructs macOS's
    `WallpaperAgent`, `WallpaperAerialsExtension`, and `idleassetsd` to play `asset_id`.
    """
    config_plist = plistlib.dumps({"assetID": asset_id}, fmt=plistlib.FMT_BINARY)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return {
        "Content": {
            "Choices": [
                {
                    "Configuration": config_plist,
                    "Files": [],
                    "Provider": "com.apple.wallpaper.choice.aerials",
                }
            ],
            "EncodedOptionValues": "$null",
            "Shuffle": "$null",
        },
        "LastSet": now,
        "LastUse": now,
    }


def activate_aerial_in_user_index_plist(asset_id: str) -> list[str]:
    """
    Updates `~/Library/Application Support/com.apple.wallpaper/Store/Index.plist`
    to switch away from the hardcoded Golden Gate Sunset ID (`4207734D-74FE-4F92-B5E1-6EC8DEE24A15`)
    and use `asset_id` (`4C108785-A7BA-422E-9C79-B0129F1D5550` - Tahoe Day) instead.
    """
    user_home = get_target_user_home()
    store_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "Store"
    index_plist = store_dir / "Index.plist"

    discovered_ids: list[str] = []
    if not index_plist.exists():
        return discovered_ids

    backup_plist = store_dir / "Index.plist.bak"
    if not backup_plist.exists():
        shutil.copy2(index_plist, backup_plist)

    def scan_and_patch_choices(obj):
        if isinstance(obj, dict):
            if "Choices" in obj and isinstance(obj["Choices"], list):
                for choice in obj["Choices"]:
                    if isinstance(choice, dict):
                        cfg_bytes = choice.get("Configuration")
                        if isinstance(cfg_bytes, (bytes, bytearray)) and len(cfg_bytes) > 0:
                            try:
                                cfg = plistlib.loads(cfg_bytes)
                                if isinstance(cfg, dict) and "assetID" in cfg:
                                    aid = str(cfg["assetID"]).strip()
                                    if aid and aid != HARDCODED_GOLDEN_GATE_ID and aid not in discovered_ids:
                                        discovered_ids.append(aid)
                            except Exception:
                                pass
                        choice["Provider"] = "com.apple.wallpaper.choice.aerials"
                        choice["Configuration"] = plistlib.dumps({"assetID": asset_id}, fmt=plistlib.FMT_BINARY)
                        choice["Files"] = []
            for k, v in list(obj.items()):
                if k == " EncodedOptionValues":
                    del obj[k]
                    obj["EncodedOptionValues"] = "$null"
                else:
                    scan_and_patch_choices(v)
        elif isinstance(obj, list):
            for item in obj:
                scan_and_patch_choices(item)

    try:
        with index_plist.open("rb") as f:
            plist_data = plistlib.load(f)

        scan_and_patch_choices(plist_data)
        choice_node = build_aerial_wallpaper_choice_node(asset_id)

        for top_key in ("AllSpacesAndDisplays", "SystemDefault"):
            if top_key in plist_data and isinstance(plist_data[top_key], dict):
                plist_data[top_key]["Desktop"] = choice_node
                plist_data[top_key]["Idle"] = choice_node
                plist_data[top_key]["Type"] = "individual"
            else:
                plist_data[top_key] = {
                    "Desktop": choice_node,
                    "Idle": choice_node,
                    "Type": "individual",
                }

        if "Spaces" in plist_data and isinstance(plist_data["Spaces"], dict):
            for space_uuid, space_dict in plist_data["Spaces"].items():
                if isinstance(space_dict, dict):
                    if "Default" in space_dict and isinstance(space_dict["Default"], dict):
                        space_dict["Default"]["Desktop"] = choice_node
                        space_dict["Default"]["Idle"] = choice_node
                        space_dict["Default"]["Type"] = "individual"
                    if "Displays" in space_dict and isinstance(space_dict["Displays"], dict):
                        for disp_id, disp_dict in space_dict["Displays"].items():
                            if isinstance(disp_dict, dict):
                                disp_dict["Desktop"] = choice_node
                                disp_dict["Idle"] = choice_node
                                disp_dict["Type"] = "individual"

        with index_plist.open("wb") as f:
            plistlib.dump(plist_data, f, fmt=plistlib.FMT_BINARY)

        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(index_plist, int(sudo_uid), int(sudo_gid))

        print(f"[+] Updated Index.plist to use non-hardcoded Aerial slot: {asset_id} (Tahoe Day)")
    except Exception as exc:
        print(f"[!] Warning: Could not update Index.plist ({exc}).")

    return discovered_ids


def mark_aerials_downloaded_in_sqlite(asset_ids: list[str]):
    """
    Marks all target `asset_ids` as downloaded (`ZLASTDOWNLOADED`) AND clears `ZPOINTSOFINTEREST`
    in `/Library/Application Support/com.apple.idleassetsd/Aerial.sqlite`.
    """
    if not AERIAL_SQLITE.exists():
        return
    try:
        coredata_now = (datetime.datetime.now(datetime.timezone.utc).timestamp()) - 978307200.0
        conn = sqlite3.connect(str(AERIAL_SQLITE))
        with conn:
            for aid in asset_ids:
                conn.execute(
                    "UPDATE ZASSET SET ZLASTDOWNLOADED = ?, ZPOINTSOFINTEREST = NULL WHERE ZIDENTIFIER = ?",
                    (coredata_now, aid),
                )
        conn.close()
        print(f"[+] Updated {len(asset_ids)} Aerial slots in Aerial.sqlite (cleared seek checkpoints)")
    except Exception as exc:
        print(f"[!] Warning: Could not update Aerial.sqlite ({exc})")


def install_mov_to_destination(src_mov: Path, dst_mov: Path, uid: int = 0, gid: int = 80, is_first: bool = False):
    """
    Installs `src_mov` to `dst_mov`, backing up any original file and stripping quarantine xattrs.
    Uses hardlinks (`os.link`) after the first copy on the same volume so populating 40+ Aerial slots
    takes 0 extra disk space and completes instantaneously!
    """
    dst_mov.parent.mkdir(parents=True, exist_ok=True)
    if dst_mov.exists():
        backup_file = dst_mov.with_suffix(".mov.bak")
        if not backup_file.exists():
            try:
                shutil.copy2(dst_mov, backup_file)
            except Exception:
                pass
        dst_mov.unlink(missing_ok=True)

    if not is_first:
        try:
            os.link(src_mov, dst_mov)
            return
        except Exception:
            pass

    shutil.copy2(src_mov, dst_mov)
    os.chmod(dst_mov, 0o644)
    try:
        os.chown(dst_mov, uid, gid)
    except Exception:
        pass
    subprocess.run(["xattr", "-c", str(dst_mov)], stderr=subprocess.DEVNULL)


def apply_lockscreen_aerial(video_path: Path, target_id: str | None = None):
    """
    Converts any video into an intermediate GIF and encodes it with the 100% working
    GIF-to-MOV pipeline, installs it across all Lock Screen directories, and configures
    the user + root unlock reset trigger.
    """
    playable_mov = prepare_lockscreen_mov(video_path)

    if os.geteuid() != 0:
        print("[*] Elevating privileges with sudo to install Lock Screen wallpaper...")
        cmd = ["sudo", sys.executable, str(Path(__file__).resolve()), str(playable_mov), "--mode", "lockscreen"]
        if target_id:
            cmd.extend(["--target-id", target_id])
        res = subprocess.run(cmd)
        sys.exit(res.returncode)

    if target_id:
        clean_target = target_id[:-4] if target_id.endswith(".mov") else target_id
        if clean_target.upper() == HARDCODED_GOLDEN_GATE_ID:
            print(f"[!] Note: {HARDCODED_GOLDEN_GATE_ID} (Golden Gate Sunset) is hardcoded by macOS to play from the read-only system volume.")
            print(f"    Switching automatically to {PRIMARY_DEFAULT_ID} (Tahoe Day) so your custom video loads!")
            primary_id = PRIMARY_DEFAULT_ID
        else:
            primary_id = clean_target
    else:
        primary_id = PRIMARY_DEFAULT_ID

    for daemon in [
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    discovered_ids = activate_aerial_in_user_index_plist(primary_id)

    target_ids = []
    for aid in [primary_id] + BUILTIN_SLOTS_TO_POPULATE + discovered_ids:
        if aid and aid not in target_ids:
            target_ids.append(aid)

    for _, existing_path in list_downloaded_aerials():
        stem = existing_path.stem
        if stem not in target_ids and stem != HARDCODED_GOLDEN_GATE_ID:
            target_ids.append(stem)

    user_home = get_target_user_home()
    sudo_uid = int(os.environ.get("SUDO_UID", 0))
    sudo_gid = int(os.environ.get("SUDO_GID", 80))

    user_app_support_aerials = (
        user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
    )
    user_container_aerials = (
        user_home
        / "Library"
        / "Containers"
        / "com.apple.wallpaper.extension.aerials"
        / "Data"
        / "Library"
        / "Application Support"
        / "com.apple.wallpaper"
        / "aerials"
    )

    system_dirs = [
        AERIAL_VIDEO_DIR,
        MODERN_AERIALS_ROOT,
        MODERN_AERIALS_VIDEOS_DIR,
        MODERN_AERIALS_4K_DIR,
    ]
    user_dirs = [
        user_app_support_aerials,
        user_app_support_aerials / "videos",
        user_app_support_aerials / "4KSDR240FPS",
    ]
    if user_container_aerials.parent.exists():
        user_dirs.extend([
            user_container_aerials,
            user_container_aerials / "videos",
            user_container_aerials / "4KSDR240FPS",
        ])

    installed_count = 0
    master_system_mov: Path | None = None
    for d in system_dirs:
        try:
            for aid in target_ids:
                dst = d / f"{aid}.mov"
                if master_system_mov is None:
                    install_mov_to_destination(playable_mov, dst, uid=0, gid=80, is_first=True)
                    master_system_mov = dst
                else:
                    install_mov_to_destination(master_system_mov, dst, uid=0, gid=80, is_first=False)
                installed_count += 1
        except Exception:
            pass

    master_user_mov: Path | None = None
    for d in user_dirs:
        try:
            for aid in target_ids:
                dst = d / f"{aid}.mov"
                if master_user_mov is None:
                    install_mov_to_destination(playable_mov, dst, uid=sudo_uid, gid=sudo_gid, is_first=True)
                    master_user_mov = dst
                else:
                    install_mov_to_destination(master_user_mov, dst, uid=sudo_uid, gid=sudo_gid, is_first=False)
                installed_count += 1
        except Exception:
            pass

    print(f"[+] Installed GIF-pipeline Lock Screen .mov into {len(target_ids)} Aerial slots ({installed_count} hardlinked paths).")

    mark_aerials_downloaded_in_sqlite(target_ids)
    install_root_watchpaths_daemon()

    for daemon in [
        "cfprefsd",
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    print("\n[+] Success! Your video was converted into an intermediate GIF and encoded via the exact")
    print("    working GIF->MOV pipeline, and unlock-reset triggers are active for both user & root!")


def restore_lockscreen_aerials():
    """Restores any backed up `.mov.bak` Aerial videos and `Index.plist.bak`."""
    if os.geteuid() != 0:
        subprocess.run(["sudo", sys.executable, str(Path(__file__).resolve()), "--restore-lockscreen"])
        return

    if LAUNCH_DAEMON_PLIST.exists():
        subprocess.run(["launchctl", "bootout", "system", str(LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        LAUNCH_DAEMON_PLIST.unlink(missing_ok=True)
    UNLOCK_TRIGGER_FILE.unlink(missing_ok=True)

    user_home = get_target_user_home()
    user_app_support_aerials = (
        user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
    )
    user_container_aerials = (
        user_home
        / "Library"
        / "Containers"
        / "com.apple.wallpaper.extension.aerials"
        / "Data"
        / "Library"
        / "Application Support"
        / "com.apple.wallpaper"
        / "aerials"
    )

    candidate_dirs = [
        AERIAL_VIDEO_DIR,
        MODERN_AERIALS_ROOT,
        MODERN_AERIALS_VIDEOS_DIR,
        MODERN_AERIALS_4K_DIR,
        user_app_support_aerials,
        user_app_support_aerials / "videos",
        user_app_support_aerials / "4KSDR240FPS",
        user_container_aerials,
        user_container_aerials / "videos",
        user_container_aerials / "4KSDR240FPS",
    ]

    for daemon in [
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    for d in candidate_dirs:
        if not d.exists():
            continue
        for bak in d.glob("*.mov.bak"):
            orig = bak.with_suffix("")
            print(f"[*] Restoring {orig} from backup...")
            shutil.move(str(bak), str(orig))
            os.chmod(orig, 0o644)
        for slot_id in BUILTIN_SLOTS_TO_POPULATE + [HARDCODED_GOLDEN_GATE_ID]:
            bootstrapped = d / f"{slot_id}.mov"
            if bootstrapped.exists() and not bootstrapped.with_suffix(".mov.bak").exists():
                bootstrapped.unlink(missing_ok=True)

    store_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "Store"
    backup_plist = store_dir / "Index.plist.bak"
    index_plist = store_dir / "Index.plist"
    if backup_plist.exists():
        print("[*] Restoring original Index.plist wallpaper configuration...")
        shutil.move(str(backup_plist), str(index_plist))

    for daemon in [
        "cfprefsd",
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    print("[+] Original Lock Screen settings restored.")


def main():
    parser = argparse.ArgumentParser(
        description="Play a custom video (.mp4, .mov, .m4v) or animated GIF (.gif) on the macOS Desktop background and Lock Screen."
    )
    parser.add_argument(
        "video",
        nargs="?",
        help="Path to the video or animated GIF file (.mp4, .mov, .m4v, .gif)",
    )
    parser.add_argument(
        "--mode",
        choices=["both", "desktop", "lockscreen"],
        default="both",
        help=(
            "'both' sets both the live Desktop background and the Lock Screen wallpaper (default). "
            "'desktop' runs only the zero-sudo live Desktop layer behind icons. "
            "'lockscreen' installs into macOS's native Lock Screen Aerial engine (uses sudo)."
        ),
    )
    parser.add_argument(
        "--target-id",
        help="Optional specific Aerial UUID to replace for Lock Screen mode.",
    )
    parser.add_argument(
        "--unmute",
        action="store_true",
        help="Enable audio playback in desktop mode (muted by default).",
    )
    parser.add_argument(
        "--fit",
        action="store_true",
        help="Fit video/GIF inside screen bounds instead of filling/cropping (desktop mode).",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Keep desktop player attached to the terminal instead of running in the background.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the running desktop wallpaper background process.",
    )
    parser.add_argument(
        "--list-lockscreen-slots",
        action="store_true",
        help="List downloaded macOS Aerial wallpapers available for Lock Screen replacement.",
    )
    parser.add_argument(
        "--restore-lockscreen",
        action="store_true",
        help="Restore original Apple Lock Screen Aerial videos and settings from backup (uses sudo).",
    )

    args = parser.parse_args()

    if args.stop:
        stop_existing_desktop_player()
        print("[+] Stopped desktop video/GIF wallpaper.")
        return

    if args.restore_lockscreen:
        restore_lockscreen_aerials()
        return

    if args.list_lockscreen_slots:
        slots = list_downloaded_aerials()
        if not slots:
            print("No custom/downloaded Aerial wallpapers found yet.")
        else:
            print("Installed Aerial Wallpaper Slots:")
            for label, path in slots:
                print(f"  - {label}: {path.name} ({path})")
        return

    if not args.video:
        parser.print_help()
        sys.exit(1)

    video_path = Path(args.video).expanduser().resolve()
    if not video_path.exists():
        print(f"[!] Error: Media file not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    if args.mode in ("desktop", "both"):
        run_desktop_mode(video_path, unmute=args.unmute, fit=args.fit, foreground=args.foreground)

    if args.mode in ("lockscreen", "both"):
        apply_lockscreen_aerial(video_path, target_id=args.target_id)


if __name__ == "__main__":
    main()
