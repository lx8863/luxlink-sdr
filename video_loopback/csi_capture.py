"""Independent CSI acquisition using fresh IQ from cyclic OFDM training."""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
from threading import Event
import time
import numpy as np
from .csi import CsiLog
from .modem import make_packet
from .radio import RadioSettings, LoopbackRadio, IncompleteBurst


def collect_csi(settings,path,seconds=0,hz=20,stop=None,callback=None):
    if not 1<=hz<=25:
        raise ValueError('CSI target rate must be 1..25 Hz')
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError('CSI duration must be finite and nonnegative; 0 means manual stop')
    settings=replace(settings,waveform='ofdm',ofdm_superframe=False,
                     modulation='qpsk',batch_packets=1,check_crc=True,ofdm_fft_size=256)
    stop=stop or Event()
    times=[]
    reads=invalid=crc_failed=valid_packets=0
    started=time.monotonic()
    log=CsiLog(path,settings)
    metadata=Path(path).with_suffix('.json')
    def publish(sample):
        log.write(sample)
        times.append(sample['timestamp'])
        if callback:
            callback(sample)
    try:
        config=json.loads(metadata.read_text(encoding='utf-8'))
        config.update(acquisition='independent_cyclic_training',target_hz=hz,training_only_estimates_allowed=True)
        metadata.write_text(json.dumps(config,indent=2),encoding='utf-8')
        if not stop.is_set():
            with LoopbackRadio(settings) as radio:
                # Starting the cyclic TX must not require a decodable payload.
                # CSI only requires a valid pair of training symbols.
                try:
                    radio.transceive_batch([make_packet(0,0,1,bytes(range(256))*4)], stop=stop)
                except IncompleteBurst:
                    pass
                radio.set_channel_callback(publish)
                next_read=time.monotonic()+1/hz
                while not stop.is_set() and (seconds<=0 or time.monotonic()-started<seconds):
                    if stop.wait(max(0,next_read-time.monotonic())):
                        break
                    if seconds>0 and time.monotonic()-started>=seconds:
                        break
                    reads+=1
                    before=len(times)
                    packets=radio.capture_csi()
                    valid_packets+=len(packets)
                    if len(times)==before:
                        invalid+=1
                    elif not packets:
                        crc_failed+=1
                    next_read=max(next_read+1/hz,time.monotonic())
    except InterruptedError:
        if not stop.is_set():
            raise
    finally:
        log.close()
    gaps=np.diff(times)
    return dict(csi_only=True,csi_samples=len(times),csi_csv=str(path),
                seconds=round(time.monotonic()-started,3),target_hz=hz,
                actual_hz=round((len(times)-1)/(times[-1]-times[0]),2) if len(times)>1 else 0,
                interval_p95_ms=round(float(np.percentile(gaps,95))*1000,1) if len(gaps) else 0,
                interval_max_ms=round(float(max(gaps))*1000,1) if len(gaps) else 0,
                capture_calls=reads,invalid_reads=invalid,payload_crc_failed_reads=crc_failed,
                stopped=stop.is_set(),frames=0,lost_frames=0,packets=valid_packets,
                mode=settings.mode)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('rf','digital','simulate'),default='rf')
    p.add_argument('--seconds',type=float,default=10)
    p.add_argument('--hz',type=float,default=20)
    p.add_argument('--uri',default='usb:')
    p.add_argument('--output',type=Path,default=Path('examples/csi_capture.csv'))
    args=p.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    stats=collect_csi(RadioSettings(mode=args.mode,uri=args.uri,sample_rate=4000000,
                                    tx_gain_db=0,rx_gain_db=30),args.output,args.seconds,args.hz)
    args.output.with_suffix('.report.json').write_text(json.dumps(stats,indent=2),encoding='utf-8')
    print(json.dumps(stats,indent=2))


if __name__=='__main__':
    main()
