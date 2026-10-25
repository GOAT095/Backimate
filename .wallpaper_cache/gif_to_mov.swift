
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
