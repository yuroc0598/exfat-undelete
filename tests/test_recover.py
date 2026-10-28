import glob
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_test_image as m  # noqa: E402

EXPECTED = {"video1.mp4": 1, "video2.MOV": 2, "lost.mp4": 3, "reused.mp4": 4}
SIZES = {"video1.mp4": 10000, "video2.MOV": 9000, "lost.mp4": 5000, "reused.mp4": 8192}


def run(*args):
    return subprocess.run([sys.executable, "-I", os.path.join(ROOT, "exfat_undelete.py"), *args],
                          check=True, capture_output=True, text=True)


def contents(folder):
    out = {}
    for p in glob.glob(os.path.join(folder, "*")):
        with open(p, "rb") as f:
            out[os.path.basename(p)] = f.read()
    return out


class RecoverTest(unittest.TestCase):
    def test_recover(self):
        with tempfile.TemporaryDirectory() as tmp:
            img, out = os.path.join(tmp, "card.img"), os.path.join(tmp, "out")
            m.main(img)
            res = run(img, "--out", out)
            got = contents(out)
            for name, seed in EXPECTED.items():
                self.assertEqual(got[name], m.data(seed, SIZES[name]), name)
            self.assertNotIn("live.mp4", got)
            self.assertIn("1/2 clusters reused", res.stdout)

    def test_carve_orphans_and_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            img, out = os.path.join(tmp, "card.img"), os.path.join(tmp, "out")
            m.main(img)
            res = run(img, "--carve", "--out", out)
            got = contents(out)
            carved = [v for k, v in got.items() if k.startswith("20250101_000000_o")]
            # contiguous orphan, and one split around live data (rebuilt across the gap)
            self.assertIn(m.orphan_video(), carved)
            self.assertIn(m.FRAG_VIDEO(), carved)
            # allocated-but-unreferenced clusters are treated as deleted ...
            self.assertIn(m.ALLOC_ORPHAN(), carved)
            # ... but an existing file is left alone, and named in the report
            self.assertNotIn(m.LIVE_VIDEO(), carved)
            self.assertIn("/livevideo.mp4", res.stderr)
            self.assertEqual(len(carved), 3)  # nothing carved twice
            self.assertIn("12s", res.stdout)
            self.assertEqual(got["frag.mp4"], m.data(7, m.FRAG_FILE_SIZE))
            self.assertIn("2 fragments (estimated)", res.stdout)

    def test_no_filesystem_unaligned(self):
        """Reformatted/unreadable card: videos at arbitrary sectors, found by signature."""
        mp4 = m.orphan_video(20000, 5)
        avi_body = bytes((i * 13) % 251 for i in range(1_200_000))
        avi = b"RIFF" + struct.pack("<I", len(avi_body) + 8) + b"AVI LIST" + avi_body  # size = total - 8
        ts = b"".join(b"\x00\x00\x00\x01" + b"\x47" + bytes([(i * 7 + j) % 251 for j in range(187)])
                      for i in range(6500))
        image = bytearray(8 << 20)

        def put(sector, blob):
            image[sector * 512:sector * 512 + len(blob)] = blob

        put(101, mp4)
        put(500, avi)
        put(4000, ts)
        # decoys: ftyp not on a sector boundary; mp4 with no moov (overwritten tail)
        put(3000, b"\x00" * 200 + struct.pack(">I4s4s", 24, b"ftyp", b"isom") + b"\x00" * 200)
        put(3500, struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 0, b"isom", b"mp41")
            + struct.pack(">I4s", 5000, b"mdat") + b"\x01" * 4992)
        with tempfile.TemporaryDirectory() as tmp:
            img, out = os.path.join(tmp, "card.img"), os.path.join(tmp, "out")
            with open(img, "wb") as f:
                f.write(image)
            res = run(img, "--carve", "--out", out)
            got = list(contents(out).values())
            self.assertEqual(len(got), 3, res.stdout)
            self.assertIn(mp4, got)
            self.assertIn(avi, got)
            self.assertIn(ts, got)
            self.assertIn("1 unusable", res.stderr)

    def test_listing_without_out_when_not_a_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "card.img")
            m.main(img)
            res = run(img)
            self.assertIn("Listing only", res.stdout)


if __name__ == "__main__":
    unittest.main()
