"""Generate a short, reproducible motion clip for RF loopback checks."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np


def generate(path: Path, seconds: int = 30, fps: int = 2,
             width: int = 160, height: int = 90, detail: bool = False) -> Path:
    if seconds <= 0 or fps <= 0 or width < 32 or height < 32:
        raise ValueError("seconds, fps, width, and height must be positive")
    if width % 2 or height % 2:
        raise ValueError("Test video width and height must be even")
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps,
                             (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not create test video: {path}")
    total = seconds * fps
    texture = (np.random.default_rng(72030).integers(0, 256, (height, width, 3), dtype=np.uint8)
               if detail else None)
    try:
        for index in range(total):
            background = (27, 24, 22) if index % 2 == 0 else (25, 34, 30)
            banner = (81, 52, 28) if index % 2 == 0 else (44, 83, 53)
            frame = np.full((90, 160, 3), background, dtype=np.uint8)
            cv2.rectangle(frame, (0, 0), (159, 20), banner, -1)
            cv2.putText(frame, "AD9361 RF VIDEO", (8, 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, (245, 245, 245), 1,
                        cv2.LINE_AA)
            cv2.rectangle(frame, (8, 27), (151, 63), (68, 62, 53), 1)
            # Sweep four times over the clip, so consecutive received frames
            # visibly differ even at the low RF link frame rate.
            position = 10 + round((index % max(1, fps * 8)) /
                                  max(1, fps * 8 - 1) * 123)
            cv2.rectangle(frame, (position, 34), (position + 15, 54),
                          (76, 224, 244), -1)
            cv2.circle(frame, (148 - (position - 10), 45), 5, (235, 139, 93), -1)
            cv2.putText(frame, f"T={index / fps:04.1f}s  F={index + 1:02d}/{total}",
                        (7, 78), cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                        (246, 246, 246), 1, cv2.LINE_AA)
            cv2.rectangle(frame, (7, 83), (152, 86), (62, 58, 52), -1)
            cv2.rectangle(frame, (7, 83), (7 + round(145 * (index + 1) / total), 86),
                          (93, 210, 105), -1)
            if (width, height) != (160, 90):
                frame = cv2.resize(frame, (width, height),
                                   interpolation=cv2.INTER_AREA)
            if texture is not None:
                # Native-resolution moving detail stresses compression much
                # more than enlarging the original 160x90 demonstration.
                frame = cv2.addWeighted(frame, 0.75, np.roll(texture, index * 7, axis=1), 0.25, 0)
            writer.write(frame)
    finally:
        writer.release()
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an AD9361 loopback test video")
    parser.add_argument("--output", type=Path,
                        default=Path("examples/test_30s.mp4"))
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--fps", type=int, default=2)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--height", type=int, default=90)
    parser.add_argument("--detail", action="store_true", help="Native-resolution moving texture for bitrate stress")
    args = parser.parse_args()
    print(generate(args.output, args.seconds, args.fps,
                   args.width, args.height, args.detail))


if __name__ == "__main__":
    main()
