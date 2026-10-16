"""Minimal Windows desktop control panel for the AD9361 video link."""

from __future__ import annotations

import argparse
from datetime import datetime
from io import BytesIO
import json
import math
import multiprocessing as mp
import os
import time
from threading import Thread
from pathlib import Path
from queue import Empty
from queue import Full
from collections import deque
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageTk, ImageOps

from .radio import RadioSettings
from .video import VideoSettings, run_video
from .profiles import PROFILES, PROFILE_FILES, apply_profile
from .channel import ChannelLog
from .channel_ui import ChannelPanel
from .ofdm_ui import OfdmPanel
from .csi import CsiLog


def _put_latest(queue, value):
    """Never block playback on a slow preview consumer."""
    try:
        queue.put_nowait(value)
    except Full:
        try:
            queue.get_nowait()
        except Empty:
            pass
        try:
            queue.put_nowait(value)
        except Full:
            pass


def _run_csi_worker(settings,events,stop,path,channel_events,seconds=0):
    from .csi_capture import collect_csi
    try:
        events.put(('csi_log',str(path)))
        def publish(sample):
            try:
                channel_events.put_nowait(sample)
            except Full:
                try:
                    channel_events.get_nowait()
                except Empty:
                    pass
                try:
                    channel_events.put_nowait(sample)
                except Full:
                    pass
        stats=collect_csi(settings,path,seconds=seconds,stop=stop,callback=publish)
        path.with_suffix('.report.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
        events.put(('done',stats))
    except Exception as exc:
        events.put(('error',str(exc)))
        events.put(('done',None))


def _run_worker(settings: VideoSettings, events, stop, report_path: Path | None,
                chart_events=None, channel_events=None, preview_events=None, fullscreen_requested=None) -> None:
    channel_log = None
    csi_log = None
    try:
        log_name = settings.output_path.stem + datetime.now().strftime(".channel_%Y%m%d_%H%M%S_%f.csv")
        channel_log = ChannelLog(settings.output_path.with_name(log_name), settings.radio)
        events.put(("channel_log", str(channel_log.path)))
        if settings.radio.waveform == 'ofdm':
            csi_log = CsiLog(settings.output_path.with_name(log_name.replace('.csv','.csi.csv')), settings.radio)
            events.put(('csi_log',str(csi_log.path)))
        last_channel = 0.0
        diagnostics_count = [0]
        def publish_channel(sample):
            nonlocal last_channel
            if sample.get('kind')=='throughput_reference':
                events.put(('throughput_reference',sample['mbps']))
                return
            now = time.monotonic()
            if now - last_channel < (0.04 if sample.get('kind')=='ofdm' else 0.1):
                return
            last_channel = now
            if sample.get("kind") != "ofdm":
                channel_log.write(sample)
            else:
                diagnostics_count[0] += 1
                if csi_log is not None:
                    csi_log.write(sample)
            if channel_events is not None:
                try:
                    channel_events.put_nowait(sample)
                except Full:
                    try:
                        channel_events.get_nowait()
                    except Empty:
                        pass
                    try:
                        channel_events.put_nowait(sample)
                    except Full:
                        pass
        def publish_constellation(tx, rx):
            if settings.radio.waveform == "ofdm":
                return  # OFDM panel uses a throttled diagnostic queue instead.
            if chart_events is None:
                events.put(("constellation", (tx, rx)))
                return
            if rx is None:
                return
            sample = (tx, rx, time.monotonic())
            try:
                chart_events.put_nowait(sample)
            except Full:
                try:
                    chart_events.get_nowait()
                except Empty:
                    pass
                try:
                    chart_events.put_nowait(sample)
                except Full:
                    pass
        preview_metrics=[0,0]
        def send_pair(frame_no: int, tx_frame: np.ndarray,
                      rx_frame: np.ndarray | None) -> None:
            if frame_no % max(1, round(settings.target_fps / 60)):
                return
            right = rx_frame if rx_frame is not None else np.zeros_like(tx_frame)
            # A smaller preview keeps 60 fps IPC/painting practical; the saved
            # receiver video keeps the full configured resolution.
            fullscreen=fullscreen_requested is not None and fullscreen_requested.is_set()
            preview_width = min(1920 if fullscreen else 960, tx_frame.shape[1])
            preview_metrics[0]=max(preview_metrics[0],preview_width)
            preview_metrics[1]+=int(fullscreen)
            preview_size = (preview_width, max(1, round(preview_width * tx_frame.shape[0] / tx_frame.shape[1])))
            left_preview = cv2.resize(tx_frame, preview_size, interpolation=cv2.INTER_AREA)
            identical = rx_frame is not None and np.shares_memory(tx_frame, rx_frame)
            right_preview = None if identical else cv2.resize(right, preview_size, interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(
                ".jpg",
                left_preview if identical else np.hstack((left_preview, right_preview)),
                [cv2.IMWRITE_JPEG_QUALITY, 95])
            if ok:
                pair = (frame_no, encoded.tobytes(), rx_frame is not None,
                        identical, time.monotonic())
                if preview_events is not None:
                    _put_latest(preview_events, pair)
                else:
                    events.put(("pair_jpeg", pair))

        pending_frame = [None]
        def remember_tx(number, frame):
            pending_frame[0] = frame

        def publish_rx(number, frame):
            if pending_frame[0] is not None:
                send_pair(number, pending_frame[0], frame)
                pending_frame[0] = None

        last_progress=[0.0]
        def publish_progress(received,total):
            now=time.monotonic()
            if received==0 or received>=total or now-last_progress[0]>=0.1:
                events.put(('transfer_progress',(received,total,now)))
                last_progress[0]=now
        stats = run_video(
            settings,
            status=lambda s: events.put(("status", s)),
            stop=stop,
            tx_frame_ready=remember_tx
            if settings.transport == "frame" else None,
            rx_frame_ready=publish_rx
            if settings.transport == "frame" else None,
            constellation_ready=publish_constellation,
            pair_frame_ready=send_pair if settings.transport in ("stream", "original") else None,
            channel_ready=publish_channel,transfer_progress=publish_progress)
        stats["channel_samples"] = channel_log.samples
        stats["ofdm_diagnostics_samples"] = diagnostics_count[0]
        stats["channel_csv"] = str(channel_log.path)
        stats['csi_samples'] = csi_log.samples if csi_log is not None else 0
        stats['csi_csv'] = str(csi_log.path) if csi_log is not None else None
        stats['preview_max_width']=preview_metrics[0]
        stats['fullscreen_preview_frames']=preview_metrics[1]
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        # Ensure the last preview has reached its pipe before publishing done
        # on the separate control queue. The UI continues draining both.
        if preview_events is not None:
            preview_events.close()
            preview_events.join_thread()
        events.put(("done", stats))
    except InterruptedError as exc:
        if stop.is_set():
            events.put(('cancelled', '已停止视频传输；已接收的数据保留，文件可能不完整'))
        else:
            events.put(('error', str(exc)))
            events.put(('done', None))
    except Exception as exc:
        events.put(("error", str(exc)))
        events.put(("done", None))
    finally:
        if channel_log is not None:
            channel_log.close()
        if csi_log is not None:
            csi_log.close()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self._timer_period = False
        if os.name == "nt":
            import ctypes
            self._timer_period = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        root.title("LuxLink SDR 视频无线传输系统")
        icon_dir=Path(__file__).resolve().parent/'assets'
        if os.name=='nt':
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('LuxLinkSDR.VideoLoopback')
            if (icon_dir/'app-icon.ico').is_file():
                root.iconbitmap(default=str(icon_dir/'app-icon.ico'))
        elif (icon_dir/'app-icon-64.png').is_file():
            self._app_icon=tk.PhotoImage(file=str(icon_dir/'app-icon-64.png'))
            root.iconphoto(True,self._app_icon)
        root.geometry(f"{min(1600, root.winfo_screenwidth()-80)}x{min(960, root.winfo_screenheight()-80)}")
        root.minsize(1000, 700)
        self.context = mp.get_context("spawn")
        self.events = self.context.Queue()
        self.preview_events = self.context.Queue(maxsize=1)
        self.chart_events = self.context.Queue(maxsize=1)
        self.channel_events = self.context.Queue(maxsize=1)
        self.chart_display_times = deque()
        self._latest_chart_sample = None
        self._last_chart_draw = 0.0
        self._chart_age_ms = []
        self.stop = self.context.Event()
        self.fullscreen_requested=self.context.Event()
        self._rx_fullscreen=None
        self._rx_fullscreen_photo=None
        self._rx_fullscreen_rgb=None
        self.worker: mp.Process | None = None
        self.tx_photo = None
        self.rx_photo = None
        self.pair_photo = None
        self.chart_photos = {}
        self._chart_points = {}
        self._chart_resize_jobs = {}
        self.ui_pair_seconds = 0.0
        self.ui_chart_seconds = 0.0
        self.ui_poll_seconds = 0.0
        self.ui_pair_calls = 0
        self._display_times = []
        self._display_ages = []
        self._display_frame_numbers = []
        self.ui_chart_calls = 0
        demo = Path.cwd() / "examples" / "test_30s_30fps_96x54.mp4"
        if not demo.is_file():
            demo = Path.cwd() / "examples" / "test_30s_30fps.mp4"
        if not demo.is_file():
            demo = Path.cwd() / "examples" / "test_30s_60fps.mp4"
        if not demo.is_file():
            demo = Path.cwd() / "examples" / "test_30s.mp4"
        if not demo.is_file():
            demo = Path.cwd() / "examples" / "demo_input.mp4"
        self.input = tk.StringVar(value=str(demo) if demo.is_file() else "")
        self.output = tk.StringVar(value=str(Path.cwd() / "examples" / "test_30s_30fps_96x54_received.mp4"))
        self.uri = tk.StringVar(value="usb:")
        self.mode = tk.StringVar(value="rf")
        self.modulation = tk.StringVar(value="64qam")
        self.waveform = tk.StringVar(value="single-carrier")
        self.ofdm_fft_size = tk.StringVar(value='256')
        self.ofdm_training_packets=0
        self.transport = tk.StringVar(value="stream")
        self.stream_bitrate = tk.StringVar(value="15")
        self.check_crc = tk.BooleanVar(value=True)
        self.encoder_preset = tk.StringVar(value="veryfast")
        self.playback_mode = tk.StringVar(value="预缓冲")
        self.active_crc = True
        self.channel = tk.StringVar(value="0")
        self.frequency = tk.StringVar(value="2400")
        self.tx_gain = tk.StringVar(value="0")
        self.rx_gain = tk.StringVar(value="10")
        self.width = tk.StringVar(value="96")
        self.height = tk.StringVar(value="54")
        self.fps = tk.StringVar(value="30")
        self.quality = tk.StringVar(value="35")
        self.max_frames = tk.StringVar(value="0")
        self.sample_rate = tk.StringVar(value="2")
        self.sps = tk.StringVar(value="8")
        self.batch_packets = tk.StringVar(value="1")
        self.segment_seconds = tk.StringVar(value="1")
        self.playback_buffer_segments = tk.StringVar(value="1")
        self.summary = tk.StringVar(value="等待开始")
        self.transfer_percent=tk.DoubleVar(value=0)
        self.transfer_caption=tk.StringVar(value='传输进度：等待开始')
        self.channel_mode = tk.StringVar(value="当前未采集")
        self.tx_caption = tk.StringVar(value="编码后发送画面：等待")
        self.rx_caption = tk.StringVar(value="接收画面：等待")
        self.tx_constellation_caption = tk.StringVar(value="发送星座图：等待")
        self.rx_constellation_caption = tk.StringVar(value="接收星座图：等待")
        self.report_path: Path | None = None
        self.rx_frames_seen = 0
        self.rx_packets_seen = 0
        self.tx_packets_seen = 0
        self.last_compared_frame: int | None = None
        self.pending_tx_frame: tuple[int, np.ndarray] | None = None
        self.finished = False
        self._preview_images = {}
        self._resize_jobs = {}
        self._build()
        self.transport.trace_add("write", self._update_transport_controls)
        self._update_transport_controls()
        root.after(15, self._poll)
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.after(150, self._bring_to_front)

    def _bring_to_front(self):
        self.root.withdraw()
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _row(self, parent, label, var, row, width=18):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=3)
        entry = ttk.Entry(parent, textvariable=var, width=width)
        entry.grid(row=row, column=1, sticky="ew", padx=5, pady=3)
        return entry

    def _build(self):
        navigation = ttk.Frame(self.root)
        navigation.pack(fill="x", padx=8, pady=(8, 0))
        navigation.columnconfigure((0, 1, 2), weight=1, uniform="navigation")
        self.navigation = navigation
        progress=ttk.Frame(self.root)
        progress.pack(fill='x',padx=12,pady=(6,0))
        ttk.Label(progress,textvariable=self.transfer_caption,width=48).pack(side='left',padx=(0,8))
        self.transfer_bar=ttk.Progressbar(progress,variable=self.transfer_percent,maximum=100)
        self.transfer_bar.pack(side='left',fill='x',expand=True)
        style = ttk.Style(self.root)
        style.layout("FullWidth.TNotebook.Tab", [])
        self.tabs = ttk.Notebook(self.root, style="FullWidth.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        area = ttk.Frame(self.tabs, padding=8)
        controls = ttk.Frame(self.tabs, padding=16)
        self.channel_tab = ttk.Frame(self.tabs,padding=12)
        channel_actions=ttk.Frame(self.channel_tab)
        channel_actions.pack(fill='x',pady=(0,6))
        self.csi_start_button=ttk.Button(channel_actions,text='开始独立 CSI 采集（目标 20 Hz）',command=self._start_csi)
        self.csi_start_button.pack(side='left',padx=4)
        ttk.Button(channel_actions,text='停止采集',command=self.stop.set).pack(side='left',padx=4)
        ttk.Label(channel_actions,text='独立采集与视频互斥；复用设置页的射频参数，固定使用 QPSK 训练波形').pack(side='left',padx=10)
        ttk.Label(self.channel_tab,textvariable=self.channel_mode).pack(fill='x',pady=(0,4))
        self.tabs.add(area, text="演示")
        self.tabs.add(controls, text="设置")
        self.tabs.add(self.channel_tab,text='信道状态')
        self.demo_tab = area
        area.columnconfigure(1, weight=1)
        area.columnconfigure(2, weight=1)
        controls.columnconfigure(1, weight=1)
        self.controls = controls
        self.navigation_index = tk.IntVar(value=0)
        for index, label in enumerate(("演示 / OFDM 观测", "参数与文件设置", "信道状态")):
            ttk.Radiobutton(navigation, text=label, style="Toolbutton", variable=self.navigation_index,
                            value=index, command=lambda i=index: self.tabs.select(i)).grid(
                row=0, column=index, sticky="ew", ipady=8)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.navigation_index.set(self.tabs.index("current")))
        self._row(controls, "输入视频", self.input, 0, 58)
        ttk.Button(controls, text="选择", command=self._browse_input).grid(row=0, column=2)
        self._row(controls, "接收视频", self.output, 1, 58)
        ttk.Button(controls, text="选择", command=self._browse_output).grid(row=1, column=2)
        self._row(controls, "IIO USB URI", self.uri, 2)
        ttk.Button(controls, text="检测板卡", command=self._detect_radio).grid(row=2, column=2, sticky="w")
        ttk.Label(controls, text="链路模式").grid(row=3, column=0, sticky="w", padx=5)
        ttk.Combobox(controls, textvariable=self.mode, values=("digital", "rf", "simulate"),
                     width=16, state="readonly").grid(row=3, column=1, sticky="w", padx=5)
        ttk.Label(controls, text="digital=芯片内部回环，rf=天线/射频线，simulate=无板测试").grid(
            row=3, column=2, sticky="w")
        fields = ttk.Frame(controls)
        fields.grid(row=4, column=0, columnspan=3, sticky="ew", pady=5)
        for i, (name, var) in enumerate((
            ("通道 0/1", self.channel), ("频率 MHz", self.frequency),
            ("TX 增益 dB", self.tx_gain), ("RX 增益 dB", self.rx_gain),
            ("宽", self.width), ("高", self.height), ("目标 fps", self.fps),
            ("JPEG 质量", self.quality), ("最多帧数 (0=全部)", self.max_frames),
            ("IQ 采样率 MSPS", self.sample_rate), ("每符号采样 2/4/8", self.sps),
            ("每批包数（原码流最多512）", self.batch_packets), ("视频段秒数", self.segment_seconds),
            ("起播缓冲段数", self.playback_buffer_segments))):
            col = (i % 3) * 2
            row = i // 3
            ttk.Label(fields, text=name).grid(row=row, column=col, padx=(6, 2), pady=4)
            entry = ttk.Entry(fields, textvariable=var, width=10)
            entry.grid(row=row, column=col + 1, padx=2)
            if var is self.max_frames:
                self.max_frames_entry = entry
        toolbar = ttk.Frame(area)
        toolbar.grid(row=0, column=0, columnspan=3, sticky="ew", pady=4)
        self.start_button = ttk.Button(toolbar, text="开始发送并接收", command=self._start)
        self.start_button.pack(side="left", padx=4)
        ttk.Button(toolbar, text="停止", command=self.stop.set).pack(side="left", padx=4)
        buttons = ttk.Frame(controls)
        buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Label(buttons, text="调制").pack(side="left", padx=(12, 3))
        ttk.Combobox(buttons, textvariable=self.modulation,
                     values=("qpsk", "16qam", "64qam", "256qam"), width=7,
                     state="readonly").pack(side="left")
        ttk.Label(buttons, text="传输").pack(side="left", padx=(10, 3))
        ttk.Combobox(buttons, textvariable=self.transport,
                     values=("frame", "stream", "original"), width=9,
                     state="readonly").pack(side="left")
        ttk.Label(buttons, text="码率 kbps").pack(side="left", padx=(10, 3))
        ttk.Entry(buttons, textvariable=self.stream_bitrate, width=5).pack(side="left")
        ttk.Checkbutton(buttons, text="CRC 校验", variable=self.check_crc,
                        command=lambda: self.summary.set("CRC 设置将在下一次开始时生效")).pack(side="left", padx=8)
        ttk.Label(buttons, text="编码").pack(side="left", padx=3)
        ttk.Combobox(buttons, textvariable=self.encoder_preset,
                     values=("veryfast", "fast", "medium", "slow"), width=8,
                     state="readonly").pack(side="left")
        playback_options = ttk.Frame(controls)
        playback_options.grid(row=6, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Label(playback_options, text="播放方式").pack(side="left", padx=5)
        ttk.Combobox(playback_options, textvariable=self.playback_mode,
                     values=("预缓冲", "实时（低延迟）", "完整接收后播放（原画质）"), state="readonly", width=26).pack(side="left")
        ttk.Label(playback_options, text="完整接收后播放适用于 original：保留画质，需等待传输完成").pack(side="left", padx=12)
        waveform_options = ttk.Frame(controls)
        waveform_options.grid(row=7, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Label(waveform_options, text="波形").pack(side="left", padx=5)
        ttk.Combobox(waveform_options, textvariable=self.waveform,
                     values=("single-carrier", "ofdm"), width=14, state="readonly").pack(side="left")
        ttk.Label(waveform_options, text="OFDM FFT").pack(side='left',padx=(10,3))
        ttk.Combobox(waveform_options,textvariable=self.ofdm_fft_size,values=('256','512','1024','2048'),
                     width=6,state='readonly').pack(side='left')
        ttk.Label(waveform_options, text="CP=64；独立 CSI 固定使用 256 FFT").pack(side="left")
        presets = ttk.LabelFrame(controls, text="视频参数预设（点击加载，下次开始生效）", padding=6)
        presets.grid(row=9, column=0, columnspan=3, sticky="ew", pady=8)
        self.preset_buttons = {}
        for index, (name, label) in enumerate((
                ("ofdm-original-720p", "OFDM 原码流 / 保持源画质"),
                ("ofdm-original-1080p30-64qam", "1080p30 原画质 / 64QAM"),
                ("ofdm-video", "OFDM 360p / 150 kbps"),
                ("ofdm-1080p60", "OFDM 1080p60 / 400 kbps"),
                ("720p30-rf", "单载波 720p30 / 500 kbps"),
                ("720p30", "单载波 720p30 / 3 Mbps（试验）"),
                ("legacy", "低速 96×54 / 15 kbps"))):
            presets.columnconfigure(index % 3, weight=1, uniform="presets")
            button = ttk.Button(presets, text=label, command=lambda key=name: self._profile(key))
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4)
            self.preset_buttons[name] = button
        ttk.Label(presets, text="原码流保留完整文件，不支持最多帧数限制；分辨率/帧率/码率以源文件为准。当前链路可能卡顿。",
                  wraplength=850).grid(row=3, column=0, columnspan=3, sticky="w", padx=4, pady=4)
        ttk.Label(area, textvariable=self.summary).grid(row=4, column=0, columnspan=3, sticky="w")

        panes = ttk.Panedwindow(area, orient="horizontal")
        self.main_panes = panes
        panes.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=6)
        area.rowconfigure(2, weight=1)
        self.video_panels = []
        for side, caption in (("tx", self.tx_caption), ("rx", self.rx_caption)):
            panel = ttk.LabelFrame(panes, text="发送端" if side == "tx" else "接收端", padding=8)
            setattr(self, f"{side}_panel", panel)
            panes.add(panel, weight=1)
            panel.columnconfigure(0, weight=1)
            panel.rowconfigure(1, weight=1)
            header=ttk.Frame(panel)
            header.grid(row=0,column=0,sticky='ew',pady=4)
            ttk.Label(header,textvariable=caption).pack(side='left',fill='x',expand=True)
            if side=='rx':
                self.rx_fullscreen_button=ttk.Button(header,text='全屏',command=self._open_rx_fullscreen)
                self.rx_fullscreen_button.pack(side='right',padx=(8,0))
            preview = tk.Canvas(panel, width=640, height=360, bg="#101923", highlightthickness=0)
            preview.grid(row=1, column=0, sticky="nsew", pady=(0, 12))
            setattr(self, f"{side}_preview", preview)
            if side=='rx': preview.bind('<Double-Button-1>',lambda event:self._open_rx_fullscreen())
            self.video_panels.append(preview)
            preview.bind("<Configure>", lambda event, name=side: self._schedule_preview_resize(name))
        charts = ttk.Frame(self.tx_panel, height=280)
        self.legacy_charts = charts
        charts.grid(row=2, column=0, sticky="ew")
        charts.grid_propagate(False)
        charts.columnconfigure((0, 1), weight=1, uniform="charts")
        charts.rowconfigure(1, weight=1)
        for column, (side, chart_caption, color) in enumerate((
                ("tx", self.tx_constellation_caption, "#a3f96b"),
                ("rx", self.rx_constellation_caption, "#53d9ff"))):
            ttk.Label(charts, textvariable=chart_caption, wraplength=320).grid(
                row=0, column=column, sticky="w", padx=4, pady=4)
            chart = tk.Canvas(charts, width=320, height=230, bg="#0b1424", highlightthickness=0)
            chart.grid(row=1, column=column, sticky="nsew", padx=4, pady=4)
            setattr(self, f"{side}_canvas", chart)
            chart.bind("<Configure>", lambda event, target=chart, shade=color:
                       self._schedule_chart_resize(target, shade))
            self._draw_constellation(chart, None, color)
        self.reserved_panel = ttk.Frame(self.rx_panel, height=280)
        self.reserved_panel.grid(row=2, column=0, sticky="ew")
        self.reserved_panel.grid_propagate(False)
        self.reserved_panel.columnconfigure(0, weight=1)
        self.reserved_panel.rowconfigure(0, weight=1)
        self.channel_panel = ChannelPanel(self.reserved_panel, self.summary.set)
        self.channel_panel.grid(row=0, column=0, sticky="nsew")
        self.ofdm_panel = OfdmPanel(area, controls, self.channel_tab)
        self.ofdm_panel.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)
        self.ofdm_panel.grid_remove()
        self.log = tk.Text(area, height=3, state="disabled")
        self.log.grid(row=5, column=0, columnspan=3, sticky="ew", pady=3)
        self.latest_chart_time = 0.0
        self._link_wait_message=''
        self.live_chart_updates = 0

    def _toggle_controls(self):
        self.tabs.select(self.controls)

    def _update_transport_controls(self, *_):
        original = self.transport.get() == "original"
        if original:
            self.max_frames.set("0")
        self.max_frames_entry.configure(state="disabled" if original else "normal")

    def _update_waveform_view(self):
        active = self.waveform.get() == "ofdm"
        if active:
            self.legacy_charts.grid_remove()
            self.reserved_panel.grid_remove()
            self.ofdm_panel.grid()
        else:
            self.legacy_charts.grid()
            self.reserved_panel.grid()
            self.ofdm_panel.grid_remove()

    def _schedule_preview_resize(self, side):
        pending = self._resize_jobs.get(side)
        if pending is not None:
            self.root.after_cancel(pending)
        self._resize_jobs[side] = self.root.after(80, lambda: self._resize_preview(side))

    def _resize_preview(self, side):
        self._resize_jobs[side] = None
        image = self._preview_images.get(side)
        if image is not None:
            self._display_image(getattr(self, f"{side}_preview"), image, side)

    def _schedule_chart_resize(self, canvas, color):
        pending = self._chart_resize_jobs.get(canvas)
        if pending is not None:
            self.root.after_cancel(pending)
        self._chart_resize_jobs[canvas] = self.root.after(
            60, lambda: self._resize_chart(canvas, color))

    def _resize_chart(self, canvas, color):
        self._chart_resize_jobs[canvas] = None
        self._draw_constellation(canvas, self._chart_points.get(canvas), color)

    def _draw_constellation(self, canvas: tk.Canvas, points: np.ndarray | None,
                            color: str) -> None:
        drawing_start = time.perf_counter()
        self._chart_points[canvas] = points
        width = canvas.winfo_width() if canvas.winfo_width() > 1 else int(canvas.cget("width"))
        height = canvas.winfo_height() if canvas.winfo_height() > 1 else int(canvas.cget("height"))
        img = Image.new("RGB", (width, height), "#0b1424")
        draw = ImageDraw.Draw(img)
        cx, cy = width / 2, height / 2
        scale = min(width, height) / 3.2
        draw.line((12, cy, width - 12, cy), fill="#52627b", width=1)
        draw.line((cx, 12, cx, height - 12), fill="#52627b", width=1)
        draw.text((width - 18, cy - 20), "I", fill="#95a7c0")
        draw.text((cx + 10, 14), "Q", fill="#95a7c0")
        if color == "#a3f96b" and points is not None:
            count = len(np.unique(np.round(points.real, 4)))
            if count in (2, 4, 8, 16):
                self._plot_bits_per_axis = round(math.log2(count))
        bits_per_axis = getattr(self, "_plot_bits_per_axis", {"qpsk": 1, "16qam": 2,
                              "64qam": 3, "256qam": 4}[self.modulation.get()])
        max_level = 2 ** bits_per_axis - 1
        scale_qam = math.sqrt(2 * (2 ** (2 * bits_per_axis) - 1) / 3)
        levels = tuple(x / scale_qam for x in range(-max_level, max_level + 1, 2))
        for re in levels:
            for im in levels:
                x, y = cx + re * scale, cy - im * scale
                draw.ellipse((x - 7, y - 7, x + 7, y + 7), outline="#4a6175")
        if points is not None:
            radius = 5 if color == "#a3f96b" else 2
            for symbol in points:
                x = cx + float(np.real(symbol)) * scale
                y = cy - float(np.imag(symbol)) * scale
                if 4 <= x <= width - 4 and 4 <= y <= height - 4:
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                                 fill=color)
        if canvas not in self.chart_photos:
            photo = ImageTk.PhotoImage(img)
            self.chart_photos[canvas] = photo
            canvas.create_image(0, 0, image=photo, anchor="nw")
        else:
            if self.chart_photos[canvas].width() != width or self.chart_photos[canvas].height() != height:
                photo = ImageTk.PhotoImage(img)
                self.chart_photos[canvas] = photo
                canvas.delete("all")
                canvas.create_image(0, 0, image=photo, anchor="nw")
            else:
                self.chart_photos[canvas].paste(img)
        self.ui_chart_seconds += time.perf_counter() - drawing_start
        self.ui_chart_calls += 1

    def _display_image(self, canvas, image, side):
        width, height = max(100, canvas.winfo_width()), max(100, canvas.winfo_height())
        self._preview_images[side] = image
        if side=='rx' and getattr(self,'_rx_fullscreen',None) is not None:
            self._render_rx_fullscreen()
            return self.rx_photo
        size = (width, height)
        image = (ImageOps.contain(image, size, method=Image.Resampling.BILINEAR)
                 if getattr(self, 'active_original', False) else
                 ImageOps.fit(image, size, method=Image.Resampling.BILINEAR, centering=(0.5, 0.5)))
        photo = getattr(self, f"{side}_photo")
        if photo is not None and (photo.width(), photo.height()) == image.size:
            photo.paste(image)
        else:
            photo = ImageTk.PhotoImage(image)
            canvas.delete("all")
            canvas.create_image(width / 2, height / 2, image=photo, anchor="center", tags="video")
            setattr(self, f"{side}_photo", photo)
        canvas.coords("video", width / 2, height / 2)
        return photo

    def _open_rx_fullscreen(self):
        if self._rx_fullscreen is not None:
            self._rx_fullscreen.lift()
            return
        window=tk.Toplevel(self.root)
        self._rx_fullscreen=window
        window.title('接收端视频全屏')
        window.configure(bg='black')
        window.withdraw()
        self._rx_fullscreen_canvas=tk.Canvas(window,bg='black',highlightthickness=0)
        self._rx_fullscreen_canvas.pack(fill='both',expand=True)
        self._rx_fullscreen_canvas.bind('<Configure>',lambda event:self._render_rx_fullscreen())
        window.bind('<Escape>',lambda event:self._close_rx_fullscreen())
        window.protocol('WM_DELETE_WINDOW',self._close_rx_fullscreen)
        ttk.Button(window,text='退出全屏（Esc）',command=self._close_rx_fullscreen).place(
            relx=1,x=-16,y=16,anchor='ne')
        self.fullscreen_requested.set()
        self.ofdm_panel.suspend_rendering=True
        self.ofdm_panel.csi_panel.suspend_rendering=True
        self._place_rx_fullscreen(window)
        window.focus_set()
        self._render_rx_fullscreen()

    def _place_rx_fullscreen(self,window):
        if os.name!='nt':
            window.attributes('-fullscreen',True)
            window.deiconify()
            return
        # Tk's fullscreen state can select the primary display. Use the main
        # window's native monitor bounds, including negative desktop coordinates.
        import ctypes
        from ctypes import wintypes
        class MonitorInfo(ctypes.Structure):
            _fields_=[('cbSize',wintypes.DWORD),('rcMonitor',wintypes.RECT),
                      ('rcWork',wintypes.RECT),('dwFlags',wintypes.DWORD)]
        user32=ctypes.WinDLL('user32',use_last_error=True)
        user32.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
        user32.GetAncestor.restype=wintypes.HWND
        user32.MonitorFromWindow.argtypes=[wintypes.HWND,wintypes.DWORD]
        user32.MonitorFromWindow.restype=wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes=[wintypes.HANDLE,ctypes.POINTER(MonitorInfo)]
        user32.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,
                                     ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.UINT]
        self.root.update_idletasks()
        owner=user32.GetAncestor(self.root.winfo_id(),2)
        monitor=user32.MonitorFromWindow(owner,2)
        info=MonitorInfo();info.cbSize=ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor,ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        window.overrideredirect(True)
        window.update_idletasks()
        window.deiconify()
        window.update_idletasks()
        hwnd=user32.GetAncestor(window.winfo_id(),2)
        rect=info.rcMonitor
        if not user32.SetWindowPos(hwnd,None,rect.left,rect.top,
                                  rect.right-rect.left,rect.bottom-rect.top,0x0040):
            raise ctypes.WinError(ctypes.get_last_error())
        window.lift()

    def _render_rx_fullscreen(self):
        if getattr(self,'_rx_fullscreen',None) is None: return
        canvas=self._rx_fullscreen_canvas
        image=self._preview_images.get('rx')
        width,height=max(1,canvas.winfo_width()),max(1,canvas.winfo_height())
        if image is None:
            canvas.delete('all')
            self._rx_fullscreen_photo=None
            canvas.create_text(width/2,height/2,text='等待接收视频',fill='white',tags='waiting')
            return
        scale=min(width/image.width,height/image.height)
        size=(max(1,round(image.width*scale)),max(1,round(image.height*scale)))
        if image.size!=size:
            cached=self._rx_fullscreen_rgb
            rgb=cached[1] if cached is not None and cached[0] is image else np.asarray(image)
            image=Image.fromarray(cv2.resize(rgb,size,interpolation=cv2.INTER_LINEAR))
        photo=self._rx_fullscreen_photo
        if photo is not None and (photo.width(),photo.height())==image.size:
            photo.paste(image)
        else:
            photo=ImageTk.PhotoImage(image)
            self._rx_fullscreen_photo=photo
            canvas.delete('all')
            canvas.create_image(width/2,height/2,image=photo,tags='video')
        canvas.coords('video',width/2,height/2)

    def _close_rx_fullscreen(self,restore=True):
        window=getattr(self,'_rx_fullscreen',None)
        if window is None: return
        self._rx_fullscreen=None
        self.fullscreen_requested.clear()
        self._rx_fullscreen_photo=None
        self._rx_fullscreen_rgb=None
        window.destroy()
        self.ofdm_panel.suspend_rendering=False
        self.ofdm_panel.csi_panel.suspend_rendering=False
        if restore:
            for side in ('tx','rx'):
                image=self._preview_images.get(side)
                if image is not None: self._display_image(getattr(self,side+'_preview'),image,side)
            self.ofdm_panel.draw()
            self.ofdm_panel.csi_panel.draw()

    def _show_frame(self, canvas, frame):
        side = "tx" if canvas is self.tx_preview else "rx"
        if frame is None:
            canvas.delete("all")
            canvas.create_text(canvas.winfo_width()/2, canvas.winfo_height()/2,
                               text="本帧未通过校验", fill="white")
            setattr(self, f"{side}_photo", None)
            self._preview_images.pop(side, None)
            return None
        return self._display_image(canvas, Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), side)

    def _show_pair(self, frame_no, encoded, received, identical=False, sent_at=None):
        started = time.perf_counter()
        fullscreen=getattr(self,'_rx_fullscreen',None) is not None
        if fullscreen:
            rgb=cv2.imdecode(np.frombuffer(encoded,np.uint8),getattr(cv2,'IMREAD_COLOR_RGB',cv2.IMREAD_COLOR))
            if rgb is None: raise ValueError('接收预览图像解码失败')
            if not hasattr(cv2,'IMREAD_COLOR_RGB'): rgb=cv2.cvtColor(rgb,cv2.COLOR_BGR2RGB)
            middle=rgb.shape[1]//2
            self._preview_images['tx']=Image.fromarray(rgb if identical else rgb[:,:middle])
            rx_rgb=rgb if identical else rgb[:,middle:]
            self._preview_images['rx']=self._preview_images['tx'] if identical else Image.fromarray(rx_rgb)
            self._rx_fullscreen_rgb=(self._preview_images['rx'],rx_rgb)
            self._render_rx_fullscreen()
        else:
            self._show_windowed_pair(encoded,identical)
        self.tx_caption.set(f"编码后发送画面：第 {frame_no + 1} 帧")
        self.rx_caption.set(f"接收画面：第 {frame_no + 1} 帧" +
                            (("（通过 CRC）" if self.active_crc else "（CRC 未校验）") if received else "（丢失）"))
        if not self.finished:
            self.summary.set(f"运行中：播放第 {frame_no + 1} 帧")
        self.ui_pair_seconds += time.perf_counter() - started
        self.ui_pair_calls += 1
        if getattr(self, 'active_original', False):
            self.tx_caption.set(f"原码流核对后画面：第 {frame_no + 1} 帧")
        displayed_at = time.monotonic()
        self._display_times.append(displayed_at)
        self._display_frame_numbers.append(frame_no)
        if sent_at is not None:
            self._display_ages.append((displayed_at - sent_at) * 1000)
        if not self.finished and len(self._display_times) > 1:
            recent = self._display_times[-60:]
            display_fps = (len(recent)-1) / max(1e-6, recent[-1]-recent[0])
            recent_max = max(np.diff(recent)) * 1000
            self.summary.set(f"播放第 {frame_no + 1} 帧 | 实际界面刷新 {display_fps:.1f} fps | "
                             f"近 60 帧最大间隔 {recent_max:.0f} ms")
        if getattr(self,'_link_wait_message','') and not self.finished:
            self.summary.set(self._link_wait_message)

    def _show_windowed_pair(self,encoded,identical):
        with Image.open(BytesIO(encoded)) as source:
            source.load()
            if identical:
                image = source.copy()
                photo=self._display_image(self.tx_preview, image, "tx")
                tx_size=(self.tx_preview.winfo_width(),self.tx_preview.winfo_height())
                rx_size=(self.rx_preview.winfo_width(),self.rx_preview.winfo_height())
                if tx_size==rx_size:
                    self._preview_images['rx']=image
                    self.rx_photo=photo
                    if not self.rx_preview.find_withtag('video'):
                        self.rx_preview.delete('all')
                        self.rx_preview.create_image(rx_size[0]/2,rx_size[1]/2,
                                                     image=photo,anchor='center',tags='video')
                    else:
                        self.rx_preview.itemconfigure('video',image=photo)
                        self.rx_preview.coords('video',rx_size[0]/2,rx_size[1]/2)
                else:
                    self._display_image(self.rx_preview, image, "rx")
            else:
                middle = source.width // 2
                self._display_image(self.tx_preview, source.crop((0, 0, middle, source.height)), "tx")
                self._display_image(self.rx_preview, source.crop((middle, 0, source.width, source.height)), "rx")

    def _browse_input(self):
        path = filedialog.askopenfilename(filetypes=[("视频文件", "*.mp4 *.avi *.mov *.mkv"),
                                                   ("所有文件", "*.*")])
        if path:
            self.input.set(path)

    def _browse_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if path:
            self.output.set(path)

    def _detect_radio(self):
        if self.worker is not None and self.worker.is_alive():
            self.summary.set("请在停止传输后检测板卡")
            return
        def detect():
            try:
                from .native import prepare_libiio
                prepare_libiio()
                import iio
                devices = iio.scan_contexts()
                self.events.put(("devices", devices))
            except Exception as exc:
                self.events.put(("status", f"检测失败：{exc}"))
        self.summary.set("正在检测 IIO 板卡…")
        Thread(target=detect, daemon=True).start()

    def _profile(self, name):
        preset = PROFILES[name]
        for key in ("width", "height", "fps", "transport", "modulation", "sps",
                    "batch_packets", "segment_seconds"):
            getattr(self, key).set(str(preset[key]))
        self.stream_bitrate.set(str(preset["bitrate_kbps"]))
        self.waveform.set(preset.get("waveform", "single-carrier"))
        self.ofdm_fft_size.set(str(preset.get('ofdm_fft_size',256)))
        self.ofdm_training_packets=0
        if 'mode' in preset: self.mode.set(preset['mode'])
        if 'crc' in preset: self.check_crc.set(preset['crc'])
        if 'playback_mode' in preset:
            self.playback_mode.set({'realtime':'实时（低延迟）','buffered':'预缓冲',
                                   'download':'完整接收后播放（原画质）'}[preset['playback_mode']])
        if name in PROFILE_FILES:
            paths=PROFILE_FILES[name]
            self.input.set(str((Path.cwd()/paths['input']).resolve()))
            self.output.set(str((Path.cwd()/paths['output']).resolve()))
            self.report_path=(Path.cwd()/paths['report']).resolve()
        self._update_waveform_view()
        self.sample_rate.set(str(preset["sample_rate"] / 1e6))
        self.playback_buffer_segments.set(str(preset.get("playback_buffer_segments", 1)))
        for key in ("tx_gain", "rx_gain"):
            if key in preset:
                getattr(self, key).set(str(preset[key]))
        if preset["transport"] == "original":
            self.summary.set(f"已加载 {preset['width']}×{preset['height']} / {preset['fps']} fps 原码流，"
                             f"{preset['modulation'].upper()}、{preset['sample_rate']/1e6:g} MSPS、FFT {self.ofdm_fft_size.get()}；保持源画质")
        else:
            self.summary.set(f"已加载预设：{preset['width']}×{preset['height']} / {preset['fps']} fps，"
                             f"{preset['bitrate_kbps']} kbps 转码；码率越低越容易模糊。")

    def _radio_settings(self, csi=False) -> RadioSettings:
        return RadioSettings(
            uri=self.uri.get().strip(), mode=self.mode.get(), channel=int(self.channel.get()),
            waveform='ofdm' if csi else self.waveform.get(),
            ofdm_superframe=False if csi else self.transport.get() == "original",
            ofdm_fft_size=256 if csi else int(self.ofdm_fft_size.get()),
            ofdm_training_packets=0 if csi else self.ofdm_training_packets,
            ofdm_streaming=not csi and self.waveform.get()=='ofdm' and self.transport.get()=='original',
            frequency_hz=round(float(self.frequency.get()) * 1_000_000),
            tx_gain_db=float(self.tx_gain.get()), rx_gain_db=float(self.rx_gain.get()),
            modulation='qpsk' if csi else self.modulation.get(), sample_rate=round(float(self.sample_rate.get()) * 1e6),
            samples_per_symbol=2 if csi else int(self.sps.get()),
            batch_packets=1 if csi else int(self.batch_packets.get()),
            check_crc=True if csi else self.check_crc.get())

    def _settings(self) -> VideoSettings:
        radio = self._radio_settings()
        return VideoSettings(
            Path(self.input.get()), Path(self.output.get()), radio,
            width=int(self.width.get()), height=int(self.height.get()),
            target_fps=float(self.fps.get()), jpeg_quality=int(self.quality.get()),
            max_frames=0 if self.transport.get() == "original" else int(self.max_frames.get()),
            transport=self.transport.get(),
            stream_bitrate=int(self.stream_bitrate.get()) * 1000,
            segment_seconds=float(self.segment_seconds.get()),
            playback_buffer_segments=int(self.playback_buffer_segments.get()), constellation_hz=20,
            encoder_preset=self.encoder_preset.get(),
            playback_mode=('download' if self.playback_mode.get().startswith('完整接收') else
                           'realtime' if self.playback_mode.get().startswith('实时') else 'buffered'))

    def _start(self):
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            settings = self._settings()
            if settings.playback_mode == 'download' and settings.transport != 'original':
                raise ValueError('完整接收后播放须选择 original 原码流传输')
            if not settings.input_path.is_file():
                raise ValueError("请选择存在的视频文件")
            if settings.output_path.resolve() == settings.input_path.resolve():
                raise ValueError("接收视频不能覆盖输入视频")
        except (ValueError, OSError) as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        self._reset_run_queues()
        self._link_wait_message=''
        self.tabs.select(self.demo_tab)
        self._preview_images.clear()
        if self._rx_fullscreen is not None: self._render_rx_fullscreen()
        self.channel_panel.reset(settings.radio)
        self._update_waveform_view()
        self.ofdm_panel.csi_panel.render_interval=.1
        self.ofdm_panel.reset(settings.radio)
        while True:
            try:
                self.channel_events.get_nowait()
            except Empty:
                break
        self._latest_chart_sample = None
        self.chart_display_times.clear()
        self._chart_age_ms.clear()
        while True:
            try:
                self.chart_events.get_nowait()
            except Empty:
                break
        self.stop.clear()
        self.active_crc = settings.radio.check_crc
        self.active_original = settings.transport == "original"
        self.finished = False
        self.start_button.configure(state="disabled")
        self.csi_start_button.configure(state='disabled')
        self.transfer_bar.stop()
        self.transfer_percent.set(0)
        if settings.transport=='original':
            self.transfer_bar.configure(mode='determinate')
            self.transfer_caption.set('传输进度：0.0% · 正在准备')
        else:
            self.transfer_bar.configure(mode='indeterminate')
            self.transfer_caption.set('传输中')
            self.transfer_bar.start(50)
        self.summary.set("正在发送与接收…")
        self.summary.set(f"{settings.radio.waveform} / {settings.radio.modulation} / {settings.radio.mode} — 正在发送与接收")
        self.channel_mode.set('当前：视频附带 CSI，刷新受视频传输批次限制；停止视频后可开始独立 20 Hz 采集')
        if settings.radio.waveform == "ofdm":
            self.channel_panel.check_stale()
        self.rx_frames_seen = 0
        self.rx_packets_seen = 0
        self.tx_packets_seen = 0
        self.last_compared_frame = None
        self.pending_tx_frame = None
        self.tx_photo = None
        self.rx_photo = None
        self.pair_photo = None
        self.ui_pair_seconds = 0.0
        self.ui_chart_seconds = 0.0
        self.ui_poll_seconds = 0.0
        self.ui_pair_calls = 0
        self._display_times.clear()
        self._display_ages.clear()
        self._display_frame_numbers.clear()
        self.ui_chart_calls = 0
        for canvas in self.video_panels:
            canvas.delete("all")
        self.latest_chart_time = 0.0
        self.live_chart_updates = 0
        self.tx_caption.set("编码后发送画面：等待")
        self.rx_caption.set("接收画面：等待")
        self.tx_constellation_caption.set("发送星座图：等待")
        self.rx_constellation_caption.set("接收星座图：等待")
        self._draw_constellation(self.tx_canvas, None, "#a3f96b")
        self._draw_constellation(self.rx_canvas, None, "#53d9ff")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        self.worker = self.context.Process(
            target=_run_worker,
            args=(settings, self.events, self.stop, self.report_path, self.chart_events,
                  self.channel_events, self.preview_events,self.fullscreen_requested),
            daemon=True)
        self._launch_worker()

    def _reset_run_queues(self):
        # The previous worker has exited. Fresh IPC prevents delayed feeder
        # messages from a previous run from resetting the current UI.
        for name, size in (('events', 0), ('preview_events', 1),
                           ('chart_events', 1), ('channel_events', 1)):
            getattr(self, name).close()
            setattr(self, name, self.context.Queue(maxsize=size))
        self._worker_dead_since = None

    def _launch_worker(self):
        try:
            self.worker.start()
        except Exception as exc:
            self.worker = None
            self.finished = True
            self.start_button.configure(state='normal')
            self.csi_start_button.configure(state='normal')
            self.summary.set(f'启动失败：{exc}')

    def _check_worker_exit(self):
        if self.worker is None or self.finished or self.worker.is_alive():
            self._worker_dead_since = None
            return
        # Allow time for queued done/error events to reach the UI first.
        if getattr(self, '_worker_dead_since', None) is None:
            self._worker_dead_since = time.monotonic()
        elif time.monotonic() - self._worker_dead_since > 1:
            self.ofdm_panel.waterfall.freeze()
            self.transfer_bar.stop()
            self.transfer_caption.set(f'传输中断 · {self.transfer_percent.get():.1f}%')
            self.finished = True
            self.start_button.configure(state='normal')
            self.csi_start_button.configure(state='normal')
            self.summary.set(f'工作进程意外退出（退出码 {self.worker.exitcode}），可重新开始')

    def _start_csi(self):
        if self.worker is not None and self.worker.is_alive():
            self.summary.set('请先停止当前视频或采集，再开始独立 CSI 采集')
            return
        try:
            settings=self._radio_settings(csi=True)
            duration=getattr(self,'csi_duration',0)
            if not math.isfinite(duration) or duration < 0:
                raise ValueError('CSI 采集时长须为非负有限数，0 表示手动停止')
            path=Path.cwd()/'examples'/datetime.now().strftime('csi_live_%Y%m%d_%H%M%S_%f.csv')
            path.parent.mkdir(parents=True,exist_ok=True)
        except (ValueError,OSError,OverflowError) as exc:
            messagebox.showerror('参数错误',str(exc))
            return
        self._reset_run_queues()
        self.stop.clear()
        self.finished=False
        self._display_times.clear()
        self._display_ages.clear()
        self._display_frame_numbers.clear()
        self.ui_pair_calls=0
        self.ui_pair_seconds=self.ui_chart_seconds=self.ui_poll_seconds=0.0
        self.ui_chart_calls=self.live_chart_updates=0
        self._chart_age_ms.clear()
        self.chart_display_times.clear()
        self._latest_chart_sample=None
        self.latest_chart_time=0.0
        self.ofdm_panel.csi_panel.render_interval=.05
        self.ofdm_panel.reset(settings)
        self.tabs.select(self.channel_tab)
        self.start_button.configure(state='disabled')
        self.csi_start_button.configure(state='disabled')
        self.csi_report_path=path.with_suffix('.report.json')
        self.summary.set('独立 CSI 采集中：目标 20 Hz，实际速率见信道状态')
        self.channel_mode.set('当前：独立 CSI 采集，目标 20 Hz；未传输视频')
        self.worker=self.context.Process(target=_run_csi_worker,
            args=(settings,self.events,self.stop,path,self.channel_events,getattr(self,'csi_duration',0)),daemon=True)
        self._launch_worker()

    def _poll(self):
        poll_start = time.perf_counter()
        latest_pair = self._take_preview()
        self._pending_tx_points = None
        self._pending_rx_points = None
        latest_channel = None
        try:
            while True:
                latest_channel = self.channel_events.get_nowait()
        except Empty:
            pass
        if latest_channel is not None:
            if latest_channel.get("kind") == "ofdm":
                self.ofdm_panel.update_sample(latest_channel)
            else:
                self.channel_panel.update_sample(latest_channel)
        self.channel_panel.check_stale()
        self.ofdm_panel.check_stale()
        try:
            while True:
                sample = self.chart_events.get_nowait()
                if sample[1] is not None:
                    self._latest_chart_sample = sample
        except Empty:
            pass
        try:
            while time.perf_counter() - poll_start < 0.012:
                kind, value = self.events.get_nowait()
                if kind == 'throughput_reference':
                    self.ofdm_panel.quality_panel.required_mbps=value
                elif kind == 'transfer_progress':
                    received,total,stamp=value
                    self.ofdm_panel.quality_panel.update_progress(received,total,stamp)
                    if total>0 and received>=total:
                        self.ofdm_panel.waterfall.freeze()
                    percent=min(100,max(0,received*100/total)) if total else 0
                    self.transfer_percent.set(percent)
                    self.transfer_caption.set(f'传输进度：{percent:.1f}% · {received/1048576:.2f} / {total/1048576:.2f} MB')
                elif kind == "status":
                    if str(value).startswith('等待信号：'):
                        self.ofdm_panel.quality_panel.set_waiting(True)
                        self._link_wait_message=str(value)
                        self.summary.set(str(value))
                    elif str(value).startswith('信号已恢复'):
                        self.ofdm_panel.quality_panel.set_waiting(False)
                        self._link_wait_message=''
                        self.summary.set(str(value))
                    self.log.configure(state="normal")
                    self.log.insert("end", str(value) + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "channel_log":
                    self.channel_panel.csv_path = value
                elif kind == 'csi_log':
                    self.ofdm_panel.csi_panel.csv_path = value
                elif kind == "devices":
                    selected = next((uri for uri in value if uri.startswith("usb:")), None)
                    if selected:
                        self.uri.set(selected)
                    self.summary.set("；".join(f"{uri}: {label}" for uri, label in value.items())
                                     if value else "未检测到 IIO 板卡")
                elif kind == "pair_jpeg":
                    latest_pair = value
                elif kind == "tx_frame":
                    frame_no, frame = value
                    self.pending_tx_frame = (frame_no, frame)
                    if self.last_compared_frame is None:
                        self.tx_photo = self._show_frame(self.tx_preview, frame)
                        self.tx_caption.set(f"编码后发送画面：第 {frame_no + 1} 帧，等待接收")
                        self.rx_caption.set(f"接收画面：正在等待第 {frame_no + 1} 帧")
                    else:
                        self.tx_caption.set(
                            f"编码后发送画面：对比第 {self.last_compared_frame + 1} 帧；"
                            f"射频正在发送第 {frame_no + 1} 帧")
                    self.summary.set(
                        f"运行中：已收到 {self.rx_frames_seen} 帧 / {self.rx_packets_seen} 包；"
                        f"当前发送第 {frame_no + 1} 帧")
                elif kind == "rx_frame":
                    frame_no, frame = value
                    if self.pending_tx_frame is not None and self.pending_tx_frame[0] == frame_no:
                        self.tx_photo = self._show_frame(self.tx_preview,
                                                         self.pending_tx_frame[1])
                    self.pending_tx_frame = None
                    self.last_compared_frame = frame_no
                    self.tx_caption.set(f"编码后发送画面：第 {frame_no + 1} 帧（与接收帧对齐）")
                    self.rx_photo = self._show_frame(self.rx_preview, frame)
                    if frame is not None:
                        self.rx_frames_seen += 1
                        self.rx_caption.set(
                            f"接收画面：第 {frame_no + 1} 帧，{'CRC 通过' if self.active_crc else 'CRC 未校验'}，"
                            f"刷新 {datetime.now():%H:%M:%S}")
                    else:
                        self.rx_caption.set(f"接收画面：第 {frame_no + 1} 帧接收失败")
                    self.summary.set(
                        f"运行中：已收到 {self.rx_frames_seen} 帧 / {self.rx_packets_seen} 包")
                elif kind == "constellation":
                    tx_points, rx_points = value
                    self._pending_tx_points = tx_points
                    if rx_points is not None:
                        self._pending_rx_points = rx_points
                        self.latest_chart_time = time.monotonic()
                        self.live_chart_updates += 1
                elif kind == "error":
                    messagebox.showerror("运行失败", str(value))
                elif kind == 'cancelled':
                    self.ofdm_panel.waterfall.freeze()
                    self.transfer_bar.stop()
                    self.transfer_caption.set(f'已停止 · {self.transfer_percent.get():.1f}%')
                    self.finished = True
                    self.start_button.configure(state='normal')
                    self.csi_start_button.configure(state='normal')
                    self.summary.set(str(value))
                    self.channel_mode.set('已停止；可选择独立 CSI 采集或视频传输')
                elif kind == "done":
                    self.ofdm_panel.waterfall.freeze()
                    self.transfer_bar.stop()
                    if value is None:
                        self.transfer_caption.set(f'传输失败 · {self.transfer_percent.get():.1f}%')
                    elif not value.get('csi_only') and not self.active_original:
                        self.transfer_bar.configure(mode='determinate')
                        self.transfer_percent.set(100)
                        self.transfer_caption.set('传输结束')
                    final_pair = self._take_preview()
                    if final_pair is not None:
                        latest_pair = final_pair
                    if latest_pair is not None:
                        self._show_pair(*latest_pair)
                        latest_pair = None
                    self.finished = True
                    self.channel_mode.set('采集/传输已结束，保留最后数据')
                    self.start_button.configure(state="normal")
                    self.csi_start_button.configure(state='normal')
                    if value is not None:
                        gaps = np.diff(self._display_times) * 1000
                        value.update({
                            "display_gap_p95_ms": round(float(np.percentile(gaps, 95)), 1) if len(gaps) else 0,
                            "display_gap_max_ms": round(float(np.max(gaps)), 1) if len(gaps) else 0,
                            "display_gaps_over_100ms": int(np.sum(gaps > 100)),
                            "display_gaps_over_200ms": int(np.sum(gaps > 200)),
                            "display_queue_age_p95_ms": round(float(np.percentile(self._display_ages, 95)), 1) if self._display_ages else 0,
                            "display_skipped_frames": int(np.sum(np.maximum(0, np.diff(self._display_frame_numbers)-1))),
                        })
                        value.update({
                            "ui_pair_seconds": round(self.ui_pair_seconds, 3),
                            "ui_chart_seconds": round(self.ui_chart_seconds, 3),
                            "ui_poll_seconds": round(self.ui_poll_seconds, 3),
                            "ui_pair_calls": self.ui_pair_calls,
                            "ofdm_plot_updates": self.ofdm_panel.updates,
                            "ofdm_plot_render_p95_ms": round(float(np.percentile(self.ofdm_panel.render_times_ms,95)),2) if self.ofdm_panel.render_times_ms else 0,
                            "ofdm_display_interval_p95_ms": round(float(np.percentile(np.diff(self.ofdm_panel.display_times)*1000,95)),2) if len(self.ofdm_panel.display_times)>1 else 0,
                            "ofdm_display_interval_max_ms": round(float(np.max(np.diff(self.ofdm_panel.display_times)*1000)),2) if len(self.ofdm_panel.display_times)>1 else 0,
                            "csi_plot_render_p95_ms": round(float(np.percentile(self.ofdm_panel.csi_panel.render_times_ms,95)),2) if self.ofdm_panel.csi_panel.render_times_ms else 0,
                            "ui_chart_calls": self.ui_chart_calls,
                            "live_chart_updates": self.live_chart_updates,
                            "chart_queue_age_ms_mean": round(float(np.mean(self._chart_age_ms)), 1) if self._chart_age_ms else 0,
                        })
                        report_path = self.csi_report_path if value.get('csi_only') else self.report_path
                        panel = self.ofdm_panel.csi_panel
                        display_gaps = np.diff(panel.display_times)*1000
                        value.update({
                            'csi_display_interval_p95_ms': round(float(np.percentile(display_gaps,95)),2) if len(display_gaps) else 0,
                            'csi_display_interval_max_ms': round(float(np.max(display_gaps)),2) if len(display_gaps) else 0,
                            'csi_display_age_p95_ms': round(float(np.percentile(panel.queue_ages_ms,95)),2) if panel.queue_ages_ms else 0,
                            'csi_display_timing_window_samples': len(panel.display_times),
                        })
                        if report_path is not None:
                            try:
                                report_path.write_text(
                                    json.dumps(value, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                            except OSError as exc:
                                self.log.configure(state='normal')
                                self.log.insert('end', f'报告保存失败：{exc}\n')
                                self.log.configure(state='disabled')
                        self.summary.set(
                            f"完成：收到 {value['frames']} 帧，丢失 {value['lost_frames']} 帧，"
                            f"显示 {self.ui_pair_calls} 次，最大显示间隔 {value['display_gap_max_ms']} ms，"
                            f"超过 100 ms {value['display_gaps_over_100ms']} 次")
                        if value.get('original_stream'):
                            quality = '载荷与原片一致' if value.get('media_payload_identical') else '完整性未通过'
                            realtime = '实时播放通过' if value.get('realtime_playback') else '实时播放未通过（吞吐/播放不足）'
                            self.summary.set(f"原码流：{quality}；{realtime}；收到 {value['frames']} 帧")
                            if value.get('download_before_playback'):
                                playback = '按源节奏播放通过' if value.get('smooth_playback') else '播放仍有迟到'
                                self.summary.set(f"原画质完整接收：{quality}；{playback}；收到 {value['frames']} 帧")
                        if value.get('csi_only'):
                            self.summary.set(f"CSI 采集结束：{value['csi_samples']} 次，实际 {value['actual_hz']:.1f} Hz，间隔 P95 {value['interval_p95_ms']:.0f} ms")
                    else:
                        self.summary.set("运行失败")
        except Empty:
            pass
        self._check_worker_exit()
        now = time.monotonic()
        if self._latest_chart_sample is not None and now - self._last_chart_draw >= 1 / 20:
            tx, rx, captured = self._latest_chart_sample
            self._latest_chart_sample = None
            self._pending_tx_points, self._pending_rx_points = tx, rx
            self.latest_chart_time = captured
            self.live_chart_updates += 1
            self._last_chart_draw = now
            self.chart_display_times.append(now)
            while self.chart_display_times and now - self.chart_display_times[0] > 1:
                self.chart_display_times.popleft()
            self._chart_age_ms.append(max(0, (now - captured) * 1000))
        if self._pending_tx_points is not None and self.tx_canvas.winfo_ismapped():
            self._draw_constellation(self.tx_canvas, self._pending_tx_points, "#a3f96b")
        if self._pending_rx_points is not None and self.rx_canvas.winfo_ismapped():
            self._draw_constellation(self.rx_canvas, self._pending_rx_points, "#53d9ff")
            order = 2 ** (2 * getattr(self, "_plot_bits_per_axis", 3))
            self.tx_constellation_caption.set(f"发送星座图：当前 {order}QAM 实际符号")
            age = max(0, (time.monotonic() - self.latest_chart_time) * 1000)
            self.rx_constellation_caption.set(f"接收星座图：显示 {len(self.chart_display_times)} Hz · 队列延迟 {age:.0f} ms")
        elif self.latest_chart_time and time.monotonic() - self.latest_chart_time > 1:
            self.rx_constellation_caption.set("接收星座图：暂无新的有效采样")
        self._pending_tx_points = self._pending_rx_points = None
        if latest_pair is not None:
            self._show_pair(*latest_pair)
        self.ui_poll_seconds += time.perf_counter() - poll_start
        # Poll the IPC queue frequently; a 16 ms Tk timer can round up to a
        # 31 ms cycle on Windows and turn a 30 fps stream into alternating gaps.
        self.root.after(1 if self._rx_fullscreen is not None else 4, self._poll)

    def _take_preview(self):
        try:
            return self.preview_events.get_nowait()
        except Empty:
            return None

    def _close(self):
        self._close_rx_fullscreen(restore=False)
        self.stop.set()
        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self._close)
        else:
            if self._timer_period:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
                self._timer_period = False
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="AD9361 视频自发自收图形界面")
    parser.add_argument('--page',choices=('demo','settings','channel'),default='demo')
    parser.add_argument('--auto-csi',action='store_true',help='打开后开始独立 CSI 采集')
    parser.add_argument('--csi-seconds',type=float,default=0,help='独立采集时长，0 为手动停止')
    parser.add_argument("--auto-start", action="store_true", help="打开窗口后自动开始")
    parser.add_argument('--ofdm-training-packets',type=int,default=0,
                        help='实验性批内训练间隔；0 保留整批训练')
    parser.add_argument("--input", type=Path, help="预选输入视频")
    parser.add_argument("--output", type=Path, help="预选接收视频")
    parser.add_argument("--mode", choices=("rf", "digital", "simulate"), help="预选链路模式")
    parser.add_argument("--waveform", choices=("single-carrier", "ofdm"))
    parser.add_argument("--modulation", choices=("qpsk", "16qam", "64qam", "256qam"),
                        help="预选调制方式")
    parser.add_argument("--transport", choices=("frame", "stream", "original"), help="original 保留原码流")
    parser.add_argument("--bitrate-kbps", type=int, help="H.264 流码率")
    parser.add_argument("--fps", type=float, help="预选目标视频帧率")
    parser.add_argument("--width", type=int, help="预选传输图像宽度")
    parser.add_argument("--height", type=int, help="预选传输图像高度")
    parser.add_argument("--max-frames", type=int, help="最多处理的帧数")
    parser.add_argument("--report", type=Path, help="完成后保存测试统计 JSON")
    parser.add_argument("--tx-gain", type=float, help="预选 TX 增益 dB")
    parser.add_argument("--rx-gain", type=float, help="预选 RX 增益 dB")
    parser.add_argument("--uri", help="预选 IIO URI")
    parser.add_argument("--sample-rate", type=int, help="IQ 采样率 samples/s")
    parser.add_argument("--sps", type=int, choices=(2, 4, 8))
    parser.add_argument('--ofdm-fft-size',type=int,choices=(256,512,1024,2048),default=256)
    parser.add_argument("--batch-packets", type=int)
    parser.add_argument("--segment-seconds", type=float)
    parser.add_argument("--playback-buffer-segments", type=int)
    parser.add_argument("--crc", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--encoder-preset", choices=("veryfast", "fast", "medium", "slow"))
    parser.add_argument("--playback-mode", choices=("buffered", "realtime", "download"), default="buffered")
    apply_profile(parser)
    args = parser.parse_args()
    if args.transport == "original" and args.max_frames not in (None, 0):
        parser.error("原码流模式保留完整文件，--max-frames 必须为 0")
    if args.playback_mode == 'download' and args.transport != 'original':
        parser.error('--playback-mode download requires --transport original')
    root = tk.Tk()
    app = App(root)
    app.start_page = args.page
    app.csi_duration = args.csi_seconds
    app.check_crc.set(args.crc)
    app.ofdm_fft_size.set(str(args.ofdm_fft_size))
    app.ofdm_training_packets=args.ofdm_training_packets
    app.playback_mode.set({'download':'完整接收后播放（原画质）',
                           'realtime':'实时（低延迟）','buffered':'预缓冲'}[args.playback_mode])
    if args.encoder_preset is not None:
        app.encoder_preset.set(args.encoder_preset)
    if args.profile == "ofdm-video":
        source = Path.cwd() / "examples" / "big_buck_bunny_realtime_full_latest.mp4"
        if args.input is None and source.is_file():
            app.input.set(str(source))
        if args.output is None:
            app.output.set(str(Path.cwd() / "examples" / "ofdm_received_gui.mp4"))
        app.report_path = Path.cwd() / "examples" / "ofdm_received_gui.json"
    if args.profile == "ofdm-1080p60":
        source = Path.cwd() / "examples" / "big_buck_bunny_downloaded_1080p60_30s.mp4"
        if not source.is_file():
            source = Path.cwd() / "examples" / "test_10s_1080p60_detail.mp4"
        if args.input is None and source.is_file():
            app.input.set(str(source))
        if args.output is None:
            app.output.set(str(Path.cwd() / "examples" / "ofdm_1080p60_received_gui.mp4"))
        app.report_path = Path.cwd() / "examples" / "ofdm_1080p60_received_gui.json"
    if args.profile in PROFILE_FILES:
        paths=PROFILE_FILES[args.profile]
        if args.input is None: app.input.set(str((Path.cwd()/paths['input']).resolve()))
        if args.output is None: app.output.set(str((Path.cwd()/paths['output']).resolve()))
        app.report_path=(Path.cwd()/paths['report']).resolve()
    if args.profile == "ofdm-original-720p":
        if args.input is None:
            app.input.set(str(Path.cwd() / "examples" / "city_road_720p_original.mp4"))
        if args.output is None:
            app.output.set(str(Path.cwd() / "examples" / "city_720p_original_received_gui.mp4"))
        app.report_path = Path.cwd() / "examples" / "city_720p_original_received_gui.json"
    if args.profile == "720p30-rf":
        demo_720 = Path.cwd() / "examples" / "test_30s_720p30_detail.mp4"
        if args.input is None and demo_720.is_file():
            app.input.set(str(demo_720))
        if args.output is None:
            app.output.set(str(Path.cwd() / "examples" / "received_720p30_gui.mp4"))
    if args.input is not None:
        app.input.set(str(args.input.resolve()))
        root.title(f"LuxLink SDR 视频无线传输系统 — {args.input.name}")
    if args.output is not None:
        app.output.set(str(args.output.resolve()))
    if args.mode is not None:
        app.mode.set(args.mode)
    if args.waveform is not None:
        app.waveform.set(args.waveform)
        root.title(f"LuxLink SDR 视频无线传输系统 — {app.mode.get()}")
    if args.modulation is not None:
        app.modulation.set(args.modulation)
    if args.transport is not None:
        app.transport.set(args.transport)
    if args.bitrate_kbps is not None:
        app.stream_bitrate.set(str(args.bitrate_kbps))
    if args.fps is not None:
        app.fps.set(str(args.fps))
    if args.width is not None:
        app.width.set(str(args.width))
    if args.height is not None:
        app.height.set(str(args.height))
    if args.max_frames is not None:
        app.max_frames.set(str(args.max_frames))
    if args.tx_gain is not None:
        app.tx_gain.set(str(args.tx_gain))
    if args.rx_gain is not None:
        app.rx_gain.set(str(args.rx_gain))
    if args.uri is not None:
        app.uri.set(args.uri)
    if args.sample_rate is not None:
        app.sample_rate.set(str(args.sample_rate / 1e6))
    for key in ("sps", "batch_packets", "segment_seconds", "playback_buffer_segments"):
        if getattr(args, key) is not None:
            getattr(app, key).set(str(getattr(args, key)))
    if args.report is not None:
        app.report_path = args.report
    if args.auto_csi:
        root.after(800,app._start_csi)
    elif args.auto_start:
        root.after(800, app._start)
    app._update_waveform_view()
    app.ofdm_panel.reset(app._radio_settings(csi=args.auto_csi))
    app.tabs.select({'demo':app.demo_tab,'settings':app.controls,'channel':app.channel_tab}[args.page])
    if app.transport.get() == "original":
        app.summary.set("原码流模式：不重编码、不降画质；实时能力取决于源码率和链路吞吐，推荐 run_original_realtime.bat 已测配置")
    root.mainloop()


if __name__ == "__main__":
    main()
