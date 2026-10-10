"""Apple Vision person segmentation through pyobjc (skipped when Vision/pyobjc is unavailable)."""

import unittest

import numpy as np

import synth  # noqa: F401
from zfx.masks import keep_main_people, postprocess, vision_available

AVAILABLE, WHY = vision_available()


class KeepMainPeopleTests(unittest.TestCase):
    def frame(self):
        m = np.zeros((200, 300), np.uint8)
        m[60:200, 180:280] = 255    # presenter: 140 x 100
        m[20:50, 20:50] = 255       # face on a screen: 30 x 30 (6 % of the presenter)
        m[80:200, 20:110] = 255     # second person: 120 x 90 (77 %)
        return m

    def test_small_blob_dropped_people_kept(self):
        out = keep_main_people(self.frame(), 0.25)
        self.assertEqual(out[30, 30], 0)
        self.assertEqual(out[100, 200], 255)
        self.assertEqual(out[100, 50], 255)

    def test_zero_ratio_keeps_everything(self):
        m = self.frame()
        self.assertTrue(np.array_equal(keep_main_people(m, 0), m))

    def test_empty_and_single_blob_unchanged(self):
        empty = np.zeros((10, 10), np.uint8)
        self.assertTrue(np.array_equal(keep_main_people(empty), empty))
        one = empty.copy()
        one[2:5, 2:5] = 255
        self.assertTrue(np.array_equal(keep_main_people(one), one))

    def test_postprocess_applies_filter(self):
        soft = self.frame().astype(np.float32) / 255.0
        out = next(postprocess([soft], dilate_px=0))
        self.assertEqual(out[30, 30], 0)
        self.assertEqual(out[100, 200], 255)


@unittest.skipUnless(AVAILABLE, f"Apple Vision unavailable: {WHY}")
class VisionTests(unittest.TestCase):
    def test_segment_shape_and_range(self):
        from zfx.masks import PersonSegmenter
        seg = PersonSegmenter("balanced")
        frame = np.zeros((180, 320, 3), np.uint8)
        frame[:, :, 2] = 180
        soft = seg.segment(frame)
        self.assertEqual(soft.shape, (180, 320))
        self.assertEqual(soft.dtype, np.float32)
        self.assertGreaterEqual(float(soft.min()), 0.0)
        self.assertLessEqual(float(soft.max()), 1.0)
        self.assertLess(float(soft.mean()), 0.2)   # a flat blue frame has no person


if __name__ == "__main__":
    unittest.main()
