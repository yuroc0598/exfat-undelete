import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXPECTED = {"video1.mp4": 1, "video2.MOV": 2, "lost.mp4": 3, "reused.mp4": 4}


def data(seed, size):
    return bytes((i * 7 + seed) % 251 for i in range(size))


class RecoverTest(unittest.TestCase):
    def test_recover(self):
        sizes = {"video1.mp4": 10000, "video2.MOV": 9000, "lost.mp4": 5000, "reused.mp4": 8192}
        with tempfile.TemporaryDirectory() as tmp:
            img, out = os.path.join(tmp, "card.img"), os.path.join(tmp, "out")
            subprocess.run([sys.executable, "-I", os.path.join(HERE, "make_test_image.py"), img],
                           check=True)
            res = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "exfat_undelete.py"),
                                  img, "--out", out], check=True, capture_output=True, text=True)
            for name, seed in EXPECTED.items():
                with open(os.path.join(out, name), "rb") as f:
                    self.assertEqual(f.read(), data(seed, sizes[name]), name)
            self.assertFalse(os.path.exists(os.path.join(out, "live.mp4")))
            self.assertIn("1/2 clusters reused", res.stdout)


if __name__ == "__main__":
    unittest.main()
