"""Chunk-only publication and rigid extension keep original geometry and files."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("BLK_CPU_THREADS", "2")
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "mapping"))
os.environ.setdefault("BLK_REGISTRATION_EXECUTABLE", str(repo / "build/local/native/blk_registration"))
import numpy as np
from additional_registration import loadReference, prepareAdditional
from chunk_result import loadChunkReference
from manual_placement import placementMatrix
from result_products import loadRegistration
from studio_worker import runRegistration
import register_scans as registration


class ChunkActions(unittest.TestCase):
    def test_finalize_and_extend_disconnected_chunks(self):
        rng = np.random.default_rng(146)
        surfaces = []
        for axis, limits in enumerate(((-3,4),(-2,3),(0,2.7))):
            for level in limits:
                xyz = rng.uniform([-3,-2,0],[4,3,2.7],(700,3)); xyz[:,axis] = level
                surfaces.append(xyz)
        world = np.concatenate(surfaces)
        world += rng.normal(0, .001, world.shape)
        poses = [placementMatrix(p) for p in ([12,-7,2,.18],[12.4,-7.1,2,.2],[13,-6.7,2.1,.23],[12.8,-6.5,2,.21])]
        with tempfile.TemporaryDirectory(prefix="blk-chunk-actions-") as temporary:
            root = Path(temporary)
            paths = []
            for i, pose in enumerate(poses):
                path = root / f"scan{i}.ply"
                registration.writePly(path, (world-pose[:3,3])@pose[:3,:3], np.arange(len(world),dtype=np.uint16))
                paths.append(path)
            chunks = []
            for number, indices in enumerate(([0,1],[2]),1):
                chunks.append({"key": f"chunk-{number}", "number": number, "rows": [i+1 for i in indices],
                    "scans": [{"row":i+1,"name":paths[i].name,"source_path":paths[i].name,
                        "source_sha256":registration.hashFile(paths[i]),"original_points":len(world),
                        "transform_chunk_from_scan":(poses[i] if number==1 else np.eye(4)).tolist()} for i in indices]})
            source = root / "partial_chunks.json"
            source.write_text(json.dumps({"status":"disconnected_partial_result","chunks":chunks}))
            before = source.read_bytes()
            for number, chunk in enumerate(chunks):
                output=root/f"final{number}"; output.mkdir()
                report=root/f"report{number}"; report.mkdir()
                config={"chunk_source":str(source),"chunk_source_sha256":registration.hashFile(source),
                    "chunk_key":chunk["key"],"name":"chosen","poses_path":str(output/"relative_poses.json"),
                    "output_path":str(output/"chosen_1cm.ply"),"review_directory":str(output)}
                runRegistration(config,report)
                published,_=loadRegistration(output/"relative_poses.json")
                self.assertEqual(len(published["scans"]),len(chunk["scans"]))
                self.assertFalse(published["final_refinement_applied"])
                for scan,original in zip(published["scans"],chunk["scans"]):
                    self.assertEqual(scan["transform_map_from_scan"],original["transform_chunk_from_scan"])
                    self.assertFalse(Path(scan["file"]).is_absolute())
                self.assertTrue((output/"result_review_and_manage.html").is_file())
                self.assertTrue((output/"html_source/level_00.png").is_file() or list((output/"html_source").glob('level_*.png')))
                with self.assertRaises(FileExistsError): runRegistration(config,report)
            with self.assertRaisesRegex(ValueError,"changed"): loadChunkReference(source,"missing")
            output=root/"extended"; output.mkdir()
            report=root/"extended-report"; report.mkdir()
            config={"name":"extended","paths":list(map(str,paths)),"voxel_m":.01,
                "reference_poses":str(source),"reference_sha256":registration.hashFile(source),
                "additional_initial_guesses":[[0,0,0,0],[13,-6.7,2.1,.23],[12.8,-6.5,2,.21]],
                "output_path":str(output/"extended_1cm.ply"),"poses_path":str(output/"relative_poses.json"),"review_directory":str(output)}
            reference=loadReference(config)
            records=registration.prepare([{"output_path":str(path)} for path in paths],report)
            prepared=copy.deepcopy(config); prepareAdditional(prepared,records,reference)
            self.assertEqual([c["rows"] for c in prepared["manual_chunks"]],[[1,2],[3],[4]])
            bad=copy.deepcopy(prepared); bad["manual_chunks"][0]["scans"][1]["transform_chunk_from_scan"][0][3]+=.1
            with self.assertRaisesRegex(ValueError,"internal transforms changed"): prepareAdditional(bad,records,reference)
            runRegistration(config,report)
            saved=json.loads((output/"relative_poses.json").read_text())
            self.assertEqual(len(saved["scans"]),4)
            for i in (0,1): self.assertEqual(saved["scans"][i]["transform_map_from_scan"],poses[i].tolist())
            for i in (2,3): np.testing.assert_allclose(saved["scans"][i]["transform_map_from_scan"],poses[i],atol=.015)
            self.assertEqual(source.read_bytes(),before)


if __name__ == "__main__": unittest.main()
