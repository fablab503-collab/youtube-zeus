"""Apple Vision person segmentation through pyobjc (skipped when Vision/pyobjc is unavailable)."""

import unittest

import numpy as np

import synth  # noqa: F401
from zfx.masks import vision_available

AVAILABLE, WHY = vision_available()


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
