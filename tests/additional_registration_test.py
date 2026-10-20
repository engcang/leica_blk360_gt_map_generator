"""Exercise fixed-reference extension with the actual native registration pipeline."""
import copy
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
import register_scans as registration
from manual_placement import placementMatrix
from additional_registration import loadReference, prepareAdditional, anchorReference
from studio_worker import runRegistration


class AdditionalTests(unittest.TestCase):
    def test_reference_frame_and_source_invariance(self):
        rng = np.random.default_rng(146)
        surfaces = []
        for axis, limits in enumerate(((-3,4),(-2,3),(0,2.7))):
            for level in limits:
                xyz = rng.uniform([-3,-2,0],[4,3,2.7],(700,3))
                xyz[:,axis] = level
                surfaces.append(xyz)
        world = np.concatenate(surfaces)
        world += rng.normal(0, .001, world.shape)
        transforms = [placementMatrix(p) for p in [[12,-7,2,.18],[32,-7,2,.18],[13,-6.7,2.1,.23]]]
        with tempfile.TemporaryDirectory(prefix="blk-additional-") as temporary:
            root = Path(temporary).resolve()
            old = root / "original"; old.mkdir()
            output = root / "extended"; output.mkdir()
            report = root / "report"; report.mkdir()
            paths = []
            for i, transform in enumerate(transforms):
                scene = world + ([20,0,0] if i == 1 else [0,0,0])
                xyz = (scene - transform[:3,3]) @ transform[:3,:3]
                path = old / f"scan{i}.ply"
                values = np.arange(len(xyz),dtype=np.uint16)
                if os.environ.get("BLK_TEST_RGB") == "1":
                    values = np.column_stack([values % 256, (values * 3) % 256, (values * 7) % 256])
                registration.writePly(path, xyz, values)
                paths.append(path)
            manifest = {"schema_version":3,"units":"metres","result_name":"original","coordinate_frame":"survey_frame",
                "registration_voxel_m":.01,"scans":[{"index":i,"file":path.name,"name":path.name,
                "points":len(world),"sha256":registration.hashFile(path),"transform_map_from_scan":transforms[i].tolist()}
                for i,path in enumerate(paths[:2])]}
            source = old / "relative_poses.json"; source.write_text(json.dumps(manifest),encoding="utf-8")
            original_bytes = source.read_bytes()
            config = {"name":"extended","paths":list(map(str,paths)),"voxel_m":.01,
                "output_path":str(output/"extended_1cm.ply"),"poses_path":str(output/"relative_poses.json"),
                "review_directory":str(output),"reference_poses":str(source),"reference_sha256":registration.hashFile(source),
                "additional_initial_guesses":[[0,0,0,0],[13.02,-6.68,2.1,.235]]}
            pristine = copy.deepcopy(config)
            def forbidden(*args,**kwargs): raise AssertionError("Individual scan registration changed the reference")
            with patch.object(registration,'adjacent',forbidden), patch.object(registration,'refineGraph',forbidden):
                runRegistration(config,report)
            saved = json.loads((output/'relative_poses.json').read_text())
            self.assertEqual(saved['coordinate_frame'],'survey_frame')
            self.assertEqual(saved['reference']['fixed_scan_count'],2)
            self.assertEqual(saved['registration_mode'],'additional_rigid_reference_v1')
            for i in range(2): self.assertEqual(saved['scans'][i]['transform_map_from_scan'],manifest['scans'][i]['transform_map_from_scan'])
            np.testing.assert_allclose(saved['scans'][2]['transform_map_from_scan'],transforms[2],atol=.004)
            self.assertEqual(source.read_bytes(),original_bytes)
            self.assertTrue((output/'result_review_and_manage.html').exists())
            self.assertFalse((output/'extended_1cm.ply').exists())
            records = registration.prepare([{"output_path":str(p)} for p in paths],report)
            reference = loadReference(pristine)
            bad = copy.deepcopy(records); bad[0]['source_sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'changed'): prepareAdditional(pristine,bad,reference)
            with self.assertRaisesRegex(ValueError,'changed'): loadReference({**pristine,'reference_sha256':'0'*64})
            with self.assertRaisesRegex(ValueError,'cannot refine'): loadReference({**pristine,'refine_individual_scans':True})
            with self.assertRaisesRegex(ValueError,'separate'): loadReference({**pristine,'poses_path':str(source)})
            with self.assertRaisesRegex(ValueError,'distinct'): loadReference({**pristine,'paths':[str(paths[0]),str(paths[1]),str(paths[0])]})
            # Reframing a resumed merged chunk must recover the original arbitrary map frame.
            offset = placementMatrix([21,12,-4,.3])
            reframed = {i:offset@t for i,t in enumerate(transforms)}
            _, final = anchorReference(config,reframed)
            for i in range(2): np.testing.assert_array_equal(final[i],transforms[i])
            reframed[1][0,3] += .1
            with self.assertRaisesRegex(ValueError,'geometry changed'): anchorReference(config,reframed)
            # Without manual guesses the worker must use KISS to initialize the new scan.
            automatic = copy.deepcopy(pristine); automatic['additional_initial_guesses'][1]=None
            prepareAdditional(automatic,records,reference)
            from rigid_chunks import validateChunks
            groups,_,placed = validateChunks(automatic,records)
            self.assertEqual(groups,[[0,1],[2]])
            self.assertEqual(set(placed),{0})
            # Exercise disconnected publication and a resumed arrangement in component coordinates.
            partial_output = root/'partial'; partial_output.mkdir()
            partial_report = root/'partial-report'; partial_report.mkdir()
            partial = copy.deepcopy(pristine)
            partial.update(output_path=str(partial_output/'extended_1cm.ply'), poses_path=str(partial_output/'relative_poses.json'),review_directory=str(partial_output))
            partial['additional_initial_guesses'][1] = [400,0,0,0]
            runRegistration(partial,partial_report)
            self.assertFalse((partial_output/'relative_poses.json').exists())
            self.assertTrue((partial_output/'chunks_review.html').exists())
            chunks = json.loads((partial_output/'partial_chunks.json').read_text())['chunks']
            self.assertEqual([chunk['rows'] for chunk in chunks],[[1,2],[3]])
            resumed = copy.deepcopy(pristine)
            resumed.update(output_path=partial['output_path'],poses_path=partial['poses_path'],review_directory=partial['review_directory'],manual_chunks=chunks)
            relative = np.linalg.inv(transforms[0]) @ transforms[2]
            resumed['chunk_initial_guesses'] = {chunks[0]['key']:[0,0,0,0],chunks[1]['key']:[relative[0,3]+.02,relative[1,3]+.02,relative[2,3],float(np.arctan2(relative[1,0],relative[0,0]))+.005]}
            runRegistration(resumed,partial_report)
            if (partial_output/'relative_poses.json').exists():
                extended=json.loads((partial_output/'relative_poses.json').read_text())
                for i in range(2): self.assertEqual(extended['scans'][i]['transform_map_from_scan'],manifest['scans'][i]['transform_map_from_scan'])
            else:
                # Native convergence may reject an otherwise close synthetic fit: retain inspectable chunks.
                audit=json.loads((partial_report/'rigid_chunks/rigid_chunks_v1/result.json').read_text())
                self.assertFalse(audit['coarse_attempts'][0]['converged'])
                self.assertFalse(audit['coarse_attempts'][0]['accepted'])
                self.assertEqual(json.loads((partial_report/'progress.json').read_text())['status'],'needs_connections')
                updated=json.loads((partial_output/'partial_chunks.json').read_text())['chunks']
                fixed=updated[0]['scans']
                np.testing.assert_allclose(np.linalg.inv(fixed[0]['transform_chunk_from_scan'])@fixed[1]['transform_chunk_from_scan'],
                    np.linalg.inv(transforms[0])@transforms[1],atol=1e-8)
            self.assertEqual(source.read_bytes(),original_bytes)
            # A single-reference automatic case runs real KISS-Matcher, then the same local refinement.
            single = copy.deepcopy(manifest); single['scans']=single['scans'][:1]
            single_file=old/'single.json'; single_file.write_text(json.dumps(single),encoding='utf-8')
            auto_output=root/'automatic'; auto_output.mkdir(); auto_report=root/'auto-report'; auto_report.mkdir()
            automatic={**pristine,'paths':[str(paths[0]),str(paths[2])],'reference_poses':str(single_file),
                'reference_sha256':registration.hashFile(single_file),'additional_initial_guesses':[[0,0,0,0],None],
                'output_path':str(auto_output/'extended_1cm.ply'),'poses_path':str(auto_output/'relative_poses.json'),'review_directory':str(auto_output)}
            import rigid_chunks
            with patch('rigid_chunks.align',wraps=rigid_chunks.align) as align:
                runRegistration(automatic,auto_report)
                self.assertTrue(any(call.args[0]=='kiss' for call in align.call_args_list))
            automated=json.loads((auto_output/'relative_poses.json').read_text())
            self.assertEqual(automated['scans'][0]['transform_map_from_scan'],single['scans'][0]['transform_map_from_scan'])
            np.testing.assert_allclose(automated['scans'][1]['transform_map_from_scan'],transforms[2],atol=.004)
            print('PASS: real additional Nano-GICP/1 cm ICP/PGO; exact reference poses/frame, source guards, final review and poses-only output.')


if __name__ == '__main__': unittest.main()
