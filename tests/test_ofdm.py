import unittest
import numpy as np
from video_loopback import ofdm
from video_loopback.modem import make_packet, simulate_capture
from video_loopback.radio import LoopbackRadio, RadioSettings


class OfdmTests(unittest.TestCase):
    def test_diagnostics_from_received_packet(self):
        payload = b"OFDM diagnostics" * 20
        wave = ofdm.modulate(make_packet(2, 0, 1, payload))
        capture = simulate_capture(wave, snr_db=35)
        capture *= np.exp(0.4j)
        samples = []
        decoded = ofdm.demodulate_packets(capture, on_diagnostics=samples.append)
        self.assertEqual(decoded[0].payload, payload)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['spectrum_db'].shape, (1024,))
        self.assertTrue(np.all(np.isfinite(samples[0]['spectrum_db'])))
        self.assertAlmostEqual(float(np.max(samples[0]['spectrum_db'])), 0)
        self.assertGreater(np.mean(np.abs(samples[0]['before']-samples[0]['after'])), .1)
        self.assertEqual(len(ofdm.DATA), 192)
        self.assertEqual(len(ofdm.PILOTS), 8)

    def test_multipath_frequency_offset_and_crc(self):
        rng = np.random.default_rng(42)
        for modulation in ("qpsk", "16qam", "64qam", "256qam"):
            with self.subTest(modulation=modulation):
                payload = rng.bytes(1024)
                wave = ofdm.modulate(make_packet(41, 0, 1, payload), modulation)
                rx = simulate_capture(wave, snr_db=42)
                rx = np.convolve(rx, np.r_[1., np.zeros(6), 0.2 + 0.1j])[:len(rx)]
                rx *= np.exp(1j * 0.001 * np.arange(len(rx)))
                packets = ofdm.demodulate_packets(rx, modulation)
                self.assertEqual(len(packets), 1)
                self.assertEqual(packets[0].payload, payload)

    def test_batch_reassembly_and_deduplication(self):
        packets = [make_packet(52, i, 4, bytes([i]) * 1024) for i in range(4)]
        with LoopbackRadio(RadioSettings(mode="simulate", waveform="ofdm")) as radio:
            received = radio.transceive_batch(packets)
        self.assertEqual([p.payload for p in received], [bytes([i]) * 1024 for i in range(4)])

    def test_noise_and_truncation_rejected(self):
        wave = ofdm.modulate(make_packet(1, 0, 1, b"video"))
        self.assertEqual(ofdm.demodulate_packets(wave[:500]), [])
        noise = np.random.default_rng(1).normal(size=len(wave) * 2).astype(np.complex64)
        self.assertEqual(ofdm.demodulate_packets(noise), [])


if __name__ == "__main__":
    unittest.main()
