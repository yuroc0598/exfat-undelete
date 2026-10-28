#!/usr/bin/env python3
"""Build a tiny synthetic exFAT image (MBR + volume) with deleted videos for testing."""
import struct
import sys

SECT, SPC, CL = 512, 8, 4096
FAT_OFF, FAT_LEN, HEAP_OFF, NCL = 24, 8, 64, 1000
PART_LBA = 2048
BITMAP_C, ROOT_C, DCIM_C, OLD_C = 2, 4, 6, 7


def data(seed, size):
    return bytes((i * 7 + seed) % 251 for i in range(size))


def checksum(buf):
    cs = 0
    for i, b in enumerate(buf):
        if i in (2, 3):
            continue
        cs = ((((cs & 1) << 15) | (cs >> 1)) + b) & 0xFFFF
    return cs


def file_set(name, first, size, isdir=False, deleted=False, nofat=True, mtime=0x5A2B6000):
    u = name.encode("utf-16-le")
    names = [u[i:i + 30].ljust(30, b"\0") for i in range(0, len(u), 30)]
    prim = bytearray(32)
    prim[0], prim[1] = 0x85, 1 + len(names)
    struct.pack_into("<H", prim, 4, 0x10 if isdir else 0x20)
    struct.pack_into("<I", prim, 12, mtime)
    stream = bytearray(32)
    stream[0], stream[1], stream[3] = 0xC0, 0x01 | (0x02 if nofat else 0), len(name)
    struct.pack_into("<Q", stream, 8, size)
    struct.pack_into("<I", stream, 20, first)
    struct.pack_into("<Q", stream, 24, size)
    buf = bytearray(prim + stream)
    for n in names:
        buf += bytes([0xC1, 0]) + n
    struct.pack_into("<H", buf, 2, checksum(buf))  # checksum over the LIVE bytes
    if deleted:  # like a real delete: only clear the in-use bit, leave checksum alone
        for i in range(0, len(buf), 32):
            buf[i] &= 0x7F
    return bytes(buf)


def main(out):
    vol = bytearray((HEAP_OFF + NCL * SPC) * SECT)
    vbr = bytearray(512)
    vbr[3:11] = b"EXFAT   "
    struct.pack_into("<QQ", vbr, 0x40, PART_LBA, len(vol) // SECT)
    struct.pack_into("<5I", vbr, 0x50, FAT_OFF, FAT_LEN, HEAP_OFF, NCL, ROOT_C)
    vbr[0x6C], vbr[0x6D], vbr[0x6E] = 9, 3, 1
    vbr[510:512] = b"\x55\xaa"
    vol[:512] = vbr

    def put(cluster, blob):
        off = (HEAP_OFF * SECT) + (cluster - 2) * CL
        vol[off:off + len(blob)] = blob

    files = {  # name -> (first cluster, size, seed)
        "video1.mp4": (10, 10000, 1),
        "video2.MOV": (20, 9000, 2),
        "lost.mp4": (30, 5000, 3),
        "reused.mp4": (40, 8192, 4),
        "live.mp4": (50, 4096, 5),
    }
    for name, (first, size, seed) in files.items():
        put(first, data(seed, size))

    # FAT: bitmap, root, DCIM chains end-of-file; live.mp4 uses NoFatChain.
    def fat(c, v):
        struct.pack_into("<I", vol, FAT_OFF * SECT + 4 * c, v)
    for c in (BITMAP_C, ROOT_C, DCIM_C, OLD_C):
        fat(c, 0xFFFFFFFF)

    bm = bytearray((NCL + 7) // 8)
    for c in (BITMAP_C, ROOT_C, DCIM_C, 41, 50):  # in use now; 41 = overwritten by "someone"
        bm[(c - 2) >> 3] |= 1 << ((c - 2) & 7)
    put(BITMAP_C, bm)

    bitmap_entry = bytearray(32)
    bitmap_entry[0] = 0x81
    struct.pack_into("<I", bitmap_entry, 20, BITMAP_C)
    struct.pack_into("<Q", bitmap_entry, 24, len(bm))
    root = bitmap_entry
    root += file_set("DCIM", DCIM_C, CL, isdir=True)
    root += file_set("OLD", OLD_C, CL, isdir=True, deleted=True)
    root += file_set("video1.mp4", 10, 10000, deleted=True)
    root += file_set("live.mp4", 50, 4096)
    put(ROOT_C, root)
    put(DCIM_C, file_set("video2.MOV", 20, 9000, deleted=True)
        + file_set("reused.mp4", 40, 8192, deleted=True))
    put(OLD_C, file_set("lost.mp4", 30, 5000))  # lives in a deleted folder

    mbr = bytearray(512)
    mbr[446 + 4] = 0x07
    struct.pack_into("<II", mbr, 446 + 8, PART_LBA, len(vol) // SECT)
    mbr[510:512] = b"\x55\xaa"
    with open(out, "wb") as f:
        f.write(mbr + bytes(PART_LBA * SECT - 512) + vol)


if __name__ == "__main__":
    main(sys.argv[1])
