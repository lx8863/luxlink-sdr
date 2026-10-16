"""Small fixed-size QPSK packets for a same-board AD9361 loopback link."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import math
import struct
import zlib
from functools import lru_cache

import numpy as np
from scipy.fft import fft, ifft, next_fast_len


SPS = 8
PAYLOAD_SIZE = 1024
PREAMBLE_SYMBOLS = 96
MAGIC = b"EV01"
HEADER = struct.Struct("<4sIHHH")
PACKET_SIZE = HEADER.size + PAYLOAD_SIZE + 4
PACKET_SYMBOLS = PACKET_SIZE * 4
MODULATION_BITS = {"qpsk": 2, "16qam": 4, "64qam": 6, "256qam": 8}
# Randomize the fixed-size packet before QPSK so padded zero bytes do not
# become a long DC-heavy run that the AD9361 analog DC tracker removes.
SCRAMBLE_MASK = np.random.default_rng(9362).integers(
    0, 256, PACKET_SIZE, dtype=np.uint8)


@dataclass(frozen=True)
class Packet:
    frame: int
    chunk: int
    total_chunks: int
    payload: bytes
    crc_valid: bool | None = True


def _qpsk(bits: np.ndarray) -> np.ndarray:
    pairs = np.asarray(bits, dtype=np.uint8).reshape(-1, 2)
    return ((1 - 2 * pairs[:, 0].astype(np.int8)) + 1j *
            (1 - 2 * pairs[:, 1].astype(np.int8))) / math.sqrt(2)


def _square_qam(bits: np.ndarray, bits_per_symbol: int) -> np.ndarray:
    """Gray-coded square QAM, with the sign bit first on each I/Q axis."""
    axis_bits = bits_per_symbol // 2
    groups = np.asarray(bits, dtype=np.uint8).reshape(-1, 2, axis_bits)

    def levels(axis: np.ndarray) -> np.ndarray:
        gray = np.zeros(len(axis), dtype=np.int16)
        for bit in range(1, axis_bits):
            gray = (gray << 1) | axis[:, bit]
        rank = gray.copy()
        for shift in range(1, axis_bits - 1):
            rank ^= gray >> shift
        magnitude = (2 ** axis_bits - 1) - 2 * rank
        return (1 - 2 * axis[:, 0].astype(np.int16)) * magnitude

    scale = math.sqrt(2 * (2 ** bits_per_symbol - 1) / 3)
    return (levels(groups[:, 0, :]) + 1j * levels(groups[:, 1, :])) / scale


def packet_symbol_count(modulation: str) -> int:
    try:
        bits_per_symbol = MODULATION_BITS[modulation]
    except KeyError as exc:
        raise ValueError(f"Unsupported modulation: {modulation}") from exc
    return math.ceil(PACKET_SIZE * 8 / bits_per_symbol)


def _upsample(symbols: np.ndarray, factor: int = SPS) -> np.ndarray:
    result = np.zeros((len(symbols) - 1) * factor + 1, dtype=np.complex64)
    result[::factor] = symbols
    return result


def _fftconvolve(a: np.ndarray, b: np.ndarray, mode: str = "full") -> np.ndarray:
    full_length = len(a) + len(b) - 1
    a = np.asarray(a, dtype=np.complex64)
    b = np.asarray(b, dtype=np.complex64)
    if len(b) <= 81:
        result = np.convolve(a, b)
    else:
        fft_length = next_fast_len(full_length)
        result = ifft(fft(a, fft_length) * fft(b, fft_length))[:full_length]
    if mode == "full":
        return result
    if mode == "valid":
        return result[len(b) - 1:len(a)]
    raise ValueError("Unsupported convolution mode")


def _strong_peaks(values: np.ndarray, count: int = 20, sps: int = SPS) -> np.ndarray:
    local = np.flatnonzero((values[1:-1] > values[:-2]) &
                           (values[1:-1] >= values[2:])) + 1
    if not len(local):
        return np.empty(0, dtype=int)
    selected: list[int] = []
    threshold = np.max(values) * 0.35
    for index in local[np.argsort(values[local])[::-1]]:
        if values[index] < threshold:
            break
        if all(abs(int(index) - old) >= PREAMBLE_SYMBOLS * sps // 2 for old in selected):
            selected.append(int(index))
            if len(selected) >= count:
                break
    return np.asarray(selected, dtype=int)


@lru_cache(maxsize=8)
def _rrc(sps: int = SPS, span: int = 10, alpha: float = 0.35) -> np.ndarray:
    t = np.arange(-span * sps // 2, span * sps // 2 + 1, dtype=float) / sps
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-12:
            h[i] = 1 + alpha * (4 / math.pi - 1)
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-12:
            h[i] = alpha / math.sqrt(2) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * alpha)) +
                (1 - 2 / math.pi) * math.cos(math.pi / (4 * alpha)))
        else:
            h[i] = (math.sin(math.pi * x * (1 - alpha)) +
                    4 * alpha * x * math.cos(math.pi * x * (1 + alpha))) / (
                        math.pi * x * (1 - (4 * alpha * x) ** 2))
    return h / np.sqrt(np.sum(h * h))


TAPS = _rrc()
_rng = np.random.default_rng(9361)
PREAMBLE = _qpsk(_rng.integers(0, 2, PREAMBLE_SYMBOLS * 2, dtype=np.uint8))
_preamble_up = _upsample(PREAMBLE)


def make_packet(frame: int, chunk: int, total_chunks: int, payload: bytes) -> bytes:
    if not (0 <= frame < 2**32 and 0 <= chunk < total_chunks < 2**16):
        raise ValueError("Invalid frame or chunk index")
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError(f"Payload exceeds {PAYLOAD_SIZE} bytes")
    raw = HEADER.pack(MAGIC, frame, chunk, total_chunks, len(payload))
    raw += payload.ljust(PAYLOAD_SIZE, b"\0")
    return raw + struct.pack("<I", zlib.crc32(raw))


def parse_packet(raw: bytes, check_crc: bool = True) -> Packet | None:
    if len(raw) != PACKET_SIZE:
        return None
    valid = (zlib.crc32(raw[:-4]) == struct.unpack("<I", raw[-4:])[0]) if check_crc else None
    if check_crc and not valid:
        return None
    magic, frame, chunk, total_chunks, payload_len = HEADER.unpack_from(raw)
    if magic != MAGIC or total_chunks == 0 or chunk >= total_chunks or payload_len > PAYLOAD_SIZE:
        return None
    start = HEADER.size
    return Packet(frame, chunk, total_chunks, raw[start:start + payload_len], valid)


def packet_symbols(packet: bytes, modulation: str = "qpsk") -> np.ndarray:
    """Data symbols placed on TX, including the scrambled padding bytes."""
    if len(packet) != PACKET_SIZE:
        raise ValueError("Expected a fixed-size packet")
    bits_per_symbol = MODULATION_BITS.get(modulation)
    if bits_per_symbol is None:
        raise ValueError(f"Unsupported modulation: {modulation}")
    scrambled = np.bitwise_xor(np.frombuffer(packet, dtype=np.uint8), SCRAMBLE_MASK)
    bits = np.unpackbits(scrambled)
    pad = (-len(bits)) % bits_per_symbol
    if pad:
        bits = np.pad(bits, (0, pad))
    return _square_qam(bits, bits_per_symbol)


def constellation_points(symbols: np.ndarray, count: int = 256) -> np.ndarray:
    """Evenly sample one packet for a lightweight live I/Q scatter plot."""
    indices = np.linspace(0, len(symbols) - 1, min(count, len(symbols)), dtype=int)
    return np.asarray(symbols[indices], dtype=np.complex64).copy()


def modulate(packet: bytes, modulation: str = "qpsk", sps: int = SPS) -> np.ndarray:
    if sps not in (2, 4, 8):
        raise ValueError("Samples per symbol must be 2, 4, or 8")
    symbols = np.concatenate((PREAMBLE, packet_symbols(packet, modulation)))
    shaped = _fftconvolve(_upsample(symbols, sps), _rrc(sps))
    iq = np.concatenate((np.zeros(64), shaped, np.zeros(128)))
    iq *= 10000 / max(1.0, np.max(np.abs(iq)))
    # AD9361 DMA transfers are happier with a sample count divisible by 8.
    pad = (-len(iq)) % 8
    if pad:
        iq = np.pad(iq, (0, pad))
    return iq.astype(np.complex64)


def _symbols_to_bytes(symbols: np.ndarray, modulation: str) -> bytes:
    bits_per_symbol = MODULATION_BITS.get(modulation)
    if bits_per_symbol is None:
        raise ValueError(f"Unsupported modulation: {modulation}")
    axis_bits = bits_per_symbol // 2
    scale = math.sqrt(2 * (2 ** bits_per_symbol - 1) / 3)
    bits = np.empty((len(symbols), 2, axis_bits), dtype=np.uint8)
    for axis, values in enumerate((np.real(symbols), np.imag(symbols))):
        magnitude = np.abs(values) * scale
        rank = np.clip(np.rint(((2 ** axis_bits - 1) - magnitude) / 2),
                       0, 2 ** (axis_bits - 1) - 1).astype(np.uint8)
        gray = rank ^ (rank >> 1)
        bits[:, axis, 0] = values < 0
        for bit in range(1, axis_bits):
            bits[:, axis, bit] = (gray >> (axis_bits - 1 - bit)) & 1
    return np.packbits(bits.ravel()).tobytes()[:PACKET_SIZE]


def demodulate(samples: np.ndarray, expected_frame: int | None = None,
               expected_chunk: int | None = None,
               on_constellation: Callable[[np.ndarray], None] | None = None,
               modulation: str = "qpsk", sps: int = SPS, check_crc: bool = True) -> Packet | None:
    """Find a packet anywhere in an IQ capture; CRC decides if it is valid."""
    for packet in demodulate_packets(samples, modulation, sps, on_constellation=on_constellation,
                                     check_crc=check_crc):
        if expected_frame is not None and packet.frame != expected_frame:
            continue
        if expected_chunk is not None and packet.chunk != expected_chunk:
            continue
        return packet
    return None


def demodulate_packets(samples: np.ndarray, modulation: str = "qpsk", sps: int = SPS,
                       max_packets: int = 32,
                       on_constellation: Callable[[np.ndarray], None] | None = None,
                       check_crc: bool = True, on_channel=None) -> list[Packet]:
    """Match-filter once per capture and return unique CRC-valid packets."""
    if sps not in (2, 4, 8) or max_packets < 1:
        raise ValueError("Invalid samples per symbol or packet limit")
    data_symbols = packet_symbol_count(modulation)
    x = np.asarray(samples, dtype=np.complex64).ravel()
    if x.size < (PREAMBLE_SYMBOLS + data_symbols) * sps:
        return []
    matched = _fftconvolve(x, _rrc(sps))
    corr = _fftconvolve(matched, np.conj(_upsample(PREAMBLE, sps)[::-1]), mode="valid")
    useful = len(matched) - (PREAMBLE_SYMBOLS + data_symbols - 1) * sps
    if useful <= 0:
        return []
    strength = np.abs(corr[:useful])
    peaks = _strong_peaks(strength, max_packets * 3, sps)
    if not len(peaks):
        return []
    packets: dict[tuple[int, int], Packet] = {}
    plotted = False
    channel_reported = False
    for start in peaks:
        pre = matched[start + np.arange(PREAMBLE_SYMBOLS) * sps]
        gain = np.vdot(PREAMBLE, pre) / PREAMBLE_SYMBOLS
        if abs(gain) < 1e-6:
            continue
        data_start = start + PREAMBLE_SYMBOLS * sps
        syms = matched[data_start + np.arange(data_symbols) * sps] / gain
        if on_constellation is not None and not plotted:
            on_constellation(constellation_points(syms))
            plotted = True
        received = np.frombuffer(_symbols_to_bytes(syms, modulation), dtype=np.uint8)
        packet = parse_packet(np.bitwise_xor(received, SCRAMBLE_MASK).tobytes(), check_crc=check_crc)
        if packet is None:
            continue
        key = (packet.frame, packet.chunk)
        if key in packets:
            continue
        if on_channel is not None and not channel_reported:
            on_channel(pre, packet, int(start))
            channel_reported = True
        packets[key] = packet
        if len(packets) >= max_packets:
            break
    return sorted(packets.values(), key=lambda packet: (packet.frame, packet.chunk))


def simulate_capture(waveform: np.ndarray, snr_db: float = 24,
                     phase: float = 0.7, offset: int = 173,
                     capture_samples: int | None = None) -> np.ndarray:
    """Repeat the waveform with delay, phase and noise for offline validation."""
    x = np.concatenate((waveform, waveform, waveform)).astype(np.complex64)
    x = np.roll(x, offset)
    if capture_samples is not None:
        x = x[:capture_samples]
    x *= np.complex64(np.exp(1j * phase))
    rng = np.random.default_rng(2026)
    power = np.mean(np.abs(x) ** 2)
    sigma = math.sqrt(power / 10 ** (snr_db / 10) / 2)
    noise = sigma * (rng.standard_normal(x.size, dtype=np.float32) +
                     1j * rng.standard_normal(x.size, dtype=np.float32))
    return (x + noise).astype(np.complex64)
