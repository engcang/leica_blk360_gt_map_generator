import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

from native_backend import PIPELINE_ID
from register_scans import edgeError, hashFile, readPly, refineGraph, seedPoses, writePly
from studio_worker import connectedGroups, parsePairs, runRegistration


class RegistrationTests(unittest.TestCase):
    def testFineRefinementPreservesKnownPoseAndReportsStages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "cache").mkdir()
            x, y = np.meshgrid(np.arange(0, 1, 0.02), np.arange(0, 1, 0.02))
            x, y = x.ravel(), y.ravel()
            points = np.concatenate([np.column_stack([x, y, np.zeros(len(x))]),
                                     np.column_stack([x, np.zeros(len(x)), y]),
                                     np.column_stack([np.ones(len(x)), x, y])])
            second = np.eye(4)
            angle = 0.12
            second[:3, :3] = [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
            second[:3, 3] = [0.12, 0.08, -0.03]
            scans = []
            for index, pose in enumerate((np.eye(4), second)):
                source = root / f"scan{index}.ply"
                local = (points - pose[:3, 3]) @ pose[:3, :3]
                writePly(source, local, np.full(len(points), 1000))
                scans.append({"index": index, "name": source.name, "source_path": str(source),
                              "source_sha256": hashFile(source), "original_points": len(points),
                              "transform_map_from_scan": pose.tolist()})
            result = {"pipeline_id": PIPELINE_ID, "coordinate_frame": "first_scanner", "scans": scans,
                      "constraints": [{"source": 0, "target": 1, "retained": True, "tree_edge": True}]}
            stages = []
            refined = refineGraph(result, root, stages.append)
            np.testing.assert_allclose(refined["scans"][1]["transform_map_from_scan"], second, atol=1e-4)
            self.assertEqual(refined["registration_voxel_m"], 0.01)
            self.assertTrue(refined["constraints"][0]["retained"])
            self.assertTrue(any("Refining 1 cm connections: 1/1" in stage for stage in stages))
            self.assertTrue(any("Checking final alignment quality: 1/1" in stage for stage in stages))
            self.assertTrue((root / "quality.json").is_file())
            quality = json.loads((root / "quality.json").read_text(encoding="utf-8"))
            self.assertEqual(quality["schema_version"], 2)
            self.assertEqual(quality["usage"], "diagnostic_only")
            self.assertEqual(quality["pairs"][0]["source_name"], "scan0.ply")
            self.assertEqual(len(quality["pairs"][0]["thresholds"]), 4)

    def testInputPairsAndGroups(self):
        self.assertEqual(parsePairs("1:4, 2:5,1:4", 5), [(0, 3), (1, 4)])
        for text in ("0:2", "2:1", "1:6", "1:1", "one:two", "1:2:3"):
            with self.assertRaises(ValueError):
                parsePairs(text, 5)
        edges = [{"source": 0, "target": 1, "accepted": True},
                 {"source": 1, "target": 2, "accepted": False},
                 {"source": 2, "target": 3, "accepted": True}]
        self.assertEqual(connectedGroups(edges, 4), [[1, 2], [3, 4]])
        edges.append({"source": 0, "target": 3, "accepted": True})
        self.assertEqual(connectedGroups(edges, 4), [[1, 2, 3, 4]])

    def testDisconnectedWorkerNeverMerges(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            sources = [root / "first.ply", root / "second.ply"]
            for source in sources:
                writePly(source, np.array([[0, 0, 0]], dtype=np.float32), np.array([1]))
            config = {"paths": [str(path) for path in sources], "voxel_m": 0.01,
                      "output_path": str(root / "map.ply"), "additional_pairs": ""}
            records = [{"name": path.name, "source_path": str(path), "source_sha256": hashFile(path),
                        "source_bytes": path.stat().st_size, "source_mtime_ns": path.stat().st_mtime_ns,
                        "original_points": 1} for path in sources]
            with mock.patch("register_scans.prepare", return_value=records), \
                 mock.patch("register_scans.loadClouds", return_value=[{}, {}]), \
                 mock.patch("register_scans.adjacent", return_value=[{"source": 0, "target": 1, "accepted": False}]), \
                 mock.patch("automatic_connections.discoverConnections", side_effect=lambda _clouds, _edges, _output, _progress: _edges), \
                 mock.patch("register_scans.optimize") as optimize, \
                 mock.patch("register_scans.merge") as merge:
                runRegistration(config, root)
                optimize.assert_not_called()
                merge.assert_not_called()
            progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
            self.assertEqual(progress["status"], "needs_connections")
            self.assertEqual(progress["groups"], [[1], [2]])
            self.assertFalse((root / "map.ply").exists())
            self.assertFalse((root / "relative_poses.json").exists())
            self.assertTrue((root / "chunks_review.html").is_file())
            manifest = json.loads((root / "partial_chunks.json").read_text())
            self.assertFalse(manifest["shared_coordinate_frame"])
            self.assertEqual(manifest["groups"], [[1], [2]])
            for chunk in manifest["chunks"]:
                self.assertTrue((root / chunk["viewer"]).is_file())
                self.assertTrue((root / chunk["image"]).is_file())

    def testGraphDirection(self):
        angle = 0.6
        first = np.eye(4)
        second = np.array([[np.cos(angle), -np.sin(angle), 0, 4],
                           [np.sin(angle), np.cos(angle), 0, -2],
                           [0, 0, 1, 3],
                           [0, 0, 0, 1]])
        third = np.eye(4)
        third[:3, 3] = [-3, 5, -4]
        expected = [first, second, third]
        edges = []
        for i, j in [(0, 2), (1, 2)]:
            transform = np.linalg.inv(expected[j]) @ expected[i]
            edges.append({"source": i, "target": j, "accepted": True, "score": 0.8,
                          "transform_target_from_source": transform.tolist()})
        poses, tree = seedPoses(edges, 3)
        self.assertEqual(len(tree), 2)
        for actual, target in zip(poses, expected):
            np.testing.assert_allclose(actual, target, atol=1e-12)
        for edge in edges:
            translation, rotation = edgeError(edge, poses)
            self.assertLess(translation, 1e-10)
            self.assertLess(rotation, 1e-5)
        local = np.array([1.2, -0.8, 0.3, 1])
        target_local = np.array(edges[0]["transform_target_from_source"]) @ local
        np.testing.assert_allclose(poses[0] @ local, poses[2] @ target_local, atol=1e-12)

    def testDisconnectedGraph(self):
        with self.assertRaisesRegex(ValueError, "disconnected"):
            seedPoses([], 2)

    def testPlyRecordsAndNoOverwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "map.ply"
            xyz = np.array([[1.25, -2.5, 3], [0, 100, -4]], dtype=np.float32)
            intensity = np.array([42, 65535], dtype=np.uint16)
            writePly(path, xyz, intensity)
            raw = readPly(path)
            for i, name in enumerate(("x", "y", "z")):
                np.testing.assert_array_equal(raw[name], xyz[:, i])
            np.testing.assert_array_equal(raw["intensity"], intensity)
            del raw
            original_hash = hashFile(path)
            with self.assertRaises(FileExistsError):
                writePly(path, xyz + 1, intensity)
            self.assertEqual(original_hash, hashFile(path))
            with path.open("ab") as stream:
                stream.write(b"invalid")
            with self.assertRaisesRegex(ValueError, "count/size"):
                readPly(path)


if __name__ == "__main__":
    unittest.main()
