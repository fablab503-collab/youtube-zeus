"""composite.py, masks.postprocess and mesh drawing on synthetic arrays (no Vision, no weights)."""

import unittest

import numpy as np

import synth  # noqa: F401
from zfx.composite import composite_frame, contact_sheet, control_frame, feather, sample_indices
from zfx.masks import postprocess


def scene(h=120, w=160):
    work = np.zeros((h, w, 3), np.uint8)
    work[:, :, 1] = 200                       # green background
    depth = np.zeros((h, w, 3), np.uint8)
    depth[:, :, 0] = 250                      # red "depth"
    mask = np.zeros((h, w), np.uint8)
    mask[30:90, 50:110] = 255                 # the person
    return work, depth, mask


class CompositeTests(unittest.TestCase):
    def test_inside_depth_outside_original(self):
        work, depth, mask = scene()
        out = composite_frame(work, depth, mask, feather_px=4)
        self.assertEqual(out.shape, work.shape)
        self.assertEqual(out.dtype, np.uint8)
        np.testing.assert_array_equal(out[60, 80], depth[60, 80])   # center of the mask
        np.testing.assert_array_equal(out[5, 5], work[5, 5])        # far background

    def test_feathered_edge_is_a_blend(self):
        work, depth, mask = scene()
        out = composite_frame(work, depth, mask, feather_px=4)
        edge = out[60, 50].astype(int)
        self.assertTrue(0 < edge[0] < 250 and 0 < edge[1] < 200, edge)

    def test_feather_range(self):
        alpha = feather(scene()[2], 3)
        self.assertGreaterEqual(alpha.min(), 0.0)
        self.assertLessEqual(alpha.max(), 1.0)
        self.assertAlmostEqual(float(alpha[60, 80]), 1.0, places=4)

    def test_size_mismatch_raises(self):
        work, depth, mask = scene()
        with self.assertRaises(ValueError):
            composite_frame(work, depth[:-2], mask)

    def test_mesh_mode_keeps_original_without_face(self):
        work, depth, mask = scene()
        try:
            out = control_frame("mesh", work, depth, mask, points=None)
        except ImportError:
            self.skipTest("mediapipe not installed")
        np.testing.assert_array_equal(out, work)

    def test_mesh_lines_are_white(self):
        work, depth, mask = scene(240, 320)
        rng = np.random.default_rng(0)
        pts = np.c_[rng.uniform(0.3, 0.7, 478), rng.uniform(0.3, 0.7, 478), np.zeros(478)].tolist()
        try:
            out = control_frame("depth_mesh", work, depth, mask, points=pts)
        except ImportError:
            self.skipTest("mediapipe not installed")
        white = (out.min(axis=2) > 240).sum()
        self.assertGreater(white, 200)

    def test_contact_sheet_layout(self):
        work, depth, mask = scene()
        rows = [[work, depth, mask, work] for _ in range(4)]
        sheet = contact_sheet(rows, frame_numbers=[0, 10, 20, 31], tile_width=80, gap=2)
        tile_h = 60
        self.assertEqual(sheet.shape, (4 * tile_h + 5 * 2, 4 * 80 + 5 * 2, 3))

    def test_sample_indices(self):
        self.assertEqual(sample_indices(64), [0, 21, 42, 63])
        self.assertEqual(sample_indices(3), [0, 1, 2])
        self.assertEqual(sample_indices(0), [])


class MaskPostprocessTests(unittest.TestCase):
    def test_dilate_8px(self):
        soft = np.zeros((100, 100), np.float32)
        soft[50, 50] = 1.0
        out = list(postprocess([soft], dilate_px=8))[0]
        self.assertEqual(out[50, 58], 255)
        self.assertEqual(out[50, 59], 0)
        self.assertEqual(out[50, 42], 255)
        self.assertEqual(set(np.unique(out)), {0, 255})

    def test_ema_smooths_flicker(self):
        on = np.ones((10, 10), np.float32)
        off = np.zeros((10, 10), np.float32)
        dip = np.full((10, 10), 0.2, np.float32)
        blip = np.full((10, 10), 0.7, np.float32)
        held = list(postprocess([on, on, on, dip], dilate_px=0, alpha=0.6))
        self.assertEqual(held[3][0, 0], 255)      # 0.6*0.2 + 0.4*1.0 = 0.52 -> still a person
        quiet = list(postprocess([off, off, blip], dilate_px=0, alpha=0.6))
        self.assertEqual(quiet[2][0, 0], 0)       # 0.6*0.7 + 0.4*0.0 = 0.42 -> one-frame blip suppressed

class HeadCropTests(unittest.TestCase):
    def test_crop_follows_the_main_person_head(self):
        import shutil
        import tempfile
        from pathlib import Path
        from zfx.io import write_video
        from zfx.mesh import head_crop
        tmp = Path(tempfile.mkdtemp(prefix="zfx-crop-"))
        try:
            frames = []
            for i in range(8):
                m = np.zeros((468, 832), np.uint8)
                m[250:468, 560 + i:760 + i] = 255      # a presenter in the bottom-right corner
                m[200:250, 620 + i:700 + i] = 255      # their head
                m[60:110, 100:140] = 255               # a small face on screen (not the person)
                frames.append(m)
            write_video(frames, tmp / "mask.mp4", fps=16, crf=0)
            crop = head_crop(tmp / "mask.mp4", 832, 468)
            self.assertIsNotNone(crop)
            x0, y0, cw, ch = crop
            self.assertEqual(cw, ch)
            self.assertTrue(x0 <= 620 and x0 + cw >= 707, crop)    # covers the head
            self.assertTrue(y0 <= 200 < y0 + ch, crop)
            self.assertGreater(x0, 140)                            # ignores the small blob
            self.assertIsNone(head_crop(tmp / "missing.mp4", 832, 468))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
