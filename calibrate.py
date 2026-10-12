"""
Auto-calibration with the phone's microphone at the listening seat.

The PC plays a short chirp from one speaker at a time (every speaker in turn,
ROUNDS times), generated on the same clock as the music so it goes through the
exact same path: each speaker's sync delay, device buffer, Bluetooth lag and the
distance through the air. The phone records it all; matched-filtering the
recording tells us, per speaker, how late its chirp arrived (relative to the
first speaker) and how loud it was.
"""
import numpy as np
from scipy.signal import fftconvolve, hilbert

RATE = 48000
SLOT = 1.0                  # seconds between chirps
ROUNDS = 3                  # every speaker chirps this many times (median of these)
LEAD = 0.6                  # quiet time before the first chirp
TAIL = 2.5                  # extra recording time after the last slot (delays, Bluetooth lag)
CHIRP_S = 0.06
F0, F1 = 1200.0, 7000.0     # sweep range: fine on phone mics and small Bluetooth speakers
WINDOW = (-0.45, 0.55)      # where to look for a speaker's chirp around its expected time


def chirp(rate):
    t = np.arange(int(rate * CHIRP_S)) / rate
    k = (F1 - F0) / CHIRP_S
    return (np.sin(2 * np.pi * (F0 * t + 0.5 * k * t * t)) * np.hanning(len(t))).astype(np.float32)


def record_seconds(n):
    return LEAD + SLOT * n * ROUNDS + TAIL


class Sequence:
    """The chirp schedule, generated block by block in the capture thread.

    block() returns (signal, owner): owner is the output index whose turn it is,
    -1 while calibration is on but no-one is chirping (music muted), -2 when off.
    """

    def __init__(self, n, start):
        self.n, self.start = n, start
        self.slot = int(SLOT * RATE)
        self.c = chirp(RATE)
        self.end = start + self.slot * n * ROUNDS + int(TAIL * RATE)

    def block(self, sample, length):
        rel = sample + np.arange(length) - self.start
        slot = np.floor_divide(rel, self.slot)
        within = rel - slot * self.slot
        sig = np.zeros(length, np.float32)
        owner = np.full(length, -1.0, np.float32)
        active = (rel >= 0) & (slot < self.n * ROUNDS)
        on = active & (within < len(self.c))
        sig[on] = self.c[within[on]]
        owner[active] = slot[active] % self.n
        return sig, owner, sample + length >= self.end


def analyze(rec, rate, n):
    """rec: mono samples from the phone. Returns (per-speaker list, quality text).

    Each item is None (not heard) or {"arrival_ms": relative to speaker 0, "level_db": ...}.
    """
    rec = np.asarray(rec, np.float64)
    rec = rec - rec.mean()
    c = chirp(rate).astype(np.float64)
    if len(rec) < len(c) * 4:
        return [None] * n, "The recording was too short."
    env = np.abs(hilbert(fftconvolve(rec, c[::-1], mode="valid")))
    noise = float(np.median(env)) + 1e-12
    thr = max(float(env.max()) * 0.25, noise * 8)
    above = np.nonzero(env > thr)[0]
    if not len(above):
        return [None] * n, "No chirps were heard. Turn the volume up and try again."
    first = int(above[0])
    first += int(np.argmax(env[first:first + int(0.03 * rate)]))      # sharpen onto the peak

    slot = SLOT * rate
    times, levels = [[] for _ in range(n)], [[] for _ in range(n)]
    for k in range(n * ROUNDS):
        expected = first + k * slot
        a = max(0, int(expected + WINDOW[0] * rate))
        b = min(len(env), int(expected + WINDOW[1] * rate))
        if b <= a:
            continue
        i = a + int(np.argmax(env[a:b]))
        if env[i] < noise * 6:                                        # nothing above the noise
            continue
        times[k % n].append((i - expected) / rate)
        levels[k % n].append(env[i])

    out = []
    for i in range(n):
        if len(times[i]) < 2:
            out.append(None)
            continue
        out.append({"arrival_ms": float(np.median(times[i]) * 1000),
                    "level_db": float(20 * np.log10(np.median(levels[i]) + 1e-12)),
                    "spread_ms": float((max(times[i]) - min(times[i])) * 1000)})
    snr = 20 * np.log10(float(env[first]) / noise)
    return out, f"signal-to-noise {snr:.0f} dB"
