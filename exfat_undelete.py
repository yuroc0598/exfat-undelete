#!/usr/bin/env python3
"""Undelete video files from an exFAT volume (SD card or disk image).

Read-only on the source. Instead of carving raw data, it reads the exFAT
directory entries that survive deletion (start cluster + exact size + name)
and cross-checks the allocation bitmap to tell whether the clusters have
been reused since.

    sudo python3 exfat_undelete.py /dev/disk4                  # list
    sudo python3 exfat_undelete.py /dev/disk4 --out ~/recovered
    sudo python3 exfat_undelete.py /dev/disk4 --deep --out ~/recovered
    sudo python3 exfat_undelete.py /dev/disk4 --deep --carve   # also find old videos
"""
import argparse
import datetime
import os
import platform
import re
import struct
import sys

VIDEO_EXTS = {
    "mp4", "mov", "m4v", "avi", "mts", "m2ts", "mkv", "mpg", "mpeg", "3gp",
    "mxf", "lrv", "insv", "wmv", "flv", "webm", "mod", "vob", "braw", "r3d",
}
ALIGN = 4096  # raw devices on macOS need sector-aligned reads


class Dev:
    def __init__(self, path):
        if platform.system() == "Darwin":
            m = re.fullmatch(r"/dev/disk(\d+(?:s\d+)?)", path)
            if m:
                path = "/dev/rdisk" + m.group(1)  # raw node is much faster
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)

    def read(self, off, n):
        start = off - off % ALIGN
        end = -(-(off + n) // ALIGN) * ALIGN
        buf = bytearray()
        pos = start
        while pos < end:
            chunk = os.pread(self.fd, end - pos, pos)
            if not chunk:
                break
            buf += chunk
            pos += len(chunk)
        return bytes(buf[off - start:off - start + n])


def find_volume(dev, offset):
    """Byte offset of the exFAT boot sector (whole device, MBR, or GPT)."""
    if offset is not None:
        return offset

    def is_exfat(off):
        return dev.read(off, 512)[3:11] == b"EXFAT   "

    if is_exfat(0):
        return 0
    s0 = dev.read(0, 512)
    if s0[510:512] == b"\x55\xaa":
        for i in range(4):
            e = s0[446 + 16 * i:462 + 16 * i]
            lba = struct.unpack_from("<I", e, 8)[0]
            if e[4] in (0x07, 0xEE) and lba and is_exfat(lba * 512):
                return lba * 512
    hdr = dev.read(512, 512)
    if hdr[:8] == b"EFI PART":
        lba = struct.unpack_from("<Q", hdr, 72)[0]
        num, size = struct.unpack_from("<II", hdr, 80)
        table = dev.read(lba * 512, num * size)
        for i in range(num):
            start = struct.unpack_from("<Q", table, i * size + 32)[0]
            if start and is_exfat(start * 512):
                return start * 512
    sys.exit("No exFAT volume found. Check the device, or pass --offset BYTES.")


class Vol:
    def __init__(self, dev, base):
        self.dev, self.base = dev, base
        b = dev.read(base, 512)
        (self.fat_off, _, self.heap_off,
         self.ccount, self.root) = struct.unpack_from("<5I", b, 0x50)
        self.bps = 1 << b[0x6C]
        self.csize = self.bps << b[0x6D]
        self.bitmap = None
        self._fat_cache = {}

    def coff(self, c):
        return self.base + self.heap_off * self.bps + (c - 2) * self.csize

    def valid(self, c):
        return 2 <= c < self.ccount + 2

    def fat(self, c):
        pos = self.base + self.fat_off * self.bps + 4 * c
        blk = pos - pos % ALIGN
        if blk not in self._fat_cache:
            if len(self._fat_cache) > 256:
                self._fat_cache.clear()
            self._fat_cache[blk] = self.dev.read(blk, ALIGN)
        return struct.unpack_from("<I", self._fat_cache[blk], pos - blk)[0]

    def chain(self, c, limit):
        out = []
        while self.valid(c) and len(out) < limit:
            out.append(c)
            c = self.fat(c)
        return out

    def clusters_of(self, first, size, nofat):
        """Cluster list for an object; size None means follow the FAT to the end."""
        if not self.valid(first):
            return []
        if size is None:
            return self.chain(first, 1 << 22)
        need = -(-size // self.csize)
        if need == 0:
            return []
        if not nofat:
            ch = self.chain(first, need)
            if len(ch) == need:
                return ch
        # NoFatChain flag, or FAT entries were wiped on delete: assume contiguous.
        if first + need - 1 >= self.ccount + 2:
            return []
        return list(range(first, first + need))

    def read_clusters(self, clusters):
        return b"".join(self.dev.read(self.coff(c), self.csize) for c in clusters)

    def load_bitmap(self):
        raw = self.read_clusters(self.clusters_of(self.root, None, False))
        for i in range(len(raw) // 32):
            e = raw[i * 32:i * 32 + 32]
            if e[0] == 0:
                break
            if e[0] == 0x81:
                first = struct.unpack_from("<I", e, 20)[0]
                length = struct.unpack_from("<Q", e, 24)[0]
                cl = self.clusters_of(first, length, False)
                self.bitmap = self.read_clusters(cl)[:length]
                return

    def in_use(self, c):
        i = c - 2
        return bool(self.bitmap[i >> 3] >> (i & 7) & 1)


def dos_time(v):
    try:
        return datetime.datetime(1980 + (v >> 25), (v >> 21) & 15, (v >> 16) & 31,
                                 (v >> 11) & 31, (v >> 5) & 63, (v & 31) * 2)
    except ValueError:
        return None


def set_checksum(buf, normalize):
    cs = 0
    for i, b in enumerate(buf):
        if i in (2, 3):
            continue
        if normalize and i % 32 == 0:
            b |= 0x80
        cs = ((((cs & 1) << 15) | (cs >> 1)) + b) & 0xFFFF
    return cs


def parse_set(buf, deleted):
    """Parse one file directory entry set (primary + stream + name entries)."""
    s = buf[32:64]
    if (s[0] & 0x7F) != 0x40:
        return None
    attrs = struct.unpack_from("<H", buf, 4)[0]
    name = b"".join(buf[i:i + 32][2:] for i in range(64, len(buf), 32)
                    if (buf[i] & 0x7F) == 0x41)
    name = name.decode("utf-16-le", "replace")[:s[3]]
    return {
        "name": name,
        "isdir": bool(attrs & 0x10),
        "deleted": deleted,
        "first": struct.unpack_from("<I", s, 20)[0],
        "size": struct.unpack_from("<Q", s, 24)[0],
        "nofat": bool(s[1] & 2),
        "mtime": dos_time(struct.unpack_from("<I", buf, 12)[0]),
        "path": "",
    }


def iter_entries(raw):
    i, n = 0, len(raw) // 32
    while i < n:
        t = raw[i * 32]
        if t == 0:
            break
        if t in (0x85, 0x05):
            cnt = raw[i * 32 + 1]
            if cnt >= 2 and i + 1 + cnt <= n:
                yield parse_set(raw[i * 32:(i + 1 + cnt) * 32], t == 0x05)
            i += 1 + cnt
        else:
            i += 1


def walk(vol, first, size, nofat, path, seen, deleted_dir, out):
    if first in seen:
        return
    seen.add(first)
    raw = vol.read_clusters(vol.clusters_of(first, size, nofat))
    for ent in iter_entries(raw):
        if ent is None:
            continue
        ent["path"] = path
        ent["deleted"] = ent["deleted"] or deleted_dir
        if ent["isdir"]:
            walk(vol, ent["first"], ent["size"], ent["nofat"],
                 path + ent["name"] + "/", seen, ent["deleted"], out)
        elif ent["deleted"]:
            out.append(ent)


def deep_scan(vol, out):
    """Scan the whole cluster heap for deleted entry sets (finds deleted folders too)."""
    pat = re.compile(rb"\x05[\x02-\x13].{30}\x40.{31}\x41", re.S)
    total = vol.ccount * vol.csize
    step, overlap = 8 << 20, 1024
    start = vol.coff(2)
    for pos in range(0, total, step):
        data = vol.dev.read(start + pos, step + overlap)
        for m in pat.finditer(data):
            if m.start() % 32 or m.start() >= step:
                continue
            cnt = data[m.start() + 1]
            buf = data[m.start():m.start() + (1 + cnt) * 32]
            if len(buf) < (1 + cnt) * 32:
                continue
            stored = struct.unpack_from("<H", buf, 2)[0]
            if stored not in (set_checksum(buf, False), set_checksum(buf, True)):
                continue
            ent = parse_set(buf, True)
            if ent and not ent["isdir"]:
                ent["path"] = "(deep scan)/"
                out.append(ent)
        sys.stderr.write("\rdeep scan %3d%%" % min(100, (pos + step) * 100 // total))
    sys.stderr.write("\n")


CARVE_ATOMS = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"junk", b"uuid",
               b"pnot", b"meta", b"udta", b"moof", b"mfra", b"sidx", b"styp", b"pdin"}


def read_atom(vol, off):
    h = vol.dev.read(off, 16)
    size, typ = struct.unpack(">I4s", h[:8])
    if size == 1:
        size = struct.unpack(">Q", h[8:16])[0]
    return size, typ


def parse_mvhd(vol, moov_off):
    """(creation datetime, duration seconds) from the first child of moov, if it is mvhd."""
    d = vol.dev.read(moov_off + 8, 40)
    if d[4:8] != b"mvhd":
        return None, None
    if d[8] == 1:
        ct, ts, dur = struct.unpack_from(">QxxxxxxxxIQ", d, 12)
    else:
        ct, ts, dur = struct.unpack_from(">IxxxxII", d, 12)
    try:
        when = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=ct)
    except OverflowError:
        when = None
    if when and not 1990 <= when.year <= 2100:
        when = None
    return when, (dur / ts if ts else None)


def carve_at(vol, c, heap_end):
    """Walk MP4/MOV top-level atoms from cluster c; return the file's extent if it is complete."""
    start = pos = vol.coff(c)
    seen, moov_at = [], None
    while pos + 8 <= heap_end:
        size, typ = read_atom(vol, pos)
        if typ not in CARVE_ATOMS or size < 8 or pos + size > heap_end:
            break
        if typ == b"ftyp" and b"ftyp" in seen:
            break  # next file starts here
        seen.append(typ)
        if typ == b"moov":
            moov_at = pos
        pos += size
        if moov_at is not None and b"mdat" in seen and b"moof" not in seen:
            break
    if not seen or seen[0] != b"ftyp" or moov_at is None or b"mdat" not in seen:
        return None
    return pos - start, moov_at


def carve(vol, skip_firsts, out):
    """Find MP4/MOV files in free space by signature, with no directory entry needed."""
    cs, last = vol.csize, vol.ccount + 2
    heap_end = vol.coff(last)
    step = max(1, (8 << 20) // cs)
    next_ok, c, partial = 2, 2, 0
    while c < last:
        n = min(step, last - c)
        data = vol.dev.read(vol.coff(c), n * cs)
        for i in range(n):
            if data[i * cs + 4:i * cs + 8] != b"ftyp" or c + i < next_ok or c + i in skip_firsts:
                continue
            cc = c + i
            if vol.bitmap is not None and vol.in_use(cc):
                continue  # belongs to a live file
            res = carve_at(vol, cc, heap_end)
            if not res:
                partial += 1
                continue
            size, moov_at = res
            next_ok = cc + -(-size // cs)
            when, dur = parse_mvhd(vol, moov_at)
            ext = "mov" if data[i * cs + 8:i * cs + 12] == b"qt  " else "mp4"
            stamp = when.strftime("%Y%m%d_%H%M%S_") if when else ""
            out.append({
                "name": "%sc%d.%s" % (stamp, cc, ext), "isdir": False, "deleted": True,
                "first": cc, "size": size, "nofat": True, "mtime": when,
                "path": "(carved)/", "info": "%.0fs" % dur if dur else "",
            })
        c += n
        sys.stderr.write("\rcarving %3d%%" % min(100, (c - 2) * 100 // vol.ccount))
    sys.stderr.write("\n")
    if partial:
        print("(%d video starts had no readable moov section and were skipped: "
              "overwritten or fragmented)" % partial, file=sys.stderr)


def assess(vol, ent):
    """Fill ent['clusters'] and ent['status'] (how many clusters were reused)."""
    cl = vol.clusters_of(ent["first"], ent["size"], ent["nofat"])
    ent["clusters"] = cl
    if not cl:
        ent["status"] = "bad pointer"
    elif vol.bitmap is None:
        ent["status"] = "unknown"
    else:
        used = sum(vol.in_use(c) for c in cl)
        ent["status"] = "intact" if used == 0 else "%d/%d clusters reused" % (used, len(cl))


def runs(clusters):
    r = []
    for c in clusters:
        if r and r[-1][0] + r[-1][1] == c:
            r[-1][1] += 1
        else:
            r.append([c, 1])
    return r


def restore(vol, ent, outdir):
    name = re.sub(r"[\x00-\x1f/\\:]", "_", ent["name"]) or "unnamed_%d" % ent["first"]
    dest, n = os.path.join(outdir, name), 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(name)
        dest, n = os.path.join(outdir, "%s_%d%s" % (stem, n, ext)), n + 1
    left, done = ent["size"], 0
    with open(dest, "wb") as f:
        for first, count in runs(ent["clusters"]):
            off, run_bytes = vol.coff(first), count * vol.csize
            for p in range(0, run_bytes, 4 << 20):
                chunk = vol.dev.read(off + p, min(4 << 20, run_bytes - p, left))
                f.write(chunk)
                left -= len(chunk)
                done += len(chunk)
                sys.stderr.write("\r  %s %3d%%" % (name, done * 100 // max(ent["size"], 1)))
                if left <= 0:
                    break
            if left <= 0:
                break
    sys.stderr.write("\n")
    if ent["mtime"]:
        t = ent["mtime"].timestamp()
        os.utime(dest, (t, t))
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", help="/dev/diskN, /dev/diskNs1, or a raw image file")
    ap.add_argument("--out", help="recover into this folder (omit to only list)")
    ap.add_argument("--deep", action="store_true",
                    help="also scan the whole card (slow; finds files from deleted folders)")
    ap.add_argument("--carve", action="store_true",
                    help="also find MP4/MOV videos in free space by signature "
                         "(works after a reformat or when directory entries are gone)")
    ap.add_argument("--any", action="store_true", help="all file types, not just video")
    ap.add_argument("--ext", help="comma-separated extensions to match (overrides default)")
    ap.add_argument("--match", help="only names containing this text")
    ap.add_argument("--min-size", type=float, default=0, help="minimum size in MB")
    ap.add_argument("--offset", type=lambda x: int(x, 0), help="byte offset of the exFAT partition")
    a = ap.parse_args()

    dev = Dev(a.device)
    vol = Vol(dev, find_volume(dev, a.offset))
    print("exFAT: cluster %d KB, %d clusters" % (vol.csize // 1024, vol.ccount), file=sys.stderr)
    vol.load_bitmap()

    found = []
    walk(vol, vol.root, None, False, "/", set(), False, found)
    if a.deep:
        deep_scan(vol, found)

    if a.carve:
        carve(vol, {e["first"] for e in found}, found)

    exts = {e.strip().lower().lstrip(".") for e in a.ext.split(",")} if a.ext else VIDEO_EXTS
    uniq = {}
    for e in found:
        ext = e["name"].rsplit(".", 1)[-1].lower() if "." in e["name"] else ""
        if not a.any and ext not in exts:
            continue
        if a.match and a.match.lower() not in e["name"].lower():
            continue
        if e["size"] < a.min_size * 1e6:
            continue
        key = (e["first"], e["size"], e["name"])
        if key not in uniq or uniq[key]["path"].startswith("(deep"):
            uniq[key] = e
    files = sorted(uniq.values(), key=lambda e: (e["mtime"] or datetime.datetime.min))
    for e in files:
        assess(vol, e)

    if not files:
        print("No deleted matching files found. Try --deep, --any, or check the device.")
        return
    for i, e in enumerate(files, 1):
        when = e["mtime"].strftime("%Y-%m-%d %H:%M") if e["mtime"] else "?"
        print("%3d  %8.1f MB  %s  %-22s  %s%s  %s" %
              (i, e["size"] / 1e6, when, e["status"], e["path"], e["name"], e.get("info", "")))

    if a.out:
        os.makedirs(a.out, exist_ok=True)
        for e in files:
            if e["clusters"]:
                restore(vol, e, a.out)
        print("Done. Files written to", a.out)
    else:
        print("\nListing only. Add --out FOLDER (on your Mac, not the card) to recover.")


if __name__ == "__main__":
    main()
