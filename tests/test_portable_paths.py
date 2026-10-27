"""Relocation keeps scan identity, chunk frames, and saved poses intact."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mapping"))
from portable_paths import rebaseChunks, resolvePath
from chunk_placements import refreshChunkPlacements

spec = importlib.util.spec_from_file_location("migration", Path(__file__).resolve().parents[1] / "scripts/migrate_registration_paths.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class PortablePathsTests(unittest.TestCase):
    def test_local_copy_wins_over_still_accessible_old_mount(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root/'current/environment/output';current.mkdir(parents=True)
            old = root/'old/environment/output';old.mkdir(parents=True)
            (current/'Scan.ply').write_bytes(b'exact original points')
            (old/'Scan.ply').write_bytes(b'exact original points')
            file = current/'initial_placements.json'
            # Cover both an original absolute path and an already-converted
            # relative path that incorrectly reaches the still-mounted copy.
            for key in (str(old/'Scan.ply'), '../../../old/environment/output/Scan.ply'):
                file.write_text(json.dumps(dict(poses={key:[1,2,3,4]})))
                result = migration.migrate(root/'current', True)
                self.assertEqual(result['converted_paths'], 1)
                self.assertIsNotNone(result['changes'][0]['verified_content_sha256'])
                self.assertEqual(json.loads(file.read_text())['poses'], {'Scan.ply':[1,2,3,4]})
            (old/'Scan.ply').write_bytes(b'different scan points')
            file.write_text(json.dumps(dict(poses={str(old/'Scan.ply'):[1,2,3,4]})))
            with self.assertRaisesRegex(ValueError, 'Local copy differs'):
                migration.migrate(root/'current', True)

    def test_migration_backup_nested_base_and_idempotence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "environment/output"
            nested = output / "html_source/chunks/frame"
            nested.mkdir(parents=True)
            scan = output / "Scan.ply"
            scan.write_bytes(b"unchanged scan")
            chunk = dict(key="frame", scans=[dict(source_path=str(scan), transform=[1, 2, 3])])
            (nested / "chunk.json").write_text(json.dumps(chunk))
            placements = output / "initial_placements.json"
            placements.write_text(json.dumps(dict(poses={r"C:\old\environment\output\Scan.ply": [1, 2, 3, 4]})))
            before = {p: p.read_bytes() for p in output.rglob("*.json")}
            dry = migration.migrate(root)
            self.assertEqual(dry["converted_paths"], 2)
            self.assertTrue(all(p.read_bytes() == b for p, b in before.items()))
            result = migration.migrate(root, True)
            with zipfile.ZipFile(result["backup"]) as archive:
                for p, b in before.items():
                    self.assertEqual(archive.read(p.relative_to(root).as_posix()), b)
            self.assertEqual(json.loads(placements.read_text())["poses"], {"Scan.ply": [1, 2, 3, 4]})
            self.assertEqual(json.loads((nested / "chunk.json").read_text())["scans"][0]["source_path"], "../../../Scan.ply")
            moved = root / "moved"
            shutil.move(str(output), moved)
            new_json = moved / "html_source/chunks/frame/chunk.json"
            self.assertEqual(resolvePath(json.loads(new_json.read_text())["scans"][0]["source_path"], new_json.parent).read_bytes(), b"unchanged scan")
            self.assertEqual(migration.migrate(root)["converted_paths"], 0)

    def test_path_conversion_does_not_reset_chunk_poses(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            chunk = dict(key="frame", scans=[dict(source_path=str(output / "Scan.ply"))], viewer="view.html")
            path = output / "chunk_initial_placements.json"
            pose = [1, 2, 3, .4]
            path.write_text(json.dumps(dict(chunks=[chunk], poses={"frame": pose})))
            relative = rebaseChunks([chunk], output, output)
            refreshChunkPlacements(output, relative)
            doc = json.loads(path.read_text())
            self.assertEqual(doc["poses"]["frame"], pose)
            self.assertEqual(doc["chunks"][0]["scans"][0]["source_path"], "Scan.ply")
            refreshChunkPlacements(output, [dict(key="new")])
            archive = next((output / "html_source/placement_history").glob("*.json"))
            archived = json.loads(archive.read_text())["chunks"][0]
            self.assertEqual(resolvePath(archived["scans"][0]["source_path"], archive.parent), output / "Scan.ply")
            self.assertEqual(resolvePath(archived["viewer"], archive.parent), output / "view.html")


if __name__ == "__main__":
    unittest.main()
