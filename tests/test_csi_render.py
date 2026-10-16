import time
import gc
import tkinter as tk
import unittest
import numpy as np
from video_loopback.csi import CsiPanel


class CsiRenderTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk()
        self.root.withdraw()
        self.panel=CsiPanel(self.root,dashboard=True)
        self.panel.render_interval=0
        for canvas in (self.panel.canvas,self.panel.heat_canvas):
            canvas.winfo_ismapped=lambda: True
            canvas.winfo_width=lambda: 680
            canvas.winfo_height=lambda: 560
        self.sample=dict(timestamp=time.monotonic(),csi=np.ones(200,complex),
            subcarriers=np.r_[np.arange(-100,0),np.arange(1,101)],
            training_snr_db=30.,decision_evm_pct=1.,cfo_cycles_per_sample=0.,
            sample_rate=4000000,frequency_hz=2400000000,mode='simulate',crc_valid=True)

    def tearDown(self):
        executor=getattr(self.panel,'_heat_executor',None)
        if executor is not None:
            executor.shutdown(wait=True,cancel_futures=True)
        self.root.destroy()
        self.panel = self.root = None
        gc.collect()  # Release Tk objects on their owning thread.

    def test_heatmap_reuses_items_and_images_but_updates_pixels(self):
        p=self.panel
        p.update_sample(self.sample)
        items=p.heat_canvas.find_all()
        photos=list(p._heat_photos)
        first=photos[0]._PhotoImage__photo.get(0,0)
        p.history.clear()
        p.update_sample(dict(self.sample,timestamp=time.monotonic()+.05,csi=np.ones(200)*1.5))
        self.assertEqual(p.heat_canvas.find_all(),items)
        self.assertEqual(p._heat_photos,photos)
        photo=photos[0]._PhotoImage__photo
        self.assertNotEqual(photo.get(0,0),first)
        self.assertEqual(photo.get(photos[0].width()//2,0),(0,0,0))
        self.assertEqual(photo.get(0,photos[0].height()-1),(0,0,0))

    def test_burst_coalesces_paints_without_losing_history(self):
        p=self.panel
        p.render_interval=.1
        p.update_sample(self.sample)
        count=len(p.render_times_ms)
        for i in range(1,21):
            p.update_sample(dict(self.sample,timestamp=self.sample['timestamp']+i*.05))
        self.assertEqual(len(p.history),21)
        self.assertEqual(len(p.render_times_ms),count)
        self.assertIsNotNone(p._pending_render)
        p.draw()
        self.assertIsNone(p._pending_render)
        self.assertEqual(p._last_displayed_sample,p.sample['timestamp'])

    def test_background_heat_worker_publishes_latest_image_on_tk_thread(self):
        p=self.panel
        p.threaded_heatmaps=True
        p.update_sample(self.sample)
        self.assertIsNotNone(p._heat_job)
        first=p._heat_job[1]
        p.update_sample(dict(self.sample,timestamp=self.sample['timestamp']+.05,
                             csi=np.ones(200)*1.5))
        self.assertIs(p._heat_job[1],first)  # Never queue a second work item.
        first.result(timeout=5)
        # Complete on the owning Tk thread without pumping callbacks belonging
        # to previously destroyed test roots in Tcl's shared event loop.
        p._finish_heat_worker()
        p._heat_job[1].result(timeout=5)
        p._finish_heat_worker()
        self.assertIsNone(p._heat_job)
        self.assertEqual(len(p._heat_photos),2)
        self.assertEqual(p._heat_requested[0],p.sample['timestamp'])

    def test_resize_baseline_and_reset_rebuild_correctly(self):
        p=self.panel
        p.update_sample(self.sample)
        old=list(p._heat_photos)
        p.heat_canvas.winfo_width=lambda: 1000
        p.draw()
        self.assertNotEqual(p._heat_photos,old)
        self.assertEqual(len(p.heat_canvas.find_withtag('heat_image')),2)
        p.set_baseline()
        self.assertIsNotNone(p.baseline)
        p.reset()
        self.assertEqual(p._heat_photos,[])
        self.assertEqual(len(p.display_times),0)
        p.update_sample(self.sample)
        self.assertEqual(len(p._heat_photos),2)

    def test_repaint_does_not_inflate_display_sample_count(self):
        p=self.panel
        p.update_sample(self.sample)
        p.draw()
        p.draw()
        self.assertEqual(len(p.display_times),1)
        p.update_sample(dict(self.sample,timestamp=self.sample['timestamp']+.05))
        self.assertEqual(len(p.display_times),2)


if __name__=='__main__':
    unittest.main()
