"""Shared presets; explicit command-line arguments override every preset."""

PROFILES = {
    "ofdm-original-1080p30-64qam": dict(mode="rf",waveform="ofdm",transport="original",
                   modulation="64qam",width=1920,height=1080,fps=30,bitrate_kbps=4000,
                   sample_rate=3_000_000,ofdm_fft_size=1024,sps=2,batch_packets=256,
                   segment_seconds=.5,playback_buffer_segments=3,playback_mode="realtime",
                   tx_gain=0,rx_gain=28,crc=True),
    "ofdm-original-720p": dict(waveform="ofdm", transport="original", modulation="16qam",
                   bitrate_kbps=4000, width=1280, height=720, fps=30,
                   sample_rate=4_000_000, sps=2, batch_packets=64,
                   segment_seconds=0.5, playback_buffer_segments=3,
                   tx_gain=0, rx_gain=30),
    "ofdm-1080p60": dict(waveform="ofdm", transport="stream", modulation="16qam",
                   bitrate_kbps=400, width=1920, height=1080, fps=60,
                   sample_rate=4_000_000, sps=2, batch_packets=64,
                   segment_seconds=0.5, playback_buffer_segments=3,
                   tx_gain=-10, rx_gain=20),
    "ofdm-video": dict(waveform="ofdm", transport="stream", modulation="qpsk",
                   bitrate_kbps=150, width=640, height=360, fps=30,
                   sample_rate=4_000_000, sps=2, batch_packets=16,
                   segment_seconds=0.5, playback_buffer_segments=3,
                   tx_gain=-10, rx_gain=20),
    "legacy": dict(transport="stream", modulation="64qam", bitrate_kbps=15,
                   width=96, height=54, fps=30, sample_rate=2_000_000,
                   sps=8, batch_packets=1, segment_seconds=1.0),
    "720p30": dict(transport="stream", modulation="16qam", bitrate_kbps=3000,
                   width=1280, height=720, fps=30, sample_rate=8_000_000,
                   sps=2, batch_packets=16, segment_seconds=0.2),
    "720p30-rf": dict(transport="stream", modulation="64qam", bitrate_kbps=500,
                   width=1280, height=720, fps=30, sample_rate=16_000_000,
                   sps=4, batch_packets=64, segment_seconds=0.5,
                   playback_buffer_segments=2, tx_gain=0, rx_gain=10),
}

PROFILE_FILES={
    'ofdm-original-1080p30-64qam':dict(
        input='examples/1080p/ocean_1080p30_original.mp4',
        output='examples/1080p/ocean_1080p30_64qam_received_gui.mp4',
        report='examples/1080p/ocean_1080p30_64qam_received_gui.json'),
}


def apply_profile(parser):
    parser.add_argument("--profile", choices=tuple(PROFILES),
                        help="720p30 is an experimental high-throughput preset")
    selected, _ = parser.parse_known_args()
    if selected.profile:
        parser.set_defaults(**PROFILES[selected.profile])
