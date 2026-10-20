"""Real native chunk alignment; assert fixed internal geometry and no scan registration."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("BLK_CPU_THREADS", "4")
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo / "mapping"))
os.environ.setdefault("BLK_REGISTRATION_EXECUTABLE", str(repo / "install/mapping/bin/blk_registration.exe"))
import numpy as np
import open3d as o3d
from manual_placement import placementMatrix
import register_scans as registration
from rigid_chunks import validateChunks, composeScans, runRigidChunks, optimizeChunks
from studio_worker import runRegistration


class RigidChunkTests(unittest.TestCase):
    def test_real_alignment_and_internal_invariance(self):
        rng = np.random.default_rng(146)
        surfaces = []
        for axis, limits in enumerate(((-3,4),(-2,3),(0,2.7))):
            for level in limits:
                xyz = rng.uniform([-3,-2,0],[4,3,2.7],(700,3))
                xyz[:,axis] = level
                surfaces.append(xyz)
        world = np.concatenate(surfaces)
        transforms = [placementMatrix(pose) for pose in [[0,0,0,0],[.8,.3,.1,.1],[2.1,-.4,.1,-.12],[2.7,.1,.2,-.05],[3.1,.4,.15,.2],[3.6,.5,.15,.23]]]
        with tempfile.TemporaryDirectory(prefix="blk-rigid-test-") as directory:
            root = Path(directory).resolve(); report = root / "report"; report.mkdir()
            output = root / "output"; output.mkdir()
            paths = []
            for i, matrix in enumerate(transforms):
                path = root / f"scan{i}.ply"
                xyz = (world - matrix[:3,3]) @ matrix[:3,:3]
                registration.writePly(path, xyz, np.arange(len(xyz),dtype=np.uint16))
                paths.append(path)
            records = registration.prepare([{"output_path":str(p)} for p in paths],report)
            chunks = []
            for number, ids in enumerate(([0,1],[2,3],[4,5])):
                inverse = np.linalg.inv(transforms[ids[0]])
                chunks.append({"key":f"c{number}","number":number+1,"rows":[i+1 for i in ids],
                    "scans":[{"row":i+1,"name":paths[i].name,"source_sha256":records[i]["source_sha256"],
                              "transform_chunk_from_scan":(inverse@transforms[i]).tolist()} for i in ids]})
            config = {"paths":list(map(str,paths)),"voxel_m":.01,"name":"test",
                      "output_path":str(output/"test_1cm.ply"),"poses_path":str(output/"relative_poses.json"),
                      "review_directory":str(output),"manual_chunks":chunks,
                      "chunk_initial_guesses":{"c0":[0,0,0,0],"c1":[2.12,-.38,.1,-.115],"c2":[3.12,.38,.15,.205]}}
            # Populate the two real internal edges to evaluate preserved scan geometry.
            (report/"pairs").mkdir()
            for i,j in [(0,1),(2,3),(4,5)]:
                (report/"pairs"/f"{i:02d}_{j:02d}.json").write_text(json.dumps({"source":i,"target":j,"accepted":True}))
            def forbidden(*args,**kwargs):
                raise AssertionError("Rigid mode entered an individual-scan registration path")
            with patch.object(registration,'registerPair',forbidden), patch.object(registration,'adjacent',forbidden), patch.object(registration,'optimize',forbidden), patch.object(registration,'refineGraph',forbidden):
                runRegistration(config,report)
            result = json.loads((report/'registration.json').read_text())
            self.assertEqual(result['registration_mode'],'rigid_chunks_v1')
            self.assertTrue(result['internal_scan_poses_fixed'])
            final = [np.asarray(s['transform_map_from_scan']) for s in result['scans']]
            for group in ([0,1],[2,3],[4,5]):
                np.testing.assert_allclose(np.linalg.inv(final[group[0]])@final[group[1]],
                    np.linalg.inv(transforms[group[0]])@transforms[group[1]],atol=1e-10)
            for actual, expected in zip(final,transforms):
                np.testing.assert_allclose(actual,expected,atol=.003)
            graph = o3d.io.read_pose_graph(str(report/'rigid_chunks/rigid_chunks_v1/fine_graph_0.json'))
            self.assertEqual(len(graph.nodes),3)
            manifest=json.loads((output/'relative_poses.json').read_text())
            self.assertTrue(manifest['internal_scan_poses_fixed'])
            self.assertTrue((output/'result_review_and_manage.html').is_file())
            self.assertTrue((report/'quality.json').is_file())
            edge=result['chunk_constraints'][0]
            self.assertLessEqual(len(edge['verification_audit']),12)
            # Unchanged inputs reuse the chunk pair; no new native alignment call.
            from rigid_chunks import runRigidChunks
            with patch('rigid_chunks.refine',forbidden):
                again=runRigidChunks(config,records,report,lambda *args:None,forbidden)
            np.testing.assert_allclose(again['scans'][3]['transform_map_from_scan'],final[3],atol=1e-10)
            # Distant manual chunks are not forced together; viewer groups remain inspectable.
            disconnected={**config,'chunk_initial_guesses':{'c0':[0,0,0,0],'c1':[200,0,0,0],'c2':[400,0,0,0]}}
            published=[]
            with patch.object(registration,'registerPair',forbidden):
                partial=runRigidChunks(disconnected,records,report,lambda *args:None,published.append)
            self.assertIsNone(partial)
            self.assertEqual(len(published),1)
            from studio_worker import connectedGroups
            self.assertEqual(connectedGroups(published[0],6),[[1,2],[3,4],[5,6]])
            # Publish actual disconnected viewers and a fresh editable chunk manifest.
            from partial_chunks import createChunkReview
            publish_result=createChunkReview(records,published[0],root/'partial',report,lambda *args:None,
                pose_method='Rigid chunk alignment and chunk PGO; internal scan transforms fixed')
            self.assertEqual(publish_result['chunk_count'],3)
            new_manifest=json.loads((root/'partial/partial_chunks.json').read_text())
            for chunk in new_manifest['chunks']:
                self.assertIn('internal scan transforms fixed',chunk['pose_method'])
                a,b=chunk['scans']
                i,j=a['row']-1,b['row']-1
                np.testing.assert_allclose(np.linalg.inv(a['transform_chunk_from_scan'])@b['transform_chunk_from_scan'],
                    np.linalg.inv(transforms[i])@transforms[j],atol=1e-10)
            self.assertTrue((root/'partial/chunk_initial_placements.json').is_file())
            # The explicit opt-in is the only path allowed to invoke scan ICP/PGO.
            optional=runRigidChunks({**config,'refine_individual_scans':True},records,report,lambda *args:None,forbidden)
            self.assertFalse(optional['internal_scan_poses_fixed'])
            self.assertEqual(optional['registration_mode'],'rigid_chunks_then_individual_scans')
            self.assertFalse(json.loads((report/'poses.json').read_text())['internal_scan_poses_fixed'])
            # Reject missing/duplicate members before touching native processing.
            duplicate={**config,'manual_chunks':[chunks[0],chunks[0]]}
            with self.assertRaises(ValueError): validateChunks(duplicate,records)
            print('PASS: actual Nano-GICP/1 cm chunk ICP/PGO, 3 graph nodes for 6 scans, exact internal transforms, bounded fixed-pose checks, final exports/review, cache reuse, disconnected fallback.')


if __name__=='__main__': unittest.main()
