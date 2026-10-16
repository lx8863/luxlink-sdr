"""Live receive-IQ OFDM diagnostics, separate from the configured carrier map."""
import time
from collections import deque
import tkinter as tk
from tkinter import ttk
import numpy as np
from PIL import Image, ImageTk
from .ofdm import FFT_SIZE, CP_SIZE, DATA, PILOTS
from .ofdm import configuration
from .csi import CsiPanel
from .quality_ui import ReceptionQualityPanel
from .spectrum_waterfall import SpectrumWaterfall


class OfdmPanel(ttk.Frame):
    def __init__(self, parent, resource_parent=None, csi_parent=None):
        super().__init__(parent)
        self.sample = None
        self.settings = None
        self.updates = 0
        self._resource_size = None
        self._plot_layouts = {}
        self._scatter_photos = {}
        self._sample_times = deque(maxlen=40)
        self.render_times_ms=deque(maxlen=300)
        self.display_times=deque(maxlen=300)
        self._last_displayed_sample=None
        self.info = tk.StringVar(value="OFDM：等待真实接收 IQ")
        ttk.Label(self, textvariable=self.info).grid(row=0, column=0, columnspan=4, sticky="w", pady=4)
        self.plots = {}
        definitions = (("spectrum", "接收频谱 · 相对峰值 dB"),
                                         ("csi", "OFDM 信道状态 · CSI 幅度 / 相位"),
                                         ("before", "均衡前 · FFT 后 / 整体幅度归一化"),
                                         ("after", "均衡后 · 导频相位校正后"),
                                         ('quality','实时有效吞吐量 · 最近 30 秒'))
        if csi_parent is not None:
            self.csi_panel = CsiPanel(csi_parent,dashboard=True)
            self.csi_panel.threaded_heatmaps=True
            self.csi_panel.pack(fill='both',expand=True)
            definitions = tuple(item for item in definitions if item[0]!='csi')
        for i, (key, title) in enumerate(definitions):
            self.columnconfigure(i, weight=1, uniform="ofdm")
            box = ttk.LabelFrame(self, text=title, padding=4)
            box.grid(row=1, column=i, sticky="nsew", padx=3)
            if key=='quality':
                self.quality_panel=ReceptionQualityPanel(box)
                self.quality_panel.pack(fill='both',expand=True)
                continue
            if key == 'csi':
                self.csi_panel = CsiPanel(box)
                self.csi_panel.pack(fill='both',expand=True)
                continue
            if key=='spectrum':
                box.configure(text='接收频谱 · 每帧相对峰值 dB')
                self.waterfall=SpectrumWaterfall(box)
                self.waterfall.pack(fill='both',expand=True)
                ttk.Separator(box,orient='horizontal').pack(fill='x',pady=2)
            canvas = tk.Canvas(box, width=220, height=150 if key=='spectrum' else 235, bg="#0b1424", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            canvas.bind("<Configure>", lambda e: self.draw())
            canvas.bind('<Map>',lambda e:self.draw())
            self.plots[key] = canvas
        if resource_parent is not None:
            box = ttk.LabelFrame(resource_parent,text='子载波资源配置：256 FFT / 64 CP；192 数据 + 8 导频 + 56 空载波',padding=5)
            box.grid(row=8,column=0,columnspan=3,sticky='ew',pady=5)
            self.resource_box = box
            canvas = tk.Canvas(box,width=700,height=140,bg='#0b1424',highlightthickness=0)
            canvas.pack(fill='x',expand=True)
            canvas.bind('<Configure>',lambda e:self.draw())
            canvas.bind('<Map>',lambda e:self.draw())
            self.plots['carriers'] = canvas

    def reset(self, settings):
        self.settings = settings
        self.sample = None
        self.updates = 0
        self._sample_times.clear()
        self.render_times_ms.clear()
        self.display_times.clear()
        self._last_displayed_sample=None
        self.csi_panel.reset()
        self.quality_panel.reset()
        self.waterfall.reset(settings)
        layout=configuration(settings.ofdm_fft_size)
        self.info.set(f"OFDM | 中心 {settings.frequency_hz / 1e6:.3f} MHz | FFT {layout.fft_size} / CP {layout.cp_size} | "
                      f"{len(layout.data)} 数据 + 8 导频 | 间隔 {settings.sample_rate / layout.fft_size / 1e3:.3f} kHz | 等待采样")
        if hasattr(self,'resource_box'):
            self.resource_box.configure(text=f'子载波资源配置：{layout.fft_size} FFT / {layout.cp_size} CP；{len(layout.data)} 数据 + 8 导频')
        self.draw()

    def update_sample(self, sample):
        self.sample = sample
        self.updates += 1
        self._sample_times.append(sample['timestamp'])
        self.waterfall.update_sample(sample,render=not getattr(self,'suspend_rendering',False))
        if 'csi' in sample:
            self.csi_panel.update_sample(sample)
        layout=configuration(sample.get('fft_size',256))
        self.info.set(f"OFDM | 中心 {sample['frequency_hz'] / 1e6:.3f} MHz | {len(layout.data)} 数据 + 8 导频 | "
                      f"Δf {sample['sample_rate'] / layout.fft_size / 1e3:.3f} kHz | "
                      f"频偏估计 {sample['cfo_cycles_per_sample'] * sample['sample_rate']:+.0f} Hz | "
                      f"{'仿真' if sample['mode'] == 'simulate' else '板卡采样'} · "
                      f"CRC {'通过' if sample['crc_valid'] is True else '未通过' if sample['crc_valid'] is False else '未校验'}")
        if len(self._sample_times) > 1:
            hz = (len(self._sample_times)-1) / max(1e-9, self._sample_times[-1]-self._sample_times[0])
            self.info.set(self.info.get()+f" | 图表实际采样 {hz:.1f} Hz")
        self.draw()

    def _update_trace(self, key, canvas, left, right, top, bottom):
        """Move existing canvas items; preserve axes and avoid per-frame allocation."""
        if key == 'spectrum':
            items = canvas.find_withtag('live_trace')
            if not self.sample:
                return
            db = np.clip(self.sample['spectrum_db'][::2], -80, 0)
            coords = np.column_stack((np.linspace(left, right, len(db)),
                                      top+(bottom-top)*(-db)/80)).ravel().tolist()
            if items:
                canvas.coords(items[0], *coords)
            else:
                canvas.create_line(*coords, fill='#56d6ff', width=1.3, tags='live_trace')
            return
        if key not in ('before', 'after'):
            return
        # A single raster replaces thousands of synchronous Tcl calls per plot.
        # Keep every point; drawing cost depends on the viewport, not QAM order.
        symbols = np.asarray(self.sample[key]) if self.sample else np.empty(0,complex)
        cx, cy = (left+right)/2, (top+bottom)/2
        scale = min(right-left, bottom-top)/3.6
        width,height=max(1,int(right-left)),max(1,int(bottom-top))
        pixels=np.zeros((height,width,4),dtype=np.uint8)
        valid=np.isfinite(symbols.real)&np.isfinite(symbols.imag)
        symbols=symbols[valid]
        xs=np.rint(cx+symbols.real*scale-left).astype(np.int64)
        ys=np.rint(cy-symbols.imag*scale-top).astype(np.int64)
        color=(255,188,89,255) if key=='before' else (128,229,173,255)
        for dx,dy in ((0,0),(-1,0),(1,0),(0,-1),(0,1)):
            x,y=xs+dx,ys+dy
            keep=(x>=0)&(x<width)&(y>=0)&(y<height)
            pixels[y[keep],x[keep]]=color
        img=Image.fromarray(pixels)
        items=canvas.find_withtag('live_trace')
        photo=self._scatter_photos.get(key)
        if items and photo is not None and (photo.width(),photo.height())==(width,height):
            photo.paste(img)
        else:
            canvas.delete('live_trace')
            photo=ImageTk.PhotoImage(img)
            self._scatter_photos[key]=photo
            canvas.create_image(left,top,image=photo,anchor='nw',tags='live_trace')

    def check_stale(self):
        if not getattr(self,'suspend_rendering',False):
            self.quality_panel.refresh()
            self.waterfall.draw()
        self.csi_panel.check_stale()
        if self.sample and time.monotonic() - self.sample['timestamp'] > 1.5:
            text = self.info.get()
            if "采样已停止" not in text:
                self.info.set(text + " | 采样已停止，保留最后结果")

    def draw(self):
        if getattr(self,'suspend_rendering',False): return
        start=time.perf_counter()
        self._draw()
        self.render_times_ms.append((time.perf_counter()-start)*1000)
        if (self.sample is not None and self.sample['timestamp']!=self._last_displayed_sample
                and any(c.winfo_ismapped() for k,c in self.plots.items() if k!='carriers')):
            self._last_displayed_sample=self.sample['timestamp']
            self.display_times.append(time.monotonic())

    def _draw(self):
        size=self.sample.get('fft_size',256) if self.sample else getattr(self.settings,'ofdm_fft_size',256)
        resource=configuration(size)
        FFT_SIZE,CP_SIZE,DATA,PILOTS=resource.fft_size,resource.cp_size,resource.data,resource.pilots
        for key, canvas in self.plots.items():
            if not canvas.winfo_ismapped():
                continue
            w, h = max(220, canvas.winfo_width()), max(120 if key=='spectrum' else 180, canvas.winfo_height())
            if key == "carriers" and self._resource_size == (w, h,FFT_SIZE):
                continue
            settings = self.sample or (vars(self.settings) if self.settings else {})
            layout = (w, h, settings.get('frequency_hz'), settings.get('sample_rate'), self.sample is not None,FFT_SIZE)
            left, right, top, bottom = 38, w - (49 if key=='spectrum' else 12), 16, h - 43
            if key != 'carriers' and self._plot_layouts.get(key) == layout:
                self._update_trace(key, canvas, left, right, top, bottom)
                continue
            self._plot_layouts[key] = layout
            canvas.delete("all")
            if key == "carriers":
                self._resource_size = (w, h,FFT_SIZE)
            left, right, top, bottom = 38, w - (49 if key=='spectrum' else 12), 16, h - 43
            ink = "#a4b7cd"
            canvas.create_line(left, top, left, bottom, right, bottom, fill="#536781")
            if key == "carriers":
                kinds = np.zeros(FFT_SIZE)
                kinds[DATA] = 1
                kinds[PILOTS] = 2
                for i, value in enumerate(np.fft.fftshift(kinds)):
                    x = left + (right - left) * i / (FFT_SIZE - 1)
                    color = ("#455264", "#51d5e9", "#ffbc59")[int(value)]
                    y = bottom - (bottom - top) * (0.68 if value == 1 else 0.9 if value == 2 else 0.06)
                    canvas.create_line(x, bottom, x, y, fill=color)
                canvas.create_text(left, h - 27, text=str(-FFT_SIZE//2), anchor="w", fill=ink)
                canvas.create_text((left + right) / 2, h - 27, text="0 / DC 空", fill=ink)
                canvas.create_text(right, h - 27, text=f"+{FFT_SIZE//2-1}", anchor="e", fill=ink)
                canvas.create_text(w / 2, h - 10, text=f"青：{len(DATA)} 数据  橙：8 导频  灰：{FFT_SIZE-len(DATA)-len(PILOTS)} 空载波", fill=ink)
            elif key == "spectrum":
                for db in (0, -20, -40, -60):
                    y = top + (bottom - top) * (-db) / 80
                    canvas.create_line(left, y, right, y, fill="#233249")
                    canvas.create_text(left - 4, y, text=str(db), anchor="e", fill=ink)
                settings = self.sample or (vars(self.settings) if self.settings else {})
                center = settings.get('frequency_hz', 2400000000) / 1e6
                rate = settings.get('sample_rate', 4000000) / 1e6
                for fraction in (0, .5, 1):
                    canvas.create_text(left + (right-left)*fraction, h-27,
                                       text=f"{center+(fraction-.5)*rate:.2f}", fill=ink)
                canvas.create_text((left+right)/2, h - 10, text="频率 (MHz) · 相对峰值 (dB)", fill=ink)
                if self.sample:
                    db = np.clip(self.sample['spectrum_db'][::2], -80, 0)
                    coords = np.column_stack((np.linspace(left, right, len(db)),
                                              top + (bottom-top) * (-db) / 80)).ravel().tolist()
                    canvas.create_line(*coords, fill="#56d6ff", width=1.3, tags='live_trace')
            else:
                cx, cy = (left + right) / 2, (top + bottom) / 2
                scale = min(right-left, bottom-top) / 3.6
                canvas.create_line(left, cy, right, cy, fill="#536781")
                canvas.create_line(cx, top, cx, bottom, fill="#536781")
                canvas.create_text(right-4, cy-10, text="I", fill=ink)
                canvas.create_text(cx+10, top+6, text="Q", fill=ink)
                if self.sample:
                    self._update_trace(key, canvas, left, right, top, bottom)
                canvas.create_text(w/2, h-15, text="同一接收包的数据子载波符号", fill=ink)
            if key != "carriers" and not self.sample:
                canvas.create_text((left+right)/2, (top+bottom)/2, text="等待接收 IQ", fill=ink)
