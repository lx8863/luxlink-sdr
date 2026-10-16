"""Run the real GUI with CSI visible, using direct Tk scheduling."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from video_loopback import gui

if __name__=='__main__':
    view='channel'
    fullscreen='--rx-fullscreen' in sys.argv
    if fullscreen: sys.argv.remove('--rx-fullscreen')
    if '--diagnostic-view' in sys.argv:
        index=sys.argv.index('--diagnostic-view')
        view=sys.argv[index+1]
        del sys.argv[index:index+2]
    gui.messagebox.showerror=lambda title,message: Path('examples/channel_validation_error.txt').write_text(
        str(title)+'\n'+str(message),encoding='utf-8')
    original=gui.App._start
    def start(self):
        original(self)
        self.tabs.select(self.channel_tab if view=='channel' else self.demo_tab)
        if fullscreen: self._open_rx_fullscreen()
    gui.App._start=start
    gui.main()
