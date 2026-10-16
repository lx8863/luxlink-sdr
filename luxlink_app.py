"""Frozen Windows entry point; call freeze_support before importing the GUI."""
import multiprocessing
import os
from pathlib import Path
import sys


def main():
    frozen=getattr(sys,'frozen',False)
    if frozen:
        data=Path(sys.executable).resolve().parent/'data'
        try:
            data.mkdir(parents=True,exist_ok=True)
        except OSError:
            data=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'LuxLinkSDR'
            data.mkdir(parents=True,exist_ok=True)
        os.chdir(data)
        for name in ('stdout','stderr'):
            if getattr(sys,name) is None:
                setattr(sys,name,(data/'application.log').open('a',encoding='utf-8',buffering=1))
    if '--self-test' in sys.argv:
        index=sys.argv.index('--self-test')
        folder=Path(sys.argv[index+1]).resolve()
        from video_loopback.frozen_check import run
        run(folder)
        return
    if '--profile' not in sys.argv:
        sys.argv.extend(['--profile','ofdm-original-1080p30-64qam'])
    from video_loopback.gui import main as gui_main
    gui_main()


if __name__=='__main__':
    multiprocessing.freeze_support()
    try:
        main()
    except Exception:
        import traceback
        details=traceback.format_exc()
        if sys.stderr is not None: sys.stderr.write(details)
        if getattr(sys,'frozen',False):
            from tkinter import messagebox
            messagebox.showerror('LuxLink SDR 启动失败',details)
        raise
