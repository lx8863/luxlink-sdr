"""Persistent full duplex OFDM transport with bounded receive/decode stages."""
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty, Full
from threading import Event, Thread, Condition, Lock
import time
import traceback
import errno
from pathlib import Path
import numpy as np
from .radio import LoopbackRadio
from .modem import make_packet, parse_packet
from .ofdm import configuration


class StreamingLoopbackRadio(LoopbackRadio):
    """Same-board transport. Packet identities/CRC remain the delivery authority."""
    def __init__(self,settings):
        if settings.waveform!='ofdm' or not settings.ofdm_superframe:
            raise ValueError('Persistent transport requires original OFDM')
        if not settings.check_crc:
            raise ValueError('Persistent transport requires CRC verification')
        super().__init__(settings)
        self.group_size=settings.ofdm_training_packets or 16
        self._shutdown=Event()
        self._iq=Queue(maxsize=8)
        self._tx_iq=Queue(maxsize=2)
        self._condition=Condition()
        self._transfer_lock=Lock()
        self._device_io_lock=Lock()
        self._rx_generation=0
        self._wanted=set()
        self._received={}
        self._failure=None
        self._failure_stage=''
        self.error_log=Path(__file__).resolve().parents[1]/'examples'/'radio_worker_errors.log'
        self.link_state_callback=None
        self._threads=[]
        self._prepare=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ofdm-tx-prepare')
        self.stream_metrics=dict(tx_blocks=0,rx_blocks=0,decoded_packets=0,
                                 idle_tx_blocks=0,rx_queue_high_water=0,tx_buffer_creations=0,
                                 rx_buffer_creations=0,training_group_packets=self.group_size)
        self.stream_metrics.update(link_wait_events=0,link_wait_seconds=0.)
        self.stream_metrics.update(rx_io_recoveries=0,tx_io_recoveries=0)
        self._layout=configuration(settings.ofdm_fft_size)
        dummy=[make_packet(0xffffffff,i,self.group_size,bytes(1024)) for i in range(self.group_size)]
        reference=self._modulate(dummy,settings.modulation)
        self._block_samples=len(reference)
        self._tx_csi_scale=self._training_scale(reference)
        self._idle_wave=reference
        self._burst_count=self.group_size
        self._last_diagnostics=0
        if self.sdr is not None:
            self.sdr.tx_cyclic_buffer=False
            self.sdr._rxadc.set_kernel_buffers_count(8)
            self.sdr.rx_buffer_size=self._block_samples
            self.sdr._ctx.set_timeout(1000)

    def _training_scale(self,wave):
        c=self._layout
        training=np.fft.fft(wave[128+c.cp_size:128+c.cp_size+c.fft_size])
        return complex(np.mean(training[c.active]/c.training[c.active]))

    def _wave(self,packets):
        padded=list(packets)+[packets[-1]]*(self.group_size-len(packets))
        wave=self._modulate(padded,self.settings.modulation)
        return (wave*(self._tx_csi_scale/self._training_scale(wave))).astype(np.complex64)

    def _fail(self,exc,stage):
        if not self._shutdown.is_set():
            with self._condition:
                if self._failure is not None: return
                self._failure=exc
                self._failure_stage=stage
                try:
                    self.error_log.parent.mkdir(parents=True,exist_ok=True)
                    with self.error_log.open('a',encoding='utf-8') as log:
                        log.write(time.strftime('%Y-%m-%d %H:%M:%S')+f' [{stage}]\n')
                        log.write(''.join(traceback.format_exception(type(exc),exc,exc.__traceback__)))
                except OSError: pass
                # Release native buffer objects retained by traceback frames
                # before their IIO context is destroyed during shutdown.
                exc.__traceback__=None
                self._condition.notify_all()

    def _device_transfer(self,direction,wave=None):
        """Recover transient DMA errors without discarding delivery progress."""
        # USB backends may return Linux errno values even on Windows, where
        # Python's ETIMEDOUT/EOVERFLOW constants have different numbers.
        recoverable={errno.EIO,errno.ETIMEDOUT,errno.EPIPE,errno.EOVERFLOW,110,75}
        failures=0
        while not self._shutdown.is_set():
            try:
                name='_rxbuf' if direction=='rx' else '_txbuf'
                new_buffer=getattr(self.sdr,name,None) is None
                result=self.sdr.rx() if direction=='rx' else self.sdr.tx(wave)
                if new_buffer: self.stream_metrics[direction+'_buffer_creations']+=1
                generation=self._rx_generation
                return (result,generation) if direction=='rx' else result
            except OSError as exc:
                if exc.errno not in recoverable: raise
                if self._shutdown.is_set(): break
                self.stream_metrics[direction+'_io_recoveries']+=1
                with self._device_io_lock:
                    getattr(self.sdr,direction+'_destroy_buffer')()
                    if direction=='rx': self._rx_generation+=1
                try:
                    with self.error_log.open('a',encoding='utf-8') as log:
                        log.write(time.strftime('%Y-%m-%d %H:%M:%S')+
                                  f' [{direction} recovery] {exc!r}; rebuilding buffer\n')
                except OSError: pass
                failures+=1
                self._shutdown.wait(min(.1*failures,.5))
        raise InterruptedError('Radio transport closed')

    def _start_stream(self):
        if self._threads or self.sdr is None:
            return
        def receive():
            try:
                while not self._shutdown.is_set():
                    iq,generation=self._device_transfer('rx')
                    if isinstance(iq,list): iq=iq[0]
                    item=(np.asarray(iq,dtype=np.complex64),time.monotonic(),generation)
                    while not self._shutdown.is_set():
                        try:
                            self._iq.put(item,timeout=.05)
                            break
                        except Full: pass
                    self.stream_metrics['rx_blocks']+=1
                    self.stream_metrics['rx_queue_high_water']=max(
                        self.stream_metrics['rx_queue_high_water'],self._iq.qsize())
            except Exception as exc: self._fail(exc,'接收')
        def decode():
            tail=np.empty(0,np.complex64)
            previous_generation=None
            try:
                while not self._shutdown.is_set():
                    try: iq,captured,generation=self._iq.get(timeout=.05)
                    except Empty: continue
                    if generation!=previous_generation:
                        tail=np.empty(0,np.complex64)
                        previous_generation=generation
                    joined=np.concatenate((tail,iq))
                    tail=joined[-self._block_samples:].copy()
                    packets=self._decode(joined,self.settings.modulation,
                        max_packets=512,check_crc=True)
                    self._deliver(packets)
            except Exception as exc: self._fail(exc,'解码')
        def transmit():
            try:
                while not self._shutdown.is_set():
                    try:
                        wave=self._tx_iq.get(timeout=.02)
                        idle=False
                    except Empty:
                        wave=self._idle_wave
                        idle=True
                    if self._shutdown.is_set(): break
                    self._device_transfer('tx',wave)
                    self.stream_metrics['tx_blocks']+=1
                    self.stream_metrics['idle_tx_blocks']+=int(idle)
            except Exception as exc: self._fail(exc,'发送')
        self._threads=[Thread(target=receive,name='ofdm-rx',daemon=True),
                       Thread(target=decode,name='ofdm-decode',daemon=True),
                       Thread(target=transmit,name='ofdm-tx',daemon=True)]
        for thread in self._threads: thread.start()

    def _deliver(self,packets):
        with self._condition:
            for packet in packets:
                key=(packet.frame,packet.chunk)
                if key in self._wanted:
                    self._received[key]=packet
            self.stream_metrics['decoded_packets']+=len(packets)
            self._condition.notify_all()

    def transceive_batch(self,raw_packets,constellation_ready=None,stop=None):
        with self._transfer_lock:
            return self._transfer_batch(raw_packets,stop)

    def _transfer_batch(self,raw_packets,stop):
        if stop is not None and stop.is_set():
            raise InterruptedError('Radio transfer cancelled')
        parsed=[parse_packet(p) for p in raw_packets]
        if not 1<=len(parsed)<=512 or any(p is None for p in parsed):
            raise ValueError('Persistent transport requires 1..512 valid packets')
        keys=[(p.frame,p.chunk) for p in parsed]
        if len(set(keys))!=len(keys): raise ValueError('Duplicate packet identity')
        if any(p.frame==0xffffffff for p in parsed):
            raise ValueError('Frame 0xffffffff is reserved for idle training')
        with self._condition:
            self._wanted=set(keys)
            self._received={}
        self._start_stream()
        self.last_retries=self.last_retransmitted_packets=0
        waiting_since=None
        def check():
            if self._shutdown.is_set(): raise InterruptedError('Radio transport closed')
            if stop is not None and stop.is_set(): raise InterruptedError('Radio transfer cancelled')
            if self._failure is not None:
                raise RuntimeError(f'持续收发线程异常（{self._failure_stage}）：'
                    f'{type(self._failure).__name__}: {self._failure}；详细日志：{self.error_log}') from self._failure
        try:
            attempt=0
            while True:
                check()
                with self._condition:
                    pending=[p for p,k in zip(raw_packets,keys) if k not in self._received]
                if not pending:
                    break
                if attempt>=3 and waiting_since is None:
                    waiting_since=time.perf_counter()
                    self.stream_metrics['link_wait_events']+=1
                    if self.link_state_callback:
                        self.link_state_callback(True,len(keys)-len(pending),len(keys))
                if attempt:
                    self.last_retries+=1
                    self.last_retransmitted_packets+=len(pending)
                groups=[pending[i:i+self.group_size] for i in range(0,len(pending),self.group_size)]
                future=self._prepare.submit(self._wave,groups[0])
                for index in range(len(groups)):
                    check()
                    wave=future.result()
                    if index+1<len(groups): future=self._prepare.submit(self._wave,groups[index+1])
                    if self.sdr is None:
                        from .modem import simulate_capture
                        self._deliver(self._decode(simulate_capture(wave,snr_db=42),
                            self.settings.modulation,max_packets=32,check_crc=True))
                    else:
                        while True:
                            check()
                            try:
                                self._tx_iq.put(wave,timeout=.02)
                                break
                            except Full: pass
                deadline=time.perf_counter()+max(.3,self._iq.qsize()*.08)
                with self._condition:
                    while len(self._received)<len(keys) and time.perf_counter()<deadline:
                        check()
                        self._condition.wait(.02)
                attempt+=1
            check()
            with self._condition:
                received=[self._received[k] for k in keys]
            if waiting_since is not None and self.link_state_callback:
                self.link_state_callback(False,len(keys),len(keys))
            return received
        finally:
            if waiting_since is not None:
                self.stream_metrics['link_wait_seconds']+=time.perf_counter()-waiting_since
            with self._condition:
                self._wanted=set()

    def start_constellation_monitor(self,*args,**kwargs):
        # The receive thread already supplies real IQ diagnostics.
        return

    def close(self):
        self._shutdown.set()
        self._prepare.shutdown(wait=True,cancel_futures=True)
        for thread in self._threads: thread.join(timeout=1.2)
        if any(t.is_alive() for t in self._threads) and self.sdr is not None:
            for name in ('_rxbuf','_txbuf'):
                buffer=getattr(self.sdr,name,None)
                if buffer is not None: buffer.cancel()
            for thread in self._threads: thread.join(timeout=1.2)
        if any(t.is_alive() for t in self._threads):
            raise RuntimeError('Persistent radio worker did not stop')
        super().close()
