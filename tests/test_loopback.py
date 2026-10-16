from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from threading import Event

import cv2
import numpy as np

from video_loopback.modem import (demodulate, demodulate_packets, make_packet,
                                  modulate, parse_packet, simulate_capture)
from video_loopback.radio import IncompleteBurst, LoopbackRadio, RadioSettings
from video_loopback.test_video import generate
from video_loopback.video import VideoSettings, run_video


class ModemTests(unittest.TestCase):
    def test_selective_retry_resends_only_missing_packets_with_robust_qam(self):
        raws = [make_packet(30, i, 4, bytes([i])) for i in range(4)]
        packets = [parse_packet(raw) for raw in raws]
        requested = []
        with patch.object(LoopbackRadio, "_connect"):
            radio = LoopbackRadio(RadioSettings(mode="rf", modulation="64qam"))
        def transfer(raw, callback, stop, mode, attempts):
            requested.append((raw, mode))
            radio.last_retries = 1 if len(requested) == 1 else 0
            if len(requested) == 1:
                raise IncompleteBurst([(30, i) for i in range(4)],
                                      {(30, 0): packets[0], (30, 2): packets[2]}, 2)
            return [parse_packet(item) for item in raw]
        with patch.object(radio, "_transceive_batch", side_effect=transfer):
            result = radio.transceive_batch(raws)
        self.assertEqual([packet.chunk for packet in result], [0, 1, 2, 3])
        self.assertEqual(requested[1], ([raws[1], raws[3]], "16qam"))
        self.assertEqual(radio.last_retransmitted_packets, 2)
        radio.close()
    def test_realtime_mode_limits_segment_and_initial_buffer(self):
        with TemporaryDirectory() as tmp:
            source = generate(Path(tmp) / "source.mp4", seconds=1, fps=30, width=96, height=64)
            stats = run_video(VideoSettings(source, Path(tmp) / "out.mp4",
                RadioSettings(mode="simulate", modulation="16qam"),
                width=96, height=64, target_fps=30, max_frames=6, transport="stream",
                segment_seconds=2, playback_buffer_segments=3, playback_mode="realtime"),
                status=lambda _: None)
            self.assertEqual(stats["frames"], 6)
            self.assertEqual(stats["segment_seconds"], 0.5)
            self.assertEqual(stats["playback_buffer_segments"], 1)
            self.assertEqual(stats["playback_mode"], "realtime")

    def test_stream_crc_disabled_reports_unverified_packets(self):
        with TemporaryDirectory() as tmp:
            source = generate(Path(tmp) / "source.mp4", seconds=1, fps=30, width=96, height=64)
            stats = run_video(VideoSettings(source, Path(tmp) / "out.mp4",
                RadioSettings(mode="simulate", modulation="16qam", check_crc=False),
                width=96, height=64, target_fps=30, max_frames=6, transport="stream",
                segment_seconds=0.2), status=lambda _: None)
            self.assertFalse(stats["crc_enabled"])
            self.assertEqual(stats["frames"], 6)
            self.assertEqual(stats["unverified_packets"], stats["packets"])
            self.assertGreater(stats["unverified_packets"], 0)

    def test_crc_checkbox_disables_only_checksum_validation(self):
        raw = bytearray(make_packet(4, 0, 1, b"hello"))
        raw[20] ^= 1
        self.assertIsNone(parse_packet(bytes(raw)))
        accepted = parse_packet(bytes(raw), check_crc=False)
        self.assertIsNotNone(accepted)
        self.assertIsNone(accepted.crc_valid)
        raw[0] ^= 1
        self.assertIsNone(parse_packet(bytes(raw), check_crc=False))

    def test_crc_disabled_modem_accepts_changed_payload(self):
        raw = bytearray(make_packet(12, 0, 1, b"hello world"))
        raw[17] ^= 1
        wave = modulate(bytes(raw), "16qam", 2)
        capture = simulate_capture(wave, snr_db=40)
        self.assertIsNone(demodulate(capture, modulation="16qam", sps=2))
        packet = demodulate(capture, modulation="16qam", sps=2, check_crc=False)
        self.assertIsNotNone(packet)
        self.assertNotEqual(packet.payload, b"hello world")

    def test_constellation_monitor_stops_before_radio_close(self):
        seen = Event()
        radio = LoopbackRadio(RadioSettings(mode="simulate", modulation="16qam",
                                            samples_per_symbol=2))
        radio.transceive_batch([make_packet(9, 0, 1, b"monitor")], lambda *_: None)
        radio.start_constellation_monitor(lambda tx, rx: seen.set(), hz=20)
        self.assertTrue(seen.wait(2), "monitor did not emit a fresh capture")
        radio.close()
        self.assertIsNone(radio._monitor)

    def test_cancel_before_stream_does_not_wait_for_decoder(self):
        with TemporaryDirectory() as tmp:
            source = generate(Path(tmp) / "input.mp4", seconds=1, fps=30)
            stop = Event()
            stop.set()
            stats = run_video(VideoSettings(source, Path(tmp) / "out.mp4",
                              RadioSettings(mode="simulate"), width=160, height=90,
                              target_fps=30, transport="stream"),
                              status=lambda _: None, stop=stop)
            self.assertEqual(stats["frames"], 0)

    def test_decode_failure_propagates_and_cleans_up(self):
        with TemporaryDirectory() as tmp:
            source = generate(Path(tmp) / "input.mp4", seconds=1, fps=30)
            with patch("video_loopback.stream.ContinuousDecoder.decode", side_effect=RuntimeError("decoder failed")), \
                 patch("video_loopback.stream._decode_segment", side_effect=RuntimeError("decoder failed")):
                with self.assertRaisesRegex(RuntimeError, "Video.*failed"):
                    run_video(VideoSettings(source, Path(tmp) / "out.mp4",
                              RadioSettings(mode="simulate"), width=160, height=90,
                              target_fps=30, transport="stream", segment_seconds=0.2),
                              status=lambda _: None)

    def test_burst_all_packets_across_cyclic_boundary(self):
        rng = np.random.default_rng(33)
        payloads = [rng.bytes(1024) for _ in range(16)]
        for sps in (2, 4, 8):
            with self.subTest(sps=sps):
                wave = np.concatenate([modulate(make_packet(18, i, 16, payload), "16qam", sps)
                                       for i, payload in enumerate(payloads)])
                capture = simulate_capture(wave, snr_db=30, phase=1.4,
                                           offset=len(wave) // 2 + 137,
                                           capture_samples=2 * len(wave) + 512)
                packets = demodulate_packets(capture, "16qam", sps, max_packets=16)
                self.assertEqual([packet.chunk for packet in packets], list(range(16)))
                self.assertEqual([packet.payload for packet in packets], payloads)

    def test_batch_ignores_stale_capture_and_uses_no_fixed_sleep(self):
        raws = [make_packet(20, i, 4, bytes([i]) * 1024) for i in range(4)]
        stale = np.concatenate([modulate(make_packet(19, i, 4, b"stale"), "16qam", 2)
                                for i in range(4)])
        wave = np.concatenate([modulate(raw, "16qam", 2) for raw in raws])
        device = Mock()
        device.rx.side_effect = [simulate_capture(stale, snr_db=35),
                                 simulate_capture(wave, snr_db=35)]
        with patch.object(LoopbackRadio, "_connect"), patch("video_loopback.radio.time.sleep") as sleep:
            radio = LoopbackRadio(RadioSettings(mode="digital", modulation="16qam",
                                                samples_per_symbol=2, batch_packets=4))
            radio.sdr = device
            packets = radio.transceive_batch(raws)
            self.assertEqual([packet.frame for packet in packets], [20] * 4)
            self.assertEqual(radio.last_retries, 1)
            device.tx.assert_called_once()
            device.rx_destroy_buffer.assert_called_once()
            sleep.assert_not_called()
            radio.close()

    def test_missing_packet_is_reported(self):
        raw = make_packet(21, 0, 1, b"missing")
        device = Mock()
        device.rx.return_value = np.zeros(10000, dtype=np.complex64)
        with patch.object(LoopbackRadio, "_connect"):
            radio = LoopbackRadio(RadioSettings(mode="digital", attempts=2))
            radio.sdr = device
            with self.assertRaisesRegex(RuntimeError, "Missing 1/1"):
                radio.transceive_batch([raw])
            radio.close()

    def test_720p30_short_segments_and_fps_conversion(self):
        with TemporaryDirectory() as tmp:
            source = generate(Path(tmp) / "input.mp4", seconds=1, fps=25, width=1280, height=720)
            output = Path(tmp) / "received.mp4"
            stats = run_video(VideoSettings(
                source, output, RadioSettings(mode="simulate", modulation="16qam",
                    sample_rate=8_000_000, samples_per_symbol=2, batch_packets=16),
                width=1280, height=720, target_fps=30, max_frames=30,
                transport="stream", stream_bitrate=3_000_000, segment_seconds=0.2),
                status=lambda _: None)
            self.assertEqual(stats["frames"], 30)
            self.assertEqual(stats["decoder_sessions"], 1)
            self.assertEqual(stats["decoder_fallbacks"], 0)
            self.assertEqual(stats["lost_frames"], 0)
            self.assertEqual(stats["segments"], 5)
            capture = cv2.VideoCapture(str(output))
            self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), 30)
            self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), 1280)
            self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)), 720)
            self.assertEqual(capture.get(cv2.CAP_PROP_FPS), 30)
            capture.release()

    def test_packet_roundtrip_with_noise_delay_and_phase(self):
        payload = bytes(np.random.default_rng(42).integers(0, 256, 941, dtype=np.uint8))
        original = make_packet(123, 1, 3, payload)
        waveform = modulate(original)
        result = demodulate(simulate_capture(waveform, snr_db=20, phase=1.2, offset=253), 123, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result.payload, payload)
        self.assertEqual(result.total_chunks, 3)

    def test_crc_rejects_corruption(self):
        original = bytearray(make_packet(4, 0, 1, b"hello"))
        original[50] ^= 1
        self.assertIsNone(parse_packet(bytes(original)))

    def test_packet_from_two_cyclic_dma_periods(self):
        raw = make_packet(7, 0, 1, b"short test")
        wave = modulate(raw)
        for offset in (0, 51, len(wave) // 2, len(wave) - 37):
            capture = np.tile(wave, 3)[offset:offset + 2 * len(wave) + 512]
            found = demodulate(capture * np.exp(0.8j), 7, 0)
            self.assertIsNotNone(found, f"failed at cyclic offset {offset}")
            self.assertEqual(found.payload, b"short test")

    def test_qam_orders_with_random_packet(self):
        payload = np.random.default_rng(7).integers(0, 256, 1024, dtype=np.uint8).tobytes()
        raw = make_packet(5, 1, 2, payload)
        for modulation, snr in (("qpsk", 24), ("16qam", 30),
                                ("64qam", 38), ("256qam", 46)):
            with self.subTest(modulation=modulation):
                received = demodulate(
                    simulate_capture(modulate(raw, modulation), snr_db=snr),
                    5, 1, modulation=modulation)
                self.assertIsNotNone(received)
                self.assertEqual(received.payload, payload)

    def test_video_file_roundtrip_without_board(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.mp4"
            output = Path(tmp) / "received.mp4"
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 3, (96, 64))
            self.assertTrue(writer.isOpened())
            for n in range(3):
                frame = np.full((64, 96, 3), (n * 70, 30, 130), dtype=np.uint8)
                cv2.putText(frame, str(n), (25, 45), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                writer.write(frame)
            writer.release()
            tx_frames = []
            rx_frames = []
            constellation_updates = []
            result = run_video(VideoSettings(source, output, RadioSettings(mode="simulate"),
                                             width=96, height=64, target_fps=3, max_frames=3),
                               status=lambda _message: None,
                               tx_frame_ready=lambda n, frame: tx_frames.append((n, frame.copy())),
                               rx_frame_ready=lambda n, frame: rx_frames.append((n, frame.copy() if frame is not None else None)),
                               constellation_ready=lambda tx, rx: constellation_updates.append(
                                   (len(tx), None if rx is None else len(rx))))
            self.assertEqual(result["frames"], 3)
            self.assertEqual(result["lost_frames"], 0)
            self.assertEqual([n for n, _frame in tx_frames], [0, 1, 2])
            self.assertEqual([n for n, _frame in rx_frames], [0, 1, 2])
            self.assertTrue(all(frame.shape == (64, 96, 3) for _n, frame in rx_frames))
            self.assertTrue(all(np.array_equal(tx[1], rx[1])
                                for tx, rx in zip(tx_frames, rx_frames)))
            self.assertTrue(any(rx_count == 256 for _tx_count, rx_count in constellation_updates))
            cap = cv2.VideoCapture(str(output))
            self.assertTrue(cap.isOpened())
            self.assertEqual(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), 3)
            cap.release()

    def test_one_second_60fps_stream_without_board(self):
        with TemporaryDirectory() as tmp:
            source = generate(Path(tmp) / "input.mp4", seconds=1, fps=60)
            output = Path(tmp) / "received.mp4"
            matching_pairs = []
            stats = run_video(
                VideoSettings(source, output, RadioSettings(mode="simulate", modulation="16qam"),
                              width=160, height=90, target_fps=60, max_frames=60,
                              transport="stream", stream_bitrate=30_000,
                              playback_buffer_segments=2),
                status=lambda _message: None,
                pair_frame_ready=lambda _n, tx, rx: matching_pairs.append(
                    rx is not None and np.array_equal(tx, rx)))
            self.assertEqual(stats["frames"], 60)
            self.assertEqual(stats["lost_frames"], 0)
            self.assertEqual(len(matching_pairs), 60)
            self.assertTrue(all(matching_pairs))
            capture = cv2.VideoCapture(str(output))
            self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), 60)
            self.assertEqual(capture.get(cv2.CAP_PROP_FPS), 60)
            capture.release()


if __name__ == "__main__":
    unittest.main()
