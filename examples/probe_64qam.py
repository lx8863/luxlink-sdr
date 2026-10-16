"""Bounded RF probe for 64QAM gain selection; no video transcoding."""
import sys,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from threading import Event,Timer
import numpy as np
from video_loopback.streaming_radio import StreamingLoopbackRadio
from video_loopback.radio import RadioSettings
from video_loopback.modem import make_packet

if __name__=='__main__':
    rng=np.random.default_rng(64)
    results=[]
    for gain in (20,28,30):
        settings=RadioSettings(mode='rf',waveform='ofdm',ofdm_superframe=True,
            ofdm_streaming=True,ofdm_fft_size=1024,modulation='64qam',sample_rate=3000000,
            tx_gain_db=0,rx_gain_db=gain,batch_packets=256)
        stop=Event();timer=Timer(12,stop.set)
        samples=[];total=retries=0
        with StreamingLoopbackRadio(settings) as radio:
            radio.set_channel_callback(lambda x:samples.append((x['training_snr_db'],x['decision_evm_pct'])))
            started=time.perf_counter();timer.start()
            try:
                for batch in range(8):
                    payloads=[rng.bytes(1024) for _ in range(256)]
                    packets=[make_packet(batch,i,256,x) for i,x in enumerate(payloads)]
                    received=radio.transceive_batch(packets,stop=stop)
                    assert [p.payload for p in received]==payloads
                    total+=256*1024;retries+=radio.last_retransmitted_packets
            except InterruptedError: pass
            finally: timer.cancel()
            elapsed=time.perf_counter()-started
            result=dict(rx_gain_db=gain,bytes=total,seconds=round(elapsed,3),
                mbps=round(total*8/elapsed/1e6,3),retransmitted_packets=retries,
                completed_batches=total//(256*1024),
                evm_median=float(np.median([x[1] for x in samples])) if samples else None,
                snr_median=float(np.median([x[0] for x in samples])) if samples else None)
            results.append(result);print(json.dumps(result),flush=True)
        Path('examples/1080p30_64qam_gain_probe.json').write_text(json.dumps(results,indent=2))
