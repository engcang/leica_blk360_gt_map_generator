import copy
import json
import pathlib
import tempfile
import unittest

import numpy as np

from register_scans import hashFile, writePly
from review_registration import createReview, inferLevels, plotFrame


class ReviewTests(unittest.TestCase):
    def testTiltedFloorAndHeightBands(self):
        x, y = np.meshgrid(np.linspace(-4, 4, 35), np.linspace(-4, 4, 35))
        points = np.column_stack([x.ravel(), y.ravel(), np.full(x.size, -1.5)])
        angle = 0.03
        tilt = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0], [-np.sin(angle), 0, np.cos(angle)]])
        points = points @ tilt.T
        normals = np.tile(tilt[:, 2], (len(points), 1))
        poses = [np.eye(4) for _ in range(4)]
        for pose, position in zip(poses, ([0, 0, 0], [30, 0, 0.05], [20, 0, -2.4], [10, 0, -4.5])):
            pose[:3, 3] = tilt @ position
        rotation, origin, method = plotFrame(points, normals, poses)
        positions = (np.array([pose[:3, 3] for pose in poses]) - origin) @ rotation.T
        np.testing.assert_allclose(positions[:, 2], [0, 0.05, -2.4, -4.5], atol=1e-8)
        np.testing.assert_allclose(rotation @ rotation.T, np.eye(3), atol=1e-10)
        self.assertAlmostEqual(np.linalg.det(rotation), 1)
        self.assertIn("floor patch", method)
        self.assertEqual([level["stations"] for level in inferLevels(positions)], [[3], [2], [0, 1]])
        ramp = np.array([[0, 0, 0], [1, 0, 0.7], [2, 0, 1.4]])
        self.assertEqual(len(inferLevels(ramp)), 2)

    def testReviewWithoutCachePreservesGeometry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "Example scan.ply"
            points = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 1]], dtype=np.float32)
            writePly(source, points, np.full(3, 100))
            before = hashFile(source)
            result = {"scans": [{"index": 0, "name": "Example & scan.ply", "source_path": str(source),
                                  "transform_map_from_scan": np.eye(4).tolist()}]}
            original = copy.deepcopy(result)
            review = root / "output/example_review"
            summary = createReview(result, root, review)
            self.assertEqual(hashFile(source), before)
            self.assertEqual(result, original)
            self.assertEqual(summary["levels"][0]["stations"], [0])
            for name in summary["images"]:
                self.assertTrue((review / "html_source" / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual({path.name for path in review.iterdir()}, {"html_source", "result_review_and_manage.html"})
            page = (review / "result_review_and_manage.html").read_text(encoding="utf-8")
            self.assertIn("Example &amp; scan.ply", page)
            self.assertNotIn(str(root), page)
            self.assertIn('src="html_source/alignment_review.png"', page)
            self.assertIn("Generate raw PLY", page)
            saved = json.loads((review / "html_source/review_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["stations"][0]["map_xyz_m"], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
