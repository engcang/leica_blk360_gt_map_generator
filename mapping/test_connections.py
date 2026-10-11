import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

from automatic_connections import discoverConnections, selectConnections
from register_scans import seedPoses
from studio_worker import connectedGroups


def makeEdge(_i, _j, _poses, _overlap=0.6, _shift=0):
    transform = np.linalg.inv(_poses[_j]) @ _poses[_i]
    transform[0, 3] += _shift
    return {"source": _i, "target": _j, "accepted": True,
            "score": _overlap, "harmonic_overlap_12cm": _overlap,
            "transform_target_from_source": transform.tolist()}


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.poses_ = []
        for index in range(6):
            angle = index * 0.27
            transform = np.eye(4)
            transform[:3, :3] = [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
            transform[:3, 3] = [2 * index, index % 3, -2 if index >= 4 else 0]
            self.poses_.append(transform)
        self.edges_ = [makeEdge(i, i + 1, self.poses_) for i in (0, 2, 4)]

    def testMultipleGroupsRejectIsolatedFalseConnection(self):
        pairs = [makeEdge(i, j, self.poses_) for i, j in ((0, 2), (1, 3), (0, 3), (2, 4), (3, 5))]
        pairs.append(makeEdge(1, 2, self.poses_, 0.9, 20))
        selected, _ = selectConnections(self.edges_, pairs, 6)
        self.assertEqual({(edge["source"], edge["target"]) for edge in selected}, {(0, 2), (1, 3), (0, 3), (2, 4), (3, 5)})
        poses, _ = seedPoses([*self.edges_, *selected], 6)
        np.testing.assert_allclose(poses, self.poses_, atol=1e-12)

    def testAmbiguousOrSingleHypothesisStaysDisconnected(self):
        single = [makeEdge(0, 2, self.poses_, 0.9)]
        self.assertEqual(selectConnections(self.edges_, single, 6)[0], [])
        pairs = [makeEdge(i, j, self.poses_) for i, j in ((0, 2), (1, 3))]
        pairs += [makeEdge(i, j, self.poses_, 0.7, 20) for i, j in ((0, 3), (1, 2))]
        #Codex: Use a common component-frame displacement for the competing placement.
        for edge in pairs[2:]:
            transform = np.array(edge["transform_target_from_source"])
            transform[:3, 3] = (np.linalg.inv(self.poses_[edge["target"]]) @ self.poses_[edge["source"]])[:3, 3]
            transform[:3, 3] += self.poses_[edge["target"]][:3, :3].T @ np.array([20, 0, 0])
            edge["transform_target_from_source"] = transform.tolist()
        self.assertEqual(selectConnections(self.edges_, pairs, 6)[0], [])

    def testCachedSearchAndResumeAvoidRepeatedNativeCalls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "pairs").mkdir()
            for edge in self.edges_:
                (root / "pairs" / f"{edge['source']:02d}_{edge['target']:02d}.json").write_text(json.dumps(edge))
            def matchPair(_source, _target, _i, _j):
                return makeEdge(_i, _j, self.poses_)
            with mock.patch("automatic_connections.registerPair", side_effect=matchPair) as matcher:
                result = discoverConnections([None] * 6, self.edges_, root, lambda _stage: None)
                self.assertEqual(connectedGroups(result, 6), [[1, 2, 3, 4, 5, 6]])
                self.assertEqual(matcher.call_count, 12)
            with mock.patch("automatic_connections.registerPair") as matcher:
                result = discoverConnections([None] * 6, self.edges_, root, lambda _stage: None)
                matcher.assert_not_called()
                self.assertEqual(connectedGroups(result, 6), [[1, 2, 3, 4, 5, 6]])

    def testInterleavedComponentOrientations(self):
        edges = [makeEdge(0, 3, self.poses_), makeEdge(1, 2, self.poses_)]
        pairs = [makeEdge(0, 1, self.poses_), makeEdge(2, 3, self.poses_)]
        selected, _ = selectConnections(edges, pairs, 4)
        poses, _ = seedPoses([*edges, *selected], 4)
        np.testing.assert_allclose(poses, self.poses_[:4], atol=1e-12)

    def testConflictingThirdGroupCycleIsRejected(self):
        pairs = [makeEdge(i, j, self.poses_, 0.8) for i, j in ((0, 2), (1, 3), (2, 4), (3, 5))]
        for i, j in ((0, 4), (1, 5)):
            edge = makeEdge(i, j, self.poses_, 0.5)
            transform = np.array(edge["transform_target_from_source"])
            transform[:3, 3] += self.poses_[j][:3, :3].T @ np.array([20, 0, 0])
            edge["transform_target_from_source"] = transform.tolist()
            pairs.append(edge)
        selected, audit = selectConnections(self.edges_, pairs, 6)
        self.assertEqual(len(selected), 4)
        rejected = next(item for item in audit if item["groups"] == [0, 2])
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "inconsistent_with_existing_connections")
        poses, _ = seedPoses([*self.edges_, *selected], 6)
        np.testing.assert_allclose(poses, self.poses_, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
