"""AD9361 USB radio and a repeatable offline substitute."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import time
from threading import Event, Lock, Thread
from functools import partial

import numpy as np

from .modem import (Packet, constellation_points, demodulate_packets, modulate,
                    packet_symbols, simulate_capture, PREAMBLE, _rrc, _fftconvolve)
from .channel import TrainingChannelEstimator
from .native import prepare_libiio


@dataclass(frozen=True)
class RadioSettings:
    uri: str = "usb:"
    mode: str = "digital"  # digital, rf, or simulate
    channel: int = 0
    frequency_hz: int = 2_400_000_000
    sample_rate: int = 2_000_000
    tx_gain_db: float = -30.0
    rx_gain_db: float = 35.0
    attempts: int = 8
    modulation: str = "qpsk"
    samples_per_symbol: int = 8
    batch_packets: int = 1
    settle_seconds: float = 0.0
    check_crc: bool = True
    waveform: str = "single-carrier"
    ofdm_superframe: bool = False
    ofdm_fft_size: int = 256
    ofdm_training_packets: int = 0
    ofdm_streaming: bool = False


class IncompleteBurst(RuntimeError):
    def __init__(self, keys, received, attempts):
        self.received = received.copy()
        missing = [key for key in keys if key not in received]
        super().__init__(f"Missing {len(missing)}/{len(keys)} burst packets after "
                         f"{attempts} captures: {missing[:4]}")


class LoopbackRadio:
    def __init__(self, settings: RadioSettings):
        if settings.mode not in ("digital", "rf", "simulate"):
            raise ValueError("Mode must be digital, rf, or simulate")
        if settings.channel not in (0, 1):
            raise ValueError("Channel must be 0 or 1")
        if settings.modulation not in ("qpsk", "16qam", "64qam", "256qam"):
            raise ValueError("Modulation must be qpsk, 16qam, 64qam, or 256qam")
        if settings.sample_rate < 521_000 or settings.frequency_hz <= 0 or settings.attempts < 1:
            raise ValueError("Invalid sample rate, RF frequency, or retry count")
        if settings.samples_per_symbol not in (2, 4, 8):
            raise ValueError("Samples per symbol must be 2, 4, or 8")
        if not 0<=settings.ofdm_training_packets<=512:
            raise ValueError('OFDM training interval must be 0..512 packets')
        limit=512 if settings.waveform=='ofdm' and settings.ofdm_superframe else 64
        if not 1 <= settings.batch_packets <= limit or not 0 <= settings.settle_seconds <= 1:
            raise ValueError(f"Batch packets must be 1..{limit} and settle seconds 0..1")
        self.settings = settings
        if settings.waveform not in ("single-carrier", "ofdm"):
            raise ValueError("Waveform must be single-carrier or ofdm")
        if settings.waveform == "ofdm":
            from . import ofdm
            ofdm.configuration(settings.ofdm_fft_size)
            self._modulate = partial(ofdm.modulate,fft_size=settings.ofdm_fft_size)
            self._demodulate = partial(ofdm.demodulate_packets,fft_size=settings.ofdm_fft_size)
        else:
            self._modulate = modulate
            self._demodulate = demodulate_packets
        self.sdr = None
        self._rx_size = None
        self.last_retries = 0
        self.last_retransmitted_packets = 0
        self._active_modulation = settings.modulation
        self.channel_callback = None
        self._tx_references = {}
        self._channel_estimator = TrainingChannelEstimator(PREAMBLE)
        self._io_lock = Lock()
        self._transfer_pending = Event()
        self._diagnostics_window = Event()
        self._monitor_stop = Event()
        self._monitor = None
        self._monitor_wave = None
        self._monitor_points = None
        self._last_diagnostics = 0.0
        if settings.mode != "simulate":
            self._connect()

    def set_channel_callback(self, callback):
        self.channel_callback = callback

    def _decode(self, *args, **kwargs):
        if self.settings.waveform == "ofdm" and self.settings.ofdm_superframe:
            kwargs["burst_packets"] = self._burst_count
        if self.settings.waveform == "ofdm" and self.channel_callback is not None:
            now = time.monotonic()
            if now - self._last_diagnostics >= 0.04:
                def publish(sample):
                    self._last_diagnostics = time.monotonic()
                    sample.update(sample_rate=self.settings.sample_rate,
                                  fft_size=self.settings.ofdm_fft_size,cp_size=64,
                                  channel=self.settings.channel,
                                  frequency_hz=self.settings.frequency_hz,
                                  mode=self.settings.mode, timestamp=self._last_diagnostics)
                    sample['csi'] = sample.pop('csi_raw') / self._tx_csi_scale
                    self.channel_callback(sample)
                kwargs["on_diagnostics"] = publish
        return self._demodulate(*args, **kwargs)

    def capture_csi(self):
        """Read fresh IQ from the current cyclic training transmission."""
        if self._monitor_wave is None:
            raise RuntimeError('Start a training transmission before CSI capture')
        with self._io_lock:
            rx = (self.sdr.rx() if self.sdr is not None else
                  simulate_capture(self._monitor_wave, snr_db=38))
            if isinstance(rx,list):
                rx=rx[0]
            self._last_diagnostics=0.0  # Dedicated acquisition has its own pacing.
            return self._decode(rx,self._active_modulation,self.settings.samples_per_symbol,
                                max_packets=1,check_crc=True,training_only=True)

    def _channel_capture_callback(self, rx, timestamp, modulation):
        if self.settings.waveform == "ofdm":
            return None  # Single-carrier training estimator does not describe OFDM CSI.
        def report(pre, packet, start):
            reference = self._tx_references.get((packet.frame, packet.chunk))
            if reference is None or self.channel_callback is None:
                return
            begin = max(0, start - (len(_rrc(self.settings.samples_per_symbol)) - 1) // 2)
            raw = np.asarray(rx)[begin:begin + len(PREAMBLE) * self.settings.samples_per_symbol]
            rms = float(np.sqrt(np.mean(np.abs(raw) ** 2))) if len(raw) else 0
            sample = self._channel_estimator.estimate(pre, reference, timestamp, packet, modulation, rms)
            self.channel_callback(sample)
        return report if self.channel_callback is not None else None

    def _connect(self) -> None:
        prepare_libiio()
        try:
            import adi
        except ImportError as exc:
            raise RuntimeError("pyadi-iio is missing; install requirements.txt") from exc
        # AD9361 has two complex TX/RX paths. The board in IIO-Scope exposes both.
        try:
            sdr = adi.ad9361(uri=self.settings.uri)
        except Exception as exc:
            raise RuntimeError(
                f"Could not open AD9361 at {self.settings.uri!r}. "
                "Use the exact usb:X.Y.Z URI shown in IIO Oscilloscope or iio_info -s."
            ) from exc
        try:
            sdr.tx_cyclic_buffer = True
            sdr.rx_enabled_channels = [self.settings.channel]
            sdr.tx_enabled_channels = [self.settings.channel]
            if self.settings.ofdm_superframe:
                sdr._rxadc.set_kernel_buffers_count(1)
            sdr.sample_rate = self.settings.sample_rate
            actual_rate = int(sdr.sample_rate)
            if abs(actual_rate - self.settings.sample_rate) > self.settings.sample_rate * 0.02:
                raise RuntimeError(f"Board returned sample rate {actual_rate}, expected {self.settings.sample_rate}")
            bandwidth = max(200_000, round(1.35 * self.settings.sample_rate /
                                          self.settings.samples_per_symbol))
            if self.settings.waveform == "ofdm":
                bandwidth = round(self.settings.sample_rate * 0.9)
            sdr.rx_rf_bandwidth = bandwidth
            sdr.tx_rf_bandwidth = bandwidth
            sdr.rx_lo = self.settings.frequency_hz
            sdr.tx_lo = self.settings.frequency_hz
            setattr(sdr, f"tx_hardwaregain_chan{self.settings.channel}", self.settings.tx_gain_db)
            setattr(sdr, f"gain_control_mode_chan{self.settings.channel}", "manual")
            setattr(sdr, f"rx_hardwaregain_chan{self.settings.channel}", self.settings.rx_gain_db)
            # 1 is TX->RX digital BIST; 0 uses the physical RX input.
            sdr.loopback = 1 if self.settings.mode == "digital" else 0
            self.sdr = sdr
        except Exception:
            try:
                sdr.loopback = 0
            except Exception:
                pass
            raise

    def transceive(self, raw_packet: bytes, frame: int, chunk: int,
                   constellation_ready: Callable[[np.ndarray, np.ndarray | None], None] | None = None) -> Packet:
        packets = self.transceive_batch([raw_packet], constellation_ready)
        packet = packets[0]
        if (packet.frame, packet.chunk) != (frame, chunk):
            raise RuntimeError("Received packet identity differs from requested packet")
        return packet

    def transceive_batch(self, raw_packets: list[bytes],
                         constellation_ready: Callable[[np.ndarray, np.ndarray | None], None] | None = None,
                         stop=None) -> list[Packet]:
        self._transfer_pending.set()
        try:
            with self._io_lock:
                from .modem import parse_packet
                limit=512 if self.settings.waveform=='ofdm' and self.settings.ofdm_superframe else 64
                if not raw_packets or len(raw_packets) > limit:
                    raise ValueError(f"A burst requires 1..{limit} packets")
                originals = [parse_packet(raw) for raw in raw_packets]
                if any(packet is None for packet in originals):
                    raise ValueError("Burst contains an invalid packet")
                keys = [(packet.frame, packet.chunk) for packet in originals]
                if len(set(keys)) != len(keys):
                    raise ValueError("Burst packet identities must be unique")
                pending = list(raw_packets)
                found = {}
                captures = 0
                self.last_retransmitted_packets = 0
                for round_no in range(3):
                    mode = self.settings.modulation
                    if round_no and self.settings.mode == "rf" and mode in ("64qam", "256qam"):
                        mode = "16qam"
                    if round_no:
                        self.last_retransmitted_packets += len(pending)
                    try:
                        packets = self._transceive_batch(pending, constellation_ready, stop,
                                                        mode, 1 if self.settings.ofdm_superframe else min(self.settings.attempts, 2))
                        captures += self.last_retries + 1
                        found.update({(packet.frame, packet.chunk): packet for packet in packets})
                        self.last_retries = max(0, captures - 1)
                        return [found[key] for key in keys]
                    except IncompleteBurst as exc:
                        captures += self.last_retries + 1
                        found.update(exc.received)
                        pending = [raw for raw, key in zip(raw_packets, keys) if key not in found]
                self.last_retries = max(0, captures - 1)
                raise IncompleteBurst(keys, found, captures)
        finally:
            self._transfer_pending.clear()

    def _transceive_batch(self, raw_packets, constellation_ready=None, stop=None,
                          modulation=None, attempts=None):
        """Send a contiguous burst; one DMA update and matched filter per batch.

        Cyclic DMA repeats the burst until every requested packet is received.
        This is a same-device loopback API, not a remote acknowledgement protocol.
        """
        from .modem import parse_packet
        modulation = modulation or self.settings.modulation
        attempts = attempts or self.settings.attempts
        limit=512 if self.settings.waveform=='ofdm' and self.settings.ofdm_superframe else 64
        if not raw_packets or len(raw_packets) > limit:
            raise ValueError(f"A burst requires 1..{limit} packets")
        expected = [parse_packet(raw) for raw in raw_packets]
        if any(packet is None for packet in expected):
            raise ValueError("Burst contains an invalid packet")
        keys = [(packet.frame, packet.chunk) for packet in expected]
        key_set=set(keys)
        if len(set(keys)) != len(keys):
            raise ValueError("Burst packet identities must be unique")
        group=self.settings.ofdm_training_packets
        grouped=self.settings.waveform=='ofdm' and self.settings.ofdm_superframe and group>0
        burst_count = min(len(raw_packets),group) if grouped else len(raw_packets)
        monitoring_window=grouped and self._monitor_wave is not None
        if monitoring_window:
            self._diagnostics_window.set()
            self._io_lock.release()
        try:
            if grouped:
                groups=[raw_packets[i:i+burst_count] for i in range(0,len(raw_packets),burst_count)]
            # Pad only the final training block; receiver identity deduplication
            # removes these repeated packets without changing the file payload.
                groups[-1]+=groups[-1][-1:]*(burst_count-len(groups[-1]))
                waves=[self._modulate(part,modulation) for part in groups]
                training_peak=abs(waves[0][128+64])
                waves=[part*(training_peak/max(1e-12,abs(part[128+64]))) for part in waves]
            else:
                waves = ([self._modulate(raw_packets, modulation)]
                 if self.settings.waveform == "ofdm" and self.settings.ofdm_superframe else
                 [self._modulate(raw, modulation, self.settings.samples_per_symbol) for raw in raw_packets])
            wave = np.concatenate(waves)
        finally:
            if monitoring_window:
                self._diagnostics_window.clear()
                self._io_lock.acquire()
        self._burst_count=burst_count
        self._active_modulation=modulation
        if self.settings.waveform == 'ofdm':
            from .ofdm import configuration
            layout=configuration(self.settings.ofdm_fft_size)
            FFT_SIZE,CP_SIZE,ACTIVE,TRAINING=layout.fft_size,layout.cp_size,layout.active,layout.training
            tx_training = np.fft.fft(wave[128+CP_SIZE:128+CP_SIZE+FFT_SIZE])
            self._tx_csi_scale = complex(np.mean(tx_training[ACTIVE]/TRAINING[ACTIVE]))
        if self.channel_callback is not None and self.settings.waveform != "ofdm":
            taps = _rrc(self.settings.samples_per_symbol)
            indices = self._channel_estimator.indices
            self._tx_references = {}
            for packet, waveform in zip(expected, waves):
                training_capture = _fftconvolve(waveform[:64 + len(PREAMBLE) *
                                                           self.settings.samples_per_symbol + len(taps)], taps)
                known = training_capture[64 + len(taps) - 1 + indices * self.settings.samples_per_symbol]
                reference = np.vdot(PREAMBLE[indices], known) / len(indices)
                self._tx_references[(packet.frame, packet.chunk)] = reference
        tx_points = (constellation_points(packet_symbols(raw_packets[0], modulation))
                     if constellation_ready else None)
        self._monitor_wave = wave
        self._monitor_packet_samples = len(wave) // len(raw_packets)
        self._monitor_group_samples = len(waves[0])
        self._monitor_points = tx_points
        if constellation_ready is not None:
            constellation_ready(tx_points, None)

        def received_points(rx_points: np.ndarray) -> None:
            if constellation_ready is not None:
                constellation_ready(tx_points, rx_points)

        self.last_retries = 0
        sdr = self.sdr
        if self.settings.mode != "simulate":
            assert sdr is not None
            # Destroy old RX DMA too: otherwise queued captures can contain the
            # previous burst, especially when captures have the same size.
            sdr.rx_destroy_buffer()
            rx_size = len(wave) * (1 if self.settings.ofdm_superframe else 2) + 512
            sdr.rx_buffer_size = rx_size
            self._rx_size = rx_size
            sdr.tx_destroy_buffer()
            sdr.tx(wave)
            if self.settings.settle_seconds:
                time.sleep(self.settings.settle_seconds)
        found: dict[tuple[int, int], Packet] = {}
        for attempt in range(attempts):
            if stop is not None and stop.is_set():
                raise InterruptedError("Radio transfer cancelled")
            if sdr is not None and self._rx_size!=rx_size:
                sdr.rx_destroy_buffer()
                sdr.rx_buffer_size=rx_size
                self._rx_size=rx_size
            rx = (simulate_capture(wave, snr_db=38, capture_samples=len(wave) * 2 + 512)
                  if sdr is None else sdr.rx())
            if isinstance(rx, list):
                rx = rx[0]
            if self.settings.ofdm_superframe and len(rx)<2*len(wave):
                # This same-board cyclic mode reconstructs a second period
                # from the observation. Preserve RF phase continuity using
                # the extra 512 genuinely captured overlapping samples.
                observation=np.asarray(rx,dtype=np.complex64)
                period=observation[:len(wave)]
                overlap=observation[len(wave):]
                reference=period[:len(overlap)]
                energy=float(np.vdot(reference,reference).real)
                rotation=np.vdot(reference,overlap)/max(1e-12,energy)
                rotation=rotation/max(1e-12,abs(rotation))
                rx=np.concatenate((observation,period[len(overlap):]*rotation))
            timestamp = time.monotonic()
            # Diagnostics may borrow the USB device while CPU packet decoding
            # runs. TX and RX buffer changes remain serialized by _io_lock.
            if grouped:
                self._diagnostics_window.set()
                self._io_lock.release()
            try:
                packets = self._decode(rx, modulation,
                                         self.settings.samples_per_symbol,
                                         max_packets=len(keys), on_constellation=received_points,
                                         check_crc=self.settings.check_crc,
                                         on_channel=self._channel_capture_callback(rx, timestamp, modulation))
            finally:
                if grouped:
                    self._diagnostics_window.clear()
                    self._io_lock.acquire()
            self.last_retries = attempt
            for packet in packets:
                key = (packet.frame, packet.chunk)
                if key in key_set:
                    found[key] = packet
            if len(found) == len(keys):
                return [found[key] for key in keys]
        raise IncompleteBurst(keys, found, attempts)

    def start_constellation_monitor(self, callback, hz: float = 8) -> None:
        """Capture fresh cyclic RX data between transfers, with TX priority."""
        if self._monitor is not None or not 0 < hz <= 20:
            return

        def monitor():
            next_sample = time.monotonic()
            while not self._monitor_stop.wait(max(0, next_sample - time.monotonic())):
                next_sample = max(next_sample + 1 / hz, time.monotonic())
                if ((self._transfer_pending.is_set() and not self._diagnostics_window.is_set()) or self._monitor_wave is None or
                        (self.settings.waveform != 'ofdm' and self._monitor_points is None)):
                    continue
                if not self._io_lock.acquire(blocking=False):
                    continue
                try:
                    if self._transfer_pending.is_set() and not self._diagnostics_window.is_set():
                        continue
                    if self.sdr is not None:
                        # A scatter plot needs only a few packets, not the
                        # multi-megabyte burst capture used by video transport.
                        size = (self._monitor_group_samples * 2 + 512
                                if self.settings.waveform == 'ofdm' else
                                self._monitor_packet_samples * 3 + 512)
                        if self._rx_size != size:
                            self.sdr.rx_destroy_buffer()
                            self.sdr.rx_buffer_size = size
                            self._rx_size = size
                    rx = (self.sdr.rx() if self.sdr is not None else
                          simulate_capture(self._monitor_wave, snr_db=38))
                    if isinstance(rx, list):
                        rx = rx[0]
                    timestamp = time.monotonic()
                    self._decode(rx, self._active_modulation,
                        self.settings.samples_per_symbol, max_packets=1,
                        on_constellation=lambda points: callback(self._monitor_points, points),
                        check_crc=self.settings.check_crc,
                        on_channel=self._channel_capture_callback(rx, timestamp, self._active_modulation),
                        **({'training_only': True,'diagnostic_only':self.settings.ofdm_training_packets>0}
                           if self.settings.waveform == 'ofdm' else {}))
                except Exception:
                    # Monitoring is optional; transfer errors are reported by
                    # the foreground path. The UI marks old samples as stale.
                    pass
                finally:
                    self._io_lock.release()

        self._monitor = Thread(target=monitor, daemon=True)
        self._monitor.start()

    def close(self) -> None:
        self._monitor_stop.set()
        if self._monitor is not None:
            self._monitor.join()
            self._monitor = None
        if self.sdr is None:
            return
        try:
            self.sdr.tx_destroy_buffer()
        finally:
            try:
                self.sdr.rx_destroy_buffer()
            finally:
                # Release the digital BIST route when this app exits.
                try:
                    self.sdr.loopback = 0
                except Exception:
                    pass  # The USB device may have been unplugged during a run.
                self.sdr = None

    def __enter__(self) -> "LoopbackRadio":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
