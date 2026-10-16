"""Small, low-overhead live channel panel for the desktop SDR application."""
from collections import deque
import math
from pathlib import Path
import shutil
import time
import tkinter as tk
from tkinter import ttk, filedialog

import numpy as np


class ChannelPanel(ttk.Frame):
    def __init__(self, parent, status):
        super().__init__(parent)
        self.status = status
        self.last = None
        self.baseline = None
        self.history = deque(maxlen=300)
        self.csv_path = None
        self.symbol_rate = 1.0
        self.info = tk.StringVar(value="等待前导信道估计")
        self.quality = tk.StringVar(value="单载波等效信道 · 相位未校准 · 非 Wi-Fi 子载波 CSI")
        ttk.Label(self, text="信道状态 / 感知数据采集", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 3))
        ttk.Label(self, textvariable=self.info).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(self, textvariable=self.quality).grid(row=2, column=0, columnspan=2, sticky="w")
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=3)
        self.canvases = {}
        for key, title in (("time", "幅度 / 相位时序"), ("frequency", "等效频响")):
            canvas = tk.Canvas(self.tabs, width=640, height=140, bg="#0b1424", highlightthickness=0)
            self.tabs.add(canvas, text=title)
            canvas.bind("<Configure>", lambda _event: self.draw())
            self.canvases[key] = canvas
        self.tabs.bind("<<NotebookTabChanged>>", lambda _event: self.draw())
        ttk.Button(self, text="设为基线", command=self.set_baseline).grid(row=4, column=0, sticky="w")
        ttk.Button(self, text="保存 CSV 副本", command=self.export).grid(row=4, column=1, sticky="e")
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)

    def reset(self, radio):
        self.last = self.baseline = None
        self.history.clear()
        self.csv_path = None
        self.symbol_rate = radio.sample_rate / radio.samples_per_symbol
        self.info.set("等待前导信道估计")
        self.quality.set("单载波等效信道 · 相位未校准 · 非 Wi-Fi 子载波 CSI")
        if radio.waveform == "ofdm":
            self.info.set("OFDM：256 点 FFT / 64 点循环前缀 / 192 数据子载波 / 8 导频")
            self.quality.set("训练符号信道均衡 + 导频相位校正；此面板暂不导出 OFDM CSI")
        self.draw()

    def set_baseline(self):
        if self.last is None:
            self.status("尚未收到可用信道数据")
            return
        self.baseline = complex(self.last["gain_real"], self.last["gain_imag"])
        self.history.clear()
        self.update_sample(self.last)
        self.status("已设置幅度/相位参考基线；这不是相位校准")

    def update_sample(self, sample):
        self.last = sample
        gain = complex(sample["gain_real"], sample["gain_imag"])
        if self.baseline is None:
            self.baseline = gain
        ratio = gain / self.baseline if abs(self.baseline) > 1e-12 else 1 + 0j
        amplitude = 20 * math.log10(max(abs(ratio), 1e-12))
        phase = float(np.angle(ratio, deg=True))
        self.history.append((sample["host_rx_monotonic_s"], amplitude, phase))
        self.info.set(f"Δ幅度 {amplitude:+.2f} dB   Δ相位 {phase:+.1f}°   "
                      f"前导拟合 SNR {sample['training_fit_snr_db']:.1f} dB")
        self.quality.set(f"RX RMS {sample['rx_preamble_rms_counts']:.1f} 计数   "
                         f"前导 EVM {sample['training_fit_evm_pct']:.1f}%   "
                         f"CRC {'通过' if sample['crc_status']=='passed' else '未校验'} · 相位未校准")
        self.draw()

    def check_stale(self):
        if self.last is not None and time.monotonic() - self.last["host_rx_monotonic_s"] > 1.5:
            self.quality.set("暂无新信道数据；图中保留最后一次估计 · 非 Wi-Fi CSI")

    def _trace(self, canvas, xs, ys, rectangle, color, label, phase=False):
        left, top, right, bottom = rectangle
        canvas.create_text(left, top + 2, text=label, anchor="nw", fill=color)
        top += 18
        canvas.create_line(left, bottom, right, bottom, fill="#33445c")
        if not len(xs):
            return
        ymin, ymax = float(np.min(ys)), float(np.max(ys))
        floor = 10 if phase else 1
        if ymax - ymin < floor:
            center = (ymin + ymax) / 2
            ymin, ymax = center - floor/2, center + floor/2
        xmin, xmax = float(xs[0]), float(xs[-1])
        span = max(xmax - xmin, 1e-6)
        coords = []
        previous = None
        for x, y in zip(xs, ys):
            if phase and previous is not None and abs(y - previous) > 180:
                if len(coords) >= 4:
                    canvas.create_line(*coords, fill=color, width=1.5)
                coords = []
            coords.extend((left + (x-xmin)/span*(right-left),
                           bottom - (y-ymin)/(ymax-ymin)*(bottom-top)))
            previous = y
        if len(coords) >= 4:
            canvas.create_line(*coords, fill=color, width=1.5)
        canvas.create_text(right, top, text=f"{ymax:.1f}", anchor="ne", fill="#93a6bd")
        canvas.create_text(right, bottom, text=f"{ymin:.1f}", anchor="se", fill="#93a6bd")

    def draw(self):
        if not self.tabs.tabs():
            return
        key = "time" if self.tabs.select() == str(self.canvases["time"]) else "frequency"
        canvas = self.canvases[key]
        width = max(150, canvas.winfo_width())
        height = max(100, canvas.winfo_height())
        canvas.delete("all")
        if self.last is None:
            canvas.create_text(width/2, height/2, text="等待真实采样的已知前导", fill="#93a6bd")
            return
        if key == "time":
            history = np.array(self.history)
            history = history[history[:, 0] >= history[-1, 0] - 30]
            xs = history[:, 0] - history[-1, 0]
            a, p = history[:, 1], history[:, 2]
            label_a, label_p = "相对幅度 dB · 最近 30 秒", "相对环绕相位 °（未校准）"
        else:
            response = np.array(self.last["response_real"]) + 1j * np.array(self.last["response_imag"])
            xs = np.linspace(-0.5, 0.5, 64, endpoint=False) * self.symbol_rate / 1e6
            a = 20 * np.log10(np.maximum(abs(response), 1e-12))
            p = np.angle(response, deg=True)
            label_a, label_p = "等效 H(f) 幅度 dB · 符号域频率 MHz", "H(f) 相位 ° · 7 抽头推算 64 点"
        middle = height/2
        self._trace(canvas, xs, a, (8, 0, width-8, middle-3), "#76ddb1", label_a)
        self._trace(canvas, xs, p, (8, middle, width-8, height-5), "#7db4ff", label_p, phase=True)

    def export(self):
        if self.csv_path is None or not Path(self.csv_path).exists():
            self.status("尚无可保存的信道 CSV")
            return
        selected = filedialog.asksaveasfilename(defaultextension=".csv",
                    initialfile=Path(self.csv_path).name, filetypes=[("CSV", "*.csv")])
        if not selected:
            return
        target, source = Path(selected), Path(self.csv_path)
        if target.resolve() != source.resolve():
            shutil.copy2(source, target)
            meta = source.with_suffix(".json")
            if meta.exists():
                shutil.copy2(meta, target.with_suffix(".json"))
        self.status(f"已保存信道数据：{target}")
