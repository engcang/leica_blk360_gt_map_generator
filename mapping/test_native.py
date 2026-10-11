"""Exercise the actual native engines with a known rigid transform."""

import pathlib
import tempfile
import unittest

import numpy as np

from native_backend import align, backendInfo


class NativeTests(unittest.TestCase):
    def testKnownRigidTransform(self):
        rng = np.random.default_rng(427)
        parts = []
        for index in range(12):
            points = rng.uniform(-1, 1, (1500, 3))
            points[:, index % 3] = 0
            parts.append(points * rng.uniform(0.4, 2, 3) + rng.uniform(-5, 5, 3))
        source = np.concatenate(parts).astype("<f4")
        angle = 0.47
        expected = np.array([[np.cos(angle), -np.sin(angle), 0, 1.7],
                             [np.sin(angle), np.cos(angle), 0, -2.2],
                             [0, 0, 1, 0.8],
                             [0, 0, 0, 1]])
        target = (source @ expected[:3, :3].T + expected[:3, 3] + rng.normal(0, 0.001, source.shape)).astype("<f4")
        with tempfile.TemporaryDirectory(prefix="native path \u6d4b\u8bd5 ") as directory:
            root = pathlib.Path(directory)
            source_path, target_path = root / "source.xyz", root / "target.xyz"
            source.tofile(source_path)
            target.tofile(target_path)
            global_result = align("kiss", source_path, target_path, resolution=0.3)
            self.assertTrue(global_result["valid"], global_result)
            self.assertGreater(global_result["inliers"], 10)
            np.testing.assert_allclose(global_result["transform_target_from_source"], expected, atol=0.05)
            local_result = align("nano", source_path, target_path,
                                 initial=global_result["transform_target_from_source"], max_distance=0.3)
            self.assertTrue(local_result["valid"], local_result)
            np.testing.assert_allclose(local_result["transform_target_from_source"], expected, atol=0.002)
            print("Native engines recovered rotation and translation; Unicode paths passed", flush=True)

    def testVersions(self):
        self.assertEqual(backendInfo()["kiss_matcher"], "1.0.2")


if __name__ == "__main__":
    unittest.main()
