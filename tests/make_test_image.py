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


ORPHAN_MDAT = 6000
ORPHAN_CREATED = 3818534400  # 2025-01-01 00:00:00 UTC in seconds since 1904
ORPHAN_SECONDS = 12


def orphan_video(mdat_size=ORPHAN_MDAT, seed=9):
    ftyp = struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 0, b"isom", b"mp41")
    mdat = struct.pack(">I4s", mdat_size, b"mdat") + data(seed, mdat_size - 8)
    mvhd = struct.pack(">I4sB3xIIII", 108, b"mvhd", 0, ORPHAN_CREATED, ORPHAN_CREATED,
                       1000, ORPHAN_SECONDS * 1000).ljust(108, b"\0")
    moov = struct.pack(">I4s", 116, b"moov") + mvhd
    return ftyp + mdat + moov


FRAG_FILE_SIZE = 9000


def ALLOC_ORPHAN():
    return orphan_video(7000, 13)


def LIVE_VIDEO():
    return orphan_video(6500, 14)


def FRAG_VIDEO():
    return orphan_video(9000, 11)


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

    put(60, orphan_video())  # no directory entry anywhere: only carving can find it

    def put_frag(clusters, blob):
        for i, c in enumerate(clusters):
            put(c, blob[i * CL:(i + 1) * CL])

    # Fragmented orphan video: clusters 70, 72, 73; live data occupies 71 in between.
    put_frag([70, 72, 73], FRAG_VIDEO())
    put(71, data(20, CL))
    # Allocated-but-unreferenced video (bitmap says in use, no directory entry points at it):
    put(90, ALLOC_ORPHAN())
    # A real, existing video that must be left alone.
    put(100, LIVE_VIDEO())
    # Deleted fragmented file with a wiped FAT chain: clusters 80, 82, 83; live data on 81.
    put_frag([80, 82, 83], data(7, FRAG_FILE_SIZE))
    put(81, data(21, CL))

    # FAT: bitmap, root, DCIM chains end-of-file; live.mp4 uses NoFatChain.
    def fat(c, v):
        struct.pack_into("<I", vol, FAT_OFF * SECT + 4 * c, v)
    for c in (BITMAP_C, ROOT_C, DCIM_C, OLD_C):
        fat(c, 0xFFFFFFFF)

    bm = bytearray((NCL + 7) // 8)
    for c in (BITMAP_C, ROOT_C, DCIM_C, 41, 50, 71, 81, 90, 91, 100, 101):  # in use now
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
    root += file_set("livevideo.mp4", 100, len(LIVE_VIDEO()))
    put(ROOT_C, root)
    put(DCIM_C, file_set("video2.MOV", 20, 9000, deleted=True)
        + file_set("reused.mp4", 40, 8192, deleted=True)
        + file_set("frag.mp4", 80, FRAG_FILE_SIZE, deleted=True, nofat=False))
    put(OLD_C, file_set("lost.mp4", 30, 5000))  # lives in a deleted folder

    mbr = bytearray(512)
    mbr[446 + 4] = 0x07
    struct.pack_into("<II", mbr, 446 + 8, PART_LBA, len(vol) // SECT)
    mbr[510:512] = b"\x55\xaa"
    with open(out, "wb") as f:
        f.write(mbr + bytes(PART_LBA * SECT - 512) + vol)


if __name__ == "__main__":
    main(sys.argv[1])
