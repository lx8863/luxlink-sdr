from collections import deque
import unittest
import numpy as np
from video_loopback.spectrum_waterfall import SpectrumWaterfall


class WaterfallHistoryTests(unittest.TestCase):
    def test_frozen_history_survives_time_resize_and_late_samples(self):
        panel=object.__new__(SpectrumWaterfall)
        panel.palette=np.repeat(np.arange(256,dtype=np.uint8)[:,None],3,axis=1)
        panel.history=deque([(100.,np.array([-80.,0.]))])
        panel.frozen_at=None
        panel.draw=lambda **kwargs:None
        panel.freeze()
        before=panel.raster(100.)
        panel.update_sample(dict(timestamp=120.,spectrum_db=np.array([0.,-80.])))
        self.assertEqual(len(panel.history),1)
        np.testing.assert_array_equal(panel.raster(120.),before)
        panel.reset(type('Settings',(),{})())
        self.assertIsNone(panel.frozen_at)
        self.assertFalse(panel.history)

    def test_time_rows_frequency_order_and_missing_samples(self):
        panel=object.__new__(SpectrumWaterfall)
        panel.palette=np.repeat(np.arange(256,dtype=np.uint8)[:,None],3,axis=1)
        panel.history=deque([(99.,np.array([-80.,0.])),(100.,np.array([0.,-80.]))])
        pixels=panel.raster(100.)
        np.testing.assert_array_equal(pixels[99,0],[255]*3)
        np.testing.assert_array_equal(pixels[99,-1],[0]*3)
        np.testing.assert_array_equal(pixels[89,0],[0]*3)
        np.testing.assert_array_equal(pixels[89,-1],[255]*3)
        np.testing.assert_array_equal(pixels[98,0],[11,20,36])
        np.testing.assert_array_equal(panel.raster(111.)[99,0],[11,20,36])
