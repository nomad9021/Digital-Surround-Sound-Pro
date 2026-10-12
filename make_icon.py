"""Draws the app icon (speaker + sound waves on a blue tile) and writes surround.ico."""
import struct
import sys
import zlib

import numpy as np

N = 1024  # draw big, then shrink for smooth edges
y, x = np.mgrid[0:N, 0:N] / N

# rounded-square tile with a blue -> purple gradient
r = 0.2
dx = np.maximum(np.abs(x - .5) - (.5 - r), 0)
dy = np.maximum(np.abs(y - .5) - (.5 - r), 0)
tile = (dx ** 2 + dy ** 2) <= r ** 2
t = (x + y) / 2
rgb = np.stack([0x5b + (0x9b - 0x5b) * t, 0x8c + (0x5d - 0x8c) * t, 0xff + (0xf0 - 0xff) * t], -1)

# speaker: box + cone
box = (x > .2) & (x < .34) & (y > .4) & (y < .6)
cone = (x >= .34) & (x < .52) & (np.abs(y - .5) < .1 + (x - .34) * 1.1)
# three sound waves
d = np.hypot(x - .5, y - .5)
ang = np.abs(np.arctan2(y - .5, x - .5)) < .75
waves = np.zeros_like(tile)
for rad in (.14, .24, .34):
    waves |= ang & (np.abs(d - rad) < .028) & (x > .55)
white = tile & (box | cone | waves)

img = np.zeros((N, N, 4))
img[..., :3] = np.where(white[..., None], 255, rgb)
img[..., 3] = tile * 255


def shrink(a, size):
    f = N // size
    return a.reshape(size, f, size, f, 4).mean(axis=(1, 3)).astype(np.uint8)


def png(a):
    h, w = a.shape[:2]
    raw = b"".join(b"\x00" + a[i].tobytes() for i in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


sizes = [256, 64, 32, 16]
pngs = [png(shrink(img, s)) for s in sizes]
out = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
for s, p in zip(sizes, pngs):
    out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(p), offset)
    offset += len(p)
with open(sys.argv[1], "wb") as f:
    f.write(out + b"".join(pngs))

if len(sys.argv) > 2:   # square PNG for the phone home screen (iOS rounds the corners itself)
    full = img.copy()
    full[..., :3] = np.where((box | cone | waves)[..., None], 255, rgb)
    full[..., 3] = 255
    with open(sys.argv[2], "wb") as f:
        f.write(png(shrink(full, 256)))
