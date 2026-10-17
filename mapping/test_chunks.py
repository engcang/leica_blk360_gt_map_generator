"""Regression tests for partial results and verified merged-component bridges."""
import json
import pathlib
import tempfile
import unittest
from unittest import mock
import numpy as np

from automatic_connections import componentPoses
from chunk_connections import componentKey, discoverChunkConnections, scanPrior, verifyBridge
from register_scans import makeCloud
from studio_worker import connectedGroups
from test_connections import makeEdge


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.poses = [np.eye(4) for _ in range(3)]
        self.poses[1][:3, 3] = [1, -.2, 0]
        angle = .47
        self.poses[2][:3, :3] = [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
        self.poses[2][:3, 3] = [1.7, -2.2, .8]
        self.edges = [makeEdge(0, 1, self.poses)]
        self.records = [{"source_sha256": str(i)} for i in range(3)]

    def testScanPriorHandlesReversedAndInterleavedRows(self):
        local, _ = componentPoses(self.edges, 3)
        bridge = np.linalg.inv(self.poses[2])
        i, j, prior = scanPrior(1, 2, local, bridge)
        self.assertEqual((i, j), (1, 2))
        np.testing.assert_allclose(prior, np.linalg.inv(self.poses[2]) @ self.poses[1])
        i, j, reversed_prior = scanPrior(2, 1, local, np.linalg.inv(bridge))
        self.assertEqual((i, j), (1, 2))
        np.testing.assert_allclose(reversed_prior, prior, atol=1e-12)

    def testCacheChangesWhenPoseOrSourceChanges(self):
        poses, _ = componentPoses(self.edges, 3)
        key = componentKey([0, 1], poses, self.records)
        poses[1][0, 3] += .01
        self.assertNotEqual(key, componentKey([0, 1], poses, self.records))
        key = componentKey([0, 1], poses, self.records)
        self.records[1]["source_sha256"] = "changed"
        self.assertNotEqual(key, componentKey([0, 1], poses, self.records))

    def testSinglePairNeverPublishesBridgeAndFailedAttemptResumesFromCache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "pairs").mkdir()
            cloud = {"coarse_path": root / "coarse.xyz"}
            alignment = {"converged": True, "harmonic_overlap_12cm": .8, "forward_overlap_12cm": .8,
                         "backward_overlap_12cm": .8, "inlier_rmse_m": .01, "relative_tilt_deg": 0,
                         "transform_target_from_source": np.eye(4).tolist()}
            with mock.patch("chunk_connections.aggregate", return_value=cloud), \
                 mock.patch("chunk_connections.align", return_value={"valid": True, "inliers": 50, "transform_target_from_source": np.eye(4).tolist()}), \
                 mock.patch("chunk_connections.refine", return_value=alignment), \
                 mock.patch("chunk_connections.verifyBridge", return_value=[makeEdge(0, 2, self.poses)]):
                result = discoverChunkConnections([None] * 3, self.edges, self.records, root, lambda s: None)
            self.assertEqual(connectedGroups(result, 3), [[1, 2], [3]])
            self.assertEqual(list((root / "pairs").glob("*.json")), [])
            with mock.patch("chunk_connections.align") as native:
                discoverChunkConnections([None] * 3, self.edges, self.records, root, lambda s: None)
                native.assert_not_called()

    def testNativeMergedChunksRecoverKnownPoseAndReuseSuccessfulAttempt(self):
        rng = np.random.default_rng(427)
        parts = []
        for index in range(12):
            points = rng.uniform(-1, 1, (1500, 3))
            points[:, index % 3] = 0
            parts.append(points * rng.uniform(.4, 2, 3) + rng.uniform(-5, 5, 3))
        world = np.concatenate(parts)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            (root / "pairs").mkdir()
            clouds = []
            for i, pose in enumerate(self.poses):
                local = (world - pose[:3, 3]) @ pose[:3, :3]
                item = {}
                for level, voxel in (("fine", .08), ("coarse", .30)):
                    item[level] = makeCloud(local).voxel_down_sample(voxel)
                    item[level + "_path"] = root / f"{i}_{level}.xyz"
                    np.asarray(item[level].points, dtype="<f4").tofile(item[level + "_path"])
                clouds.append(item)
            result = discoverChunkConnections(clouds, self.edges, self.records, root, print)
            self.assertEqual(connectedGroups(result, 3), [[1, 2, 3]])
            poses, _ = componentPoses(result, 3)
            np.testing.assert_allclose(poses[2], self.poses[2], atol=.01)
            self.assertGreaterEqual(sum(edge.get("chunk_connection", False) for edge in result), 2)
            with mock.patch("chunk_connections.align") as native:
                repeated = discoverChunkConnections(clouds, self.edges, self.records, root, lambda s: None)
                self.assertEqual(connectedGroups(repeated, 3), [[1, 2, 3]])
                native.assert_not_called()

    def testVerificationRejectsCorrectionAwayFromChunkHypothesis(self):
        clouds = [{"coarse": makeCloud(np.random.default_rng(9).normal(size=(100, 3)))} for _ in range(2)]
        local = {0: np.eye(4), 1: np.eye(4)}
        wrong = makeEdge(0, 1, [np.eye(4), np.eye(4)], _shift=2)
        with mock.patch("chunk_connections.registerPair", return_value=wrong):
            result = verifyBridge([0], [1], local, np.eye(4), clouds, lambda s: None)
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["accepted"])


if __name__ == "__main__":
    unittest.main()
