"""Check fine-registration equivalence and safe reuse of completed pair work."""

import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mapping"))
import numpy as np
import open3d as o3d

from fine_registration import cloudFingerprint, informationFromCorrespondences, refinePair


def cloud(_points, _normals):
    result = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(_points))
    result.normals = o3d.utility.Vector3dVector(_normals)
    return result


class FineRegistrationTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(21)
        points = rng.uniform(-1, 1, (1200, 3))
        normals = rng.normal(size=(len(points), 3))
        normals /= np.linalg.norm(normals, axis=1)[:, None]
        self.target = cloud(points, normals)
        self.source = cloud(points + [0.002, -0.001, 0.001], normals)
        self.registration = o3d.pipelines.registration

    def testInformationMatchesOpen3dWithPartialOverlapAndRepeatedTargets(self):
        points = np.asarray(self.target.points)
        source = cloud(np.concatenate((points[:800] + 0.001, points[:100] + 0.002, points[800:] + 20)),
                       np.zeros((1300, 3)))
        transform = np.eye(4)
        transform[:3, 3] = [-0.001, 0.0003, 0]
        match = self.registration.evaluate_registration(source, self.target, 0.03, transform)
        expected = self.registration.get_information_matrix_from_point_clouds(source, self.target, 0.03, transform)
        actual = informationFromCorrespondences(self.target, match.correspondence_set)
        np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-10)
        self.assertEqual(actual[3, 3], len(match.correspondence_set))
        np.testing.assert_array_equal(actual, actual.T)

    def testEmptyMatchesGiveZeroInformation(self):
        np.testing.assert_array_equal(informationFromCorrespondences(self.target, np.empty((0, 2), dtype=int)), np.zeros((6, 6)))

    def testFullIcpResultAndInformationAgreeWithOriginalPipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            result, reused = refinePair(self.source, self.target, np.eye(4), pathlib.Path(directory) / "pair.json", "source", "target")
            baseline = self.registration.registration_icp(
                self.source, self.target, 0.03, np.eye(4),
                self.registration.TransformationEstimationPointToPlane(self.registration.TukeyLoss(0.01)),
                self.registration.ICPConvergenceCriteria(relative_fitness=1e-6, relative_rmse=1e-6, max_iteration=30))
            expected = self.registration.get_information_matrix_from_point_clouds(self.source, self.target, 0.03, baseline.transformation)
            self.assertFalse(reused)
            np.testing.assert_allclose(result["transform"], baseline.transformation, rtol=1e-10, atol=1e-10)
            np.testing.assert_allclose(result["information"], expected, rtol=1e-10, atol=1e-8)
            self.assertEqual(result["correspondences"], len(baseline.correspondence_set))
            self.assertAlmostEqual(result["fitness"], baseline.fitness, places=12)

    def testCacheReusesExactInputsButInvalidatesChangedPoseCloudAndSettings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "pairs" / "pair.json"
            original, _ = refinePair(self.source, self.target, np.eye(4), path, "source", "target")
            with mock.patch("fine_registration.o3d.pipelines.registration.registration_icp", side_effect=AssertionError("unexpected ICP")):
                reused, hit = refinePair(self.source, self.target, np.eye(4), path, "source", "target")
            self.assertTrue(hit)
            self.assertEqual(original, reused)
            changed = np.eye(4)
            changed[0, 3] = 0.0001
            for initial, source_key, target_key in ((changed, "source", "target"), (changed, "new-source", "target"), (changed, "new-source", "new-target")):
                _, hit = refinePair(self.source, self.target, initial, path, source_key, target_key)
                self.assertFalse(hit)
            with mock.patch.dict("fine_registration.FINE_SETTINGS", {"iterations": 29}):
                _, hit = refinePair(self.source, self.target, changed, path, "new-source", "new-target")
                self.assertFalse(hit)
            path.write_text("{broken", encoding="utf-8")
            _, hit = refinePair(self.source, self.target, np.eye(4), path, "source", "target")
            self.assertFalse(hit)

    def testGraphResumePreservesPosesAndQuality(self):
        from native_backend import PIPELINE_ID
        from register_scans import makeCloud, refineGraph, saveCache
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "cache").mkdir()
            scans = []
            for index, value in enumerate((self.source, self.target)):
                path = root / "cache" / f"{index:02d}_1cm.npz"
                saveCache(path, points=np.asarray(value.points), normals=np.asarray(value.normals))
                with np.load(path) as data:
                    restored = makeCloud(data["points"], data["normals"])
                self.assertEqual(cloudFingerprint(value), cloudFingerprint(restored))
                scans.append({"index": index, "name": f"scan{index}.ply", "source_path": str(root / f"scan{index}.ply"),
                              "source_sha256": str(index), "original_points": len(value.points),
                              "transform_map_from_scan": np.eye(4).tolist()})
            initial = {"pipeline_id": PIPELINE_ID, "coordinate_frame": "scan0", "registration_voxel_m": 0.08,
                       "scans": scans, "constraints": [{"source": 0, "target": 1, "retained": True, "tree_edge": True}]}
            first = refineGraph(copy.deepcopy(initial), root)
            quality = json.loads((root / "quality.json").read_text())
            with mock.patch("fine_registration.o3d.pipelines.registration.registration_icp", side_effect=AssertionError("unexpected ICP")):
                resumed = refineGraph(copy.deepcopy(initial), root)
            self.assertEqual(first, resumed)
            self.assertEqual(quality, json.loads((root / "quality.json").read_text()))
            self.assertTrue((root / "poses.json").is_file())

    def testConcurrentPairsPreserveGeometryAndMatchSerialResults(self):
        from parallel_runtime import orderedParallel
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            before = [cloudFingerprint(value) for value in (self.source, self.target)]

            def runPair(_item):
                label, index = _item
                initial = np.eye(4)
                initial[0, 3] = index * 0.0001
                result, _ = refinePair(self.source, self.target, initial, root / f"{label}-{index}.json", *before)
                return result

            serial = list(orderedParallel(runPair, [("serial", index) for index in range(3)], 1))
            parallel = list(orderedParallel(runPair, [("parallel", index) for index in range(3)], 3))
            for first, second in zip(serial, parallel):
                np.testing.assert_allclose(first["transform"], second["transform"], rtol=1e-10, atol=1e-10)
                np.testing.assert_allclose(first["information"], second["information"], rtol=1e-10, atol=1e-8)
            self.assertEqual(before, [cloudFingerprint(value) for value in (self.source, self.target)])

    def testFingerprintCoversCoordinatesAndNormals(self):
        original = cloudFingerprint(self.source)
        np.asarray(self.source.points)[0, 0] += 0.001
        changed = cloudFingerprint(self.source)
        self.assertNotEqual(original, changed)
        np.asarray(self.source.normals)[0, 0] += 0.001
        self.assertNotEqual(changed, cloudFingerprint(self.source))
        self.assertIsInstance(cloudFingerprint(o3d.geometry.PointCloud()), str)


if __name__ == "__main__":
    unittest.main()
