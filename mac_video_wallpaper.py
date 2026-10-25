#!/usr/bin/env python3


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
import uuid
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
"""

SWIFT_GIF_CONVERTER_SOURCE = r"""
import Foundation
import ImageIO
import AVFoundation
import CoreVideo
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("Usage: gif_to_mov <input.gif> <output.mov>\n", stderr)
    exit(1)
}

let inputURL = URL(fileURLWithPath: args[1])
let outputURL = URL(fileURLWithPath: args[2])

guard let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil) else {
    fputs("Error: Could not read GIF source at \(inputURL.path)\n", stderr)
    exit(1)
}

let frameCount = CGImageSourceGetCount(source)
guard frameCount > 0, let firstImage = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("Error: GIF contains no frames.\n", stderr)
    exit(1)
}

let rawW = max(64, firstImage.width)
let rawH = max(64, firstImage.height)
let width = (rawW % 2 == 0) ? rawW : rawW + 1
let height = (rawH % 2 == 0) ? rawH : rawH + 1

// Try hardware codecs first (HEVC, H.264), falling back to universal Motion-JPEG inside .mov
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
    exit(1)
}

func frameDuration(at index: Int, source: CGImageSource) -> Double {
    var delay = 0.1
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

// Pad short GIFs so the resulting MOV is at least 5 seconds long for smooth Lock Screen playback
var singleLoopDuration = 0.0
for i in 0..<frameCount {
    singleLoopDuration += frameDuration(at: i, source: source)
}
let repeatCount = max(1, Int(ceil(5.0 / max(singleLoopDuration, 0.05))))

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

if writer.status != .completed {
    fputs("Error finishing MOV write: \(String(describing: writer.error))\n", stderr)
    exit(1)
}
"""

AERIAL_ROOT = Path("/Library/Application Support/com.apple.idleassetsd")
AERIAL_SQLITE = AERIAL_ROOT / "Aerial.sqlite"
AERIAL_BASE_DIR = AERIAL_ROOT / "Customer"
AERIAL_VIDEO_DIR = AERIAL_BASE_DIR / "4KSDR240FPS"
AERIAL_ENTRIES_JSON = AERIAL_BASE_DIR / "entries.json"
DEFAULT_AERIAL_ID = "DAD82DCE-F3AE-4AEC-8A79-1694D412FC0A"  # Built-in Yosemite Aerial slot
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


def ensure_gif_converter_binary() -> Path:
    """Compiles the native Swift ImageIO/AVFoundation GIF-to-MOV converter if not cached."""
    return compile_swift_helper(
        "gif_to_mov",
        SWIFT_GIF_CONVERTER_SOURCE,
        ["Foundation", "ImageIO", "AVFoundation", "CoreVideo", "CoreGraphics"],
    )


def convert_gif_to_mov(media_path: Path) -> Path:
    """
    Converts a `.gif` into a `.mov` video inside CACHE_DIR using either `ffmpeg`
    (if installed) or the native macOS ImageIO + AVFoundation Swift helper.
    """
    if media_path.suffix.lower() != ".gif":
        return media_path

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stat = media_path.stat()
    fingerprint = hashlib.sha256(
        f"{media_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    converted_mov = CACHE_DIR / f"{media_path.stem}_{fingerprint}.mov"

    if converted_mov.exists() and converted_mov.stat().st_size > 0:
        print(f"[*] Using cached MOV conversion for GIF: {converted_mov.name}")
        return converted_mov

    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        print(f"[*] Converting animated GIF '{media_path.name}' to MOV via ffmpeg...")
        cmd = [
            ffmpeg_bin,
            "-y",
            "-stream_loop",
            "4",
            "-i",
            str(media_path.resolve()),
            "-movflags",
            "faststart",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            str(converted_mov),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and converted_mov.exists():
            print(f"[+] Converted GIF to {converted_mov}")
            return converted_mov

    converter_bin = ensure_gif_converter_binary()
    print(f"[*] Converting animated GIF '{media_path.name}' to MOV via native macOS AVFoundation...")
    result = subprocess.run(
        [str(converter_bin), str(media_path.resolve()), str(converted_mov)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[!] Failed to convert GIF to MOV:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"[+] Converted GIF to {converted_mov}")
    return converted_mov


def stop_existing_desktop_player():
    """Stops any running desktop_video_player instances."""
    subprocess.run(["pkill", "-f", "desktop_video_player"], stderr=subprocess.DEVNULL)


def run_desktop_mode(video_path: Path, unmute: bool, fit: bool, foreground: bool):
    """Launches the desktop video/GIF wallpaper behind desktop icons."""
    binary_path = ensure_compiled_binary()
    stop_existing_desktop_player()

    # If invoked via sudo, launch the GUI desktop window as the actual logged-in console user
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
    """Finds downloaded macOS Aerial wallpaper `.mov` files on the system."""
    if not AERIAL_VIDEO_DIR.exists():
        return []
    videos = sorted(AERIAL_VIDEO_DIR.glob("*.mov"), key=lambda p: p.stat().st_mtime, reverse=True)
    name_map = {}
    if AERIAL_ENTRIES_JSON.exists():
        try:
            data = json.loads(AERIAL_ENTRIES_JSON.read_text(encoding="utf-8"))
            for asset in data.get("assets", []):
                asset_id = asset.get("id")
                label = asset.get("accessibilityLabel") or asset.get("localizedNameKey") or asset_id
                if asset_id:
                    name_map[f"{asset_id}.mov"] = label
        except Exception:
            pass

    return [(name_map.get(v.name, "Downloaded Aerial Video"), v) for v in videos]


def build_aerial_wallpaper_choice_node(asset_id: str) -> dict:
    """
    Constructs the `Index.plist` dictionary structure that instructs macOS's
    `WallpaperAgent` and Lock Screen (`idleassetsd`) to play `asset_id`.
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
            " EncodedOptionValues": "$null",
            "Shuffle": "$null",
        },
        "LastSet": now,
        "LastUse": now,
    }


def activate_aerial_in_user_index_plist(asset_id: str):
    """
    Updates `~/Library/Application Support/com.apple.wallpaper/Store/Index.plist`
    so both the Lock Screen (`Idle`) and Desktop (`SystemDefault` / `AllSpacesAndDisplays`)
    actively use the target Aerial `asset_id` immediately without requiring the user
    to manually open System Settings > Wallpaper.
    """
    user_home = get_target_user_home()
    store_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "Store"
    index_plist = store_dir / "Index.plist"

    if not index_plist.exists():
        print(f"[!] Note: {index_plist} not found; you may need to select the Aerial wallpaper in System Settings > Wallpaper.")
        return

    backup_plist = store_dir / "Index.plist.bak"
    if not backup_plist.exists():
        shutil.copy2(index_plist, backup_plist)

    try:
        with index_plist.open("rb") as f:
            plist_data = plistlib.load(f)

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

        # Preserve original ownership if running under sudo
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(index_plist, int(sudo_uid), int(sudo_gid))

        print(f"[+] Updated Lock Screen & Wallpaper configuration in {index_plist.name} (Asset ID: {asset_id})")
    except Exception as exc:
        print(f"[!] Warning: Could not automatically update Index.plist ({exc}).")


def mark_aerial_downloaded_in_sqlite(asset_id: str):
    """
    Marks `asset_id` as downloaded (`ZLASTDOWNLOADED`) in `/Library/Application Support/com.apple.idleassetsd/Aerial.sqlite`
    so `idleassetsd` recognizes the `.mov` file immediately even if no Aerials were previously downloaded.
    """
    if not AERIAL_SQLITE.exists():
        return
    try:
        # CoreData timestamp epoch is 2001-01-01 00:00:00 UTC (978307200 seconds after Unix epoch)
        coredata_now = (datetime.datetime.now(datetime.timezone.utc).timestamp()) - 978307200.0
        conn = sqlite3.connect(str(AERIAL_SQLITE))
        with conn:
            conn.execute(
                "UPDATE ZASSET SET ZLASTDOWNLOADED = ? WHERE ZIDENTIFIER = ?",
                (coredata_now, asset_id),
            )
        conn.close()
        print(f"[+] Registered asset {asset_id} as downloaded in Aerial.sqlite")
    except Exception as exc:
        print(f"[!] Warning: Could not update Aerial.sqlite ({exc})")


def apply_lockscreen_aerial(video_path: Path, target_id: str | None = None):
    """
    Installs the user's video or converted GIF into macOS's Lock Screen Aerial cache,
    marks it downloaded in `Aerial.sqlite`, and activates it in `Index.plist`.
    """
    # Convert GIF to MOV BEFORE elevating to sudo so AVFoundation hardware encoders run in the user's session
    playable_path = convert_gif_to_mov(video_path)

    if os.geteuid() != 0:
        print("[*] Elevating privileges with sudo to install Lock Screen wallpaper...")
        cmd = ["sudo", sys.executable, str(Path(__file__).resolve()), str(playable_path), "--mode", "lockscreen"]
        if target_id:
            cmd.extend(["--target-id", target_id])
        res = subprocess.run(cmd)
        sys.exit(res.returncode)

    AERIAL_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = list_downloaded_aerials()

    if target_id:
        clean_id = target_id[:-4] if target_id.endswith(".mov") else target_id
        target_file = AERIAL_VIDEO_DIR / f"{clean_id}.mov"
    elif downloaded:
        label, target_file = downloaded[0]
        clean_id = target_file.stem
        print(f"[*] Using existing downloaded Aerial wallpaper slot: {label} ({target_file.name})")
    else:
        # Zero Aerials were previously downloaded on this Mac! Bootstrap the built-in Yosemite slot automatically.
        clean_id = DEFAULT_AERIAL_ID
        target_file = AERIAL_VIDEO_DIR / f"{clean_id}.mov"
        print(f"[*] No previously downloaded Aerials found; bootstrapping built-in slot {clean_id}.mov...")

    if target_file.exists():
        backup_file = target_file.with_suffix(".mov.bak")
        if not backup_file.exists():
            print(f"[*] Backing up original Apple video to {backup_file.name}...")
            shutil.copy2(target_file, backup_file)

    print(f"[*] Installing '{video_path.name}' into {target_file}...")
    shutil.copy2(playable_path, target_file)
    os.chmod(target_file, 0o644)
    os.chown(target_file, 0, 80)  # root:admin

    mark_aerial_downloaded_in_sqlite(clean_id)
    activate_aerial_in_user_index_plist(clean_id)

    # Restart macOS wallpaper and Lock Screen idle assets daemons so changes take effect immediately
    for daemon in ["WallpaperAgent", "WallpaperVideoExtension", "idleassetsd", "cfprefsd"]:
        subprocess.run(["killall", daemon], stderr=subprocess.DEVNULL)

    print("[+] Success! Your custom video/GIF is now active on the macOS Lock Screen.")
    print(f"    To restore your previous Lock Screen settings later, run:\n    sudo python3 {Path(__file__).resolve()} --restore-lockscreen")


def restore_lockscreen_aerials():
    """Restores any backed up `.mov.bak` Aerial videos and `Index.plist.bak`."""
    if os.geteuid() != 0:
        subprocess.run(["sudo", sys.executable, str(Path(__file__).resolve()), "--restore-lockscreen"])
        return

    if AERIAL_VIDEO_DIR.exists():
        for bak in AERIAL_VIDEO_DIR.glob("*.mov.bak"):
            orig = bak.with_suffix("")  # strips .bak, leaving .mov
            print(f"[*] Restoring {orig.name} from backup...")
            shutil.move(str(bak), str(orig))
            os.chmod(orig, 0o644)

        # If we bootstrapped DEFAULT_AERIAL_ID without a prior backup, remove our injected file
        bootstrapped = AERIAL_VIDEO_DIR / f"{DEFAULT_AERIAL_ID}.mov"
        if bootstrapped.exists() and not bootstrapped.with_suffix(".mov.bak").exists():
            print(f"[*] Removing bootstrapped Lock Screen file {bootstrapped.name}...")
            bootstrapped.unlink(missing_ok=True)

    user_home = get_target_user_home()
    store_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "Store"
    backup_plist = store_dir / "Index.plist.bak"
    index_plist = store_dir / "Index.plist"
    if backup_plist.exists():
        print("[*] Restoring original Index.plist wallpaper configuration...")
        shutil.move(str(backup_plist), str(index_plist))

    for daemon in ["WallpaperAgent", "WallpaperVideoExtension", "idleassetsd", "cfprefsd"]:
        subprocess.run(["killall", daemon], stderr=subprocess.DEVNULL)

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
            print("No downloaded Aerial wallpapers found in /Library/Application Support/com.apple.idleassetsd/Customer/4KSDR240FPS")
            print(f"Note: Running with --mode lockscreen or --mode both will automatically bootstrap slot {DEFAULT_AERIAL_ID} for you.")
        else:
            print("Downloaded Aerial Wallpaper Slots:")
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
