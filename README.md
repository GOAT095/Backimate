<div align="center">

# 🎬 Backimate

**Play any custom video (`.mp4`, `.mov`, `.m4v`) or animated `.gif` as your live macOS Desktop Wallpaper and Lock Screen.**

[![macOS](https://img.shields.io/badge/macOS-Sonoma%20%7C%20Sequoia%20%7C%20Tahoe%20(14%2B)-000000?style=for-the-badge&logo=apple&logoColor=white)](#)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](#)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero%20(Native%20AVFoundation)-2EA44F?style=for-the-badge)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](#license)

</div>

---

## ✨ Why Backimate?

Most macOS live wallpaper tools only work on the Desktop, require heavy third-party apps, or break on newer macOS releases because Apple hardcodes the default **Golden Gate Sunset** (`4207734D-74FE-4F92-B5E1-6EC8DEE24A15`) wallpaper to a read-only system volume file (`/System/Library/Wallpapers/.default/Golden Gate.mov`).

**Backimate** solves both Desktop and Lock Screen live wallpapers in a single, zero-dependency Python script:

- **🖥️ True Desktop Video & GIF Layer (`--mode desktop`):** Compiles a lightweight native macOS `Cocoa` + `AVFoundation` helper on the fly that pins a borderless, click-through video (`AVPlayerLooper`) or animated GIF (`NSImageView`) window strictly behind your desktop icons (`kCGDesktopWindowLevel`) across all Spaces and monitors.
- **🔒 Native Lock Screen & Aerial Integration (`--mode lockscreen`):** Automatically packages your `.mp4`, `.mov`, or `.gif` into a genuine Apple QuickTime `.mov` container, strips `com.apple.quarantine` extended attributes, updates `Aerial.sqlite` and `Index.plist`, and populates non-hardcoded macOS Aerial slots (**Tahoe Day**, **Sequoia Morning**, **Yosemite**, etc.) so your animation plays smoothly on the macOS Lock Screen and transitions onto the Desktop when you unlock.
- **🎞️ Built-In Animated `.gif` Engine:** Renders `.gif` files natively via `AppKit` on the Desktop with zero conversion delay, and automatically converts `.gif` files into hardware-accelerated QuickTime `.mov` loops for the Lock Screen using native macOS `ImageIO` + `AVFoundation` (or `ffmpeg` if installed).
- **🪶 Zero `pip` Dependencies:** Uses only the Python standard library and macOS's built-in Xcode Command Line Tools (`xcrun swiftc`).
- **♻️ One-Command Backup & Restore:** Automatically backs up original Apple `.mov` assets and your `Index.plist` before making changes, so you can revert to stock macOS settings at any time with `--restore-lockscreen`.

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/Backimate.git
cd Backimate
chmod +x mac_video_wallpaper.py
```

*(Prerequisite: macOS 14 Sonoma, macOS 15 Sequoia, or macOS 26/27 Tahoe with Xcode Command Line Tools installed. If you don't have Command Line Tools yet, run `xcode-select --install`.)*

### 2. Set a Video or GIF on Both Desktop & Lock Screen (Default)

```bash
sudo python3 mac_video_wallpaper.py /path/to/your/video_or_animation.mp4
```

Or with an animated `.gif`:

```bash
sudo python3 mac_video_wallpaper.py /path/to/your/animation.gif
```

> **💡 Lock Screen Tip:** Because macOS hardcodes **Golden Gate Sunset** to the read-only system bridge video, open **System Settings > Wallpaper** once, make sure **Show as screen saver** is enabled, and click **Tahoe Day** (right next to Golden Gate Sunset under *Landscape*), **Sequoia**, or **Yosemite**. Backimate pre-populates all of those slots with your custom video so it plays immediately when you lock your screen (`Ctrl + Cmd + Q`).

---

## 🛠️ Usage & CLI Options

```text
usage: mac_video_wallpaper.py [-h] [--mode {both,desktop,lockscreen}]
                              [--target-id TARGET_ID] [--unmute] [--fit]
                              [--foreground] [--stop]
                              [--list-lockscreen-slots] [--restore-lockscreen]
                              [video]
```

| Command / Flag | Description | Requires `sudo`? |
| :--- | :--- | :---: |
| `python3 mac_video_wallpaper.py file.mp4 --mode desktop` | Play video or `.gif` on the Desktop background only (behind desktop icons, across all Spaces and monitors). | No |
| `sudo python3 mac_video_wallpaper.py file.mp4` | Apply to **both** the Desktop background and the macOS Lock Screen (`--mode both` is default). | Yes |
| `sudo python3 mac_video_wallpaper.py file.mp4 --mode lockscreen` | Install video or `.gif` into the native macOS Lock Screen Aerial engine only. | Yes |
| `--unmute` | Enable audio playback in Desktop mode (muted by default). | No |
| `--fit` | Letterbox/fit the video or GIF inside screen bounds instead of aspect-filling the screen. | No |
| `--foreground` | Keep the Desktop player attached to your terminal window (`Ctrl+C` to stop) instead of daemonizing in the background. | No |
| `python3 mac_video_wallpaper.py --stop` | Stop the running background Desktop video/GIF wallpaper process. | No |
| `python3 mac_video_wallpaper.py --list-lockscreen-slots` | List all installed/downloaded macOS Aerial `.mov` slots on your Mac. | No |
| `sudo python3 mac_video_wallpaper.py --restore-lockscreen` | Restore original Apple Aerial videos and your original `Index.plist` Lock Screen configuration from backup. | Yes |

---

## 🧠 How It Works Under the Hood

macOS separates the user Desktop session from the Lock Screen (`loginwindow` / `WallpaperAerialsExtension` / `idleassetsd`):

1. **Desktop Engine (`AVQueuePlayer` + `AVPlayerLooper` / `NSImageView`):**
   On first run, Backimate compiles a tiny native Swift binary into `.wallpaper_cache/desktop_video_player`. It creates a borderless `NSWindow` for every connected display at window level `kCGDesktopWindowLevel` with `.canJoinAllSpaces`, `.stationary`, and `ignoresMouseEvents = true`. If you plug in an external monitor or change resolutions, it automatically rebuilds the wallpaper layers.
2. **Media Remuxer & GIF Transcoder (`AVAssetExportSession` & `AVAssetWriter`):**
   macOS's Lock Screen daemon rejects raw `.gif` files and ISO `.mp4` files renamed to `.mov`. Backimate compiles `.wallpaper_cache/media_to_mov`, which losslessly remuxes `.mp4`/`.m4v` files into genuine Apple QuickTime (`.MOV/QT`) containers and encodes `.gif` animations into smooth looping QuickTime tracks.
3. **Lock Screen Aerial Injection & Hardcode Bypass:**
   On modern macOS, `WallpaperAerialsExtension` checks if the selected wallpaper ID is `4207734D-74FE-4F92-B5E1-6EC8DEE24A15` (*Golden Gate Sunset*) and forces `/System/Library/Wallpapers/.default/Golden Gate.mov` if so. Backimate automatically switches your active `~/Library/Application Support/com.apple.wallpaper/Store/Index.plist` choice to non-hardcoded Landscape Aerial slots (`4C108785-A7BA-422E-9C79-B0129F1D5550` *Tahoe Day*, *Sequoia*, *Yosemite*, etc.), populates both `/Library/Application Support/com.apple.idleassetsd/Customer/4KSDR240FPS/` and `/Library/Application Support/com.apple.wallpaper/aerials/`, clears `com.apple.quarantine` attributes, updates `Aerial.sqlite`, and restarts the wallpaper daemons.

---

## 🔍 Search Keywords & Topics

`macos-wallpaper` · `live-wallpaper-macos` · `video-wallpaper-mac` · `lockscreen-video-mac` · `gif-wallpaper-mac` · `macos-lock-screen` · `idleassetsd` · `aerial-wallpaper` · `macos-sonoma-wallpaper` · `macos-sequoia-wallpaper` · `macos-tahoe-wallpaper` · `avfoundation` · `python-macos`

---

## 📄 License

Distributed under the **MIT License**. Feel free to fork, customize, and share!
