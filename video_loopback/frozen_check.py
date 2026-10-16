"""End-to-end validation of the actual frozen GUI and spawned worker."""
import json
from pathlib import Path
import subprocess
import sys
import time


def run(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    from .native import prepare_libiio
    prepare_libiio()
    import iio
    import adi
    import imageio_ffmpeg
    source=folder/'source.mp4'
    report=folder/'received.json'
    if report.exists(): raise RuntimeError('Use an empty self-test directory')
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error',
                    '-f','lavfi','-i','testsrc2=size=128x72:rate=30','-t','0.4',
                    '-c:v','libx264','-y',str(source)],check=True,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    from . import gui
    errors=[]
    gui.messagebox.showerror=lambda title,message:errors.append(str(message))
    original=gui.App.__init__
    def initialize(app,root):
        original(app,root)
        deadline=time.monotonic()+90
        def check():
            if errors or time.monotonic()>deadline:
                errors.append('Frozen GUI validation failed or timed out')
                app._close();return
            if app.worker is not None and app.finished:
                app._close();return
            root.after(100,check)
        root.after(100,check)
    gui.App.__init__=initialize
    sys.argv=[sys.argv[0],'--profile','ofdm-original-1080p30-64qam','--mode','simulate',
              '--input',str(source),'--output',str(folder/'received.mp4'),
              '--report',str(report),'--auto-start']
    gui.main()
    stats=json.loads(report.read_text(encoding='utf-8')) if report.exists() else {}
    valid=(not errors and stats.get('frames')==12 and stats.get('complete_transfer')
           and stats.get('media_payload_identical'))
    result=dict(passed=bool(valid),frozen=bool(getattr(sys,'frozen',False)),
                frames=stats.get('frames'),complete_transfer=stats.get('complete_transfer'),
                media_payload_identical=stats.get('media_payload_identical'),
                libiio_loaded=True,ffmpeg_executed=True,errors=errors)
    (folder/'self-test-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    if not valid: raise RuntimeError(str(result))
