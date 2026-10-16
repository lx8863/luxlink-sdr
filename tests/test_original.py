from pathlib import Path
import tempfile
import unittest
import subprocess
import time
import imageio_ffmpeg
from video_loopback.original import media_hashes
from video_loopback.radio import RadioSettings
from video_loopback.video import VideoSettings, run_video
from video_loopback import ofdm
from video_loopback.modem import make_packet, simulate_capture


class OriginalTests(unittest.TestCase):
    def test_periodic_training_preserves_partial_group_and_monitor_does_not_decode_payload(self):
        from video_loopback.radio import LoopbackRadio
        import numpy as np
        packets=[make_packet(7,i,73,bytes([i])*1024) for i in range(73)]
        samples=[]
        settings=RadioSettings(mode='simulate',waveform='ofdm',ofdm_superframe=True,
            ofdm_fft_size=1024,ofdm_training_packets=16,modulation='256qam',batch_packets=256)
        with LoopbackRadio(settings) as radio:
            radio.set_channel_callback(samples.append)
            received=radio.transceive_batch(packets)
            self.assertEqual([p.payload for p in received],[bytes([i])*1024 for i in range(73)])
            self.assertFalse(radio._diagnostics_window.is_set())
            diagnostics=[]
            result=ofdm.demodulate_packets(simulate_capture(radio._monitor_wave,snr_db=42),
                '256qam',fft_size=1024,burst_packets=16,diagnostic_only=True,
                on_diagnostics=diagnostics.append)
            self.assertEqual(result,[])
            self.assertEqual(len(diagnostics),1)
            self.assertIsNone(diagnostics[0]['crc_valid'])
            self.assertEqual(len(diagnostics[0]['csi_raw']),800)

    def test_large_fft_burst_preserves_payload_and_reports_actual_carriers(self):
        import numpy as np
        rng=np.random.default_rng(93)
        for size in (512,1024,2048):
            packets=[make_packet(7,i,16,rng.bytes(1024)) for i in range(16)]
            layout=ofdm.configuration(size)
            wave=ofdm.modulate(packets,'64qam',fft_size=size)
            capture=simulate_capture(wave,snr_db=42)
            samples=[]
            received=ofdm.demodulate_packets(capture,'64qam',burst_packets=16,
                max_packets=16,fft_size=size,on_diagnostics=samples.append)
            from video_loopback.modem import parse_packet
            self.assertEqual([p.payload for p in received],[parse_packet(p).payload for p in packets])
            self.assertEqual(len(samples[0]['csi_raw']),len(layout.active))
        self.assertEqual(ofdm.FFT_SIZE,256,'Large FFT must not change independent CSI defaults')

    def test_pilots_track_gain_changes_through_long_burst(self):
        import numpy as np
        from video_loopback.modem import parse_packet
        packets=[make_packet(7,i,16,bytes([i])*1024) for i in range(16)]
        layout=ofdm.configuration(1024)
        wave=ofdm.modulate(packets,'64qam',fft_size=1024)
        stride=layout.fft_size+layout.cp_size
        rows=(len(wave)-256)//stride
        blocks=wave[128:-128].reshape(rows,stride)
        blocks[2:]*=np.linspace(.65,1.35,rows-2)[:,None]
        received=ofdm.demodulate_packets(simulate_capture(wave,snr_db=45),'64qam',
            burst_packets=16,max_packets=16,fft_size=1024)
        self.assertEqual([p.payload for p in received],[parse_packet(p).payload for p in packets])

    def test_superframe_reassembles_all_crc_packets(self):
        packets = [make_packet(7, i, 8, bytes([i])*1024) for i in range(8)]
        for modulation in ('qpsk', '16qam', '64qam', '256qam'):
            capture = simulate_capture(ofdm.modulate(packets, modulation), snr_db=42)
            received = ofdm.demodulate_packets(capture, modulation, burst_packets=8)
            self.assertEqual([p.payload for p in received], [bytes([i])*1024 for i in range(8)])

    def test_original_payload_and_decoded_frames_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'source.mp4', Path(folder)/'received.mp4'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
                            '-f', 'lavfi', '-i', 'testsrc2=size=128x72:rate=30', '-t', '0.4',
                            '-c:v', 'libx264', str(source)], check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            settings = VideoSettings(source, target, RadioSettings(mode='simulate',waveform='ofdm',
                                     ofdm_superframe=True,modulation='16qam',batch_packets=64),
                                     transport='original',constellation_hz=20)
            samples, displayed = [], []
            progress=[]
            stats = run_video(settings, status=lambda text: None,
                              transfer_progress=lambda received,total: progress.append((received,total)),
                              channel_ready=lambda sample: samples.append(sample['timestamp']),
                              pair_frame_ready=lambda *_: displayed.append(time.monotonic()))
            self.assertEqual(media_hashes(source), media_hashes(target))
            self.assertEqual(stats['frames'], 12)
            self.assertTrue(stats['complete_transfer'])
            self.assertEqual(progress[0],(0,stats['transport_bytes']))
            self.assertEqual(progress[-1],(stats['transport_bytes'],stats['transport_bytes']))
            self.assertEqual([received for received,total in progress],
                             sorted(received for received,total in progress))
            self.assertTrue(stats['media_payload_identical'])
            self.assertFalse(stats['transcoded'])
            self.assertEqual(stats['effective_batch_packets'], 64)
            self.assertGreater(len(samples), 2)
            self.assertGreater(samples[-1], displayed[0]+.15,
                               'Fresh diagnostics must continue during buffered playback')


if __name__ == '__main__':
    unittest.main()
