"""Per-subcarrier OFDM training estimates and an inexpensive Tk display."""
import csv
from dataclasses import asdict
import json
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk, filedialog
import shutil
from collections import deque
import colorsys
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image, ImageTk


_HEAT_ANCHORS = np.array([[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]],float)
_HEAT_PALETTE = np.column_stack([
    np.interp(np.linspace(0,4,256), np.arange(5), _HEAT_ANCHORS[:,channel])
    for channel in range(3)]).astype(np.uint8)


def _heat_images(amp, phase, amin, amax, width, height):
    """Pure NumPy/Pillow work; never touch Tk from this worker."""
    images=[]
    carriers=amp.shape[1]
    middle=carriers//2
    for data,lo,hi in ((amp,amin,amax),(phase,-np.pi,np.pi)):
        indices=np.rint(np.clip((data-lo)/(hi-lo),0,1)*255).astype(np.uint8)
        pixels=np.zeros((200,carriers+1,3),np.uint8)
        colors=_HEAT_PALETTE[indices]
        pixels[:len(colors),:middle]=colors[:,:middle]
        pixels[:len(colors),middle+1:]=colors[:,middle:]
        images.append(Image.fromarray(pixels).resize((width,height),Image.Resampling.NEAREST))
    return images


class CsiLog:
    def __init__(self, path, settings):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create metadata before opening CSV so metadata failures cannot leak
        # an open recording handle.
        self.path.with_suffix('.json').write_text(json.dumps({
            'radio': asdict(settings), 'estimator': 'H[k]=(Y1[k]+Y2[k])/(2*X[k]); normalized by known transmitted FFT',
            'phase_calibrated': False, 'amplitude_calibrated': False,
            'training_validation': {'both_matched_correlations_min': 0.45,
                                    'pair_correlation_min': 0.8},
            'note': 'Includes RF front-end gains and synchronization phase/timing; not calibrated path loss or Wi-Fi CSI format.'
        },ensure_ascii=False,indent=2),encoding='utf-8')
        self.file = self.path.open('w', newline='', encoding='utf-8-sig')
        self.writer = csv.writer(self.file)
        self.writer.writerow(['sample','host_monotonic_s','subcarrier','frequency_hz',
                              'h_real','h_imag','magnitude_db','phase_rad','training_snr_db',
                              'decision_evm_pct','cfo_hz','crc_valid','mode','training_correlation',
                              'training_pair_correlation'])
        self.samples = 0

    def write(self, sample):
        h = sample['csi']
        for k, value in zip(sample['subcarriers'], h):
            self.writer.writerow([self.samples,sample['timestamp'],int(k),
                sample['frequency_hz']+int(k)*sample['sample_rate']/sample.get('fft_size',256),
                float(value.real),float(value.imag),float(20*np.log10(max(1e-12,abs(value)))),
                float(np.angle(value)),sample['training_snr_db'],sample['decision_evm_pct'],
                sample['cfo_cycles_per_sample']*sample['sample_rate'],sample['crc_valid'],sample['mode'],
                sample.get('training_correlation',''),sample.get('training_pair_correlation','')])
        self.samples += 1
        self.file.flush()

    def close(self):
        self.file.close()


class CsiPanel(ttk.Frame):
    def __init__(self, parent, dashboard=False):
        super().__init__(parent)
        self.sample = None
        self.baseline = None
        self.csv_path = None
        self.history = deque(maxlen=300)
        self._limits={}
        self._origin=None
        self.render_times_ms=deque(maxlen=300)
        self.display_times=deque(maxlen=300)
        self.queue_ages_ms=deque(maxlen=300)
        self._last_displayed_sample=None
        self.dashboard = dashboard
        self._heat_photos = []
        self._heat_layout = None
        self._legend_view = None
        self.bind('<Destroy>',self._close_heat_worker,add='+')
        self.info = tk.StringVar(value='等待有效 OFDM 训练符号')
        ttk.Label(self, textvariable=self.info, wraplength=1400 if dashboard else 220).pack(fill='x',pady=4)
        self.view = tk.StringVar(value='时间变化（10 个子载波）')
        selector = ttk.Combobox(self,textvariable=self.view,state='readonly',width=25,
                               values=('时间变化（10 个子载波）','频率响应'))
        selector.pack(fill='x')
        selector.bind('<<ComboboxSelected>>',lambda e:self.draw())
        graph_parent = self
        if dashboard:
            ttk.Label(self,text='左侧：10 个固定子载波的时间变化 / 全部子载波的频率响应；右侧：全部有效子载波的幅相热力图').pack(fill='x',pady=5)
            self.legend = ttk.Frame(self)
            self.legend.pack(fill='x',pady=3)
            self.legend_labels=[]
            for i in range(10):
                color='#%02x%02x%02x'%tuple(int(v*255) for v in colorsys.hsv_to_rgb(i/10,.65,.95))
                label=tk.Label(self.legend,text=f'载波 {i+1}',fg=color,bg='#0b1424',padx=8)
                label.grid(row=0,column=i,sticky='ew')
                self.legend.columnconfigure(i,weight=1)
                self.legend_labels.append(label)
            self.frequency_legend=ttk.Label(self.legend,text='蓝色：幅度响应（dB）；橙色：展开相位（°）。横轴为全部有效子载波。')
            self.charts=ttk.Frame(self)
            self.charts.pack(fill='both',expand=True)
            self.charts.columnconfigure((0,1),weight=1,uniform='csi_charts')
            self.charts.rowconfigure(0,weight=1)
            graph_parent=self.charts
        self.canvas = tk.Canvas(graph_parent,width=220,height=177,bg='#0b1424',highlightthickness=0)
        if dashboard:
            self.canvas.grid(row=0,column=0,sticky='nsew',padx=(0,5))
            self.heat_canvas=tk.Canvas(self.charts,width=440,height=300,bg='#0b1424',highlightthickness=0)
            self.heat_canvas.grid(row=0,column=1,sticky='nsew',padx=(5,0))
            self.heat_canvas.bind('<Configure>',lambda e:self.draw_heatmaps())
        else:
            self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',lambda e:self.draw())
        self.bind('<Map>',lambda e:self.draw())
        actions = ttk.Frame(self)
        actions.pack(fill='x')
        ttk.Button(actions,text='设为基线',command=self.set_baseline).pack(side='left')
        ttk.Button(actions,text='清除基线',command=self.clear_baseline).pack(side='left')
        ttk.Button(actions,text='重置坐标',command=self.reset_limits).pack(side='left')
        ttk.Button(actions,text='保存 CSI',command=self.export).pack(side='right')

    def reset(self):
        self.sample = self.baseline = None
        self.csv_path = None
        self.history.clear()
        self._limits.clear()
        self._origin=None
        self.render_times_ms.clear()
        self.display_times.clear()
        self.queue_ages_ms.clear()
        self._last_displayed_sample=None
        self.info.set('200 个有效子载波 · 幅相未校准 · 等待采样')
        self.draw()

    def update_sample(self, sample):
        self.sample = sample
        if self._origin is None:
            self._origin=sample['timestamp']
        if not self.history or self.history[-1][0]!=sample['timestamp']:
            self.history.append((sample['timestamp'],np.asarray(sample['csi']).copy()))
        self.info.set(f"训练一致性 SNR {sample['training_snr_db']:.1f} dB · "
                      f"判决 EVM {sample['decision_evm_pct']:.1f}%\n"
                      f"CFO {sample['cfo_cycles_per_sample']*sample['sample_rate']:+.0f} Hz · "
                      f"{'相对基线' if self.baseline is not None else '未校准幅相'} · "
                      f"{'仿真' if sample['mode']=='simulate' else '板卡实测'}")
        if len(self.history)>1:
            recent=list(self.history)[-40:]
            actual=(len(recent)-1)/max(1e-9,recent[-1][0]-recent[0][0])
            self.info.set(self.info.get()+f' | 实际 CSI {actual:.1f} Hz')
        if self.dashboard:
            self.info.set(self.info.get()+f" | 中心 {sample['frequency_hz']/1e6:.3f} MHz | "
                          f"采样 {sample['sample_rate']/1e6:.2f} MSPS | "
                          f"Δf {sample['sample_rate']/sample.get('fft_size',256)/1000:.3f} kHz | {len(sample['csi'])} 载波 · 单收发通道 · "
                          f"CRC {'通过' if sample.get('crc_valid') is True else '未通过' if sample.get('crc_valid') is False else '未校验'}")
            chosen=np.linspace(0,len(sample['csi'])-1,10,dtype=int)
            for label,k in zip(self.legend_labels,np.asarray(sample['subcarriers'])[chosen]):
                label.configure(text=f'k={int(k):+d}')
        # Acquisition/history are independent of the GUI paint cadence. Coalesce
        # paints so a burst of diagnostics cannot monopolize the video thread.
        interval=getattr(self,'render_interval',.05)
        delay=interval-(time.perf_counter()-getattr(self,'_last_render',0))
        if delay<=0:
            self.draw()
        elif getattr(self,'_pending_render',None) is None:
            self._pending_render=self.after(max(1,int(delay*1000)),self.draw)

    def set_baseline(self):
        if self.sample is not None:
            self.baseline = np.asarray(self.sample['csi']).copy()
            self._limits.clear()
            self.update_sample(self.sample)

    def clear_baseline(self):
        self.baseline = None
        self._limits.clear()
        if self.sample is not None:
            self.update_sample(self.sample)

    def reset_limits(self):
        self._limits.clear()
        self.draw()

    def fixed_limits(self,key,values,span):
        if key not in self._limits:
            lo,hi=float(np.min(values)),float(np.max(values))
            width=max(span,(hi-lo)*1.4)
            mid=(hi+lo)/2
            self._limits[key]=(mid-width/2,mid+width/2)
        return self._limits[key]

    def export(self):
        if self.csv_path and Path(self.csv_path).is_file():
            target = filedialog.asksaveasfilename(defaultextension='.csv',filetypes=[('CSI CSV','*.csv')])
            if target and Path(target).resolve()!=Path(self.csv_path).resolve():
                shutil.copyfile(self.csv_path,target)

    def check_stale(self):
        if self.sample and time.monotonic()-self.sample['timestamp']>1.5:
            text = self.info.get()
            if '暂停更新' not in text:
                self.info.set(text+' · 暂停更新')

    def draw(self):
        pending=getattr(self,'_pending_render',None)
        if pending is not None:
            self.after_cancel(pending)
            self._pending_render=None
        if getattr(self,'suspend_rendering',False): return
        started=time.perf_counter()
        self._draw()
        self._last_render=time.perf_counter()
        self.render_times_ms.append((time.perf_counter()-started)*1000)
        if (self.sample is not None and self.canvas.winfo_ismapped() and
                self.sample['timestamp'] != self._last_displayed_sample):
            now=time.monotonic()
            self._last_displayed_sample=self.sample['timestamp']
            self.display_times.append(now)
            self.queue_ages_ms.append(max(0,now-self.sample['timestamp'])*1000)

    def _draw(self):
        if not self.canvas.winfo_ismapped():
            return
        if self.dashboard:
            view = self.view.get()
            if view != self._legend_view:
                self._legend_view = view
                if view.startswith('时间变化'):
                    self.frequency_legend.grid_remove()
                    for label in self.legend_labels:
                        label.grid()
                else:
                    for label in self.legend_labels:
                        label.grid_remove()
                    self.frequency_legend.grid(row=0,column=0,columnspan=10,sticky='w')
            self.draw_heatmaps()
        c = self.canvas
        c.delete('all')
        w,h = max(220,c.winfo_width()),max(120,c.winfo_height())
        left,right=39,w-10
        if self.sample is None:
            c.create_text(w/2,h/2,text='等待逐子载波 CSI',fill='#a4b7cd')
            return
        values = np.asarray(self.sample['csi'])
        if self.baseline is not None:
            values = values/np.where(np.abs(self.baseline)>1e-12,self.baseline,1e-12)
        indices = np.asarray(self.sample['subcarriers'])
        bound=max(abs(indices))
        if self.view.get().startswith('时间变化'):
            self.draw_history(c,w,h,left,right)
            return
        amp = 20*np.log10(np.maximum(np.abs(values),1e-12))
        phase = np.unwrap(np.angle(values))*180/np.pi
        for row,(y,label,color) in enumerate(((amp,'幅度 dB','#56d6ff'),(phase,'相位 °','#ffbc59'))):
            top=row*h/2+15
            bottom=(row+1)*h/2-17
            lo,hi=self.fixed_limits(f'frequency_{row}',y,12 if row==0 else 360)
            c.create_text(left,top-7,text=label,anchor='w',fill=color)
            c.create_text(left-3,top+3,text=f'{hi:.0f}',anchor='e',fill='#a4b7cd')
            c.create_text(left-3,bottom,text=f'{lo:.0f}',anchor='e',fill='#a4b7cd')
            c.create_line(left,top,left,bottom,right,bottom,fill='#536781')
            # Leave the DC gap explicit rather than joining -1 and +1.
            for mask in (indices<0,indices>0):
                coords=np.column_stack((left+(indices[mask]+bound)/(2*bound)*(right-left),
                                        bottom-(np.clip(y[mask],lo,hi)-lo)/(hi-lo)*(bottom-top))).ravel().tolist()
                c.create_line(*coords,fill=color,width=1.2)
            for k in (-bound,0,bound):
                c.create_text(left+(k+bound)/(2*bound)*(right-left),bottom+8,text=str(k),fill='#a4b7cd')

    def draw_history(self,c,w,h,left,right):
        if len(self.history)<2:
            c.create_text(w/2,h/2,text='等待至少两次 CSI 采样',fill='#a4b7cd')
            return
        times=np.array([item[0] for item in self.history])
        times-=self._origin
        end=max(10.,times[-1])
        begin=end-10
        mask=times>=begin
        times=times[mask]
        values=np.array([item[1] for item in self.history])
        values=values[mask]
        if self.baseline is not None:
            values=values/np.where(np.abs(self.baseline)>1e-12,self.baseline,1e-12)
        count=10
        chosen=np.linspace(0,values.shape[1]-1,count,dtype=int)
        values=values[:,chosen]
        amplitude=20*np.log10(np.maximum(np.abs(values),1e-12))
        phase=np.angle(values)*180/np.pi
        xs=left+(times-begin)/10*(right-left)
        for row,(ys,label) in enumerate(((amplitude,f'幅度 dB · {count} 条载波'),(phase,'相位 ° · ±180° 未校准'))):
            top,bottom=row*h/2+15,(row+1)*h/2-17
            lo,hi=self.fixed_limits('time_amplitude',ys,12) if row==0 else (-180,180)
            c.create_text(left,top-7,text=label,anchor='w',fill='#a4b7cd')
            c.create_line(left,top,left,bottom,right,bottom,fill='#536781')
            for i in range(count):
                color='#%02x%02x%02x'%tuple(int(v*255) for v in colorsys.hsv_to_rgb(i/count,.65,.95))
                coords=np.column_stack((xs,bottom-(np.clip(ys[:,i],lo,hi)-lo)/(hi-lo)*(bottom-top))).ravel().tolist()
                if len(coords)>=4:
                    c.create_line(*coords,fill=color,width=1,tags='csi_trace')
            c.create_text(left-3,top,text=f'{hi:.0f}',anchor='e',fill='#a4b7cd')
            c.create_text(left-3,bottom,text=f'{lo:.0f}',anchor='e',fill='#a4b7cd')
            c.create_text(left,bottom+8,text=f'{begin:.1f} s',anchor='w',fill='#a4b7cd')
            c.create_text(right,bottom+8,text=f'{end:.1f} s',anchor='e',fill='#a4b7cd')

    def draw_heatmaps(self):
        if getattr(self,'suspend_rendering',False): return
        if not self.dashboard:
            return
        c=self.heat_canvas
        if not c.winfo_ismapped():
            return
        w,h=max(300,c.winfo_width()),max(180,c.winfo_height())
        if not self.history or self.sample is None:
            if self._heat_layout != ('empty',w,h):
                c.delete('all')
                self._heat_photos=[]
                self._heat_layout=('empty',w,h)
                c.create_text(w/2,h/2,text='等待 CSI 幅度 / 相位热力图',fill='#a4b7cd')
            return
        values=np.array([item[1] for item in list(self.history)[-200:]])
        if self.baseline is not None:
            values=values/np.where(np.abs(self.baseline)>1e-12,self.baseline,1e-12)
        amp=20*np.log10(np.maximum(np.abs(values),1e-12))
        phase=np.angle(values)
        amin,amax=self.fixed_limits('heat_amplitude',amp,12)
        carriers=len(self.sample['csi'])
        middle=carriers//2
        bound=int(max(abs(np.asarray(self.sample['subcarriers']))))
        layout=(w,h,amin,amax,carriers)
        image_width=max(1,int(w/2-79))
        image_height=max(1,int(h-106))
        if getattr(self,'threaded_heatmaps',False):
            if not hasattr(self,'_heat_executor'):
                self._heat_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='csi-image')
                self._heat_job=None
                self._heat_ready=None
                self._heat_requested=None
            request=(self.sample['timestamp'],layout,id(self.baseline))
            if self._heat_job is None and request!=self._heat_requested:
                self._heat_requested=request
                self._heat_job=(layout,self._heat_executor.submit(
                    _heat_images,amp,phase,amin,amax,image_width,image_height))
                self._heat_timer=self.after(5,self._finish_heat_worker)
            if self._heat_ready is None or self._heat_ready[0]!=layout:
                return
            images=self._heat_ready[1]
        else:
            images=_heat_images(amp,phase,amin,amax,image_width,image_height)
        rebuild=layout != self._heat_layout
        if rebuild:
            c.delete('all')
            self._heat_photos=[]
            self._heat_layout=layout
        for column,(data,title,lo,hi) in enumerate(((amp,'CSI 幅度 dB',amin,amax),
                                                 (phase,'CSI 相位 rad',-np.pi,np.pi))):
            x0=column*w/2+32
            x1=(column+1)*w/2-47
            top,bottom=38,h-68
            # Lookup colors in a precomputed palette instead of interpolating
            # RGB floats for all 40,000 cells on every update.
            img=images[column]
            if not rebuild:
                self._heat_photos[column].paste(img)
                continue
            photo=ImageTk.PhotoImage(img)
            self._heat_photos.append(photo)
            c.create_image(x0,top,image=photo,anchor='nw',tags='heat_image')
            c.create_text((x0+x1)/2,16,text=title,fill='#e4eef9')
            c.create_text(x0-4,top,text='0',anchor='e',fill='#a4b7cd')
            c.create_text(x0-4,bottom,text='199',anchor='e',fill='#a4b7cd')
            for fraction,label in ((0,str(-bound)),(.5,'DC'),(1,f'+{bound}')):
                c.create_text(x0+(x1-x0)*fraction,bottom+13,text=label,fill='#a4b7cd')
            for step in range(64):
                y0=top+(bottom-top)*step/64
                color='#%02x%02x%02x'%tuple(_HEAT_PALETTE[round((1-step/63)*255)])
                c.create_rectangle(x1+7,y0,x1+18,y0+(bottom-top)/64+1,fill=color,outline='')
            for fraction in (0,.5,1):
                c.create_text(x1+20,top+(bottom-top)*fraction,text=f'{hi-fraction*(hi-lo):.1f}',anchor='w',fill='#a4b7cd')
        if rebuild:
            c.create_text(w/2,h-36,text='横轴：子载波编号；纵轴：最近 200 次采样（不足留黑）',fill='#a4b7cd')
            c.create_text(w/2,h-16,text='热图颜色表示数值，不代表天线；幅相未校准',fill='#a4b7cd')

    def _finish_heat_worker(self):
        if self._heat_job is None:
            return
        layout,future=self._heat_job
        if not future.done():
            self._heat_timer=self.after(5,self._finish_heat_worker)
            return
        self._heat_job=None
        self._heat_ready=(layout,future.result())
        self.draw_heatmaps()

    def _close_heat_worker(self,event):
        if event.widget is self:
            for name in ('_heat_timer','_pending_render'):
                timer=getattr(self,name,None)
                if timer is not None:
                    self.after_cancel(timer)
                    setattr(self,name,None)
            executor=getattr(self,'_heat_executor',None)
            if executor is not None:
                executor.shutdown(wait=False,cancel_futures=True)
