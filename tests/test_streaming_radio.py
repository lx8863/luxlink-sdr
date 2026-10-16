import time
import errno
import tempfile
from pathlib import Path
import unittest
from queue import Queue,Empty
from threading import Event,Timer
from unittest.mock import patch,Mock
import numpy as np
from video_loopback.streaming_radio import StreamingLoopbackRadio
from video_loopback.radio import RadioSettings
from video_loopback.modem import make_packet,simulate_capture,parse_packet


class FakeDuplex:
    def __init__(self,drop_first=False,fail_rx=False):
        self._ctx=self._rxadc=Mock()
        self._rxbuf=self._txbuf=None
        self.rx_creations=self.tx_creations=0
        self.samples=Queue()
        self.drop_first=drop_first
        self.fail_rx=fail_rx
    def tx(self,wave):
        if self._txbuf is None:
            self._txbuf=Mock();self.tx_creations+=1
        if self.drop_first:
            self.drop_first=False
            return
        self.samples.put(simulate_capture(wave,snr_db=42))
    def rx(self):
        if self._rxbuf is None:
            self._rxbuf=Mock();self.rx_creations+=1
        if self.fail_rx: raise OSError('USB disconnected')
        try: return self.samples.get(timeout=.05)
        except Empty: return np.zeros(self.rx_buffer_size,np.complex64)
    def tx_destroy_buffer(self): self._txbuf=None
    def rx_destroy_buffer(self): self._rxbuf=None


class StreamingRadioTests(unittest.TestCase):
    def settings(self):
        return RadioSettings(mode='rf',waveform='ofdm',ofdm_superframe=True,
            ofdm_streaming=True,ofdm_fft_size=1024,modulation='16qam',
            batch_packets=256,sample_rate=3000000)
    def radio(self,fake):
        with patch.object(StreamingLoopbackRadio,'_connect',lambda radio:setattr(radio,'sdr',fake)):
            radio=StreamingLoopbackRadio(self.settings())
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        radio.error_log=Path(folder.name)/'radio_errors.log'
        return radio
    def packets(self,batch,count):
        rng=np.random.default_rng(batch)
        payloads=[rng.bytes(1024) for _ in range(count)]
        return payloads,[make_packet(batch,i,count,p) for i,p in enumerate(payloads)]
    def test_persistent_buffers_partial_blocks_and_idle_diagnostics(self):
        fake=FakeDuplex()
        samples=[]
        with self.radio(fake) as radio:
            radio.set_channel_callback(samples.append)
            for batch,count in ((1,17),(2,41)):
                expected,packets=self.packets(batch,count)
                received=radio.transceive_batch(packets)
                self.assertEqual([p.payload for p in received],expected)
                self.assertTrue(all(p.crc_valid for p in received))
            self.assertEqual((fake.tx_creations,fake.rx_creations),(1,1))
            before=len(samples)
            deadline=time.monotonic()+1
            while len(samples)==before and time.monotonic()<deadline: time.sleep(.01)
            self.assertGreater(len(samples),before,'Idle RF training must keep plots alive')
        self.assertTrue(all(not t.is_alive() for t in radio._threads))
    def test_missing_block_selective_retry_and_exact_order(self):
        with self.radio(FakeDuplex(drop_first=True)) as radio:
            expected,packets=self.packets(5,32)
            received=radio.transceive_batch(packets)
            self.assertEqual([p.payload for p in received],expected)
            self.assertEqual(radio.last_retries,1)
            self.assertEqual(radio.last_retransmitted_packets,16)

    def test_signal_loss_waits_past_old_retry_limit_then_resumes_partial_batch(self):
        fake=FakeDuplex()
        transmit=fake.tx
        restored=Event()
        calls=[0]
        def interrupted(wave):
            calls[0]+=1
            if calls[0]==1 or restored.is_set(): transmit(wave)
        fake.tx=interrupted
        states=[]
        timer=Timer(1.8,restored.set)
        with self.radio(fake) as radio:
            radio.link_state_callback=lambda *state:states.append(state)
            expected,packets=self.packets(8,32)
            timer.start()
            try:
                with patch.object(radio,'_wave',wraps=radio._wave) as prepare:
                    result=radio.transceive_batch(packets)
            finally: timer.cancel()
            self.assertEqual([p.payload for p in result],expected)
            self.assertGreaterEqual(radio.last_retries,4)
            self.assertEqual(states[0],(True,16,32))
            self.assertEqual(states[-1],(False,32,32))
            first_block_sends=sum(parse_packet(call.args[0][0]).chunk==0 for call in prepare.call_args_list)
            self.assertEqual(first_block_sends,1,'Already received chunks must survive the outage')
    def test_receive_failure_propagates_and_stops_workers(self):
        with self.radio(FakeDuplex(fail_rx=True)) as radio:
            _,packets=self.packets(3,16)
            with self.assertRaisesRegex(RuntimeError,'线程异常（接收）'):
                radio.transceive_batch(packets)
        self.assertTrue(all(not t.is_alive() for t in radio._threads))

    def test_transient_rx_io_error_rebuilds_buffer_and_preserves_transfer(self):
        fake=FakeDuplex()
        receive=fake.rx
        fail_once=[True]
        def rx():
            if fail_once[0]:
                fail_once[0]=False
                raise OSError(errno.EIO,'Input/output error')
            return receive()
        fake.rx=rx
        with self.radio(fake) as radio:
            expected,packets=self.packets(9,32)
            result=radio.transceive_batch(packets)
            self.assertEqual([p.payload for p in result],expected)
            self.assertEqual(radio.stream_metrics['rx_io_recoveries'],1)
            self.assertIsNone(radio._failure)
            self.assertIn('rx recovery',radio.error_log.read_text(encoding='utf-8'))

    def test_transient_tx_io_error_retries_same_wave_without_losing_packets(self):
        fake=FakeDuplex()
        transmit=fake.tx
        fail_once=[True]
        def tx(wave):
            if fail_once[0]:
                fail_once[0]=False
                raise OSError(errno.ETIMEDOUT,'Timed out')
            return transmit(wave)
        fake.tx=tx
        with self.radio(fake) as radio:
            expected,packets=self.packets(10,32)
            result=radio.transceive_batch(packets)
            self.assertEqual([p.payload for p in result],expected)
            self.assertEqual(radio.stream_metrics['tx_io_recoveries'],1)
            self.assertIsNone(radio._failure)

    def test_linux_usb_timeout_errno_is_recoverable_on_windows(self):
        fake=FakeDuplex()
        receive=fake.rx
        fail_once=[True]
        def rx():
            if fail_once[0]:
                fail_once[0]=False
                raise OSError(110,'USB backend timed out')
            return receive()
        fake.rx=rx
        with self.radio(fake) as radio:
            expected,packets=self.packets(11,16)
            received=radio.transceive_batch(packets)
            self.assertEqual([p.payload for p in received],expected)
            self.assertEqual(radio.stream_metrics['rx_io_recoveries'],1)
            self.assertIsNone(radio._failure)
    def test_pre_cancelled_transfer_never_starts_workers(self):
        with self.radio(FakeDuplex()) as radio:
            stop=Event();stop.set()
            _,packets=self.packets(4,16)
            with self.assertRaises(InterruptedError): radio.transceive_batch(packets,stop=stop)
            self.assertEqual(radio._threads,[])

    def test_stop_during_receive_wait_releases_all_workers(self):
        fake=FakeDuplex()
        fake.tx=lambda wave: None  # Simulate a channel delivering no packets.
        stop=Event()
        timer=Timer(1.3,stop.set)
        with self.radio(fake) as radio:
            _,packets=self.packets(6,32)
            timer.start()
            begin=time.monotonic()
            try:
                with self.assertRaises(InterruptedError): radio.transceive_batch(packets,stop=stop)
            finally: timer.cancel()
            self.assertLess(time.monotonic()-begin,1.8)
            self.assertEqual(radio.stream_metrics['link_wait_events'],1)
        self.assertTrue(all(not t.is_alive() for t in radio._threads))
