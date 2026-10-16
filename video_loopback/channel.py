"""Known-training estimates of an equivalent single-carrier channel.

Includes pulse shaping, analog filtering and timing error. This is not Wi-Fi
OFDM CSI, calibrated path loss, or a phase-coherent sensing measurement.
"""
from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import numpy as np


class TrainingChannelEstimator:
    def __init__(self, training):
        self.training = np.asarray(training, dtype=np.complex128)
        self.lags = np.arange(-3, 4)
        self.indices = np.arange(12, len(self.training) - 12)
        self.design = self.training[self.indices[:, None] - self.lags[None, :]]
        self.inverse = np.linalg.pinv(self.design)
        self.frequency_cycles = np.linspace(-0.5, 0.5, 64, endpoint=False)
        self.transform = np.exp(-2j * np.pi * self.frequency_cycles[:, None] * self.lags)

    def estimate(self, received_training, tx_reference, timestamp, packet, modulation,
                 raw_preamble_rms):
        if abs(tx_reference) < 1e-9:
            raise ValueError("Missing TX training normalization")
        observed = np.asarray(received_training)[self.indices]
        taps_raw = self.inverse @ observed
        fitted = self.design @ taps_raw
        error = observed - fitted
        signal = max(float(np.mean(np.abs(fitted) ** 2)), 1e-20)
        noise = max(float(np.sum(np.abs(error) ** 2) /
                          (len(error) - len(self.lags))), 1e-20)
        taps = taps_raw / tx_reference
        response = self.transform @ taps
        gain = taps[3]
        return {
            "host_rx_monotonic_s": float(timestamp),
            "host_rx_utc": datetime.fromtimestamp(
                time.time() + timestamp - time.monotonic(), timezone.utc).isoformat(timespec="milliseconds"),
            "frame": packet.frame, "chunk": packet.chunk,
            "crc_status": "passed" if packet.crc_valid else "unchecked",
            "modulation": modulation,
            "gain_real": float(gain.real), "gain_imag": float(gain.imag),
            "gain_db_counts_ratio": float(20 * np.log10(max(abs(gain), 1e-12))),
            "phase_deg_uncalibrated": float(np.angle(gain, deg=True)),
            "training_fit_snr_db": float(10 * np.log10(signal / noise)),
            "training_fit_evm_pct": float(100 * np.sqrt(np.mean(np.abs(error) ** 2) / signal)),
            "rx_preamble_rms_counts": float(raw_preamble_rms),
            "tx_training_reference_real": float(np.real(tx_reference)),
            "tx_training_reference_imag": float(np.imag(tx_reference)),
            "tap_real": taps.real.tolist(), "tap_imag": taps.imag.tolist(),
            "response_real": response.real.tolist(), "response_imag": response.imag.tolist(),
        }


class ChannelLog:
    SCALARS = ("host_rx_monotonic_s", "host_rx_utc", "frame", "chunk", "crc_status",
               "modulation", "gain_real", "gain_imag", "gain_db_counts_ratio",
               "phase_deg_uncalibrated", "training_fit_snr_db", "training_fit_evm_pct",
               "rx_preamble_rms_counts", "tx_training_reference_real", "tx_training_reference_imag")

    def __init__(self, path: Path, radio):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", newline="", encoding="utf-8-sig")
        self.fields = list(self.SCALARS)
        for name, count in (("tap_real", 7), ("tap_imag", 7),
                            ("response_real", 64), ("response_imag", 64)):
            self.fields.extend(f"{name}_{index}" for index in range(count))
        self.writer = csv.DictWriter(self.file, fieldnames=self.fields)
        self.writer.writeheader()
        metadata = {
            "estimator": "7-tap symbol-spaced least-squares equivalent single-carrier channel",
            "radio": asdict(radio), "simulation": radio.mode == "simulate",
            "tap_lags_symbols": list(range(-3, 4)),
            "frequency_hz_relative_to_center": (np.linspace(-0.5, 0.5, 64, endpoint=False) *
                                                   radio.sample_rate / radio.samples_per_symbol).tolist(),
            "independent_fitted_coefficients": 7, "derived_frequency_points": 64,
            "time_source": "host immediately after IQ rx() returns; not hardware timestamps",
            "phase_calibrated": False, "rf_power_calibrated": False,
            "tx_packet_amplitude_normalization_removed": True,
            "limitations": "Includes transceiver filters, timing and PLL errors. Irregular sampling; "
                           "derived frequency bins are not measured OFDM subcarriers. Not ranging or detection.",
        }
        path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self.samples = 0

    def write(self, sample):
        row = {key: sample[key] for key in self.SCALARS}
        for name in ("tap_real", "tap_imag", "response_real", "response_imag"):
            row.update({f"{name}_{index}": value for index, value in enumerate(sample[name])})
        self.writer.writerow(row)
        self.file.flush()
        self.samples += 1

    def close(self):
        self.file.close()
