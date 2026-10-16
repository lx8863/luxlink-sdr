"""Download the published 1080p HLS renditions without re-encoding."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urljoin
import av
import imageio_ffmpeg

DEST=Path(__file__).resolve().parent/'1080p'
CLIPS=[
    ('ocean_waves_timelapse_1080p2997', 'https://dareful.com/ocean-waves-timelapse/',
     'https://stream.mux.com/6Zqf0102Fn2OT6sbPRaCEjnwWvHr4YUANthRUuB02O98OI.m3u8'),
    ('ocean_sea_waves_1080p2997', 'https://dareful.com/ocean-sea-waves-water/',
     'https://stream.mux.com/T5U023yExs52dbcXiJziRyqFox801L01KqwKlmBhJx5oeo.m3u8'),
]

def fetch(url,path):
    subprocess.run(['curl.exe','--fail','--location','--silent','--show-error',
                    '--retry','2','--max-time','45',url,'--output',str(path)],check=True)

def download(name,page,url):
    with TemporaryDirectory(prefix='ocean_material_') as folder:
        folder=Path(folder)
        master=folder/'master.m3u8';fetch(url,master)
        lines=master.read_text().splitlines()
        rendition=next(urljoin(url,lines[i+1]) for i,line in enumerate(lines)
                       if line.startswith('#EXT-X-STREAM-INF:') and 'RESOLUTION=1920x1080' in line)
        playlist=folder/'local.m3u8';fetch(rendition,playlist)
        local=[];count=0
        for line in playlist.read_text().splitlines():
            if line and not line.startswith('#'):
                part=folder/f'segment{count}.ts'
                fetch(urljoin(rendition,line),part)
                local.append(part.name);count+=1
                print(name,'segment',count,flush=True)
            elif line.startswith('#EXT-X-KEY') or line.startswith('#EXT-X-MAP'):
                raise RuntimeError('Unsupported encrypted or fragmented playlist')
            else: local.append(line)
        playlist.write_text('\n'.join(local)+'\n')
        output=DEST/(name+'.mp4')
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-loglevel','error',
                        '-y','-i',str(playlist),'-map','0:v:0','-c','copy',
                        '-movflags','+faststart',str(output)],check=True)
        with av.open(str(output)) as media:
            video=media.streams.video[0]
            frames=sum(1 for _ in media.decode(video))
            info=dict(file=output.name,width=video.width,height=video.height,
                      fps=str(video.average_rate),frames=frames,
                      seconds=float(video.duration*video.time_base),
                      bytes=output.stat().st_size,source_page=page,source_url=url,
                      license='CC BY 4.0',credit='Video courtesy of Dareful',
                      transcoded=False)
        output.with_suffix('.source.json').write_text(json.dumps(info,indent=2),encoding='utf-8')
        print(json.dumps(info),flush=True)

if __name__=='__main__':
    for clip in CLIPS: download(*clip)
