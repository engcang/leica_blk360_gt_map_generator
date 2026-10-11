import json
import pathlib
import shutil
import tempfile
import unittest
import zipfile

import numpy as np

from export_dataset import exportDataset, saveLocalPoses, saveMapPoses
from register_scans import hashFile, writePly


class ExportTests(unittest.TestCase):
    def testMapAndTransformsWithoutReadingOriginals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            records = root / "map.registration"
            records.mkdir()
            scans = []
            for index in range(2):
                folder = root / f"input{index}"
                folder.mkdir()
                path = folder / "Same name.ply"
                writePly(path, np.array([[index, 2, 3]], dtype=np.float32), np.array([4321]))
                transform = np.eye(4)
                transform[:3, 3] = [index, index + 1, -index]
                scans.append({"index": index, "source_path": str(path), "original_points": 1,
                              "source_sha256": hashFile(path), "transform_map_from_scan": transform.tolist()})
            registration = {"coordinate_frame": "first_scanner", "registration_voxel_m": 0.01, "scans": scans}
            (records / "registration.json").write_text(json.dumps(registration))
            map_path = root / "map_1cm.ply"
            writePly(map_path, np.array([[1, 2, 3]], dtype=np.float32), np.array([5678]))
            (records / "map.json").write_text(json.dumps({"file": "../map_1cm.ply", "voxel_m": 0.01, "sha256": hashFile(map_path)}))
            saveLocalPoses(registration, records)
            local = json.loads((records / "poses.json").read_text())
            self.assertEqual((records / local["scans"][0]["file"]).resolve(), pathlib.Path(scans[0]["source_path"]))
            metadata = json.loads((records / "map.json").read_text())
            manifest_path = saveMapPoses(registration, metadata, map_path)
            manifest = json.loads(manifest_path.read_text())
            self.assertEqual(manifest_path.name, "map_relative_poses.json")
            self.assertEqual(manifest["map"]["file"], "map_1cm.ply")
            self.assertFalse(manifest["source_files_included"])
            with self.assertRaises(FileExistsError):
                saveMapPoses(registration, metadata, map_path)
            #Codex: Export must succeed even if originals have been moved offline.
            for scan in scans:
                pathlib.Path(scan["source_path"]).unlink()
            destination = root / "map.dataset.zip"
            legacy = root / "legacy.zip"
            exportDataset(records, legacy, lambda _stage: None)
            #Codex: The published map and manifest must be sufficient after cache cleanup.
            self.assertTrue(records.resolve().is_relative_to(root))
            shutil.rmtree(records)
            exportDataset(manifest_path, destination, lambda _stage: None)
            with zipfile.ZipFile(destination) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()), {"map_1cm.ply", "map_relative_poses.json"})
                self.assertEqual(archive.read("map_1cm.ply"), map_path.read_bytes())
                manifest = json.loads(archive.read("map_relative_poses.json"))
                self.assertNotIn(str(root), archive.read("map_relative_poses.json").decode())
                for expected, actual in zip(scans, manifest["scans"]):
                    self.assertEqual((root / actual["file"]).resolve(), pathlib.Path(expected["source_path"]))
                    self.assertEqual(actual["sha256"], expected["source_sha256"])
                    self.assertEqual(actual["transform_map_from_scan"], expected["transform_map_from_scan"])
                self.assertNotEqual(manifest["scans"][0]["file"], manifest["scans"][1]["file"])
            with self.assertRaises(FileExistsError):
                exportDataset(manifest_path, destination, lambda _stage: None)
            changed = map_path
            changed.write_bytes(changed.read_bytes() + b"changed")
            bad = root / "changed.zip"
            with self.assertRaisesRegex(ValueError, "changed since"):
                exportDataset(manifest_path, bad, lambda _stage: None)
            self.assertFalse(bad.exists())
            self.assertFalse(bad.with_suffix(".zip.pending").exists())


if __name__ == "__main__":
    unittest.main()
