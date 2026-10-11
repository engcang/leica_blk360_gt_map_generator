import base64
import gzip
import json
import pathlib
import re
import tempfile
import unittest

import numpy as np

from export_dataset import saveRegistrationPoses
from register_scans import hashFile, readPly, writePly
from result_products import generateProduct
from result_page import writeProductSnapshot


class ProductTests(unittest.TestCase):
    def makeResult(self, _root):
        points = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 1]], dtype=float)
        values = np.array([0, 100, 32768, 65535])
        rotation = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
        pose = np.eye(4)
        pose[:3, :3] = rotation
        pose[:3, 3] = [10, 20, 3]
        scans = []
        for index, transform in enumerate((np.eye(4), pose)):
            path = _root / f"scan {index}.ply"
            writePly(path, points, values)
            scans.append({"index": index, "source_path": str(path), "name": path.name,
                          "source_sha256": hashFile(path), "original_points": len(points),
                          "transform_map_from_scan": transform.tolist()})
        result = {"scans": scans, "coordinate_frame": "First scan", "registration_voxel_m": 0.01}
        poses = saveRegistrationPoses(result, _root / "relative_poses.json", "Test map")
        return poses, points, values, pose

    def testRawPreservesCoordinatesIntensityOrderAndOriginals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            poses, points, values, pose = self.makeResult(root)
            original = poses.read_bytes()
            hashes = [hashFile(path) for path in sorted(root.glob("*.ply"))]
            product = generateProduct(poses, "raw", root / "work", lambda _message: None)
            raw = readPly(pathlib.Path(product["output_path"]))
            xyz = np.column_stack([raw[axis] for axis in ("x", "y", "z")])
            np.testing.assert_array_equal(xyz, np.vstack([points, points @ pose[:3, :3].T + pose[:3, 3]]))
            np.testing.assert_array_equal(raw["intensity"], np.tile(values, 2))
            del raw
            self.assertEqual(product["points"], 8)
            self.assertIsNone(product["voxel_m"])
            snapshot = (root / "html_source/products.js").read_text(encoding="utf-8")
            self.assertIn("Test map_raw.ply", snapshot)
            self.assertNotIn(str(root), snapshot)
            self.assertEqual(poses.read_bytes(), original)
            self.assertEqual(hashes, [hashFile(path) for path in sorted(root.glob("scan*.ply"))])
            with self.assertRaises(FileExistsError):
                generateProduct(poses, "raw", root / "work", lambda _message: None)
            self.assertFalse(list(root.glob(".product-*")))

    def testOfflineSnapshotOmitsRemovedAndOutsideFiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "html_source").mkdir()
            (root / "html_source/products.json").write_text(json.dumps({"raw": {"file": "missing.ply", "bytes": 14, "points": 1},
                                                                      "voxel": {"file": "../outside.ply", "bytes": 14, "points": 1}}), encoding="utf-8")
            writeProductSnapshot(root)
            self.assertEqual((root / "html_source/products.js").read_text(), "window.BLK_SAVED_PRODUCTS = {};\n")

    def testShareAloneDoesNotPublishIntermediatePly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            poses, _, _, _ = self.makeResult(root)
            product = generateProduct(poses, "share", root / "work", lambda _message: None)
            self.assertFalse((root / "Test map_1cm.ply").exists())
            text = pathlib.Path(product["output_path"]).read_text(encoding="utf-8")
            blocks = re.findall(r'<script id="ply-block-\d+" type="application/octet-stream">([^<]+)</script>', text)
            embedded = b"".join(gzip.decompress(base64.b64decode(block)) for block in blocks)
            self.assertIn(b"element vertex 8\n", embedded)
            voxel = generateProduct(poses, "voxel", root / "work", lambda _message: None)
            self.assertEqual(voxel["voxel_m"], 0.01)
            self.assertEqual(voxel["points"], 8)
            metadata = json.loads((root / "html_source/products.json").read_text())
            self.assertEqual(set(metadata), {"share", "voxel"})
            self.assertFalse(list(root.glob(".product-*")))

    def testChangedSourceAndNonrigidPoseAreRejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            poses, _, _, _ = self.makeResult(root)
            source = root / "scan 0.ply"
            data = source.read_bytes()
            source.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            with self.assertRaisesRegex(ValueError, "changed since registration"):
                generateProduct(poses, "raw", root / "work", lambda _message: None)
            source.write_bytes(data)
            manifest = json.loads(poses.read_text())
            manifest["scans"][1]["transform_map_from_scan"][0][0] = 2
            poses.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Invalid rigid"):
                generateProduct(poses, "raw", root / "work", lambda _message: None)
            self.assertFalse((root / "Test map_raw.ply").exists())
            self.assertFalse(list(root.glob(".product-*")))


if __name__ == "__main__":
    unittest.main()
