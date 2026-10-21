import copy
import json
import pathlib
import tempfile
import unittest

import numpy as np

from register_scans import makeCloud
from registration_quality import evaluatePair, qualityDocument, writeQualityPage


class QualityTests(unittest.TestCase):
    def testPartialOverlapUsesSeparateDirectionalDenominators(self):
        source = makeCloud([[0, 0, 0], [1, 0, 0], [10, 0, 0]], [[0, 0, 1]] * 3)
        target = makeCloud([[0, 0, 0.005], [1, 0, 0.02], [1, 0, 0.025]], [[0, 0, 1]] * 3)
        result = evaluatePair(source, target, np.eye(4), 0, 1)
        primary = result["thresholds"][2]
        self.assertAlmostEqual(primary["forward"]["mean_m"], 0.0125)
        self.assertAlmostEqual(primary["backward"]["mean_m"], 0.05 / 3)
        self.assertAlmostEqual(primary["inlier_chamfer_m"], (0.0125 + 0.05 / 3) / 2)
        self.assertAlmostEqual(primary["forward"]["rmse_m"], np.sqrt((0.005 ** 2 + 0.02 ** 2) / 2))
        self.assertAlmostEqual(primary["forward"]["p95_m"], 0.01925)
        self.assertEqual(primary["forward"]["inlier_count"], 2)
        self.assertAlmostEqual(primary["forward"]["inlier_ratio"], 2 / 3)
        self.assertEqual(primary["backward"]["inlier_ratio"], 1)
        self.assertEqual(primary["forward"]["point_to_plane"]["count"], 2)
        self.assertAlmostEqual(primary["forward"]["point_to_plane"]["p50_m"], 0.0125)
        self.assertAlmostEqual(result["thresholds"][0]["inlier_chamfer_m"], 0.005)
        self.assertEqual(result["thresholds"][0]["backward"]["inlier_count"], 1)

    def testNoOverlapAndMissingNormalsNeverReportZeroError(self):
        source = makeCloud([[0, 0, 0]])
        result = evaluatePair(source, makeCloud([[10, 0, 0]]), np.eye(4), 0, 1)
        for threshold in result["thresholds"]:
            self.assertEqual(threshold["status"], "insufficient_overlap")
            self.assertIsNone(threshold["inlier_chamfer_m"])
            self.assertEqual(threshold["forward"]["inlier_ratio"], 0)
            self.assertIsNone(threshold["forward"]["rmse_m"])
        json.dumps(qualityDocument([result]), allow_nan=False)
        overlapping = evaluatePair(source, source, np.eye(4), 0, 1)["thresholds"][2]
        self.assertEqual(overlapping["inlier_chamfer_m"], 0)
        self.assertIsNone(overlapping["forward"]["point_to_plane"]["p50_m"])

    def testEvaluationUsesFixedPoseAndDoesNotMutateGeometry(self):
        source = makeCloud([[0, 0, 0], [1, 0, 0]], [[0, 0, 1]] * 2)
        target = makeCloud([[2, 3, 0.02], [2, 4, 0.02]], [[0, 0, -2]] * 2)
        transform = np.array([[0, -1, 0, 2], [1, 0, 0, 3], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
        original = transform.copy()
        source_points = np.asarray(source.points).copy()
        source_normals = np.asarray(source.normals).copy()
        target_normals = np.asarray(target.normals).copy()
        result = evaluatePair(source, target, transform, 0, 1)["thresholds"][2]
        self.assertAlmostEqual(result["inlier_chamfer_m"], 0.02)
        self.assertAlmostEqual(result["forward"]["point_to_plane"]["mean_m"], 0.02)
        np.testing.assert_array_equal(transform, original)
        np.testing.assert_array_equal(source.points, source_points)
        np.testing.assert_array_equal(source.normals, source_normals)
        np.testing.assert_array_equal(target.normals, target_normals)

    def testNormalGateOnlyAffectsPlaneStatistics(self):
        source = makeCloud([[0, 0, 0], [1, 0, 0]], [[0, 0, 1], [1, 0, 0]])
        target = makeCloud([[0, 0, 0.005], [1, 0, 0.005]], [[0, 0, 1]] * 2)
        metric = evaluatePair(source, target, np.eye(4), 0, 1)["thresholds"][2]
        self.assertEqual(metric["forward"]["inlier_count"], 2)
        self.assertEqual(metric["forward"]["point_to_plane"]["count"], 1)

    def testPageEscapesNamesAndPreservesJsonNulls(self):
        cloud = makeCloud([[0, 0, 0]])
        pair = evaluatePair(cloud, makeCloud([[10, 0, 0]]), np.eye(4), 0, 1)
        pair["source_name"] = "</script><script>alert(1)</script>"
        quality = qualityDocument([pair])
        before = copy.deepcopy(quality)
        with tempfile.TemporaryDirectory() as directory:
            writeQualityPage(quality, pathlib.Path(directory))
            page = (pathlib.Path(directory) / "quality.html").read_text(encoding="utf-8")
            self.assertNotIn(pair["source_name"], page)
            self.assertIn("\\u003c/script\\u003e", page)
            self.assertIn('"inlier_chamfer_m": null', page)
            self.assertEqual(quality, before)
            self.assertIn('id="download"', page)
            (pathlib.Path(directory) / "quality.json").write_text(json.dumps(quality), encoding="utf-8")
            writeQualityPage(quality, pathlib.Path(directory))
            self.assertNotIn('id="download"', (pathlib.Path(directory) / "quality.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
