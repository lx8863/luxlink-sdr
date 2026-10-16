import csv
import json
from pathlib import Path
import tempfile
import unittest
from threading import Event
import numpy as np
from video_loopback import ofdm
from video_loopback.modem import make_packet
from video_loopback.radio import RadioSettings, LoopbackRadio
from video_loopback.csi import CsiLog


class CsiTests(unittest.TestCase):
    def test_ofdm_monitor_publishes_fresh_iq_between_transfers(self):
        updated = Event()
        settings = RadioSettings(mode='simulate', waveform='ofdm')
        with LoopbackRadio(settings) as radio:
            radio.transceive_batch([make_packet(5, 0, 1, b'idle monitor')])
            samples = []
            def publish(sample):
                samples.append(sample)
                if len(samples) >= 2:
                    updated.set()
            radio.set_channel_callback(publish)
            radio.start_constellation_monitor(lambda *_: None, hz=20)
            self.assertTrue(updated.wait(2), 'OFDM plots stalled between transfers')
            self.assertGreater(samples[-1]['timestamp'], samples[0]['timestamp'])
            self.assertTrue(samples[-1]['crc_valid'])

    def test_capture_starting_at_second_training_skips_false_pair(self):
        wave = ofdm.modulate(make_packet(7, 0, 1, bytes(range(256))*4))
        scale = np.mean(np.fft.fft(wave[128+ofdm.CP_SIZE:128+ofdm.STRIDE])[
            ofdm.ACTIVE] / ofdm.TRAINING[ofdm.ACTIVE])
        for offset in (129, 128+ofdm.STRIDE-1, 128+ofdm.STRIDE,
                       128+ofdm.STRIDE+1):
            with self.subTest(offset=offset):
                samples = []
                packets = ofdm.demodulate_packets(np.tile(wave, 3)[offset:],
                    on_diagnostics=samples.append, training_only=True, max_packets=1)
                self.assertTrue(packets)
                self.assertEqual(len(samples), 1)
                self.assertTrue(samples[0]['crc_valid'])
                self.assertGreater(samples[0]['training_pair_correlation'], .99)
                np.testing.assert_allclose(samples[0]['csi_raw']/scale, 1, atol=2e-6)

    def test_unpaired_training_does_not_publish_csi(self):
        wave = ofdm.modulate(make_packet(7, 0, 1, bytes(range(256))*4))
        capture = np.r_[wave[128+ofdm.STRIDE:], np.zeros(len(wave))]
        samples = []
        ofdm.demodulate_packets(capture, on_diagnostics=samples.append,
                                training_only=True)
        self.assertEqual(samples, [])

    def test_training_csi_survives_corrupted_payload(self):
        wave=ofdm.modulate(make_packet(1,0,1,b'corrupted data'))
        wave[128+2*ofdm.STRIDE:]=0
        capture=np.tile(wave,3)
        samples=[]
        packets=ofdm.demodulate_packets(capture,on_diagnostics=samples.append,training_only=True)
        self.assertFalse(packets)
        self.assertEqual(len(samples),1)
        self.assertFalse(samples[0]['crc_valid'])
        self.assertGreater(samples[0]['training_correlation'],.9)
        self.assertTrue(np.all(np.isfinite(samples[0]['csi_raw'])))

    def test_fresh_capture_reuses_training_transmission(self):
        settings=RadioSettings(mode='simulate',waveform='ofdm',modulation='qpsk')
        with LoopbackRadio(settings) as radio:
            radio.transceive_batch([make_packet(5,0,1,b'continuous CSI')])
            transmitted=radio._monitor_wave
            radio._modulate=lambda *args: self.fail('capture must not remodulate or replace TX')
            received=radio.capture_csi()
            self.assertEqual(received[0].payload,b'continuous CSI')
            self.assertIs(radio._monitor_wave,transmitted)

    def test_known_multipath_complex_response(self):
        wave = ofdm.modulate(make_packet(3,0,1,b'CSI channel test'))
        taps = np.r_[.7+0j,np.zeros(6),.1+.05j]
        capture = np.convolve(np.roll(np.tile(wave,3),173),taps).astype(np.complex64)
        samples=[]
        packets=ofdm.demodulate_packets(capture,on_diagnostics=samples.append)
        self.assertTrue(packets)
        scale=np.mean(np.fft.fft(wave[128+ofdm.CP_SIZE:128+ofdm.STRIDE])[ofdm.ACTIVE]/ofdm.TRAINING[ofdm.ACTIVE])
        estimate=samples[0]['csi_raw']/scale
        expected=np.fft.fft(taps,ofdm.FFT_SIZE)[ofdm.ACTIVE]
        np.testing.assert_allclose(estimate,expected,atol=2e-5)
        self.assertEqual(len(samples[0]['subcarriers']),200)

    def test_radio_normalization_and_csv(self):
        settings=RadioSettings(mode='simulate',waveform='ofdm')
        samples=[]
        with LoopbackRadio(settings) as radio:
            radio.set_channel_callback(samples.append)
            radio.transceive_batch([make_packet(4,0,1,b'log CSI')])
        sample=samples[0]
        self.assertEqual(len(sample['csi']),200)
        self.assertAlmostEqual(float(np.median(np.abs(sample['csi']))),1,delta=.05)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'csi.csv'
            log=CsiLog(path,settings)
            log.write(sample)
            log.close()
            with path.open(encoding='utf-8-sig',newline='') as file:
                rows=list(csv.DictReader(file))
            self.assertEqual(len(rows),200)
            self.assertEqual(rows[0]['subcarrier'],'-100')
            self.assertEqual(rows[-1]['subcarrier'],'100')
            self.assertGreater(float(rows[0]['training_pair_correlation']), .8)
            self.assertFalse(json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))['phase_calibrated'])


if __name__=='__main__':
    unittest.main()
