import tkinter as tk
import unittest
import gc
import numpy as np
from video_loopback.ofdm_ui import OfdmPanel


class OfdmPlotTests(unittest.TestCase):
    def test_live_items_are_reused_and_old_scatter_pixels_cleared(self):
        root = tk.Tk()
        root.withdraw()
        try:
            panel = OfdmPanel(root)
            panel.sample = {'before': np.ones(256, complex),
                            'spectrum_db': np.zeros(1024)}
            for key in ('before', 'spectrum'):
                canvas = panel.plots[key]
                panel._update_trace(key, canvas, 38, 600, 16, 220)
                original = canvas.find_all()
                panel.sample[key if key == 'before' else 'spectrum_db'] *= .5
                panel._update_trace(key, canvas, 38, 600, 16, 220)
                self.assertEqual(original, canvas.find_all())
            panel.sample['before'] = np.ones(2, complex)
            panel._update_trace('before', panel.plots['before'], 38, 600, 16, 220)
            items = panel.plots['before'].find_withtag('live_trace')
            self.assertEqual(len(items),1)
            panel.sample['before']=np.empty(0,complex)
            panel._update_trace('before',panel.plots['before'],38,600,16,220)
            photo=panel._scatter_photos['before']._PhotoImage__photo
            self.assertTrue(photo.transparency_get(281+42,102))
        finally:
            root.destroy()
            panel=root=None
            gc.collect()
