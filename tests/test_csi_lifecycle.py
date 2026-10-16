from pathlib import Path
from queue import Queue
from threading import Event
from unittest.mock import Mock, patch
import csv
import gc
import json
import tempfile
import time
import tkinter as tk
import unittest

from video_loopback import ofdm
from video_loopback.csi_capture import collect_csi
from video_loopback.gui import App, _run_csi_worker
from video_loopback.radio import RadioSettings


class CsiAcquisitionTests(unittest.TestCase):
    def test_valid_training_survives_crc_failure_from_start(self):
        modulate = ofdm.modulate
        def corrupt_payload(*args, **kwargs):
            wave = modulate(*args, **kwargs)
            wave[128+2*ofdm.STRIDE:] = 0
            return wave
        stop, samples = Event(), []
        def publish(sample):
            samples.append(sample)
            if len(samples) == 2:
                stop.set()
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.ofdm.modulate', corrupt_payload):
            path = Path(folder)/'nested'/'csi.csv'
            stats = collect_csi(RadioSettings(mode='simulate'), path, seconds=2,
                                stop=stop, callback=publish)
            self.assertEqual(stats['csi_samples'], 2)
            self.assertEqual(stats['invalid_reads'], 0)
            self.assertEqual(stats['payload_crc_failed_reads'], 2)
            self.assertEqual(stats['packets'], 0)
            self.assertTrue(stats['stopped'])
            with path.open(encoding='utf-8-sig', newline='') as file:
                self.assertEqual(len(list(csv.DictReader(file))), 400)

    def test_no_training_is_distinct_from_payload_crc_failure(self):
        radio = Mock()
        radio.__enter__ = Mock(return_value=radio)
        radio.__exit__ = Mock(return_value=False)
        radio.capture_csi.return_value = []
        stop = Event()
        radio.capture_csi.side_effect = lambda: (stop.set() or [])
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.csi_capture.LoopbackRadio', return_value=radio):
            stats = collect_csi(RadioSettings(mode='simulate'), Path(folder)/'csi.csv', stop=stop)
        self.assertEqual(stats['invalid_reads'], 1)
        self.assertEqual(stats['payload_crc_failed_reads'], 0)
        self.assertEqual(stats['csi_samples'], 0)

    def test_pre_cancelled_acquisition_never_opens_radio(self):
        stop = Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.csi_capture.LoopbackRadio') as radio:
            stats = collect_csi(RadioSettings(mode='simulate'), Path(folder)/'csi.csv', stop=stop)
        radio.assert_not_called()
        self.assertEqual(stats['capture_calls'], 0)

    def test_invalid_duration_rejected_before_creating_files(self):
        with tempfile.TemporaryDirectory() as folder:
            for seconds in (-1, float('inf'), float('nan')):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    collect_csi(RadioSettings(mode='simulate'), Path(folder)/'csi.csv', seconds=seconds)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_disconnect_reports_failure_and_closes_log(self):
        radio = Mock()
        radio.__enter__ = Mock(return_value=radio)
        radio.__exit__ = Mock(return_value=False)
        radio.capture_csi.side_effect = OSError('USB disconnected')
        events = Queue()
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.csi_capture.LoopbackRadio', return_value=radio):
            path = Path(folder)/'csi.csv'
            _run_csi_worker(RadioSettings(mode='simulate'), events, Event(), path, Queue(), 1)
            messages = []
            while not events.empty(): messages.append(events.get_nowait())
            self.assertIn(('error', 'USB disconnected'), messages)
            self.assertEqual(messages[-1], ('done', None))
            # Windows would reject this rename if the recording handle leaked.
            path.rename(path.with_name('closed.csv'))
        radio.__exit__.assert_called_once()


class CsiGuiLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.app.mode.set('simulate')

    def tearDown(self):
        self.app.worker = None
        self.app._close()
        for name in ('events', 'preview_events', 'chart_events', 'channel_events'):
            getattr(self.app, name).close()
        self.app = self.root = None
        gc.collect()  # Do not let later decoder threads finalize Tk objects.

    def test_csi_ignores_video_fields_and_resets_old_session(self):
        app = self.app
        for name in ('fps', 'width', 'height', 'stream_bitrate', 'sps', 'batch_packets'):
            getattr(app, name).set('invalid video setting')
        app.ui_pair_seconds = 99
        old_events, old_channel = app.events, app.channel_events
        app._latest_chart_sample = ('old', 'old', 0)
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.gui.Path.cwd', return_value=Path(folder)), patch.object(
                app.context, 'Process') as process, patch('video_loopback.gui.messagebox.showerror') as error:
            app._start_csi()
            error.assert_not_called()
            process.return_value.start.assert_called_once()
            settings = process.call_args.kwargs['args'][0]
            self.assertEqual(settings.waveform, 'ofdm')
            self.assertEqual(settings.modulation, 'qpsk')
            self.assertEqual(settings.batch_packets, 1)
            self.assertIsNot(app.events, old_events)
            self.assertIsNot(app.channel_events, old_channel)
            self.assertEqual(app.ui_pair_seconds, 0)
            self.assertIsNone(app._latest_chart_sample)

    def test_spawn_failure_restores_start_buttons(self):
        app = self.app
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.gui.Path.cwd', return_value=Path(folder)), patch.object(
                app.context, 'Process') as process:
            process.return_value.start.side_effect = OSError('spawn failed')
            app._start_csi()
            self.assertTrue(app.finished)
            self.assertIsNone(app.worker)
            self.assertEqual(str(app.csi_start_button['state']), 'normal')
            self.assertIn('spawn failed', app.summary.get())

    def test_unexpected_process_exit_restores_controls(self):
        app = self.app
        app.finished = False
        app.worker = Mock(exitcode=1)
        app.worker.is_alive.return_value = False
        with patch('video_loopback.gui.time.monotonic', return_value=10) as clock:
            app._check_worker_exit()
            clock.return_value=12
            app._check_worker_exit()
        self.assertTrue(app.finished)
        self.assertIn('意外退出', app.summary.get())

    def test_two_real_spawned_acquisitions_finish_and_write_separate_reports(self):
        app = self.app
        app.csi_duration = .3
        with tempfile.TemporaryDirectory() as folder, patch(
                'video_loopback.gui.Path.cwd', return_value=Path(folder)), patch(
                'video_loopback.gui.messagebox.showerror') as error:
            try:
                reports = []
                for _ in range(2):
                    app._start_csi()
                    deadline = time.monotonic()+10
                    while time.monotonic()<deadline:
                        app._poll()
                        if app.finished and not app.worker.is_alive():
                            break
                        time.sleep(.02)
                    self.assertTrue(app.finished)
                    self.assertFalse(app.worker.is_alive())
                    self.assertEqual(str(app.csi_start_button['state']), 'normal')
                    report = json.loads(app.csi_report_path.read_text(encoding='utf-8'))
                    self.assertGreater(report['csi_samples'], 0)
                    self.assertEqual(report['invalid_reads'], 0)
                    reports.append(app.csi_report_path)
                self.assertNotEqual(*reports)
                error.assert_not_called()
            finally:
                app.stop.set()
                if app.worker is not None and app.worker.is_alive():
                    app.worker.terminate()
                    app.worker.join(3)


if __name__ == '__main__':
    unittest.main()
