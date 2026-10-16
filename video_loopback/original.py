"""Original MP4 stream transport: no video/audio encoder or pixel resizing.

Faststart remux changes only the container. Receiver decodes exclusively from
radio-reassembled bytes and writes those bytes as the received recording.
"""
from __future__ import annotations
import hashlib
import io
import math
import json
from pathlib import Path
from queue import Queue, Empty, Full
import subprocess
import tempfile
from threading import Event, Thread
import time
import av
import imageio_ffmpeg
import numpy as np
from .modem import PAYLOAD_SIZE, make_packet
from .radio import LoopbackRadio


class QueueReader(io.RawIOBase):
    def __init__(self, queue, cancelled, stop=None):
        self.queue, self.cancelled, self.stop = queue, cancelled, stop
        self.pending = bytearray()
        self.eof = False

    def readable(self):
        return True

    def read(self, size=-1):
        if size == 0:
            return b""
        while not self.pending and not self.eof:
            if self.cancelled.is_set() or (self.stop and self.stop.is_set()):
                return b""
            try:
                item = self.queue.get(timeout=.1)
            except Empty:
                continue
            if item is None:
                self.eof = True
            else:
                self.pending.extend(item)
        count = len(self.pending) if size < 0 else min(size, len(self.pending))
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result


def media_hashes(path):
    result = {}
    with av.open(str(path)) as container:
        for stream in container.streams:
            result[stream.index] = hashlib.sha256()
        for packet in container.demux():
            if packet.size:
                result[packet.stream.index].update(bytes(packet))
    return {str(k): v.hexdigest() for k, v in result.items()}


class PlaybackTimeline:
    """Normalize source PTS; use one nominal interval for missing timestamps."""
    def __init__(self, fps):
        self.interval = 1 / fps
        self.origin = None
        self.previous = None

    def offset(self, frame):
        fallback = 0.0 if self.previous is None else self.previous + self.interval
        timestamp = (float(frame.pts * frame.time_base)
                     if frame.pts is not None and frame.time_base is not None else None)
        if timestamp is not None and math.isfinite(timestamp):
            if self.origin is None:
                self.origin = timestamp - fallback
            value = timestamp - self.origin
            if self.previous is not None and value < self.previous:
                value = fallback
        else:
            value = fallback
        self.previous = value
        return value


def run_original_video(settings, status=print, stop=None, pair_frame_ready=None,
                       constellation_ready=None, channel_ready=None, transfer_progress=None, **unused):
    if settings.playback_mode not in ('buffered', 'realtime', 'download'):
        raise ValueError('Invalid original playback mode')
    download_first = settings.playback_mode == 'download'
    if settings.max_frames != 0:
        raise ValueError("原码流模式保留完整文件，不支持最多帧数限制；请设置为 0")
    if not settings.radio.check_crc:
        raise ValueError("原码流验证必须启用 CRC")
    if settings.output_path.resolve() == settings.input_path.resolve():
        raise ValueError("接收文件不能覆盖原片")
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(settings.input_path)) as container:
        stream = container.streams.video[0]
        width, height, fps = stream.width, stream.height, float(stream.average_rate)
        media_seconds = float(stream.duration * stream.time_base)
        expected_frames = stream.frames
    stats = dict(frames=0, lost_frames=0, packets=0, bytes=0, segments=0,
                 lost_segments=0, late_frames=0, max_lateness_ms=0.,
                 playback_seconds=0., startup_seconds=0., simulation=settings.radio.mode == "simulate",
                 waveform=settings.radio.waveform, radio_mode=settings.radio.mode,
                 modulation=settings.radio.modulation, width=width, height=height,
                 target_fps=fps, media_seconds=media_seconds, source_frames=expected_frames,
                 original_stream=True, transcoded=False, crc_enabled=True,
                 selectively_retransmitted_packets=0, batch_retries=0,
                 changed_segments=0, preview_skipped_late=0)
    stats.update(playback_mode=settings.playback_mode, download_before_playback=download_first)
    stats['ofdm_fft_size']=settings.radio.ofdm_fft_size
    stats['ofdm_training_packets']=settings.radio.ofdm_training_packets
    cancelled = Event()
    stream_queue, frames = Queue(maxsize=8), Queue(maxsize=max(60, int(fps*2)))
    errors = []
    started = time.perf_counter()

    def stopped():
        return cancelled.is_set() or (stop is not None and stop.is_set())

    def check_failure():
        if errors:
            raise RuntimeError(f"原码流播放失败：{errors[0]}") from errors[0]
        if stop is not None and stop.is_set():
            cancelled.set()
            raise InterruptedError("原码流传输已停止")

    def put(queue, item):
        while not stopped():
            try:
                queue.put(item, timeout=.1)
                return True
            except Full:
                continue
        return False

    def next_frame():
        deadline = time.perf_counter() + 30
        while not stopped():
            try:
                return frames.get(timeout=.1)
            except Empty:
                if not settings.radio.ofdm_streaming and time.perf_counter() >= deadline:
                    raise RuntimeError("原码流等待解码帧超时")
        return None

    def decode():
        try:
            source = (str(settings.output_path) if download_first else
                      QueueReader(stream_queue, cancelled, stop))
            with av.open(source, mode="r", format="mp4") as received:
                for frame in received.decode(video=0):
                    if not put(frames, frame):
                        return
        except Exception as exc:
            if not stopped():
                errors.append(exc)
                cancelled.set()
        finally:
            put(frames, None)

    def play():
        try:
            buffered = []
            finished = False
            # Fixed one-second startup buffer, independent of whole file size.
            while len(buffered) < max(1, round(fps)) and not stopped():
                item = next_frame()
                if item is None:
                    finished = True
                    break
                buffered.append(item)
            begin = None
            play_started=None
            timeline = PlaybackTimeline(fps)
            for_initial = iter(buffered)
            exhausted = False
            while not cancelled.is_set() and not (stop and stop.is_set()):
                frame_waited=0.
                if not exhausted:
                    try:
                        frame = next(for_initial)
                    except StopIteration:
                        exhausted = True
                        continue
                else:
                    if finished:
                        break
                    wait_begin=time.perf_counter()
                    frame = next_frame()
                    frame_waited=time.perf_counter()-wait_begin
                if frame is None:
                    break
                if begin is None:
                    begin = time.perf_counter()
                    play_started=begin
                    stats['startup_seconds'] = round(begin-started, 3)
                deadline = begin + timeline.offset(frame)
                if settings.radio.ofdm_streaming and frame_waited>=.1:
                    pause=max(0.,time.perf_counter()-deadline)
                    if pause:
                        begin+=pause
                        deadline+=pause
                        stats['playback_pause_seconds']=round(stats.get('playback_pause_seconds',0.)+pause,3)
                        stats['playback_pause_events']=stats.get('playback_pause_events',0)+1
                delay = deadline-time.perf_counter()
                if delay > 0:
                    while not stopped() and time.perf_counter() < deadline:
                        cancelled.wait(min(.1, max(0, deadline-time.perf_counter())))
                    if stopped():
                        return
                elif -delay > 1/fps:
                    stats['late_frames'] += 1
                    stats['max_lateness_ms'] = max(stats['max_lateness_ms'], round(-delay*1000, 1))
                image = frame.to_ndarray(format="bgr24")
                if pair_frame_ready:
                    # This image is decoded from received bytes. It is shared
                    # only because every byte was compared before enqueueing.
                    pair_frame_ready(stats['frames'], image, image)
                stats['frames'] += 1
            if begin is not None:
                stats['playback_seconds'] = round(time.perf_counter()-play_started, 3)
        except Exception as exc:
            errors.append(exc)
            cancelled.set()

    with tempfile.TemporaryDirectory(prefix="ofdm_original_") as folder:
        transport = Path(folder) / "faststart.mp4"
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(settings.input_path), "-map", "0", "-c", "copy",
                        "-movflags", "+faststart", str(transport)], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stats['source_media_hashes'] = media_hashes(settings.input_path)
        stats['transport_bytes'] = transport.stat().st_size
        if transfer_progress is not None:
            transfer_progress(0,stats['transport_bytes'])
        stats['source_required_mbps'] = round(transport.stat().st_size*8/media_seconds/1e6, 3)
        if channel_ready is not None:
            channel_ready(dict(kind='throughput_reference',mbps=stats['source_required_mbps'],timestamp=time.monotonic()))
        status(f"原码流透传 {width}×{height} @ {fps:.3f} fps，约 {stats['source_required_mbps']:.3f} Mbps；不重编码")
        decoder, player = Thread(target=decode, daemon=True), Thread(target=play, daemon=True)
        if not download_first:
            decoder.start()
            player.start()
        tx_hash, rx_hash = hashlib.sha256(), hashlib.sha256()
        transfer_seconds = 0.
        try:
            if settings.radio.ofdm_streaming:
                from .streaming_radio import StreamingLoopbackRadio
                radio_class=StreamingLoopbackRadio
            else:
                radio_class=LoopbackRadio
            with radio_class(settings.radio) as radio, transport.open('rb') as source, settings.output_path.open('wb') as output:
                radio.set_channel_callback(channel_ready)
                if settings.radio.ofdm_streaming:
                    radio.link_state_callback=lambda waiting,received,total: status(
                        f'等待信号：已收到当前批次 {received}/{total} 包，保留进度；恢复后继续，可随时停止'
                        if waiting else '信号已恢复，继续接收原码流')
                monitor_enabled=(channel_ready is not None and settings.constellation_hz > 0 and
                                 (settings.radio.ofdm_training_packets>0 or
                                  not (settings.radio.ofdm_superframe and settings.radio.batch_packets>64)))
                stats['background_iq_monitor']=monitor_enabled and not settings.radio.ofdm_streaming
                stats['persistent_radio_stream']=settings.radio.ofdm_streaming
                if monitor_enabled:
                    radio.start_constellation_monitor(constellation_ready or (lambda *_: None),
                                                      settings.constellation_hz)
                # Preserve transport throughput. Diagnostics must not force
                # smaller DMA bursts and starve the video playback buffer.
                batch_size = settings.radio.batch_packets
                stats['effective_batch_packets'] = batch_size
                batch = 0
                while not cancelled.is_set() and not (stop and stop.is_set()):
                    raw = source.read(PAYLOAD_SIZE * batch_size)
                    if not raw:
                        break
                    parts = [raw[i:i+PAYLOAD_SIZE] for i in range(0, len(raw), PAYLOAD_SIZE)]
                    packets = [make_packet(batch, i, len(parts), part) for i, part in enumerate(parts)]
                    begin = time.perf_counter()
                    for attempt in range(8):
                        try:
                            received = radio.transceive_batch(packets, constellation_ready, stop)
                            break
                        except RuntimeError:
                            stats['batch_retries'] += 1
                            if attempt == 7:
                                raise
                        finally:
                            stats['selectively_retransmitted_packets'] += radio.last_retransmitted_packets
                    transfer_seconds += time.perf_counter()-begin
                    data = b''.join(p.payload for p in received)
                    if data != raw:
                        raise RuntimeError("原码流载荷不一致，拒绝继续")
                    tx_hash.update(raw)
                    rx_hash.update(data)
                    output.write(data)
                    if not download_first and not put(stream_queue, data):
                        break
                    stats['bytes'] += len(data)
                    if transfer_progress is not None:
                        transfer_progress(stats['bytes'],stats['transport_bytes'])
                    stats['packets'] += len(packets)
                    batch += 1
                    if batch % 8 == 0:
                        status(f"已正确接收 {stats['bytes']}/{stats['transport_bytes']} 字节；显示 {stats['frames']} 帧")
                check_failure()
                if settings.radio.ofdm_streaming:
                    stats['radio_stream_metrics']=dict(radio.stream_metrics)
                if download_first:
                    output.flush()
                    if stats['bytes'] != stats['transport_bytes'] or tx_hash.digest() != rx_hash.digest():
                        raise RuntimeError('完整接收校验失败，拒绝播放')
                    stats['received_media_hashes'] = media_hashes(settings.output_path)
                    if stats['received_media_hashes'] != stats['source_media_hashes']:
                        raise RuntimeError('接收媒体载荷校验失败，拒绝播放')
                    # Playback reads the verified recording on disk, so video
                    # duration and buffering do not consume unbounded RAM.
                    radio.close()
                    stats['transfer_complete_seconds'] = round(time.perf_counter()-started, 3)
                    status('原码流已完整接收并校验，开始按源时间戳播放；画质与源文件一致')
                    decoder.start()
                    player.start()
                else:
                    put(stream_queue, None)
                finish_deadline = time.perf_counter() + max(60, media_seconds + 30)
                while decoder.is_alive() or player.is_alive():
                    check_failure()
                    if time.perf_counter() >= finish_deadline:
                        raise RuntimeError("原码流解码或播放未结束")
                    decoder.join(timeout=.05)
                    player.join(timeout=.05)
                check_failure()
                if settings.radio.ofdm_streaming:
                    stats['radio_stream_metrics']=dict(radio.stream_metrics)
            stats['received_media_hashes'] = media_hashes(settings.output_path)
            stats['media_payload_identical'] = stats['source_media_hashes'] == stats['received_media_hashes']
            stats['transport_sha256'] = tx_hash.hexdigest()
            stats['received_sha256'] = rx_hash.hexdigest()
            stats['complete_transfer'] = stats['bytes'] == stats['transport_bytes']
            stats['lost_frames'] = max(0, expected_frames-stats['frames'])
            stats['net_transfer_mbps'] = round(stats['bytes']*8/max(1e-6, transfer_seconds)/1e6, 3)
            stats['radio_transfer_seconds'] = round(transfer_seconds, 3)
            stats['seconds'] = round(time.perf_counter()-started, 3)
            stats['realtime_playback'] = bool(stats['complete_transfer'] and stats['media_payload_identical']
                                             and stats['lost_frames'] == 0 and stats['max_lateness_ms'] <= 100
                                             and transfer_seconds <= media_seconds)
            stats['smooth_playback'] = bool(stats['complete_transfer'] and stats['media_payload_identical']
                                           and stats['lost_frames'] == 0 and stats['max_lateness_ms'] <= 100)
            return stats
        finally:
            cancelled.set()
            if decoder.ident is not None:
                decoder.join(timeout=2)
            if player.ident is not None:
                player.join(timeout=2)
