"""Chunk frame refresh preserves user data and never reuses stale transforms."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mapping"))
from chunk_placements import refreshChunkPlacements
from json_io import saveJson


class ChunkPlacementTests(unittest.TestCase):
    def test_changed_frames_and_unchanged_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chunks = [{"key": "a", "scans": [1]}, {"key": "b", "scans": [2]}]
            refreshChunkPlacements(root, chunks)
            path = root / "chunk_initial_placements.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["poses"], {"a": None, "b": None})
            doc["poses"] = {"a": [0,0,0,0], "b": [2,3,4,.5]}
            saveJson(path, doc)
            before = path.read_bytes()
            refreshChunkPlacements(root, chunks)
            self.assertEqual(path.read_bytes(), before)
            # Same count, new geometry: old poses must not bind by row number.
            changed = [{"key": "new-a"}, {"key": "new-b"}]
            refreshChunkPlacements(root, changed)
            self.assertEqual(json.loads(path.read_text())["poses"], {"new-a": None, "new-b": None})
            backups = list((root / "html_source/placement_history").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), doc)
            refreshChunkPlacements(root, changed)
            self.assertEqual(len(list(backups[0].parent.glob("*.json"))), 1)
            # A later merge also regenerates the set, without archiving empty poses.
            refreshChunkPlacements(root, [{"key": "merged"}])
            self.assertEqual(json.loads(path.read_text())["poses"], {"merged": None})
            self.assertEqual(len(list(backups[0].parent.glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
