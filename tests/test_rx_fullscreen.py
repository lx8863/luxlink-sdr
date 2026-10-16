import gc
import os
from io import BytesIO
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
import tkinter as tk
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from video_loopback.gui import App,_run_worker
from video_loopback.radio import RadioSettings
from video_loopback.video import VideoSettings


class ReceiverFullscreenTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows monitor placement')
    def test_fullscreen_uses_each_main_window_monitor(self):
        import ctypes
        from ctypes import wintypes
        user32=ctypes.windll.user32
        user32.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
        user32.GetAncestor.restype=wintypes.HWND
        user32.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,
                                     ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
        user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        monitors=[]
        callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HANDLE,wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT),wintypes.LPARAM)
        def collect(monitor,dc,rect,data):
            r=rect.contents
            monitors.append((r.left,r.top,r.right,r.bottom))
            return True
        user32.EnumDisplayMonitors(None,None,callback(collect),0)
        root=tk.Tk();app=App(root)
        try:
            root.update()
            owner=user32.GetAncestor(root.winfo_id(),2)
            for left,top,right,bottom in monitors:
                with self.subTest(monitor=(left,top,right,bottom)):
                    user32.SetWindowPos(owner,None,left+30,top+30,600,400,0x0040)
                    root.update()
                    app._open_rx_fullscreen();root.update()
                    hwnd=user32.GetAncestor(app._rx_fullscreen.winfo_id(),2)
                    rect=wintypes.RECT()
                    self.assertTrue(user32.GetWindowRect(hwnd,ctypes.byref(rect)))
                    self.assertEqual((rect.left,rect.top,rect.right,rect.bottom),
                                     (left,top,right,bottom))
                    app._close_rx_fullscreen()
        finally:
            app._close_rx_fullscreen(restore=False)
            for timer in root.tk.call('after','info'):
                root.after_cancel(timer)
            root.destroy();app=root=None;gc.collect()

    def test_button_updates_received_image_preserves_aspect_and_restores_preview(self):
        root=tk.Tk();root.withdraw()
        app=App(root)
        toplevel=tk.Toplevel
        def hidden_window(parent):
            window=toplevel(parent);window.withdraw();return window
        try:
            with patch('video_loopback.gui.tk.Toplevel',side_effect=hidden_window):
                app.rx_fullscreen_button.invoke()
                window=app._rx_fullscreen
                app.rx_fullscreen_button.invoke()
                self.assertIs(app._rx_fullscreen,window)
                self.assertTrue(app.fullscreen_requested.is_set())
                self.assertTrue(window.bind('<Escape>'))
                canvas=app._rx_fullscreen_canvas
                canvas.winfo_width=lambda:1200
                canvas.winfo_height=lambda:800
                pair=Image.new('RGB',(1024,256),'red')
                pair.paste(Image.new('RGB',(512,256),'green'),(512,0))
                encoded=BytesIO();pair.save(encoded,format='JPEG')
                app._show_pair(0,encoded.getvalue(),True,False)
                photo=app._rx_fullscreen_photo
                self.assertEqual((photo.width(),photo.height()),(1200,600))
                self.assertEqual(canvas.coords('video'),[600,400])
                pixel=photo._PhotoImage__photo.get(600,300)
                self.assertGreater(pixel[1],100)
                self.assertLess(pixel[0],20,'Fullscreen must show RX, not TX')
                app._show_pair(1,encoded.getvalue(),True,False)
                self.assertIs(app._rx_fullscreen_photo,photo)
                app._close_rx_fullscreen()
                self.assertFalse(app.fullscreen_requested.is_set())
                self.assertIsNone(app._rx_fullscreen)
                self.assertIsNotNone(app.rx_photo)
                self.assertFalse(app.ofdm_panel.suspend_rendering)
        finally:
            app._close_rx_fullscreen(restore=False)
            for timer in root.tk.call('after','info'):
                root.after_cancel(timer)
            root.destroy();app=root=photo=canvas=window=None;gc.collect()

    def test_worker_switches_preview_to_1080p_when_fullscreen_requested(self):
        class PreviewQueue(Queue):
            def close(self): pass
            def join_thread(self): pass
        with TemporaryDirectory() as folder:
            settings=VideoSettings(Path(folder)/'source.mp4',Path(folder)/'rx.mp4',
                RadioSettings(mode='simulate'),transport='original')
            fullscreen=Event()
            for enabled,width in ((False,960),(True,1920)):
                fullscreen.set() if enabled else fullscreen.clear()
                previews=PreviewQueue()
                def run(*args,**kwargs):
                    frame=np.zeros((1080,1920,3),np.uint8)
                    kwargs['pair_frame_ready'](0,frame,frame)
                    return {}
                with patch('video_loopback.gui.run_video',side_effect=run):
                    _run_worker(settings,Queue(),Event(),None,preview_events=previews,
                                fullscreen_requested=fullscreen)
                with Image.open(BytesIO(previews.get_nowait()[1])) as image:
                    self.assertEqual(image.width,width)
