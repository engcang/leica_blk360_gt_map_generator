"""Public sequence discovery, all-scan selection, output placement, and explicit results."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_project import discover, batch_plan, open_results, estimate_files
from trajectory_worker import inputs, publish_trajectory
from trajectory_io import ScanSource, Trajectory
from test_trajectory import fixture


class ProjectTests(unittest.TestCase):
    def test_all_scans_keeps_irregular_and_faster_than_10hz_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=fixture(root)
            scans=root/'irregular';scans.mkdir()
            stamps=[99.9,100,100.001,100.009,100.07,100.19,100.7001]
            for stamp in stamps:np.save(scans/f'{stamp:.6f}.npy',np.ones((3,3)))
            source=ScanSource(dict(path=str(scans)),Trajectory(config['trajectories'][0]))
            np.testing.assert_array_equal(source.selection(),np.arange(len(stamps)))
            self.assertLess(len(source.selection(.1)),len(stamps))
            with self.assertRaises(ValueError):source.selection(float('nan'))

    def test_discovery_and_output_are_per_sequence_and_do_not_modify_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=fixture(root)
            before=(root/'estimate.tum').read_bytes()
            found=discover(root)
            self.assertEqual(found['scans']['path'],str(root/'scans'))
            self.assertEqual(found['period_s'],0)
            self.assertEqual(found['trajectory_output'],str(root/'ground_truth_trajectory.tum'))
            found['tls_map']=config['tls_map']
            _,_,_,output,cache,ids=inputs(found)
            self.assertEqual(output,root/'.ground_truth')
            self.assertEqual(cache,output/'map_cache')
            self.assertEqual(len(ids),8)
            publish_trajectory(found,root/'estimate.tum',output)
            self.assertEqual((root/'ground_truth_trajectory.tum').read_bytes(),before)
            self.assertEqual((root/'estimate.tum').read_bytes(),before)
            self.assertNotIn('ground_truth_trajectory.tum',[Path(x['path']).name for x in estimate_files(root)])
            bad=dict(found,trajectories=[dict(path=str(root/'ground_truth_trajectory.tum'))])
            with self.assertRaisesRegex(ValueError,'replace an input'):publish_trajectory(bad,root/'estimate.tum',output)

    def test_explicit_result_parent_and_individual_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ['one','two']:
                folder=root/name;folder.mkdir();fixture(folder)
                (folder/'ground_truth_trajectory.tum').write_bytes((folder/'estimate.tum').read_bytes())
            results=open_results([root])
            self.assertEqual([r['name'] for r in results],['one','two'])
            self.assertTrue(all(len(r['scene']['trajectories'])==2 for r in results))
            selected=open_results([root/'one/estimate.tum'])
            self.assertEqual(len(selected),1)
            self.assertEqual(len(selected[0]['scene']['trajectories']),1)
            self.assertFalse((root/'one/viewer.json').exists())

    def test_batch_budget_and_duplicate_folders(self):
        configs=[dict(output_directory='/tmp/sequence-'+str(i)) for i in range(8)]
        with patch('parallel_runtime.cpuCount',return_value=28),patch('parallel_runtime.availableMemory',return_value=64*1024**3):
            budget=batch_plan(configs,16)
        self.assertGreater(budget['concurrency'],1)
        self.assertLessEqual(budget['concurrency']*budget['workers_per_sequence'],16)
        with self.assertRaisesRegex(ValueError,'more than once'):batch_plan([configs[0],configs[0]])


if __name__=='__main__':unittest.main()
