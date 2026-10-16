from dataclasses import replace
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
import multiprocessing as mp
import tempfile
import time
import tkinter as tk
import unittest

import av
import numpy as np
from PIL import Image

from video_loopback.gui import App, _put_latest, _run_worker
from video_loopback.modem import parse_packet
from video_loopback.original import PlaybackTimeline, QueueReader
from video_loopback.original import media_hashes
from video_loopback.radio import RadioSettings
from video_loopback.video import VideoSettings, run_video


class FakeRadio:
    last_retransmitted_packets = 0

    def __init__(self, *_): pass
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def set_channel_callback(self, *_): pass
    def close(self): pass
    def transceive_batch(self, packets, *_):
        return [parse_packet(packet) for packet in packets]


class OriginalRegressionTests(unittest.TestCase):
    def test_streaming_playback_pauses_and_resumes_without_catching_up(self):
        source=Path(self.folder.name)/'pause_source.mp4'
        with av.open(str(source),'w') as output:
            stream=output.add_stream('libx264',rate=10)
            stream.width=stream.height=32
            stream.pix_fmt='yuv420p'
            for _ in range(30):
                frame=av.VideoFrame.from_ndarray(np.zeros((32,32,3),np.uint8),format='rgb24')
                for packet in stream.encode(frame): output.mux(packet)
            for packet in stream.encode(): output.mux(packet)
        with av.open(str(source)) as container:
            decoded=list(container.decode(video=0))
        depleted=Event()
        class SlowDecoder:
            def __enter__(self): return self
            def __exit__(self,*_): pass
            def decode(self,**_):
                for index,frame in enumerate(decoded):
                    if index==12:
                        if not depleted.wait(3): raise RuntimeError('Playback did not consume its buffer')
                        time.sleep(.4)
                    yield frame
        real_open=av.open
        def open_received(path,*args,**kwargs):
            return SlowDecoder() if isinstance(path,QueueReader) else real_open(path,*args,**kwargs)
        radio=FakeRadio()
        radio.stream_metrics={}
        settings=replace(self.settings,input_path=source,
            radio=replace(self.settings.radio,ofdm_streaming=True))
        displayed=[]
        def preview(*_):
            displayed.append(time.monotonic())
            if len(displayed)==12: depleted.set()
        with patch('video_loopback.streaming_radio.StreamingLoopbackRadio',return_value=radio),\
             patch('video_loopback.original.av.open',side_effect=open_received):
            stats=run_video(settings,status=lambda _:None,
                pair_frame_ready=preview)
        self.assertEqual(stats['frames'],30)
        self.assertEqual(stats['late_frames'],0)
        self.assertGreater(stats['playback_pause_seconds'],.2)
        self.assertGreater(displayed[12]-displayed[11],.3)
        self.assertGreater(displayed[13]-displayed[12],.07,'Resume must preserve frame pacing')

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        source = Path(self.folder.name) / 'source.mp4'
        with av.open(str(source), 'w') as output:
            stream = output.add_stream('libx264', rate=10)
            stream.width, stream.height = 128, 72
            stream.pix_fmt = 'yuv420p'
            stream.time_base = stream.codec_context.time_base = Fraction(1, 10)
            rng = np.random.default_rng(42)
            for pts in (0, 1, 2, 5, 6, 9):
                frame = av.VideoFrame.from_ndarray(
                    rng.integers(0, 256, (72, 128, 3), dtype=np.uint8), format='rgb24')
                frame.pts, frame.time_base = pts, Fraction(1, 10)
                for packet in stream.encode(frame): output.mux(packet)
            for packet in stream.encode(): output.mux(packet)
        self.settings = VideoSettings(source, Path(self.folder.name) / 'received.mp4',
                                      RadioSettings(mode='simulate', batch_packets=1),
                                      transport='original')

    def test_frame_limit_rejected_before_creating_output(self):
        with self.assertRaisesRegex(ValueError, '最多帧数'):
            run_video(replace(self.settings, max_frames=1), status=lambda _: None)
        self.assertFalse(self.settings.output_path.exists())

    def test_download_plays_only_complete_verified_original_file(self):
        radio=FakeRadio()
        radio.closed=False
        def close(): radio.closed=True
        radio.close=close
        displayed=[]
        expected=media_hashes(self.settings.input_path)
        def preview(*_):
            self.assertTrue(radio.closed, 'RF must finish before playback begins')
            if not displayed:
                self.assertEqual(media_hashes(self.settings.output_path), expected)
            displayed.append(time.monotonic())
        with patch('video_loopback.original.LoopbackRadio', return_value=radio):
            stats=run_video(replace(self.settings,playback_mode='download'),
                            status=lambda _:None,pair_frame_ready=preview)
        self.assertEqual(stats['frames'],6)
        self.assertTrue(stats['complete_transfer'])
        self.assertTrue(stats['media_payload_identical'])
        self.assertFalse(stats['transcoded'])
        self.assertTrue(stats['smooth_playback'])
        self.assertGreaterEqual(stats['startup_seconds']+.01,stats['transfer_complete_seconds'])

    def test_download_transfer_failure_does_not_start_playback(self):
        radio=FakeRadio()
        def fail(*_): raise RuntimeError('injected transfer failure')
        radio.transceive_batch=fail
        displayed=[]
        with patch('video_loopback.original.LoopbackRadio', return_value=radio):
            with self.assertRaisesRegex(RuntimeError,'injected transfer failure'):
                run_video(replace(self.settings,playback_mode='download'),status=lambda _:None,
                          pair_frame_ready=lambda *_:displayed.append(True))
        self.assertEqual(displayed,[])

    def test_download_stop_during_transfer_cleans_up_unstarted_threads(self):
        radio,stop=FakeRadio(),Event()
        transfer=radio.transceive_batch
        def cancel(*args):
            result=transfer(*args)
            stop.set()
            return result
        radio.transceive_batch=cancel
        with patch('video_loopback.original.LoopbackRadio', return_value=radio):
            with self.assertRaises(InterruptedError):
                run_video(replace(self.settings,playback_mode='download'),stop=stop,status=lambda _:None)

    def test_user_stop_is_not_reported_as_worker_failure(self):
        stop, events = Event(), Queue()
        stop.set()
        with patch('video_loopback.gui.run_video', side_effect=InterruptedError('Radio transfer cancelled')):
            _run_worker(self.settings, events, stop, None)
        messages = []
        while not events.empty():
            messages.append(events.get_nowait())
        self.assertTrue(any(kind == 'cancelled' for kind, _ in messages))
        self.assertFalse(any(kind == 'error' for kind, _ in messages))

    def test_decoder_failure_propagates_without_external_stop(self):
        original_open = av.open
        def fail_decoder(source, *args, **kwargs):
            if isinstance(source, QueueReader):
                raise RuntimeError('injected decoder failure')
            return original_open(source, *args, **kwargs)
        stop, result = Event(), []
        def run():
            try:
                run_video(self.settings, stop=stop, status=lambda _: None)
            except Exception as exc:
                result.append(exc)
        with patch('video_loopback.original.LoopbackRadio', FakeRadio), \
                patch('video_loopback.original.av.open', fail_decoder):
            worker = Thread(target=run, daemon=True)
            worker.start()
            try:
                worker.join(5)
                self.assertFalse(worker.is_alive(), 'Decoder failure left producer blocked')
                self.assertEqual(len(result), 1)
                self.assertIn('injected decoder failure', str(result[0]))
            finally:
                stop.set()
                worker.join(5)

    def test_vfr_preview_follows_source_timestamps(self):
        displayed = []
        with av.open(str(self.settings.input_path)) as source:
            timestamps = [float(f.pts * f.time_base) for f in source.decode(video=0)]
        with patch('video_loopback.original.LoopbackRadio', FakeRadio):
            stats = run_video(self.settings, status=lambda _: None,
                              pair_frame_ready=lambda *_: displayed.append(time.monotonic()))
        self.assertTrue(stats['media_payload_identical'])
        self.assertEqual(len(displayed), len(timestamps))
        for actual, expected in zip(displayed, timestamps):
            self.assertAlmostEqual(actual-displayed[0], expected-timestamps[0], delta=.15)

    def test_stop_during_buffered_playback_exits_promptly(self):
        stop = Event()
        started = time.monotonic()
        with patch('video_loopback.original.LoopbackRadio', FakeRadio):
            with self.assertRaises(InterruptedError):
                run_video(self.settings, stop=stop, status=lambda _: None,
                          pair_frame_ready=lambda *_: stop.set())
        self.assertLess(time.monotonic()-started, 5)

    def test_timeline_missing_pts_preserves_later_source_gaps(self):
        timeline = PlaybackTimeline(10)
        def frame(pts): return SimpleNamespace(pts=pts, time_base=Fraction(1, 10))
        self.assertEqual(timeline.offset(frame(100)), 0)
        self.assertAlmostEqual(timeline.offset(frame(None)), .1)
        self.assertAlmostEqual(timeline.offset(frame(105)), .5)

    def test_spawned_worker_delivers_preview_and_completion(self):
        context = mp.get_context('spawn')
        events, previews, stop = context.Queue(), context.Queue(maxsize=1), context.Event()
        settings = replace(self.settings, radio=RadioSettings(
            mode='simulate', waveform='ofdm', modulation='16qam',
            ofdm_superframe=True, batch_packets=64))
        worker = context.Process(target=_run_worker,
                                 args=(settings, events, stop, None, None, None, previews))
        received, failures, done = [], [], []
        worker.start()
        try:
            deadline = time.monotonic() + 15
            while not done and time.monotonic() < deadline:
                try:
                    received.append(previews.get_nowait()[0])
                except Empty:
                    pass
                try:
                    kind, value = events.get(timeout=.02)
                    if kind == 'error': failures.append(value)
                    if kind == 'done': done.append(value)
                except Empty:
                    pass
            try:
                received.append(previews.get_nowait()[0])
            except Empty:
                pass
            self.assertEqual(failures, [])
            self.assertTrue(done, 'Worker failed to publish completion')
            self.assertEqual(done[0]['frames'], 6)
            self.assertEqual(received[-1], 5)
            worker.join(3)
            self.assertFalse(worker.is_alive())
        finally:
            stop.set()
            if worker.is_alive():
                worker.terminate()
                worker.join(3)
            events.close()
            previews.close()


class PreviewRegressionTests(unittest.TestCase):
    def test_slow_consumer_has_bounded_preview_backlog(self):
        queue = mp.get_context('spawn').Queue(maxsize=1)
        try:
            for i in range(2000):
                _put_latest(queue, (i, b'x'*4096))
            first = queue.get(timeout=2)
            self.assertGreater(first[0], 0)
            # Once the feeder is drained, the next frame is available normally.
            _put_latest(queue, (2000, b'final'))
            self.assertEqual(queue.get(timeout=2), (2000, b'final'))
        finally:
            queue.close()
            queue.join_thread()

    def test_shared_pixels_fit_each_preview_canvas(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = App.__new__(App)
            app.tx_preview, app.rx_preview = tk.Canvas(root), tk.Canvas(root)
            app.tx_preview.winfo_width = lambda: 800
            app.tx_preview.winfo_height = lambda: 450
            app.rx_preview.winfo_width = lambda: 320
            app.rx_preview.winfo_height = lambda: 240
            app.tx_photo = app.rx_photo = None
            app._preview_images = {}
            app.active_original = app.active_crc = True
            app.finished = True
            app.tx_caption, app.rx_caption = tk.StringVar(root), tk.StringVar(root)
            app.ui_pair_seconds = app.ui_pair_calls = 0
            app._display_times, app._display_frame_numbers, app._display_ages = [], [], []
            encoded = BytesIO()
            Image.new('RGB', (640, 360), 'red').save(encoded, format='JPEG')
            app._show_pair(0, encoded.getvalue(), True, True)
            self.assertEqual((app.tx_photo.width(), app.tx_photo.height()), (800, 450))
            self.assertEqual((app.rx_photo.width(), app.rx_photo.height()), (320, 180))
            previous = app.rx_photo
            app._show_pair(1, encoded.getvalue(), True, True)
            self.assertIs(app.rx_photo, previous)
            self.assertEqual(app.rx_preview.coords('video'), [160, 120])
            app.rx_preview.winfo_width=lambda:800
            app.rx_preview.winfo_height=lambda:450
            app._show_pair(2,encoded.getvalue(),True,True)
            self.assertIs(app.tx_photo,app.rx_photo)
            self.assertEqual(app.rx_preview.coords('video'),[400,225])
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
