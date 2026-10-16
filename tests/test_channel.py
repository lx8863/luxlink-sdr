from pathlib import Path
from tempfile import TemporaryDirectory
import csv
import json
import unittest

import numpy as np

from video_loopback.channel import TrainingChannelEstimator, ChannelLog
from video_loopback.modem import PREAMBLE, make_packet, parse_packet
from video_loopback.radio import LoopbackRadio, RadioSettings


class ChannelTests(unittest.TestCase):
    def test_known_complex_channel_and_tx_scale(self):
        estimator = TrainingChannelEstimator(PREAMBLE)
        taps = np.array([0, 0, 0.04j, 0.4*np.exp(0.3j), -0.02, 0, 0])
        packet = parse_packet(make_packet(1, 0, 1, b"test"))
        for scale in (2000, 9000):
            pre = np.zeros(len(PREAMBLE), dtype=complex)
            pre[estimator.indices] = estimator.design @ taps * scale
            sample = estimator.estimate(pre, scale, 1, packet, "16qam", 100)
            recovered = np.array(sample["tap_real"]) + 1j*np.array(sample["tap_imag"])
            np.testing.assert_allclose(recovered, taps, atol=1e-9)
            response = np.array(sample["response_real"]) + 1j*np.array(sample["response_imag"])
            np.testing.assert_allclose(response, estimator.transform @ taps, atol=1e-9)

    def test_real_modem_normalizes_different_payload_waveforms(self):
        samples = []
        with LoopbackRadio(RadioSettings(mode="simulate", modulation="16qam", samples_per_symbol=2)) as radio:
            radio.set_channel_callback(samples.append)
            for index, payload in enumerate((b"short", np.random.default_rng(8).bytes(1024))):
                radio.transceive_batch([make_packet(index, 0, 1, payload)])
        self.assertEqual(len(samples), 2)
        for sample in samples:
            gain = complex(sample["gain_real"], sample["gain_imag"])
            self.assertAlmostEqual(abs(gain), 1, delta=0.02)
            self.assertAlmostEqual(np.angle(gain), 0.7, delta=0.02)
            self.assertEqual(sample["crc_status"], "passed")
        self.assertNotEqual(samples[0]["tx_training_reference_real"], samples[1]["tx_training_reference_real"])

    def test_csv_export_has_complex_coefficients_and_metadata(self):
        samples = []
        radio_settings = RadioSettings(mode="simulate")
        with LoopbackRadio(radio_settings) as radio:
            radio.set_channel_callback(samples.append)
            radio.transceive_batch([make_packet(1, 0, 1, b"test")])
        with TemporaryDirectory() as folder:
            path = Path(folder) / "channel.csv"
            log = ChannelLog(path, radio_settings)
            log.write(samples[0]); log.close()
            with path.open(encoding="utf-8-sig") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertIn("response_imag_63", rows[0])
            meta = json.loads(path.with_suffix(".json").read_text())
            self.assertTrue(meta["simulation"])
            self.assertFalse(meta["phase_calibrated"])
            self.assertEqual(meta["independent_fitted_coefficients"], 7)


if __name__ == "__main__":
    unittest.main()
