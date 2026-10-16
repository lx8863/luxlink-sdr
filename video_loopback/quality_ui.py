"""Received media goodput, excluding retransmitted payload."""
from collections import deque
import math
import time
import tkinter as tk
from tkinter import ttk


class ReceptionQualityPanel(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.samples=deque(maxlen=4096)
        self.waiting=False
        self.required_mbps=0.
        self.previous=None
        self.complete=False
        self.origin=None
        self.ceiling=2.
        self.last_draw=0
        self.caption=tk.StringVar(value='等待视频传输统计')
        ttk.Label(self,textvariable=self.caption).pack(fill='x')
        self.canvas=tk.Canvas(self,width=220,height=210,bg='#0b1424',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',lambda event:self.draw())
        self.canvas.bind('<Map>',lambda event:self.draw())

    def reset(self):
        self.samples.clear()
        self.waiting=False
        self.required_mbps=0.
        self.previous=None
        self.complete=False
        self.origin=None
        self.ceiling=2.
        self.caption.set('等待视频传输统计')
        self.draw()

    def update_progress(self,received,total,stamp):
        if self.origin is None:
            self.origin=stamp
        if self.previous is not None:
            previous_time,previous_bytes=self.previous
            if stamp>previous_time and received>=previous_bytes:
                self.samples.append((stamp,received-previous_bytes))
        while self.samples and self.samples[0][0]<stamp-32:
            self.samples.popleft()
        self.previous=(stamp,received)
        self.complete=received>=total and total>0
        self.refresh(force=True)

    def set_waiting(self,waiting):
        self.waiting=waiting
        self.refresh(force=True)

    def buckets(self,now):
        if self.origin is None: return {}
        # Fixed windows anchored to the transfer start. Only completed windows
        # are shown, so advancing time never redistributes historical bytes.
        end=self.previous[0] if self.complete else now
        elapsed=max(0.,end-self.origin)
        count=math.ceil(elapsed) if self.complete else math.floor(elapsed)
        if count<=0: return {}
        first=max(0,count-30)
        bins={index:0. for index in range(first,count)}
        for stamp,size in self.samples:
            index=max(0,math.ceil(stamp-self.origin)-1)
            if index in bins:
                bins[index]+=size*8/1e6
        if self.complete and elapsed>count-1:
            bins[count-1]/=elapsed-(count-1)
        return {count-1-index:rate for index,rate in bins.items()}

    def refresh(self,force=False):
        now=time.monotonic()
        if not force and now-self.last_draw<.25: return
        self.last_draw=now
        current=self.buckets(now).get(0,0.)
        reference=f' / 源码率 {self.required_mbps:.2f}' if self.required_mbps else ''
        if self.waiting:
            self.caption.set(f'等待信号 · {current:.2f} Mbps')
        elif self.complete:
            self.caption.set(f'传输完成 · 末次 {current:.2f} Mbps · 保留历史')
        elif self.previous is not None:
            self.caption.set(f'{current:.2f}{reference} Mbps')
        else:
            self.caption.set('等待视频传输统计')
        self.draw(now)

    def draw(self,now=None):
        canvas=self.canvas
        if not canvas.winfo_ismapped(): return
        now=time.monotonic() if now is None else now
        w,h=max(220,canvas.winfo_width()),max(150,canvas.winfo_height())
        left,right,top,bottom=58,w-14,56,h-43
        values=self.buckets(now)
        peak=max([self.required_mbps,*values.values(),1.])
        self.ceiling=max(self.ceiling,2**math.ceil(math.log2(peak*1.15)))
        ceiling=self.ceiling
        if getattr(self,'_size',None)!=(w,h,ceiling,self.required_mbps):
            self._size=(w,h,ceiling,self.required_mbps)
            canvas.delete('all')
            ink='#a4b7cd'
            # Keep the legend above the plotting area so it cannot hide data.
            canvas.create_line(left,13,left+24,13,fill='#80e5ad',width=2,
                               tags='throughput_legend')
            canvas.create_text(left+31,13,text='有效接收速率 (Mbps)',anchor='w',fill='#80e5ad',
                               tags='throughput_legend')
            canvas.create_line(left,33,left+24,33,fill='#ffbc59',width=1.5,dash=(5,3),
                               tags='reference_legend')
            reference=(f'视频平均码率：{self.required_mbps:.2f} Mbps'
                       if self.required_mbps else '视频平均码率：等待素材信息')
            canvas.create_text(left+31,33,text=reference,anchor='w',fill='#ffbc59',
                               tags='reference_legend')
            canvas.create_text(14,(top+bottom)/2,text='有效吞吐量 (Mbps)',angle=90,
                               fill=ink,tags='y_axis_label')
            for percent in (0,ceiling/4,ceiling/2,ceiling*3/4,ceiling):
                y=bottom-percent/ceiling*(bottom-top)
                canvas.create_line(left,y,right,y,fill='#233249')
                canvas.create_text(left-4,y,text=f'{percent:g}',anchor='e',fill='#a4b7cd')
            canvas.create_line(left,top,left,bottom,right,bottom,fill='#536781')
            for seconds in (-30,-20,-10,0):
                x=right+seconds/30*(right-left)
                canvas.create_line(x,top,x,bottom,fill='#1c2a3c',dash=(2,4))
                canvas.create_text(x,h-27,text=str(seconds),fill='#a4b7cd')
            canvas.create_text((left+right)/2,h-10,text='相对时间 (s) · 0 为最新',
                               fill=ink,tags='x_axis_label')
            if self.required_mbps:
                y=bottom-self.required_mbps/ceiling*(bottom-top)
                canvas.create_line(left,y,right,y,fill='#ffbc59',dash=(5,3),width=1.3,
                                   tags='source_rate_line')
        canvas.delete('quality_trace')
        # Missing seconds remain gaps, including signal loss and idle periods.
        points=[]
        previous=None
        def flush():
            if len(points)>=4:
                canvas.create_line(*points,fill='#80e5ad',width=1.8,tags='quality_trace')
            elif points:
                x,y=points
                canvas.create_oval(x-2,y-2,x+2,y+2,fill='#80e5ad',outline='',tags='quality_trace')
        for age,percent in sorted(values.items(),reverse=True):
            if previous is not None and previous-age>1:
                flush();points=[]
            points.extend((right-(age+.5)/30*(right-left),bottom-percent/ceiling*(bottom-top)))
            previous=age
        flush()
