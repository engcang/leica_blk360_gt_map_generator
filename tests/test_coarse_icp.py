"""Native coarse fitting must retain the guarded reference solver's behavior."""
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mapping'))
from native_correspondence import CoarseIcp
from trajectory_geometry import Matcher


class CoarseTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(210)
        self.p = rng.uniform(-6, 6, (9000, 3))
        self.n = np.zeros_like(self.p)
        for axis in range(3):
            rows = slice(axis*3000, (axis+1)*3000)
            self.p[rows, axis] = axis-1.
            self.n[rows, axis] = 1.
        self.reference = Matcher.__new__(Matcher)
        self.reference.p = self.p
        self.reference.n = self.n
        self.reference.good = np.ones(len(self.p), dtype=bool)
        self.reference.workers = 1
        self.reference.tree = cKDTree(self.p)
        self.native = CoarseIcp.create(self.p, self.n, self.reference.good, 4)
        if self.native is None:
            self.skipTest('Build the native coarse solver first')

    def compare(self, points, initial):
        expected = self.reference.icp_reference(points, initial)
        actual = self.native.fit(points, initial)
        self.assertIsNotNone(actual)
        np.testing.assert_allclose(actual, expected, atol=2e-7, rtol=0)
        return actual

    def test_drift_outliers_and_buffer_resize(self):
        rng = np.random.default_rng(28)
        for size, drift in [(6000, .18), (5000, .45), (100, .01), (7000, .25)]:
            points = self.p[:size].copy()+rng.normal(0, .001, (size,3))
            points = np.r_[points, rng.uniform(20,30,(size//8,3))]
            initial = np.eye(4)
            initial[:3,:3] = Rotation.from_euler('xyz', [.02,-.03,.015]).as_matrix()
            initial[:3,3] = [drift,-drift,.1]
            self.compare(points, initial)

    def test_weak_directions_preserve_tangent_translation(self):
        # A single plane cannot determine its two tangential directions.
        points = self.p[:3000].copy()
        self.reference.p = points
        self.reference.n = self.n[:3000]
        self.reference.good = np.ones(3000, dtype=bool)
        self.reference.tree = cKDTree(points)
        self.native = CoarseIcp.create(points, self.reference.n, self.reference.good, 4)
        initial = np.eye(4); initial[:3,3] = [.13,.11,-.09]
        result = self.compare(points, initial)
        np.testing.assert_allclose(result[1:3,3], initial[1:3,3], atol=1e-8)

    def test_thread_count_does_not_change_solution(self):
        other = CoarseIcp.create(self.p, self.n, self.reference.good, 1)
        initial = np.eye(4); initial[:3,3] = [.12,-.14,.09]
        points = self.p+.0001
        a = self.native.fit(points, initial)
        b = other.fit(points, initial)
        np.testing.assert_array_equal(a, b)
        self.assertGreater(self.native.statistics['reused_points'], 0)

    def test_float32_map_is_owned_as_double(self):
        native = CoarseIcp.create(self.p.astype(np.float32), self.n.astype(np.float32),
                                 self.reference.good, 2)
        self.assertEqual(native.index.points.dtype, np.float64)
        result = native.fit(self.p+.001, np.eye(4))
        self.assertTrue(np.isfinite(result).all())

    def test_invalid_inputs_are_rejected_before_native_call(self):
        with self.assertRaises(ValueError):
            CoarseIcp.create(np.zeros((100,2)), np.zeros((100,2)), np.ones(100,bool), 2)
        with self.assertRaises(ValueError):
            self.native.fit(np.array([[np.nan,0,0]]), np.eye(4))
        with self.assertRaises(ValueError):
            self.native.fit(self.p, np.eye(3))

    def test_exact_ties_fall_back_and_missing_support_leaves_initial(self):
        # Within the initial 80cm gate, with two precisely equidistant points.
        p = np.array([[-.1,0,0],[.1,0,0]])
        native = CoarseIcp.create(p, np.tile([0.,0,1.], (2,1)), np.ones(2,bool), 2)
        self.assertIsNone(native.fit(np.zeros((100,3)), np.eye(4)))
        initial = np.eye(4); initial[0,3]=100
        np.testing.assert_array_equal(self.native.fit(self.p[:70], initial), initial)


if __name__ == '__main__':
    unittest.main()
