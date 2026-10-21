"""Check parallel export against the original two-stage voxel calculation."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np
import open3d as o3d

from ply_io import colourValues
from register_scans import hashFile, makeCloud, merge, readPly, writePly
from voxel_merge import mergeWorkers


class VoxelMergeTests(unittest.TestCase):
    def makeScans(self, root, rgb=False):
        rng = np.random.default_rng(180)
        scans = []
        for index, count in enumerate((2500, 1700, 3200)):
            path = root / f"scan {index}.ply"
            xyz = rng.uniform(-0.04, 0.04, (count, 3))
            colours = rng.integers(0, 256 if rgb else 65536, (count, 3) if rgb else count)
            writePly(path, xyz, colours)
            angle = index * 0.17
            pose = np.eye(4)
            pose[:3, :3] = [[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]
            pose[:3, 3] = [index * 0.01, -0.03, 0.005]
            scans.append({"source_path": str(path), "source_sha256": hashFile(path),
                          "original_points": count, "name": path.name, "transform_map_from_scan": pose.tolist()})
        return {"scans": scans}

    def legacyCloud(self, registration):
        points, colours = [], []
        for scan in registration["scans"]:
            raw = readPly(pathlib.Path(scan["source_path"]))
            rgb = "red" in raw.dtype.names
            cloud = makeCloud(np.column_stack([raw[axis] for axis in "xyz"]))
            values = colourValues(raw).astype(np.float64) / (255 if rgb else 65535)
            cloud.colors = o3d.utility.Vector3dVector(values if rgb else np.column_stack([values] * 3))
            cloud.transform(np.asarray(scan["transform_map_from_scan"]))
            reduced = cloud.voxel_down_sample(.01)
            points.append(np.asarray(reduced.points).copy())
            colours.append(np.asarray(reduced.colors).copy())
        cloud = makeCloud(np.concatenate(points))
        cloud.colors = o3d.utility.Vector3dVector(np.concatenate(colours))
        return cloud.voxel_down_sample(.01), rgb

    def testSerialAndSpawnedExportsMatchLegacyBytes(self):
        for rgb in (False, True):
            with self.subTest(rgb=rgb), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                registration = self.makeScans(root, rgb)
                cloud, mode = self.legacyCloud(registration)
                colour = np.asarray(cloud.colors) * 255 if mode else np.asarray(cloud.colors)[:, 0] * 65535
                expected = root / "expected.ply"
                writePly(expected, np.asarray(cloud.points), colour)
                for workers in (1, 2):
                    stages = []
                    with mock.patch.dict(os.environ, {"BLK_MERGE_WORKERS": str(workers)}):
                        merge(registration, root, .01, f"merged-{workers}.ply", False, stages.append)
                    self.assertEqual((root / f"merged-{workers}.ply").read_bytes(), expected.read_bytes())
                    self.assertIn(f"with {workers} worker", stages[0])
                    self.assertEqual(sum(stage.startswith("Merged scan") for stage in stages), 3)
                for scan in registration["scans"]:
                    self.assertEqual(hashFile(pathlib.Path(scan["source_path"])), scan["source_sha256"])
                self.assertFalse(list(root.glob(".merge-*")))

    def testChangedSourcesFailWithoutPublishingOrLeavingStaging(self):
        for workers in (1, 2):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                registration = self.makeScans(root)
                source = pathlib.Path(registration["scans"][0]["source_path"])
                data = source.read_bytes()
                source.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
                with mock.patch.dict(os.environ, {"BLK_MERGE_WORKERS": str(workers)}):
                    with self.assertRaisesRegex(ValueError, "changed since registration"):
                        merge(registration, root, .01, "bad.ply", False)
                self.assertFalse((root / "bad.ply").exists())
                self.assertFalse(list(root.glob(".merge-*")))

    def testChangesAfterAScanFinishesAreRejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            registration = self.makeScans(root)
            source = pathlib.Path(registration["scans"][0]["source_path"])
            def progress(stage):
                if stage.startswith("Combining"):
                    source.touch()
            with mock.patch.dict(os.environ, {"BLK_MERGE_WORKERS": "1"}):
                with self.assertRaisesRegex(ValueError, "changed during merge"):
                    merge(registration, root, .01, "bad.ply", False, progress)
            self.assertFalse((root / "bad.ply").exists())
            self.assertFalse(list(root.glob(".merge-*")))

    def testWorkersRespectCpuMemoryAndSmallJobs(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("voxel_merge.cpuCount", return_value=8):
            with mock.patch("voxel_merge.availableMemory", return_value=16 * 1024**3):
                self.assertEqual(mergeWorkers([100] * 8), 1)
                self.assertEqual(mergeWorkers([2_000_000] * 12), 8)
                with mock.patch.dict(os.environ, {"BLK_MERGE_WORKERS": "2"}):
                    self.assertEqual(mergeWorkers([2_000_000] * 12), 2)
            with mock.patch("voxel_merge.availableMemory", return_value=1024**3):
                self.assertEqual(mergeWorkers([2_000_000] * 12), 1)
            with mock.patch("voxel_merge.availableMemory", return_value=0):
                self.assertEqual(mergeWorkers([2_000_000] * 12), 1)


if __name__ == "__main__":
    unittest.main()
