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
from array import array
from bisect import bisect_left
import platform
import pwd
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

    def size(self):
        try:
            n = os.lseek(self.fd, 0, os.SEEK_END)
            if n:
                return n
        except OSError:
            pass
        import fcntl  # raw macOS devices report no size via lseek
        bs, cnt = bytearray(4), bytearray(8)
        fcntl.ioctl(self.fd, 0x40046418, bs)   # DKIOCGETBLOCKSIZE
        fcntl.ioctl(self.fd, 0x40086419, cnt)  # DKIOCGETBLOCKCOUNT
        return struct.unpack("<I", bs)[0] * struct.unpack("<Q", cnt)[0]

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
    return None


class Vol:
    def __init__(self, dev, base):
        self.dev, self.base = dev, base
        b = dev.read(base, 512)
        (self.fat_off, _, self.heap_off,
         self.ccount, self.root) = struct.unpack_from("<5I", b, 0x50)
        self.bps = 1 << b[0x6C]
        self.csize = self.bps << b[0x6D]
        self.bitmap = None
        self._free = None
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

    def free_list(self):
        """Sorted numbers of all clusters the allocation bitmap currently marks free."""
        if self._free is None:
            free = array("I")
            for i, byte in enumerate(self.bitmap):
                if byte == 0xFF:
                    continue
                for b in range(8):
                    c = i * 8 + b
                    if not byte >> b & 1 and c < self.ccount:
                        free.append(c + 2)
            self._free = free
        return self._free

    def free_run(self, start, count):
        """The next `count` free clusters from `start`, skipping allocated ones."""
        free = self.free_list()
        i = bisect_left(free, start)
        if i >= len(free) or free[i] != start or i + count > len(free):
            return []
        return list(free[i:i + count])

    def locate(self, first, size, nofat, deleted=False):
        """(cluster list, how it was found). size None means follow the FAT to the end."""
        if not self.valid(first):
            return [], "bad"
        if size is None:
            return self.chain(first, 1 << 22), "fat"
        need = -(-size // self.csize)
        if need == 0:
            return [], "bad"
        if not nofat:
            ch = self.chain(first, need)
            if len(ch) == need:
                return ch, "fat"
        if first + need - 1 >= self.ccount + 2:
            return [], "bad"
        run = list(range(first, first + need))
        # NoFatChain means contiguous by definition. Otherwise the file may have been
        # fragmented and its FAT chain wiped on delete: if live data now sits inside
        # the contiguous run, assume the file was written around it (free clusters only).
        if deleted and not nofat and self.bitmap is not None \
                and any(self.in_use(c) for c in run):
            gap = self.free_run(first, need)
            if gap:
                return gap, "gap-skip"
        return run, "contiguous"

    def clusters_of(self, first, size, nofat):
        return self.locate(first, size, nofat)[0]

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


class Stream:
    """A file laid out over the free clusters from `start` on, skipping allocated ones.

    This is how a fragmented file looks when it was written around files that still exist.
    """

    def __init__(self, vol, start):
        self.vol, self.free = vol, vol.free_list()
        self.idx = bisect_left(self.free, start)
        self.length = (len(self.free) - self.idx) * vol.csize

    def read(self, off, n):
        cs, out = self.vol.csize, []
        while n > 0 and off < self.length:
            i, r = divmod(off, cs)
            take = min(n, cs - r)
            out.append(self.vol.dev.read(self.vol.coff(self.free[self.idx + i]) + r, take))
            off, n = off + take, n - take
        return b"".join(out)

    def clusters(self, size):
        return list(self.free[self.idx:self.idx + -(-size // self.vol.csize)])


def read_atom(st, off):
    h = st.read(off, 16)
    if len(h) < 16:
        return 0, b""
    size, typ = struct.unpack(">I4s", h[:8])
    if size == 1:
        size = struct.unpack(">Q", h[8:16])[0]
    return size, typ


def parse_mvhd(st, moov_off):
    """(creation datetime, duration seconds) from the first child of moov, if it is mvhd."""
    d = st.read(moov_off + 8, 40)
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


def carve_at(st):
    """Walk MP4/MOV top-level atoms of a stream; return (size, moov offset) if complete."""
    pos = 0
    seen, moov_at = [], None
    while pos + 8 <= st.length:
        size, typ = read_atom(st, pos)
        if typ not in CARVE_ATOMS or size < 8 or pos + size > st.length:
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
    return pos, moov_at


class RawStream:
    """A file assumed contiguous from absolute byte offset `start`."""

    def __init__(self, dev, start, limit):
        self.dev, self.start, self.length = dev, start, limit - start

    def read(self, off, n):
        n = min(n, self.length - off)
        return self.dev.read(self.start + off, n) if n > 0 else b""


SECTOR = 512
TS_PACKET = 192  # M2TS: 4-byte timecode + 188-byte transport packet (AVCHD .MTS)


def in_heap(vol, off):
    return vol is not None and vol.coff(2) <= off < vol.coff(vol.ccount + 2)


def start_in_use(vol, off):
    """True if the cluster holding byte `off` belongs to a file that exists now."""
    if vol is None or vol.bitmap is None or not in_heap(vol, off):
        return False
    return vol.in_use((off - vol.coff(2)) // vol.csize + 2)


def overlap_pct(vol, start, size):
    """Percent of the byte range that the allocation bitmap currently marks in use."""
    if vol is None or vol.bitmap is None:
        return 0
    lo, hi = max(start, vol.coff(2)), min(start + size, vol.coff(vol.ccount + 2))
    if hi <= lo:
        return 0
    c0 = (lo - vol.coff(2)) // vol.csize + 2
    c1 = (hi - 1 - vol.coff(2)) // vol.csize + 2
    return 100 * sum(vol.in_use(c) for c in range(c0, c1 + 1)) // (c1 - c0 + 1)


def ts_sync_run(buf):
    """Number of leading 192-byte packets whose sync byte (0x47) is where it should be."""
    s = buf[4::TS_PACKET]
    return len(s) - len(s.lstrip(b"G"))


def ts_extent(dev, start, limit):
    block, pkts, off = TS_PACKET * 21845, 0, start
    while off < limit:
        buf = dev.read(off, min(block, limit - off))
        buf = buf[:len(buf) // TS_PACKET * TS_PACKET]
        if not buf:
            break
        good = ts_sync_run(buf)
        pkts += good
        if good < len(buf) // TS_PACKET:
            break
        off += len(buf)
    return pkts * TS_PACKET


def candidates(data, pos, step):
    """(offset in chunk, kind) for every sector-aligned video signature in the chunk."""
    found = []
    j = data.find(b"ftyp", 4)
    while j != -1:
        s = j - 4
        if s < step and (pos + s) % SECTOR == 0 \
                and 12 <= struct.unpack_from(">I", data, s)[0] <= 256 \
                and all(32 <= b < 127 for b in data[j + 4:j + 8]):
            found.append((s, "mp4"))
        j = data.find(b"ftyp", j + 1)
    j = data.find(b"RIFF")
    while j != -1:
        if j < step and (pos + j) % SECTOR == 0 and data[j + 8:j + 16] == b"AVI LIST":
            found.append((j, "avi"))
        j = data.find(b"RIFF", j + 1)
    syncs = data[4:step:SECTOR]
    k = syncs.find(b"G")
    while k != -1:
        s = k * SECTOR
        if len(data) - s >= TS_PACKET * 24 and ts_sync_run(data[s:s + TS_PACKET * 24]) == 24:
            found.append((s, "mts"))
        k = syncs.find(b"G", k + 1)
    return sorted(found)


def carve(vol, dev, skip_clusters, out):
    """Signature scan of the whole device at sector boundaries: MP4/MOV, AVI, MTS.

    Needs no filesystem. If the exFAT volume is readable, starts inside live files are
    skipped, and fragmented MP4/MOV are rebuilt around live clusters.
    """
    total, step, claimed = dev.size(), 8 << 20, 0
    kinds = ("mp4", "avi", "mts")
    stats = {k: {"seen": 0, "ok": 0, "live": 0, "bad": 0} for k in kinds}
    for pos in range(0, total, step):
        data = dev.read(pos, min(step + 8192, total - pos))
        for s, kind in candidates(data, pos, step):
            start, st = pos + s, stats[kind]
            if start < claimed:
                continue
            if start_in_use(vol, start):
                st["live"] += 1
                continue
            if in_heap(vol, start) and (start - vol.coff(2)) % vol.csize == 0 \
                    and (start - vol.coff(2)) // vol.csize + 2 in skip_clusters:
                continue  # already listed from its directory entry
            st["seen"] += 1
            when = dur = clusters = extents = None
            if kind == "mp4":
                stream = RawStream(dev, start, total)
                res = carve_at(stream)
                if not res and in_heap(vol, start) and vol.bitmap is not None \
                        and (start - vol.coff(2)) % vol.csize == 0:
                    stream = Stream(vol, (start - vol.coff(2)) // vol.csize + 2)
                    res = carve_at(stream)  # fragmented: rebuild around live clusters
                    clusters = stream.clusters(res[0]) if res else None
                if not res:
                    st["bad"] += 1
                    continue
                size = res[0]
                when, dur = parse_mvhd(stream, res[1])
                ext = "mov" if data[s + 8:s + 12] == b"qt  " else "mp4"
            elif kind == "avi":
                size = min(struct.unpack_from("<I", data, s + 4)[0] + 8, total - start)
                ext = "avi"
                if size < 1 << 20:
                    st["bad"] += 1
                    continue
            else:
                size, ext = ts_extent(dev, start, total), "mts"
                if size < 1 << 20:
                    st["bad"] += 1
                    continue
            if clusters:
                claimed = vol.coff(clusters[-1]) + vol.csize
            else:
                extents = [(start, size)]
                claimed = start + size
            st["ok"] += 1
            stamp = when.strftime("%Y%m%d_%H%M%S_") if when else ""
            out.append({
                "name": "%so%d.%s" % (stamp, start // SECTOR, ext), "isdir": False,
                "deleted": True, "first": start, "size": size, "nofat": True, "mtime": when,
                "path": "(carved)/", "info": "%.0fs" % dur if dur else "",
                "clusters": clusters, "extents": extents, "method": "carved",
            })
        sys.stderr.write("\rscanning %3d%%" % min(100, (pos + step) * 100 // total))
    sys.stderr.write("\n")
    names = {"mp4": "MP4/MOV", "avi": "AVI", "mts": "MTS/M2TS"}
    print("Signature scan of %.1f GB:" % (total / 1e9), file=sys.stderr)
    for k in kinds:
        s = stats[k]
        print("  %-9s %4d candidates: %d recovered, %d unusable (no complete structure: "
              "overwritten or fragmented), %d inside live files (ignored)" %
              (names[k], s["seen"] + s["live"], s["ok"], s["bad"], s["live"]), file=sys.stderr)


def assess(vol, ent):
    """Fill ent['clusters'] and ent['status'] (reuse and fragmentation)."""
    if ent.get("extents"):  # contiguous byte range from the signature scan
        if vol is None or vol.bitmap is None:
            ent["status"] = "unchecked"
        else:
            pct = overlap_pct(vol, ent["first"], ent["size"])
            ent["status"] = "intact" if pct == 0 else "%d%% on live data" % pct
        return
    if ent.get("clusters") is None:
        ent["clusters"], ent["method"] = vol.locate(ent["first"], ent["size"], ent["nofat"], True)
    cl = ent["clusters"]
    if not cl:
        ent["status"] = "bad pointer"
    elif vol.bitmap is None:
        ent["status"] = "unknown"
    else:
        used = sum(vol.in_use(c) for c in cl)
        frags = len(runs(cl))
        if used:
            ent["status"] = "%d/%d clusters reused" % (used, len(cl))
        elif frags > 1:
            exact = ent["method"] == "fat"
            ent["status"] = "%d fragments%s" % (frags, "" if exact else " (estimated)")
        else:
            ent["status"] = "intact"


def runs(clusters):
    r = []
    for c in clusters:
        if r and r[-1][0] + r[-1][1] == c:
            r[-1][1] += 1
        else:
            r.append([c, 1])
    return r


def real_home():
    """Home of the user who ran sudo, not root's."""
    if os.environ.get("SUDO_USER"):
        return pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir
    return os.path.expanduser("~")


def give_to_user(path):
    """Under sudo, files would be owned by root; hand them to the invoking user."""
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        os.chown(path, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))


def pick(files, spec):
    """'all' or '1,3,5-7' -> list of entries."""
    if spec.lower() == "all":
        return list(files)
    want = set()
    for part in spec.replace(" ", "").split(","):
        lo, _, hi = part.partition("-")
        if lo.isdigit() and (not hi or hi.isdigit()):
            want.update(range(int(lo), int(hi or lo) + 1))
    return [e for i, e in enumerate(files, 1) if i in want]


def restore(dev, vol, ent, outdir):
    name = re.sub(r"[\x00-\x1f/\\:]", "_", ent["name"]) or "unnamed_%d" % ent["first"]
    dest, n = os.path.join(outdir, name), 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(name)
        dest, n = os.path.join(outdir, "%s_%d%s" % (stem, n, ext)), n + 1
    left, done = ent["size"], 0
    with open(dest, "wb") as f:
        extents = ent.get("extents") or [(vol.coff(c), n * vol.csize)
                                         for c, n in runs(ent["clusters"])]
        for off, run_bytes in extents:
            for p in range(0, run_bytes, 4 << 20):
                chunk = dev.read(off + p, min(4 << 20, run_bytes - p, left))
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
    give_to_user(dest)
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
    base = find_volume(dev, a.offset)
    vol = Vol(dev, base) if base is not None else None
    found = []
    if vol:
        print("exFAT: cluster %d KB, %d clusters" % (vol.csize // 1024, vol.ccount),
              file=sys.stderr)
        vol.load_bitmap()
        walk(vol, vol.root, None, False, "/", set(), False, found)
        if a.deep:
            deep_scan(vol, found)
    elif a.carve:
        print("No exFAT volume found: running the signature scan only.", file=sys.stderr)
    else:
        sys.exit("No exFAT volume found. Check the device, pass --offset BYTES, "
                 "or add --carve to scan for videos without a filesystem.")

    if a.carve:
        carve(vol, dev, {e["first"] for e in found}, found)

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
        print("%3d  %8.1f MB  %s  %-26s  %s%s  %s" %
              (i, e["size"] / 1e6, when, e["status"], e["path"], e["name"], e.get("info", "")))

    out_dir, chosen = a.out, files
    if not out_dir:
        if not sys.stdin.isatty():
            print("\nListing only. Add --out FOLDER (on your Mac, not the card) to recover.")
            return
        ans = input("\nRecover which? Numbers like 1,3,5-7, 'all', or Enter to quit: ").strip()
        if not ans:
            return
        chosen = pick(files, ans)
        if not chosen:
            sys.exit("Nothing matched that selection.")
        default = os.path.join(real_home(), "recovered")
        out_dir = os.path.expanduser(input("Save to folder [%s]: " % default).strip() or default)
    os.makedirs(out_dir, exist_ok=True)
    give_to_user(out_dir)
    for e in chosen:
        if e.get("clusters") or e.get("extents"):
            print("recovered:", restore(dev, vol, e, out_dir))
    print("Done. %d file(s) in %s" % (len(chosen), out_dir))


if __name__ == "__main__":
    main()
