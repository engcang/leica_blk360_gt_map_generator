"""Colour preservation across registration products without modifying geometry."""
import base64
import gzip
import json
import pathlib
import re
import tempfile
import unittest
import numpy as np
from register_scans import readPly, writePly, hashFile, merge
from ply_io import colourValues
from result_products import mergeRaw
from share_map import createShareViewer

class RgbTests(unittest.TestCase):
    def testRawAndVoxelProductsKeepRgbWithoutIntensity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            xyz = np.array([[0, 0, 0], [.002, 0, 0], [1, 1, 1]], float)
            rgb = np.array([[10, 20, 30], [30, 40, 50], [255, 0, 128]], np.uint8)
            source = root / "source.ply"
            writePly(source, xyz, rgb)
            pose = np.eye(4); pose[:3, 3] = [2, 3, 4]
            registration = {"scans": [{"source_path": str(source), "source_sha256": hashFile(source),
                "original_points": 3, "name": source.name, "transform_map_from_scan": pose.tolist()}]}
            target = root / "raw.ply"
            mergeRaw(registration, target, lambda _: None)
            raw = readPly(target)
            self.assertNotIn("intensity", raw.dtype.names)
            np.testing.assert_array_equal(colourValues(raw), rgb)
            np.testing.assert_allclose(np.column_stack([raw[a] for a in "xyz"]), xyz + [2,3,4], atol=1e-6)
            del raw
            merge(registration, root, .01, "voxel.ply", False)
            raw = readPly(root / "voxel.ply")
            self.assertEqual(len(raw), 2)
            self.assertEqual(sorted(map(tuple, colourValues(raw))), [(20,30,40),(255,0,128)])
            del raw
            page = root / "share.html"
            createShareViewer(target, page)
            chunks = re.findall(r'<script id="ply-block-\d+" type="application/octet-stream">([^<]+)</script>', page.read_text(encoding="utf8"))
            self.assertEqual(b"".join(gzip.decompress(base64.b64decode(c)) for c in chunks), target.read_bytes())

    def testMixedProductsAndTruncatedRgbAreRejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            scans = []
            for n, values in enumerate((np.array([[1,2,3]]), np.array([123]))):
                path = root / f"{n}.ply"
                writePly(path, np.zeros((1,3)), values)
                scans.append({"source_path": str(path), "original_points": 1, "source_sha256": hashFile(path), "name":path.name, "transform_map_from_scan": np.eye(4).tolist()})
            with self.assertRaisesRegex(ValueError, "mix"):
                mergeRaw({"scans": scans}, root / "mixed.ply", lambda _: None)
            self.assertFalse((root / "mixed.ply").exists())
            path = root / "0.ply"
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaises(ValueError): readPly(path)

if __name__ == "__main__": unittest.main()
