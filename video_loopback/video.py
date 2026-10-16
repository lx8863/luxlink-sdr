"""Read a video, send its JPEG frames through QPSK, and save received frames."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable
import math
import time

import cv2
import numpy as np

from .modem import PAYLOAD_SIZE, make_packet
from .radio import LoopbackRadio, RadioSettings


@dataclass(frozen=True)
class VideoSettings:
    input_path: Path
    output_path: Path
    radio: RadioSettings
    width: int = 160
    height: int = 90
    target_fps: float = 1.0
    jpeg_quality: int = 35
    max_frames: int = 0
    transport: str = "frame"
    stream_bitrate: int = 30_000
    segment_seconds: float = 1.0
    playback_buffer_segments: int = 1
    constellation_hz: float = 0
    encoder_preset: str = "veryfast"
    playback_mode: str = "buffered"


def run_video(settings: VideoSettings,
              status: Callable[[str], None] = print,
              frame_ready: Callable[[np.ndarray], None] | None = None,
              stop: Event | None = None,
              tx_frame_ready: Callable[[int, np.ndarray], None] | None = None,
              rx_frame_ready: Callable[[int, np.ndarray | None], None] | None = None,
              constellation_ready: Callable[[np.ndarray, np.ndarray | None], None] | None = None,
              pair_frame_ready: Callable[[int, np.ndarray, np.ndarray | None], None] | None = None,
              channel_ready=None, transfer_progress=None) -> dict:
    if settings.transport == "original":
        from .original import run_original_video
        return run_original_video(settings, status=status, stop=stop,
                                  pair_frame_ready=pair_frame_ready,
                                  constellation_ready=constellation_ready,
                                  channel_ready=channel_ready,
                                  transfer_progress=transfer_progress)
    if settings.transport == "stream":
        from .stream import run_stream_video
        return run_stream_video(settings, status, frame_ready, stop,
                                tx_frame_ready, rx_frame_ready, constellation_ready,
                                pair_frame_ready, channel_ready)
    if settings.transport != "frame":
        raise ValueError("Transport must be frame or stream")
    if not settings.input_path.is_file():
        raise FileNotFoundError(settings.input_path)
    if settings.input_path.resolve() == settings.output_path.resolve():
        raise ValueError("Output video must not overwrite the input video")
    if settings.width < 32 or settings.height < 32 or settings.target_fps <= 0:
        raise ValueError("Invalid video size or frame rate")
    if settings.max_frames < 0:
        raise ValueError("Maximum frame count must be nonnegative")
    if not 5 <= settings.jpeg_quality <= 95:
        raise ValueError("JPEG quality must be 5..95")
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(settings.input_path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV could not read {settings.input_path}")
    source_fps = cap.get(cv2.CAP_PROP_FPS)
    if not math.isfinite(source_fps) or source_fps <= 0:
        source_fps = 25.0
    stride = max(1, round(source_fps / settings.target_fps))
    output_fps = source_fps / stride
    writer = cv2.VideoWriter(
        str(settings.output_path), cv2.VideoWriter_fourcc(*"mp4v"),
        output_fps, (settings.width, settings.height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"OpenCV could not create {settings.output_path}")
    stats = {"frames": 0, "lost_frames": 0, "packets": 0, "bytes": 0, "seconds": 0.0}
    started = time.monotonic()
    source_index = 0
    try:
        with LoopbackRadio(settings.radio) as radio:
            radio.set_channel_callback(channel_ready)
            status(f"Connected: {settings.radio.mode}, {settings.radio.uri}; {output_fps:.2f} output fps")
            while stop is None or not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                source_index += 1
                if (source_index - 1) % stride:
                    continue
                small = cv2.resize(frame, (settings.width, settings.height),
                                   interpolation=cv2.INTER_AREA)
                encoded_ok, encoded = cv2.imencode(
                    ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality])
                if not encoded_ok:
                    raise RuntimeError("JPEG encoding failed")
                jpg = encoded.tobytes()
                coded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if coded is None:
                    raise RuntimeError("Encoded JPEG is not decodable")
                chunks = math.ceil(len(jpg) / PAYLOAD_SIZE)
                if chunks > 65535:
                    raise RuntimeError("JPEG frame is too large for packet format")
                received: list[bytes] = []
                frame_no = stats["frames"] + stats["lost_frames"]
                if tx_frame_ready is not None:
                    tx_frame_ready(frame_no, coded)
                try:
                    for chunk_no in range(chunks):
                        if stop is not None and stop.is_set():
                            break
                        start = chunk_no * PAYLOAD_SIZE
                        raw = make_packet(frame_no, chunk_no, chunks,
                                          jpg[start:start + PAYLOAD_SIZE])
                        result = radio.transceive(
                            raw, frame_no, chunk_no, constellation_ready=constellation_ready)
                        if result.total_chunks != chunks:
                            raise RuntimeError("Received packet chunk count does not match")
                        received.append(result.payload)
                        stats["packets"] += 1
                    if len(received) != chunks:
                        break
                    received_jpg = b"".join(received)
                    if settings.radio.check_crc and received_jpg != jpg:
                        raise RuntimeError("Received JPEG bytes differ from transmitted bytes")
                    restored = cv2.imdecode(
                        np.frombuffer(received_jpg, dtype=np.uint8),
                        cv2.IMREAD_COLOR)
                    if restored is None:
                        raise RuntimeError("Received JPEG is not decodable")
                    writer.write(restored)
                    stats["frames"] += 1
                    stats["bytes"] += len(jpg)
                    if frame_ready is not None:
                        frame_ready(restored)
                    if rx_frame_ready is not None:
                        rx_frame_ready(frame_no, restored)
                    status(f"Received frame {stats['frames']}: {len(jpg)} bytes, {chunks} packets")
                except RuntimeError as exc:
                    stats["lost_frames"] += 1
                    if rx_frame_ready is not None:
                        rx_frame_ready(frame_no, None)
                    status(f"Skipped frame {frame_no}: {exc}")
                if settings.max_frames and stats["frames"] + stats["lost_frames"] >= settings.max_frames:
                    break
    finally:
        cap.release()
        writer.release()
        stats["seconds"] = round(time.monotonic() - started, 2)
    status(f"Finished: {stats['frames']} frames received, {stats['lost_frames']} lost; {settings.output_path}")
    return stats
