"""Command-line entry point, including a board-free end-to-end test mode."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .radio import RadioSettings
from .video import VideoSettings, run_video
from .profiles import apply_profile


def main() -> None:
    p = argparse.ArgumentParser(description="AD9361 video self-transmit/self-receive")
    p.add_argument("input", type=Path, help="Input MP4/AVI/MOV video")
    p.add_argument("--output", type=Path, default=Path("received_video.mp4"))
    p.add_argument("--uri", default="usb:", help="libiio URI, e.g. usb:1.24.5")
    p.add_argument("--mode", choices=("digital", "rf", "simulate"), default="digital")
    p.add_argument("--waveform", choices=("single-carrier", "ofdm"), default="single-carrier")
    p.add_argument("--modulation", choices=("qpsk", "16qam", "64qam", "256qam"), default="qpsk")
    p.add_argument("--transport", choices=("frame", "stream", "original"), default="frame")
    p.add_argument("--bitrate-kbps", type=int, default=30,
                   help="H.264 bitrate in stream mode")
    p.add_argument("--channel", type=int, choices=(0, 1), default=0)
    p.add_argument("--frequency", type=float, default=2400.0, help="TX/RX LO in MHz")
    p.add_argument("--sample-rate", type=int, default=2_000_000, help="IQ samples/s")
    p.add_argument("--sps", type=int, choices=(2, 4, 8), default=8)
    p.add_argument('--ofdm-fft-size',type=int,choices=(256,512,1024,2048),default=256)
    p.add_argument('--ofdm-streaming',action='store_true',help='持续 DMA 收发，仅用于 original OFDM')
    p.add_argument("--batch-packets", type=int, default=1, help="1..64 packets; original OFDM supports up to 512")
    p.add_argument("--settle-ms", type=float, default=0, help="Optional delay after each TX burst")
    p.add_argument("--segment-seconds", type=float, default=1.0)
    p.add_argument("--playback-buffer-segments", type=int, default=1)
    p.add_argument("--report", type=Path, help="Save measured statistics as JSON")
    p.add_argument("--crc", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--encoder-preset", choices=("veryfast", "fast", "medium", "slow"), default="veryfast")
    p.add_argument("--playback-mode", choices=("buffered", "realtime", "download"), default="buffered")
    p.add_argument("--tx-gain", type=float, default=-30.0, help="TX gain in dB (negative means attenuation)")
    p.add_argument("--rx-gain", type=float, default=35.0, help="Manual RX gain in dB")
    p.add_argument("--width", type=int, default=160)
    p.add_argument("--height", type=int, default=90)
    p.add_argument("--fps", type=float, default=1.0, help="Target video frame rate")
    p.add_argument("--quality", type=int, default=35, help="JPEG quality, 5-95")
    p.add_argument("--max-frames", type=int, default=0, help="0 means the whole file")
    apply_profile(p)
    args = p.parse_args()
    if args.transport == "original" and args.max_frames != 0:
        p.error("原码流模式保留完整文件，--max-frames 必须为 0")
    if args.playback_mode == 'download' and args.transport != 'original':
        p.error('--playback-mode download requires --transport original')
    radio = RadioSettings(
        uri=args.uri, mode=args.mode, channel=args.channel, waveform=args.waveform,
        ofdm_superframe=args.transport == "original",
        ofdm_fft_size=args.ofdm_fft_size,
        ofdm_streaming=args.ofdm_streaming,
        frequency_hz=round(args.frequency * 1_000_000),
        sample_rate=args.sample_rate, tx_gain_db=args.tx_gain, rx_gain_db=args.rx_gain,
        modulation=args.modulation, samples_per_symbol=args.sps,
        batch_packets=args.batch_packets, settle_seconds=args.settle_ms / 1000, check_crc=args.crc)
    video = VideoSettings(
        input_path=args.input, output_path=args.output, radio=radio,
        width=args.width, height=args.height, target_fps=args.fps,
        jpeg_quality=args.quality, max_frames=args.max_frames,
        transport=args.transport, stream_bitrate=args.bitrate_kbps * 1000,
        segment_seconds=args.segment_seconds, playback_buffer_segments=args.playback_buffer_segments,
        encoder_preset=args.encoder_preset, playback_mode=args.playback_mode)
    stats = run_video(video)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
