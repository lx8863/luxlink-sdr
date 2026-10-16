"""Timestamped receive-spectrum history, with gaps for missing IQ samples."""
from collections import deque
import time
import tkinter as tk
from tkinter import ttk
import cv2
import numpy as np
from PIL import Image,ImageTk


class SpectrumWaterfall(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.history=deque(maxlen=512)
        self.settings=None
        self.photo=None
        self.last_draw=0.
        self.frozen_at=None
        self.canvas=tk.Canvas(self,width=220,height=145,bg='#0b1424',highlightthickness=0)
        self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',lambda event:self.draw(force=True))
        self.canvas.bind('<Map>',lambda event:self.draw(force=True))
        self.palette=cv2.applyColorMap(np.arange(256,dtype=np.uint8)[:,None],cv2.COLORMAP_TURBO)[:,0,::-1]

    def reset(self,settings):
        self.frozen_at=None
        self.history.clear()
        self.settings=vars(settings)
        self.draw(force=True)

    def update_sample(self,sample,render=True):
        if self.frozen_at is not None: return
        self.settings=sample
        self.history.append((sample['timestamp'],np.asarray(sample['spectrum_db'],dtype=np.float32).copy()))
        if render: self.draw()

    def freeze(self):
        if self.frozen_at is None:
            self.frozen_at=self.history[-1][0] if self.history else time.monotonic()
            self.draw(force=True)

    def raster(self,now):
        if getattr(self,'frozen_at',None) is not None:
            now=self.frozen_at
        pixels=np.full((100,512,3),(11,20,36),dtype=np.uint8)
        # Ten seconds, one fixed 100 ms row. Newest data appears at the bottom.
        tick=int(now*10)
        for stamp,db in self.history:
            age=tick-int(stamp*10)
            if 0<=age<100:
                values=np.interp(np.linspace(0,len(db)-1,512),np.arange(len(db)),db)
                indices=np.rint((np.clip(values,-80,0)+80)*255/80).astype(np.uint8)
                pixels[99-age]=self.palette[indices]
        return pixels

    def draw(self,force=False):
        if self.frozen_at is not None and not force: return
        now=time.monotonic()
        canvas=self.canvas
        if not canvas.winfo_ismapped() or (not force and now-self.last_draw<.1): return
        self.last_draw=now
        w,h=max(220,canvas.winfo_width()),max(125,canvas.winfo_height())
        left,right,top,bottom=38,w-49,24,h-36
        settings=self.settings or {}
        center=settings.get('frequency_hz',2400000000)/1e6
        rate=settings.get('sample_rate',3000000)/1e6
        layout=(w,h,center,rate)
        if getattr(self,'layout',None)!=layout:
            self.layout=layout
            canvas.delete('all');self.photo=None
            ink='#a4b7cd'
            canvas.create_text(left,10,text='频谱瀑布 · 最近 10 s',anchor='w',fill=ink)
            for age in (10,5,0):
                y=bottom-age/10*(bottom-top)
                canvas.create_text(left-4,y,text=str(-age),anchor='e',fill=ink)
            canvas.create_text(10,(top+bottom)/2,text='时间 (s)',angle=90,fill=ink)
            for fraction in (0,.5,1):
                canvas.create_text(left+(right-left)*fraction,h-23,
                                   text=f'{center+(fraction-.5)*rate:.2f}',fill=ink)
            canvas.create_text((left+right)/2,h-9,text='频率 (MHz)',fill=ink)
            bar=right+9
            for i in range(64):
                rgb=self.palette[round(255*(1-i/63))]
                color='#%02x%02x%02x'%tuple(rgb)
                canvas.create_rectangle(bar,top+i/64*(bottom-top),bar+8,
                                        top+(i+1)/64*(bottom-top),fill=color,outline='')
            canvas.create_text(bar+4,10,text='dB',fill=ink)
            for value in (0,-40,-80):
                canvas.create_text(bar+11,top-value/80*(bottom-top),text=str(value),anchor='w',fill=ink)
        image=Image.fromarray(self.raster(now)).resize((max(1,right-left),max(1,bottom-top)),Image.Resampling.NEAREST)
        if self.photo is None:
            self.photo=ImageTk.PhotoImage(image)
            canvas.create_image(left,top,image=self.photo,anchor='nw',tags='waterfall_image')
        else: self.photo.paste(image)
