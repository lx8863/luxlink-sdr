from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import imageio_ffmpeg
import numpy as np

from video_loopback.decoder import ContinuousDecoder, access_unit_count
from video_loopback.stream import _encode_segments, _decode_segment
from video_loopback.test_video import generate


class DecoderTests(unittest.TestCase):
    def test_persistent_decoder_matches_standalone_across_segment_boundaries(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            source = generate(directory / "source.mp4", seconds=1, fps=30, width=96, height=64)
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            decoder = ContinuousDecoder(ffmpeg, 96, 64, 30)
            total = 0
            try:
                with _encode_segments(ffmpeg, source, directory, 96, 64, 30,
                                      30000, 18, 0.2) as segments:
                    for path in segments:
                        data = path.read_bytes()
                        frames = decoder.decode(data, access_unit_count(data))
                        expected = _decode_segment(ffmpeg, data, 96, 64)
                        np.testing.assert_array_equal(np.stack(frames), expected)
                        total += len(frames)
                self.assertEqual(total, 18)
            finally:
                decoder.close()


if __name__ == "__main__":
    unittest.main()
