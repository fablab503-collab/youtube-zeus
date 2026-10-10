"""io.py: trimming keeps fps and audio, work.mp4 is 16 fps / <=832 / even dims."""

import shutil
import tempfile
import unittest
from pathlib import Path

import synth  # noqa: F401  (adds effects/ to sys.path)
from zfx import io as zio


class WorkSizeTests(unittest.TestCase):
    def test_landscape_scaled_to_832_even(self):
        self.assertEqual(zio.work_size(1920, 1080), (832, 468))

    def test_portrait(self):
        w, h = zio.work_size(1080, 1920)
        self.assertEqual(h, 832)
        self.assertEqual(w % 2, 0)

    def test_small_kept_and_made_even(self):
        self.assertEqual(zio.work_size(321, 241), (322, 242))

    def test_odd_scaled_never_exceeds(self):
        for w, h in [(1001, 999), (4096, 2160), (835, 3)]:
            ww, hh = zio.work_size(w, h)
            self.assertLessEqual(max(ww, hh), 832)
            self.assertEqual((ww % 2, hh % 2), (0, 0))

    def test_is_url(self):
        self.assertTrue(zio.is_url("https://www.youtube.com/watch?v=d6spCMcfkew"))
        self.assertFalse(zio.is_url("/Users/me/clip.mp4"))


class TrimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="zfx-io-"))
        cls.clip = synth.make_clip(cls.tmp / "in.mp4", seconds=3.0, size="1280x720", rate=30)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_prepare_input_local(self):
        job = self.tmp / "job"
        job.mkdir()
        seen = []
        info = zio.prepare_input(str(self.clip), 0.5, 2.0, job, report=lambda s, p, m: seen.append(s))
        src, work = info["source"], info["work"]
        # source.mp4: trimmed, original fps, audio kept
        self.assertAlmostEqual(src["duration"], 2.0, delta=0.1)
        self.assertAlmostEqual(src["fps"], 30.0, delta=0.01)
        self.assertEqual((src["width"], src["height"]), (1280, 720))
        self.assertTrue(src["has_audio"])
        # audio.m4a: present, same length, stream copied (AAC in, AAC out)
        self.assertTrue((job / "audio.m4a").is_file())
        self.assertGreater((job / "audio.m4a").stat().st_size, 1000)
        dur = float(zio.run([zio.tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", str(job / "audio.m4a")]).stdout.strip())
        self.assertAlmostEqual(dur, 2.0, delta=0.1)
        codec = zio.run([zio.tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
                         "stream=codec_name", "-of", "csv=p=0", str(job / "audio.m4a")]).stdout.strip()
        self.assertEqual(codec, "aac")
        # work.mp4: 16 fps, <= 832, even, no audio, 32 frames for 2 s
        self.assertEqual((work["width"], work["height"]), (832, 468))
        self.assertAlmostEqual(work["fps"], 16.0, delta=0.01)
        self.assertFalse(work["has_audio"])
        self.assertEqual(work["frames"], 32)
        self.assertIn("trim", seen)
        self.assertIn("normalize", seen)

    def test_trim_without_audio(self):
        silent = synth.make_clip(self.tmp / "silent.mp4", seconds=1.5, size="640x360", rate=25, audio=False)
        out = self.tmp / "silent-src.mp4"
        info = zio.trim_source(silent, out, 0.0, 1.0)
        self.assertFalse(info["has_audio"])
        self.assertIsNone(zio.extract_audio(out, self.tmp / "none.m4a"))
        self.assertAlmostEqual(info["fps"], 25.0, delta=0.01)

    def test_frames_roundtrip(self):
        import numpy as np
        out = self.tmp / "rt.mp4"
        frames = [np.full((64, 96, 3), v, dtype=np.uint8) for v in (0, 128, 255)]
        zio.write_video(frames, out, fps=16)
        back = zio.read_frames(out)
        self.assertEqual(back.shape, (3, 64, 96, 3))
        self.assertLess(abs(int(back[1].mean()) - 128), 4)
        gray = zio.read_frames(out, gray=True)
        self.assertEqual(gray.shape, (3, 64, 96))

    def test_missing_input(self):
        with self.assertRaises(zio.MediaError):
            zio.prepare_input(str(self.tmp / "nope.mp4"), 0, 1, self.tmp)


if __name__ == "__main__":
    unittest.main()
