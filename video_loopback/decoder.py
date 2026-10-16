"""Persistent H.264 decoder: avoid process startup at every segment boundary."""
import re
import subprocess
from queue import Queue, Empty, Full
from threading import Event, Thread

import numpy as np


def access_unit_count(data):
    return len(re.findall(b"\x00\x00(?:\x00)?\x01\x09", data))


class ContinuousDecoder:
    def __init__(self, ffmpeg, width, height, fps):
        self.width, self.height = width, height
        self.frame_size = width * height * 3
        self.stop = Event()
        self.frames = Queue(maxsize=8)
        self.process = subprocess.Popen([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "32",
            "-analyzeduration", "0", "-threads", "1", "-flags", "low_delay",
            "-r", str(fps), "-f", "h264", "-i", "pipe:0", "-an",
            "-threads", "1", "-fps_mode", "passthrough", "-flush_packets", "1",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def read_frames():
            try:
                while not self.stop.is_set():
                    data = self.process.stdout.read(self.frame_size)
                    item = (np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
                            if len(data) == self.frame_size else None)
                    while not self.stop.is_set():
                        try:
                            self.frames.put(item, timeout=0.1)
                            break
                        except Full:
                            continue
                    if item is None:
                        return
            except (OSError, ValueError):
                pass
        self.reader = Thread(target=read_frames, daemon=True)
        self.reader.start()

    def decode(self, data, count, timeout=3):
        errors = []
        def write():
            try:
                # AUD closes the last access unit without ending stdin. Padding
                # lets the raw demuxer's partial read complete immediately.
                self.process.stdin.write(data + b"\x00\x00\x00\x01\x09\xf0" + b"\x00" * 4096)
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                errors.append(exc)
        writer = Thread(target=write, daemon=True)
        writer.start()
        frames = []
        try:
            for _ in range(count):
                frame = self.frames.get(timeout=timeout)
                if frame is None:
                    raise RuntimeError("Persistent decoder stopped")
                frames.append(frame)
        except Empty as exc:
            raise RuntimeError("Persistent decoder frame timeout") from exc
        writer.join(timeout=timeout)
        if writer.is_alive() or errors:
            raise RuntimeError("Persistent decoder input failed")
        return frames

    def close(self):
        self.stop.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()
