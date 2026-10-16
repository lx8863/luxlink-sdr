"""Paced, segmented H.264 transport for smooth video playback over the RF link."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import csv
from collections import deque
from dataclasses import replace
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Callable
import math
import subprocess
import tempfile
import time

import cv2
import imageio_ffmpeg
import numpy as np

from .modem import PAYLOAD_SIZE, make_packet
from .radio import LoopbackRadio
from .decoder import ContinuousDecoder, access_unit_count

_FFMPEG_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@contextmanager
def _encode_segments(ffmpeg: str, source: Path, folder: Path, width: int,
                     height: int, fps: int, bitrate: int, max_frames: int,
                     segment_seconds: float = 1.0, stop: Event | None = None,
                     encoder_preset: str = "veryfast"):
    """Yield finalized segments while FFmpeg is still encoding the input.

    FFmpeg paces file input in real time. Its CSV segment list reports closed
    files, so consumers never read a half-written H.264 segment.
    """
    segment_frames = max(1, round(segment_seconds * fps))
    segment_seconds = segment_frames / fps
    pattern = str(folder / "segment_%05d.h264")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-re", "-i", str(source),
               "-an", "-vf", f"fps={fps},scale={width}:{height}:flags=area",
               "-c:v", "libx264", "-preset", encoder_preset, "-tune", "zerolatency",
               "-b:v", str(bitrate), "-maxrate", str(bitrate),
               "-bufsize", str(max(bitrate // fps, round(bitrate * segment_seconds))),
               "-g", str(segment_frames), "-keyint_min", str(segment_frames), "-sc_threshold", "0", "-bf", "0",
               "-x264-params", "repeat-headers=1:aud=1", "-pix_fmt", "yuv420p"]
    if max_frames:
        command += ["-frames:v", str(max_frames)]
    command += ["-f", "segment", "-segment_time", str(segment_seconds),
                "-segment_format", "h264", "-segment_list", "pipe:1",
                "-segment_list_type", "csv", "-reset_timestamps", "1", pattern]
    completed: Queue = Queue()
    with (folder / "encoder.log").open("w+", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors,
                                   text=True, encoding="utf-8", creationflags=_FFMPEG_FLAGS)

        def collect() -> None:
            try:
                for row in csv.reader(process.stdout):
                    if row:
                        completed.put(folder / Path(row[0]).name)
                code = process.wait()
                if code:
                    errors.seek(0)
                    completed.put(RuntimeError(f"H.264 encoding failed: {errors.read()[-600:]}"))
            except Exception as exc:
                completed.put(exc)
            finally:
                completed.put(None)

        reader = Thread(target=collect, daemon=True)
        reader.start()

        def segments():
            while stop is None or not stop.is_set():
                try:
                    item = completed.get(timeout=0.2)
                except Empty:
                    continue
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item

        try:
            yield segments()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            reader.join(timeout=3)
            process.stdout.close()


def _decode_segment(ffmpeg: str, data: bytes, width: int, height: int) -> np.ndarray:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    result = subprocess.run(command, input=data, capture_output=True,
                            creationflags=_FFMPEG_FLAGS)
    frame_bytes = width * height * 3
    if result.returncode or len(result.stdout) % frame_bytes:
        raise RuntimeError(f"H.264 segment decode failed: {result.stderr[-300:]!r}")
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(-1, height, width, 3).copy()


def run_stream_video(settings, status: Callable[[str], None] = print,
                     frame_ready: Callable[[np.ndarray], None] | None = None,
                     stop: Event | None = None,
                     tx_frame_ready: Callable[[int, np.ndarray], None] | None = None,
                     rx_frame_ready: Callable[[int, np.ndarray | None], None] | None = None,
                     constellation_ready: Callable[[np.ndarray, np.ndarray | None], None] | None = None,
                     pair_frame_ready: Callable[[int, np.ndarray, np.ndarray | None], None] | None = None,
                     channel_ready=None) -> dict:
    if settings.playback_mode not in ("buffered", "realtime"):
        raise ValueError("Playback mode must be buffered or realtime")
    if settings.playback_mode == "realtime":
        settings = replace(settings, segment_seconds=min(settings.segment_seconds, 0.5),
                           playback_buffer_segments=1)
    if not settings.input_path.is_file():
        raise FileNotFoundError(settings.input_path)
    if settings.input_path.resolve() == settings.output_path.resolve():
        raise ValueError("Output video must not overwrite the input video")
    fps = round(settings.target_fps)
    if fps <= 0 or abs(fps - settings.target_fps) > 0.01:
        raise ValueError("Stream mode requires a positive integer target fps")
    if (settings.width < 32 or settings.height < 32 or settings.width % 2 or
            settings.height % 2 or settings.stream_bitrate < 10_000):
        raise ValueError("Invalid stream size or bitrate")
    if settings.max_frames < 0:
        raise ValueError("Maximum frame count must be nonnegative")
    if not math.isfinite(settings.segment_seconds) or not 1 / fps <= settings.segment_seconds <= 5:
        raise ValueError("Segment duration must be between one frame and five seconds")
    if not 1 <= settings.playback_buffer_segments <= 8:
        raise ValueError("Playback buffer must be 1..8 segments")
    if settings.encoder_preset not in ("veryfast", "fast", "medium", "slow"):
        raise ValueError("Invalid H.264 encoder preset")

    source = cv2.VideoCapture(str(settings.input_path))
    if not source.isOpened():
        raise RuntimeError(f"OpenCV could not read {settings.input_path}")
    source_fps = source.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(source_fps) or source_fps <= 0:
        source.release()
        raise ValueError("Input video has no valid frame rate")
    source.release()
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(settings.output_path), cv2.VideoWriter_fourcc(*"mp4v"),
                             fps, (settings.width, settings.height))
    if not writer.isOpened():
        source.release()
        raise RuntimeError(f"Could not create {settings.output_path}")

    started = time.monotonic()
    stats = {"frames": 0, "lost_frames": 0, "packets": 0, "bytes": 0,
             "segments": 0, "lost_segments": 0, "seconds": 0.0, "rf_seconds": 0.0,
             "late_frames": 0, "max_lateness_ms": 0.0, "packet_retries": 0,
             "startup_seconds": 0.0, "playback_seconds": 0.0}
    stats.update({"radio_transfer_seconds": 0.0, "batch_retries": 0,
                  "batch_packets": settings.radio.batch_packets,
                  "sample_rate": settings.radio.sample_rate,
                  "samples_per_symbol": settings.radio.samples_per_symbol,
                  "segment_seconds": round(max(1, round(settings.segment_seconds * fps)) / fps, 4),
                  "target_bitrate_bps": settings.stream_bitrate,
                  "simulation": settings.radio.mode == "simulate"})
    stats["waveform"] = settings.radio.waveform
    stats["width"] = settings.width
    stats["height"] = settings.height
    stats["target_fps"] = fps
    stats["radio_mode"] = settings.radio.mode
    stats["modulation"] = settings.radio.modulation
    stats["uri"] = settings.radio.uri
    stats["playback_buffer_segments"] = settings.playback_buffer_segments
    stats["playback_mode"] = settings.playback_mode
    stats["preview_skipped_late"] = 0
    stats["selectively_retransmitted_packets"] = 0
    stats.update(decoder_sessions=0, decoder_fallbacks=0, segment_decode_seconds=0.0)
    stats.update(crc_enabled=settings.radio.check_crc, unverified_packets=0,
                 changed_segments=0, encoder_preset=settings.encoder_preset)
    queue: Queue[tuple[list[np.ndarray], np.ndarray | None] | None] = Queue(maxsize=2)
    decode_queue: Queue = Queue(maxsize=2)
    record_queue: Queue = Queue(maxsize=8)
    playback_errors: list[Exception] = []
    shutdown = Event()
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def playback() -> None:
        index = 0
        playback_start: float | None = None
        try:
            buffered = deque()
            finished = False
            while len(buffered) < settings.playback_buffer_segments and not shutdown.is_set():
                if stop is not None and stop.is_set():
                    return
                try:
                    item = queue.get(timeout=0.2)
                except Empty:
                    continue
                if item is None:
                    finished = True
                    break
                buffered.append(item)
            while not shutdown.is_set():
                try:
                    item = buffered.popleft() if buffered else (None if finished else queue.get(timeout=0.2))
                except Empty:
                    if shutdown.is_set() or (stop is not None and stop.is_set()):
                        break
                    continue
                if item is None:
                    break
                tx_frames, rx_frames = item
                for local_index, tx_frame in enumerate(tx_frames):
                    if shutdown.is_set() or (stop is not None and stop.is_set()):
                        return
                    if playback_start is None:
                        playback_start = time.monotonic()
                        if settings.playback_mode == "realtime":
                            playback_start += min(0.25, settings.segment_seconds / 2)
                        stats["startup_seconds"] = round(playback_start - started, 2)
                    deadline = playback_start + index / fps
                    delay = deadline - time.monotonic()
                    if delay > 0:
                        if shutdown.wait(delay):
                            return
                    elif delay < -1 / fps:
                        stats["late_frames"] += 1
                        stats["max_lateness_ms"] = max(
                            stats["max_lateness_ms"], round(-delay * 1000, 1))
                    received = None if rx_frames is None else rx_frames[local_index]
                    show = (settings.playback_mode != "realtime" or delay >= -0.1 or
                            local_index == len(tx_frames) - 1)
                    if not show:
                        stats["preview_skipped_late"] += 1
                    if show and tx_frame_ready is not None:
                        tx_frame_ready(index, tx_frame)
                    if show and rx_frame_ready is not None:
                        rx_frame_ready(index, received)
                    if show and pair_frame_ready is not None:
                        pair_frame_ready(index, tx_frame, received)
                    if received is None:
                        enqueue_record(np.zeros_like(tx_frame))
                        stats["lost_frames"] += 1
                    else:
                        enqueue_record(received)
                        stats["frames"] += 1
                        if frame_ready is not None:
                            frame_ready(received)
                    index += 1
            if playback_start is not None:
                stats["playback_seconds"] = round(time.monotonic() - playback_start, 2)
        except Exception as exc:
            playback_errors.append(exc)

    def enqueue(item: tuple[list[np.ndarray], np.ndarray | None] | None) -> None:
        while True:
            if playback_errors:
                raise RuntimeError("Video playback failed") from playback_errors[0]
            if shutdown.is_set() or (stop is not None and stop.is_set()):
                return
            try:
                queue.put(item, timeout=0.2)
                return
            except Full:
                if stop is not None and stop.is_set():
                    return

    def enqueue_record(frame) -> None:
        while not shutdown.is_set():
            if playback_errors:
                raise RuntimeError("Video recording failed") from playback_errors[0]
            try:
                record_queue.put(frame, timeout=0.1)
                return
            except Full:
                continue

    def record_video() -> None:
        try:
            while not shutdown.is_set():
                try:
                    frame = record_queue.get(timeout=0.2)
                except Empty:
                    continue
                if frame is None:
                    return
                writer.write(frame)
        except Exception as exc:
            playback_errors.append(exc)

    def decode_playback() -> None:
        continuous = None
        try:
            while not shutdown.is_set() and (stop is None or not stop.is_set()):
                try:
                    item = decode_queue.get(timeout=0.2)
                except Empty:
                    continue
                if item is None:
                    return
                encoded, received_bytes = item
                decode_started = time.perf_counter()
                count = access_unit_count(encoded)
                if count:
                    if continuous is None:
                        continuous = ContinuousDecoder(ffmpeg, settings.width, settings.height, fps)
                        stats["decoder_sessions"] += 1
                    try:
                        tx_frames = continuous.decode(encoded, count)
                    except RuntimeError:
                        continuous.close()
                        continuous = None
                        stats["decoder_fallbacks"] += 1
                        tx_frames = _decode_segment(ffmpeg, encoded, settings.width, settings.height)
                else:
                    tx_frames = _decode_segment(ffmpeg, encoded, settings.width, settings.height)
                stats["segment_decode_seconds"] += time.perf_counter() - decode_started
                # Byte equality was checked on the receive path. Decode once
                # and reuse identical pixels for the two synchronized previews.
                rx_frames = tx_frames if received_bytes is not None else None
                if received_bytes is not None and received_bytes != encoded:
                    try:
                        rx_frames = _decode_segment(ffmpeg, received_bytes, settings.width, settings.height)
                        if len(rx_frames) != len(tx_frames):
                            raise RuntimeError("Received segment has missing frames")
                    except RuntimeError:
                        rx_frames = None
                        stats["lost_segments"] += 1
                        stats["segments"] -= 1
                enqueue((list(tx_frames), rx_frames))
        except Exception as exc:
            playback_errors.append(exc)
        finally:
            if continuous is not None:
                continuous.close()

    def enqueue_decode(item) -> None:
        while not shutdown.is_set() and (stop is None or not stop.is_set()):
            if playback_errors:
                raise RuntimeError("Video decode or playback failed") from playback_errors[0]
            try:
                decode_queue.put(item, timeout=0.2)
                return
            except Full:
                continue

    player = Thread(target=playback, daemon=True)
    decoder = Thread(target=decode_playback, daemon=True)
    recorder = Thread(target=record_video, daemon=True)
    try:
        with tempfile.TemporaryDirectory(prefix="luxlink_stream_") as temp:
            status(f"Encoding {settings.width}x{settings.height}, {fps} fps, "
                   f"{settings.stream_bitrate // 1000} kbit/s; "
                   f"{stats['segment_seconds']} second segments, "
                   f"{settings.radio.batch_packets} packets/burst")
            player.start()
            decoder.start()
            recorder.start()
            radio_started = time.monotonic()
            with LoopbackRadio(settings.radio) as radio, _encode_segments(
                    ffmpeg, settings.input_path, Path(temp), settings.width, settings.height,
                    fps, settings.stream_bitrate, settings.max_frames,
                    settings.segment_seconds, stop, settings.encoder_preset) as segments:
                radio.set_channel_callback(channel_ready)
                if constellation_ready is not None and settings.constellation_hz > 0:
                    radio.start_constellation_monitor(constellation_ready, settings.constellation_hz)
                for segment_no, path in enumerate(segments):
                    if stop is not None and stop.is_set():
                        break
                    encoded = path.read_bytes()
                    chunks = math.ceil(len(encoded) / PAYLOAD_SIZE)
                    if chunks > 65535:
                        raise ValueError("Encoded segment exceeds packet index capacity")
                    parts: list[bytes] = []
                    try:
                        for first in range(0, chunks, settings.radio.batch_packets):
                            if stop is not None and stop.is_set():
                                raise InterruptedError("Video transfer cancelled")
                            raw_packets = [make_packet(segment_no, chunk_no, chunks,
                                           encoded[chunk_no * PAYLOAD_SIZE:(chunk_no + 1) * PAYLOAD_SIZE])
                                           for chunk_no in range(first, min(chunks, first + settings.radio.batch_packets))]
                            transfer_started = time.monotonic()
                            for attempt in range(3):
                                try:
                                    packets = radio.transceive_batch(
                                        raw_packets, constellation_ready, stop)
                                    break
                                except RuntimeError:
                                    stats["batch_retries"] += 1
                                    stats["packet_retries"] += len(raw_packets)
                                    if attempt == 2:
                                        raise
                                finally:
                                    stats["selectively_retransmitted_packets"] += radio.last_retransmitted_packets
                                    stats["radio_transfer_seconds"] += time.monotonic() - transfer_started
                                    transfer_started = time.monotonic()
                            parts.extend(packet.payload for packet in packets)
                            stats["packets"] += len(packets)
                            stats["unverified_packets"] += sum(packet.crc_valid is None for packet in packets)
                        received_bytes = b"".join(parts)
                        if received_bytes != encoded:
                            stats["changed_segments"] += 1
                            if settings.radio.check_crc:
                                raise RuntimeError("Received H.264 bytes differ from transmitted bytes")
                        stats["segments"] += 1
                        stats["bytes"] += len(encoded)
                        status(f"Segment {segment_no + 1} received: "
                               f"{len(received_bytes)} bytes, {chunks} packets")
                    except RuntimeError as exc:
                        received_bytes = None
                        stats["lost_segments"] += 1
                        status(f"Segment {segment_no + 1} lost: {exc}")

                    enqueue_decode((encoded, received_bytes))
                    path.unlink(missing_ok=True)
            stats["rf_seconds"] = round(time.monotonic() - radio_started, 2)
            enqueue_decode(None)
            decoder.join()
            enqueue(None)
            player.join()
            enqueue_record(None)
            recorder.join()
            if playback_errors:
                raise RuntimeError("Video playback failed") from playback_errors[0]
    except InterruptedError:
        status("Video transfer stopped")
    finally:
        shutdown.set()
        if decoder.is_alive():
            decoder.join(timeout=5)
        if player.is_alive():
            player.join(timeout=5)
        if recorder.is_alive():
            recorder.join(timeout=5)
        source.release()
        writer.release()
        stats["seconds"] = round(time.monotonic() - started, 2)
        transfer_seconds = stats["radio_transfer_seconds"]
        stats["net_transfer_mbps"] = round(stats["bytes"] * 8 / transfer_seconds / 1e6, 3) if transfer_seconds else 0
        stats["radio_transfer_seconds"] = round(transfer_seconds, 3)
        stats["media_seconds"] = round((stats["frames"] + stats["lost_frames"]) / fps, 3)
        stats["received_media_mbps"] = (round(stats["bytes"] * 8 / stats["media_seconds"] / 1e6, 3)
                                        if stats["media_seconds"] else 0)
        stats["realtime_playback"] = bool(stats["frames"] and not stats["lost_frames"] and
                                          stats["max_lateness_ms"] <= 100)
    status(f"Finished stream: {stats['frames']} frames received, {stats['lost_frames']} lost; "
           f"{settings.output_path}")
    return stats
