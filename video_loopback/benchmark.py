"""Measure CRC-verified payload goodput before selecting a video bitrate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np

from .modem import PAYLOAD_SIZE, MODULATION_BITS, make_packet
from .profiles import apply_profile
from .radio import LoopbackRadio, RadioSettings


def benchmark(settings: RadioSettings, batches: int = 50) -> dict:
    if batches < 1:
        raise ValueError("Batches must be positive")
    rng = np.random.default_rng(72030)
    received_bytes = failed_batches = captures_retried = 0
    durations = []
    # Connection setup is deliberately outside the payload transfer timer.
    radio_class=LoopbackRadio
    if settings.ofdm_streaming:
        from .streaming_radio import StreamingLoopbackRadio
        radio_class=StreamingLoopbackRadio
    stream_metrics=None
    with radio_class(settings) as radio:
        started = time.perf_counter()
        for index in range(batches):
            payloads = [rng.bytes(PAYLOAD_SIZE) for _ in range(settings.batch_packets)]
            packets = [make_packet(index, chunk, len(payloads), payload)
                       for chunk, payload in enumerate(payloads)]
            batch_started = time.perf_counter()
            try:
                decoded = radio.transceive_batch(packets)
                if [packet.payload for packet in decoded] != payloads:
                    raise RuntimeError("Payload differs from transmitted data")
                received_bytes += sum(len(payload) for payload in payloads)
            except RuntimeError:
                failed_batches += 1
            captures_retried += radio.last_retries
            durations.append(time.perf_counter() - batch_started)
        seconds = time.perf_counter() - started
        if settings.ofdm_streaming:
            stream_metrics=dict(radio.stream_metrics)
    goodput = received_bytes * 8 / seconds / 1e6
    from .ofdm import payload_airtime_samples
    raw_rate = (settings.sample_rate * PAYLOAD_SIZE * 8 / payload_airtime_samples(settings.modulation,settings.ofdm_fft_size)
                if settings.waveform == "ofdm" else settings.sample_rate /
                settings.samples_per_symbol * MODULATION_BITS[settings.modulation])
    if settings.waveform == 'ofdm' and settings.ofdm_superframe:
        from .ofdm import configuration
        layout=configuration(settings.ofdm_fft_size)
        from .modem import packet_symbol_count
        import math
        group=(settings.ofdm_training_packets or 16) if settings.ofdm_streaming else settings.ofdm_training_packets
        count=min(group,settings.batch_packets) if group else settings.batch_packets
        groups=math.ceil(settings.batch_packets/count)
        samples = groups*(256 + (2 + math.ceil(packet_symbol_count(settings.modulation) *
                                      count / len(layout.data))) * (layout.fft_size+layout.cp_size))
        raw_rate = settings.sample_rate * PAYLOAD_SIZE * 8 * settings.batch_packets / samples
    return {"mode": settings.mode, "waveform": settings.waveform,
            'ofdm_fft_size':settings.ofdm_fft_size,
            "ofdm_superframe": settings.ofdm_superframe, "simulation": settings.mode == "simulate",
            'persistent_radio_stream':settings.ofdm_streaming,'radio_stream_metrics':stream_metrics,
            "batches": batches, "batch_packets": settings.batch_packets,
            "failed_batches": failed_batches, "extra_captures": captures_retried,
            "received_bytes": received_bytes, "seconds": round(seconds, 3),
            "net_mbps": round(goodput, 3),
            "batch_p95_ms": round(float(np.percentile(durations, 95)) * 1000, 2),
            "sample_rate": settings.sample_rate, "sps": settings.samples_per_symbol,
            "modulation": settings.modulation,
            "raw_modulation_mbps": raw_rate / 1e6,
            "suggested_video_kbps": int(goodput * 1000 * 0.6) if not failed_batches else 0,
            "meets_3mbps_video_headroom": goodput >= 5 and failed_batches == 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("simulate", "digital", "rf"), default="simulate")
    parser.add_argument("--uri", default="usb:")
    parser.add_argument("--waveform", choices=("single-carrier", "ofdm"), default="single-carrier")
    parser.add_argument("--modulation", choices=tuple(MODULATION_BITS), default="16qam")
    parser.add_argument("--sample-rate", type=int, default=8_000_000)
    parser.add_argument("--sps", type=int, choices=(2, 4, 8), default=2)
    parser.add_argument('--ofdm-fft-size',type=int,choices=(256,512,1024,2048),default=256)
    parser.add_argument('--ofdm-superframe',action='store_true')
    parser.add_argument('--ofdm-streaming',action='store_true')
    parser.add_argument("--batch-packets", type=int, default=16)
    parser.add_argument("--channel", type=int, choices=(0, 1), default=0)
    parser.add_argument("--frequency", type=float, default=2400)
    parser.add_argument("--tx-gain", type=float, default=-30)
    parser.add_argument("--rx-gain", type=float, default=10)
    parser.add_argument("--settle-ms", type=float, default=0)
    parser.add_argument("--batches", type=int, default=50)
    parser.add_argument("--report", type=Path)
    apply_profile(parser)
    args = parser.parse_args()
    stats = benchmark(RadioSettings(uri=args.uri, mode=args.mode, waveform=args.waveform,
                      ofdm_fft_size=args.ofdm_fft_size,ofdm_superframe=args.ofdm_superframe,
                      ofdm_streaming=args.ofdm_streaming,
                      modulation=args.modulation, sample_rate=args.sample_rate,
                      samples_per_symbol=args.sps, batch_packets=args.batch_packets,
                      channel=args.channel, frequency_hz=round(args.frequency * 1e6),
                      tx_gain_db=args.tx_gain, rx_gain_db=args.rx_gain,
                      settle_seconds=args.settle_ms / 1000), args.batches)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
