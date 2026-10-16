from collections import deque
import unittest
from video_loopback.quality_ui import ReceptionQualityPanel


class ThroughputHistoryTests(unittest.TestCase):
    def test_byte_deltas_elapsed_time_and_stall(self):
        panel=object.__new__(ReceptionQualityPanel)
        panel.samples=deque()
        panel.previous=None
        panel.origin=None
        panel.refresh=lambda **kwargs:None
        panel.update_progress(0,2000000,100)
        panel.update_progress(1000000,2000000,101)
        self.assertEqual(panel.buckets(101),{0:8.})
        panel.update_progress(1000000,2000000,102)
        self.assertEqual(panel.buckets(102),{1:8.,0:0.})
        panel.update_progress(2000000,2000000,104)
        self.assertEqual(panel.buckets(104),{3:8.,2:0.,1:0.,0:8.})
        self.assertTrue(panel.complete)
        self.assertEqual(panel.buckets(135),panel.buckets(104),'Completed history must freeze')

    def test_closed_history_does_not_change_between_clock_ticks(self):
        panel=object.__new__(ReceptionQualityPanel)
        panel.samples=deque([(100.2,250000),(100.8,750000),(101.4,500000)])
        panel.origin=100.
        panel.previous=(101.4,1500000)
        panel.complete=False
        self.assertEqual(panel.buckets(101.5),{0:8.})
        self.assertEqual(panel.buckets(101.9),{0:8.})
        self.assertEqual(panel.buckets(102.1),{1:8.,0:4.})
        self.assertEqual(panel.buckets(103.1),{2:8.,1:4.,0:0.})
