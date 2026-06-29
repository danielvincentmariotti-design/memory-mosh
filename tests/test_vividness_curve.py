import unittest

from memory_mosh import build_preview_ffmpeg_args, build_vividness_curve, rate_from_curve


class VividnessCurveTests(unittest.TestCase):
    def test_curve_is_loop_safe_and_clamped(self):
        curve = build_vividness_curve(8, motion_energy=[0.0, 0.2, 0.8, 1.0, 0.8, 0.2, 0.0, 0.0], cycles=2.0)
        self.assertEqual(len(curve), 8)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in curve))
        self.assertAlmostEqual(curve[0], curve[-1], places=3)

    def test_rate_is_reduced_at_high_vividness(self):
        low = rate_from_curve(0.9, 0.0)
        high = rate_from_curve(0.9, 1.0)
        self.assertEqual(low, 0.9)
        self.assertLess(high, low)

    def test_preview_args_limit_duration(self):
        args = build_preview_ffmpeg_args('input.mp4', 'preview.mp4', duration=20)
        self.assertIn('-t', args)
        self.assertIn('20', args)
        self.assertTrue(args[-1].endswith('preview.mp4'))


if __name__ == '__main__':
    unittest.main()
