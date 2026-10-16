"""Packet OFDM PHY: 256 FFT, 64 CP, training equalizer and pilot CPE.

Custom same-board waveform, not an IEEE 802.11 implementation. No FEC or
sample-clock tracking. Uses existing fixed-length scrambled CRC packets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
import numpy as np
from scipy.signal import fftconvolve, find_peaks
from scipy.fft import fft, ifft
from .modem import (PACKET_SIZE, SCRAMBLE_MASK, packet_symbols,
                    _symbols_to_bytes, parse_packet, constellation_points)

FFT_SIZE = 256
CP_SIZE = 64
STRIDE = FFT_SIZE + CP_SIZE
ACTIVE = np.r_[np.arange(-100, 0), np.arange(1, 101)] % FFT_SIZE
PILOTS = np.array([-84, -60, -36, -12, 12, 36, 60, 84]) % FFT_SIZE
DATA = np.array([k for k in ACTIVE if k not in set(PILOTS)])
PILOT_VALUES = np.array([1, 1, -1, 1, -1, -1, 1, -1], np.complex64)
_rng = np.random.default_rng(70209361)
TRAINING = np.zeros(FFT_SIZE, np.complex64)
TRAINING[ACTIVE] = _rng.choice([-1., 1.], len(ACTIVE))


def _time(grid, fft_size=FFT_SIZE, cp_size=CP_SIZE, active_count=len(ACTIVE)):
    useful = ifft(grid, axis=-1) * fft_size / np.sqrt(active_count)
    return np.concatenate((useful[..., -cp_size:], useful), axis=-1)


TRAIN_TIME = _time(TRAINING).astype(np.complex64)


@dataclass(frozen=True)
class OfdmLayout:
    fft_size: int
    cp_size: int
    active: np.ndarray
    pilots: np.ndarray
    data: np.ndarray
    training: np.ndarray
    train_time: np.ndarray


@lru_cache(maxsize=4)
def configuration(fft_size=256):
    if fft_size not in (256,512,1024,2048):
        raise ValueError('OFDM FFT size must be 256, 512, 1024 or 2048')
    half=100*fft_size//256
    active=np.r_[np.arange(-half,0),np.arange(1,half+1)]%fft_size
    pilots=(np.array([-84,-60,-36,-12,12,36,60,84])*(fft_size//256))%fft_size
    data=np.array([k for k in active if k not in set(pilots)])
    training=np.zeros(fft_size,np.complex64)
    training[active]=np.random.default_rng(70209361).choice([-1.,1.],len(active))
    return OfdmLayout(fft_size,64,active,pilots,data,training,
                      _time(training,fft_size,64,len(active)).astype(np.complex64))


def _burst_training_peaks(x, fft_size=FFT_SIZE, cp_size=CP_SIZE, train_time=TRAIN_TIME):
    """Locate repeated training in O(n), then match only small local windows."""
    FFT_SIZE,CP_SIZE,STRIDE,TRAIN_TIME=fft_size,cp_size,fft_size+cp_size,train_time
    step=4
    coarse_x=x[::step]
    width=FFT_SIZE//step
    shift=STRIDE//step
    products=np.conj(coarse_x[:-shift])*coarse_x[shift:]
    sums=np.r_[0j,np.cumsum(products,dtype=np.complex128)]
    corr=sums[width:]-sums[:-width]
    energy=np.r_[0.,np.cumsum(np.abs(coarse_x).astype(np.float64)**2)]
    a=energy[width:len(coarse_x)-shift+1]-energy[:len(coarse_x)-shift-width+1]
    b=energy[shift+width:]-energy[shift:len(coarse_x)-width+1]
    metric=np.abs(corr)/np.sqrt(np.maximum(a*b,1e-20))
    coarse,_=find_peaks(np.r_[0.,metric,0.],height=.75,distance=STRIDE//(2*step))
    scores=np.zeros(len(x)-STRIDE+1,np.float32)
    peaks=[]
    norm=float(np.vdot(TRAIN_TIME,TRAIN_TIME).real)
    for location in (coarse-1)*step:
        left=max(0,int(location)-2*CP_SIZE)
        right=min(len(scores),int(location)+CP_SIZE+1)
        if right<=left:
            continue
        window=x[left:right+STRIDE-1]
        matched=np.correlate(window,TRAIN_TIME,mode='valid')
        local_energy=np.r_[0.,np.cumsum(np.abs(window).astype(np.float64)**2)]
        power=local_energy[STRIDE:]-local_energy[:-STRIDE]
        local=np.abs(matched)/np.sqrt(np.maximum(power*norm,1e-20))
        start=left+int(np.argmax(local))
        if local[start-left]<.45 or start+2*STRIDE>len(x):
            continue
        second=start+STRIDE
        scores[start]=local[start-left]
        scores[second]=abs(np.vdot(TRAIN_TIME,x[second:second+STRIDE]))/np.sqrt(
            max(1e-20,float(np.vdot(x[second:second+STRIDE],x[second:second+STRIDE]).real)*norm))
        if scores[second]>=.45:
            peaks.append(start)
    return scores,sorted(set(peaks))


def modulate(packet, modulation="qpsk", sps=None, fft_size=256):
    layout=configuration(fft_size)
    FFT_SIZE,CP_SIZE=layout.fft_size,layout.cp_size
    ACTIVE,PILOTS,DATA,TRAIN_TIME=layout.active,layout.pilots,layout.data,layout.train_time
    symbols = (np.concatenate([packet_symbols(p, modulation) for p in packet])
               if isinstance(packet, list) else packet_symbols(packet, modulation))
    rows = math.ceil(len(symbols) / len(DATA))
    grid = np.zeros((rows, FFT_SIZE), np.complex64)
    grid[:, DATA] = np.pad(symbols, (0, rows * len(DATA) - len(symbols))).reshape(rows, -1)
    grid[:, PILOTS] = PILOT_VALUES
    # Two identical full training symbols estimate residual CFO and the channel.
    wave = np.r_[np.zeros(128), TRAIN_TIME, TRAIN_TIME,
                 _time(grid,FFT_SIZE,CP_SIZE,len(ACTIVE)).ravel(), np.zeros(128)]
    return (wave * (6500 / max(1., np.max(np.abs(wave))))).astype(np.complex64)


def demodulate_packets(samples, modulation="qpsk", sps=None, max_packets=32,
                       on_constellation=None, check_crc=True, on_channel=None,
                       on_diagnostics=None, burst_packets=1, training_only=False, fft_size=256,
                       diagnostic_only=False):
    layout=configuration(fft_size)
    FFT_SIZE,CP_SIZE=layout.fft_size,layout.cp_size
    STRIDE=FFT_SIZE+CP_SIZE
    ACTIVE,PILOTS,DATA,TRAINING,TRAIN_TIME=(layout.active,layout.pilots,layout.data,
                                          layout.training,layout.train_time)
    from .modem import packet_symbol_count
    x = np.asarray(samples, np.complex64).ravel()
    per_packet = packet_symbol_count(modulation)
    count = per_packet * burst_packets
    if diagnostic_only:
        count=min(count,2*len(DATA))
    rows = math.ceil(count / len(DATA))
    needed = (rows + 2) * STRIDE
    if len(x) < needed:
        return []
    # Normalized matched training; inspect chronological candidates and dedup.
    score,peaks=_burst_training_peaks(x,FFT_SIZE,CP_SIZE,TRAIN_TIME) if burst_packets>1 else (None,[])
    if not len(peaks):
        corr = fftconvolve(x, np.conj(TRAIN_TIME[::-1]), mode="valid")
        energy = np.r_[0., np.cumsum(np.abs(x).astype(np.float64) ** 2)]
        energy = energy[STRIDE:] - energy[:-STRIDE]
        score = np.abs(corr) / np.sqrt(np.maximum(energy, 1e-12) * np.vdot(TRAIN_TIME, TRAIN_TIME).real)
        peaks, _ = find_peaks(np.r_[0., score, 0.], height=0.45, distance=STRIDE // 2)
        peaks = peaks - 1
    found = {}
    plotted = False
    diagnosed = False
    for start in peaks:
        if start < 0 or start + needed > len(x):
            continue
        a = x[start + CP_SIZE:start + STRIDE]
        b = x[start + STRIDE + CP_SIZE:start + 2 * STRIDE]
        # Either repeated training symbol produces a matched-filter peak. A
        # capture beginning at the second one must not pair it with payload.
        pair_energy = float(np.vdot(a, a).real * np.vdot(b, b).real)
        pair_correlation = float(abs(np.vdot(a, b)) /
                                 np.sqrt(max(pair_energy, 1e-20)))
        if (score[start + STRIDE] < 0.45 or
                not np.isfinite(pair_correlation) or pair_correlation < 0.8):
            continue
        cfo = np.angle(np.vdot(a, b)) / STRIDE
        block = x[start:start + needed] * np.exp(np.complex64(-1j*cfo) * np.arange(needed,dtype=np.float32))
        grid = fft(block.reshape(-1, STRIDE)[:, CP_SIZE:], axis=-1)
        channel = (grid[0, ACTIVE] + grid[1, ACTIVE]) / (2 * TRAINING[ACTIVE])
        if np.min(np.abs(channel)) < 1e-8:
            continue
        equal = np.zeros_like(grid[2:])
        equal[:, ACTIVE] = grid[2:, ACTIVE] / channel
        pilot_gain = np.mean(equal[:, PILOTS] * PILOT_VALUES.conj(), axis=1)
        pilot_gain=np.where(np.abs(pilot_gain)>=1e-8,pilot_gain,1)
        # Track amplitude as well as common phase through long bursts. A
        # training-only gain estimate becomes stale while RF gain drifts.
        equal /= pilot_gain[:, None]
        symbols = equal[:, DATA].ravel()[:count]
        if on_constellation and not plotted:
            on_constellation(constellation_points(symbols))
            plotted = True
        valid = []
        for i in range(0 if diagnostic_only else burst_packets):
            decoded = np.frombuffer(_symbols_to_bytes(symbols[i*per_packet:(i+1)*per_packet], modulation), np.uint8)
            if len(decoded) == PACKET_SIZE:
                packet = parse_packet(np.bitwise_xor(decoded, SCRAMBLE_MASK).tobytes(), check_crc)
                if packet is not None:
                    valid.append(packet)
        packet = valid[0] if valid else None
        if packet is not None or ((training_only or diagnostic_only) and on_diagnostics is not None):
            if on_diagnostics is not None and not diagnosed:
                nfft = 1024
                # A bounded diagnostic spectrum must not FFT an entire
                # multi-megabyte video burst on the transfer critical path.
                segment = x[start:start + min(needed,16*nfft)]
                segment = segment[:len(segment) // nfft * nfft]
                if len(segment):
                    power = np.mean(np.abs(np.fft.fft(segment.reshape(-1, nfft) * np.hanning(nfft), axis=1)) ** 2, axis=0)
                    db = 10 * np.log10(np.maximum(np.fft.fftshift(power), 1e-20))
                    db -= np.max(db)
                else:
                    db = np.full(nfft, -100.)
                before = grid[2:, DATA].ravel()[:count]
                before = before / max(1e-12, np.sqrt(np.mean(np.abs(before) ** 2)))
                noise = float(np.mean(np.abs((grid[0, ACTIVE]-grid[1, ACTIVE]) / TRAINING[ACTIVE])**2)/4)
                signal = max(1e-20, float(np.mean(np.abs(channel)**2))-noise)
                from .modem import MODULATION_BITS
                scale = np.sqrt(2*(2**MODULATION_BITS[modulation]-1)/3)
                limit = 2**(MODULATION_BITS[modulation]//2)-1
                observed = constellation_points(symbols, 512)
                hard = (np.clip(2*np.rint((observed.real*scale-1)/2)+1, -limit, limit) +
                        1j*np.clip(2*np.rint((observed.imag*scale-1)/2)+1, -limit, limit))/scale
                evm = 100*np.sqrt(np.mean(np.abs(observed-hard)**2)/max(1e-20, np.mean(np.abs(hard)**2)))
                on_diagnostics({"kind": "ofdm", "spectrum_db": db.astype(np.float32),
                                "csi_raw": channel.astype(np.complex64),
                                "subcarriers": np.where(ACTIVE >= FFT_SIZE//2, ACTIVE-FFT_SIZE, ACTIVE),
                                "training_snr_db": float(10*np.log10(signal/max(1e-20,noise))),
                                "decision_evm_pct": float(evm),
                                "training_correlation": float(score[start]),
                                "training_pair_correlation": pair_correlation,
                                "before": constellation_points(before, 512),
                                "after": constellation_points(symbols, 512),
                                "cfo_cycles_per_sample": float(cfo / (2 * np.pi)),
                                "crc_valid": packet.crc_valid if packet is not None else None if diagnostic_only else False})
                diagnosed = True
            for packet in valid:
                found[(packet.frame, packet.chunk)] = packet
            if diagnostic_only:
                break
            if len(found) >= max_packets:
                break
    return sorted(found.values(), key=lambda p: (p.frame, p.chunk))


def payload_airtime_samples(modulation, fft_size=256):
    from .modem import packet_symbol_count
    layout=configuration(fft_size)
    return 256 + (2 + math.ceil(packet_symbol_count(modulation) / len(layout.data))) * (layout.fft_size+layout.cp_size)
