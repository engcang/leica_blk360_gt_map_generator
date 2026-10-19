"""Check optional placement priors, source identity, and rejection/cache behaviour."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
import numpy as np
import open3d as o3d
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mapping"))
from manual_placement import applyPlacements, collectPriors, compatibleWithPlacement, placementMatrix


class ManualPlacementTests(unittest.TestCase):
    def testYawTranslationAndValidation(self):
        matrix = placementMatrix([2, 3, 4, np.pi / 2])
        np.testing.assert_allclose(matrix @ [1, 0, 0, 1], [2, 4, 4, 1], atol=1e-12)
        np.testing.assert_array_equal(matrix[2], [0, 0, 1, 4])
        for bad in ([1, 2, 3], [0, 0, 0, float("nan")], [1e7, 0, 0, 0]):
            with self.assertRaises(ValueError):
                placementMatrix(bad)

    def testChunkCompositionAndSourceIdentity(self):
        records = [{"source_sha256": "a"}, {"source_sha256": "b"}]
        base = np.eye(4)
        base[:3, :3] = o3d.geometry.get_rotation_matrix_from_xyz([.03, -.02, .6])
        base[:3, 3] = [3, 4, 5]
        config = {"manual_chunks": [{"key": "group", "scans": [
            {"row": 2, "source_sha256": "b", "transform_chunk_from_scan": base.tolist()}]}],
            "chunk_initial_guesses": {"group": [10, -2, 1, .4]}}
        priors, membership = collectPriors(config, records)
        np.testing.assert_allclose(priors[1], placementMatrix([10, -2, 1, .4]) @ base)
        self.assertEqual(membership, {1: "group"})
        records[1]["source_sha256"] = "changed"
        with self.assertRaises(ValueError):
            collectPriors(config, records)
        with self.assertRaises(ValueError):
            collectPriors({"initial_guesses": [[0, 0, 0, 0]]}, records)

    def testNoPlacementsLeavesAutomaticEdgesUntouched(self):
        edges = [{"accepted": True}]
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            self.assertIs(applyPlacements({}, [], [], edges, root, lambda _: None), edges)
            self.assertFalse(list(root.iterdir()))

    def testRefinementReceivesRelativePriorAndInvalidatesCacheWhenEdited(self):
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.random.default_rng(42).uniform(-1, 1, (200, 3))))
        clouds = [{"coarse": cloud}, {"coarse": cloud}]
        records = [{"source_sha256": "a"}, {"source_sha256": "b"}]
        config = {"initial_guesses": [[0, 0, 0, 0], [.01, 0, 0, 0]]}
        calls = []
        def match(source, target, i, j, prior):
            calls.append(prior.copy())
            return {"source": i, "target": j, "accepted": True,
                    "transform_target_from_source": prior.tolist(), "harmonic_overlap_12cm": 1., "score": 1.}
        with tempfile.TemporaryDirectory() as folder, mock.patch("manual_placement.registerPair", side_effect=match):
            root = pathlib.Path(folder)
            edges = applyPlacements(config, clouds, records, [], root, lambda _: None)
            self.assertTrue(edges[0]["accepted"])
            self.assertAlmostEqual(calls[0][0, 3], -.01)
            applyPlacements(config, clouds, records, [], root, lambda _: None)
            self.assertEqual(len(calls), 1)
            config["initial_guesses"][1][0] = .02
            applyPlacements(config, clouds, records, [], root, lambda _: None)
            self.assertEqual(len(calls), 2)
            config["initial_guesses"][1][0] = 50
            edges = applyPlacements(config, clouds, records, edges, root, lambda _: None)
            self.assertFalse(edges[0]["accepted"])
            saved = json.loads((root / "pairs/00_01.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["accepted"])
            self.assertIn("transform_target_from_source", saved)
            self.assertEqual(len(calls), 2)

    def testConflictingAutomaticEdgeAndExcessiveCorrectionRejected(self):
        priors = {0: np.eye(4), 1: np.eye(4)}
        edge = {"source": 0, "target": 1, "accepted": True,
                "transform_target_from_source": placementMatrix([2, 0, 0, 0]).tolist()}
        self.assertFalse(compatibleWithPlacement(edge, priors)["accepted"])
        self.assertTrue(edge["accepted"])
        self.assertIs(compatibleWithPlacement(edge, {0: np.eye(4)}), edge)
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.random.default_rng(42).normal(size=(100, 3))))
        with tempfile.TemporaryDirectory() as folder, mock.patch("manual_placement.registerPair", return_value=edge):
            result = applyPlacements({"initial_guesses": [[0, 0, 0, 0]] * 2},
                [{"coarse": cloud}] * 2, [{"source_sha256": "a"}, {"source_sha256": "b"}], [], pathlib.Path(folder), lambda _: None)
            self.assertFalse(result[0]["accepted"])

if __name__ == "__main__":
    unittest.main()
