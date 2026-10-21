"""
Auto-calibration with the phone's microphone at the listening seat.

The PC plays a short chirp from one speaker at a time (every speaker in turn,
several rounds), generated on the same clock as the music so it goes through the
exact same path: each speaker's sync delay, device buffer, Bluetooth lag and the
distance through the air. The phone records it all; matched-filtering the
recording tells us, per speaker, how late its chirp arrived (relative to the
others) and how loud it was.

It works like tuning by ear, in passes: the first, long pass measures and sets
every speaker; each following pass plays the chirps again with the new settings,
measures what's left over and nudges the delays a little closer, until every
speaker lands within TOLERANCE_MS of the others (one click) or MAX_PASSES is hit.

Every pass opens with one chirp from all speakers at once: the first speaker
heard there anchors the schedule, so a speaker that can't be heard never shifts
the others into the wrong slots.
"""
import numpy as np
from scipy.signal import fftconvolve, hilbert

RATE = 48000
SLOT = 1.0                  # seconds between chirps
ROUNDS_FIRST = 6            # first pass: every speaker chirps this many times
ROUNDS_FINE = 4             # each fine-tuning pass after that
MAX_PASSES = 5
TOLERANCE_MS = 2.0          # all speakers within this of each other = one click
FINE_GAIN = 0.8             # fine passes correct 80% of what's left: settles without overshooting
LEAD = 0.6                  # quiet time before the first chirp
TAIL = 2.5                  # extra recording time after the last slot (delays, Bluetooth lag)
CHIRP_S = 0.06
F0, F1 = 1200.0, 7000.0     # sweep range: fine on phone mics and small Bluetooth speakers
WINDOW = (-0.45, 0.55)      # where to look for a speaker's chirp around its expected time
ALL = 1000.0                # "owner" value meaning every speaker plays this chirp


def chirp(rate):
    t = np.arange(int(rate * CHIRP_S)) / rate
    k = (F1 - F0) / CHIRP_S
    return (np.sin(2 * np.pi * (F0 * t + 0.5 * k * t * t)) * np.hanning(len(t))).astype(np.float32)


def rounds_for(pass_no):
    return ROUNDS_FIRST if pass_no == 0 else ROUNDS_FINE


def record_seconds(n, rounds):
    return LEAD + SLOT * (1 + n * rounds) + TAIL


class Sequence:
    """The chirp schedule, generated block by block in the capture thread.

    Slot 0 is the all-speakers chirp, then rounds x (speaker 0, 1, ... n-1).
    block() returns (signal, owner, done): owner is the output index whose turn it is,
    ALL for everyone, -1 while calibration is on but no-one is chirping (music muted).
    """

    def __init__(self, n, start, rounds):
        self.n, self.start = n, start
        self.slot = int(SLOT * RATE)
        self.slots = 1 + n * rounds
        self.c = chirp(RATE)
        self.end = start + self.slot * self.slots + int(TAIL * RATE)

    def block(self, sample, length):
        rel = sample + np.arange(length) - self.start
        slot = np.floor_divide(rel, self.slot)
        within = rel - slot * self.slot
        sig = np.zeros(length, np.float32)
        owner = np.full(length, -1.0, np.float32)
        active = (rel >= 0) & (slot < self.slots)
        on = active & (within < len(self.c))
        sig[on] = self.c[within[on]]
        s = slot[active]
        owner[active] = np.where(s == 0, ALL, (s - 1) % self.n)
        return sig, owner, sample + length >= self.end


def _arrival(env, a, b, rate):
    """Where the direct sound starts in env[a:b]: (fractional index, peak).

    The strongest peak can be a wall reflection, so take the earliest peak that's
    within 6 dB of it, then interpolate between samples.
    """
    seg = env[a:b]
    k = int(np.argmax(seg))
    peak = float(seg[k])
    lo = max(0, k - int(0.012 * rate))
    early = np.nonzero(seg[lo:k + 1] >= 0.5 * peak)[0]
    if len(early):
        j = lo + int(early[0])
        while j < k and seg[j + 1] >= seg[j]:                # climb onto that peak
            j += 1
        k = j
    d = 0.0
    if 0 < k < len(seg) - 1:
        y0, y1, y2 = seg[k - 1], seg[k], seg[k + 1]
        den = y0 - 2 * y1 + y2
        if den < 0:
            d = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    return a + k + d, peak


def _drift(points):
    """Clock drift between phone and PC, in seconds per slot (shared by all speakers)."""
    num = den = 0.0
    for pts in points:
        if len(pts) >= 2:
            m = np.array([p[0] for p in pts], float)
            t = np.array([p[1] for p in pts], float)
            mc = m - m.mean()
            num += float((mc * (t - t.mean())).sum())
            den += float((mc * mc).sum())
    d = num / den if den > 0 else 0.0
    return d if abs(d) < 3e-4 * SLOT else 0.0                # more than 300 ppm isn't drift


def _keep(pts, tol):
    """Drop chirps that disagree with the rest (a cough, a door) by more than tol seconds."""
    if len(pts) < 3:
        return pts
    med = float(np.median([p[1] for p in pts]))
    kept = [p for p in pts if abs(p[1] - med) <= tol]
    return kept if len(kept) >= 2 else pts


def analyze(rec, rate, n, rounds):
    """rec: mono samples from the phone. Returns (per-speaker list, quality text).

    Each item is None (not heard) or {"arrival_ms": comparable between speakers,
    "level_db", "spread_ms": how much its chirps disagreed, "used": chirps counted}.
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
    anchor = int(above[0])                                    # the all-speakers chirp (earliest speaker)
    anchor += int(np.argmax(env[anchor:anchor + int(0.03 * rate)]))

    slot = SLOT * rate
    points, levels = [[] for _ in range(n)], [[] for _ in range(n)]
    for r in range(rounds):
        for i in range(n):
            m = 1 + r * n + i
            expected = anchor + m * slot
            a = max(0, int(expected + WINDOW[0] * rate))
            b = min(len(env), int(expected + WINDOW[1] * rate))
            if b - a < 3:
                continue
            j, peak = _arrival(env, a, b, rate)
            if peak < noise * 6:                              # nothing above the noise
                continue
            points[i].append((m, (j - expected) / rate))
            e0, e1 = int(j - 0.001 * rate), int(j + 0.015 * rate)   # direct sound + early reflections
            levels[i].append(10 * np.log10(float(np.sum(env[max(0, e0):e1] ** 2)) + 1e-12))

    points = [_keep(p, 0.005) for p in points]
    drift = _drift(points)
    need = max(2, rounds // 2)
    out = []
    for i in range(n):
        pts = _keep([(m, t - drift * m) for m, t in points[i]], 0.002)
        if len(pts) < need:
            out.append(None)
            continue
        t = np.array([p[1] for p in pts])
        out.append({"arrival_ms": float(t.mean() * 1000),
                    "level_db": float(np.median(levels[i])),
                    "spread_ms": float(t.std() * 1000),
                    "used": len(pts)})
    snr = 20 * np.log10(float(env[anchor]) / noise)
    return out, f"signal-to-noise {snr:.0f} dB, clock drift {drift / SLOT * 1e6:+.0f} ppm"
