"""Exact fit reuse must bind every input and retain target lifetime and decisions."""
import copy
import json
import os
os.environ['OPENBLAS_NUM_THREADS'] = '1'
from pathlib import Path
import sys
import tempfile
import unittest
from collections import OrderedDict
from unittest.mock import patch, Mock

import numpy as np
import open3d as o3d

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_fine import FineMatcher
from point_to_plane import PointToPlaneTarget


class FineCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        uv = np.random.default_rng(12).uniform(-2, 2, (600, 2))
        points = np.concatenate([np.column_stack((uv, np.full(len(uv), -1.))),
            np.column_stack((uv[:,0], np.full(len(uv), 2.), uv[:,1])),
            np.column_stack((np.full(len(uv), 2.), uv))])
        self.cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        self.cloud.normals = o3d.utility.Vector3dVector(np.repeat([[0,0,1],[0,1,0],[1,0,0]], len(uv), axis=0))
        self.source = points + [.002, -.001, .001]

    def tearDown(self):
        self.tmp.cleanup()

    def matcher(self, cloud=None, caching=True):
        m = FineMatcher.__new__(FineMatcher)
        m.workers = 2; m.target = None; m.target_pose = None
        m.target_uses = 0; m.target_points = 0; m.target_digest = None
        m.result_cache = self.folder if caching else None
        m.crop = Mock(return_value=copy.deepcopy(cloud if cloud is not None else self.cloud))
        return m

    def test_exact_reuse_across_restart_and_target_lifetime(self):
        reference = self.matcher(caching=False).align(self.source, np.eye(4))
        m = self.matcher(); first = m.align(self.source, np.eye(4))
        np.testing.assert_array_equal(first[0], reference[0])
        for key in ('converged','iterations','correspondences','robust_support'):
            self.assertEqual(first[1][key], reference[1][key])
        m = self.matcher()
        with patch.object(PointToPlaneTarget, 'align', side_effect=AssertionError('Repeated exact solver call')):
            for _ in range(7):
                T, info = m.align(self.source, np.eye(4))
                np.testing.assert_array_equal(T, reference[0])
                self.assertTrue(info['result_reused'])
        self.assertEqual(m.crop.call_count, 2)
        self.assertEqual(m.target_uses, 2)

    def test_source_pose_target_normals_and_iterations_invalidate(self):
        self.matcher().align(self.source, np.eye(4))
        shifted = self.source.copy(); shifted[0,0] += .001
        pose = np.eye(4); pose[0,3] = .001
        changed = copy.deepcopy(self.cloud)
        np.asarray(changed.points)[0,0] += .001
        normals = copy.deepcopy(self.cloud)
        np.asarray(normals.normals)[0] = [.01,0,.99995]
        for source, initial, target, iterations in [
            (shifted,np.eye(4),None,60), (self.source,pose,None,60),
            (self.source,np.eye(4),changed,60), (self.source,np.eye(4),normals,60),
            (self.source,np.eye(4),None,180)]:
            with self.subTest(iterations=iterations):
                _, info = self.matcher(target).align(source, initial, iterations)
                self.assertFalse(info['result_reused'])

    def test_failed_fit_and_corrupt_cache(self):
        source = self.source + [20,0,0]
        T, info = self.matcher().align(source, np.eye(4), iterations=1)
        self.assertFalse(info['converged'])
        repeated = self.matcher().align(source, np.eye(4), iterations=1)
        self.assertFalse(repeated[1]['converged'])
        self.assertTrue(repeated[1]['result_reused'])
        path = self.folder/(info['request_key']+'.json')
        path.write_text('{truncated')
        restored = self.matcher().align(source, np.eye(4), iterations=1)
        self.assertFalse(restored[1]['result_reused'])
        np.testing.assert_array_equal(restored[0], T)
        self.assertEqual(json.loads(path.read_text())['fine']['converged'], False)

    def test_tile_cache_evicts_only_cold_tiles_and_keeps_original_points(self):
        m=FineMatcher.__new__(FineMatcher)
        m.folder=self.folder;m.tile_cache=OrderedDict();m.cache_bytes=0;m.cache_limit=72
        for i in range(4):
            np.full((3,3),i,dtype='<f4').tofile(self.folder/(str(i)+'.bin'))
        with patch('trajectory_fine.cacheBudget',return_value=72):
            first=m._tile('0');m._tile('1')
            self.assertIs(m._tile('0'),first)
            m._tile('2')
            self.assertEqual(list(m.tile_cache),['0','2'])
            self.assertEqual(m.cache_bytes,72)
            np.testing.assert_array_equal(m._tile('1'),np.ones((3,3),dtype=np.float32))
        with patch('trajectory_fine.cacheBudget',return_value=12):
            np.testing.assert_array_equal(m._tile('3'),np.full((3,3),3,dtype=np.float32))
            self.assertNotIn('3',m.tile_cache)


if __name__ == '__main__':
    unittest.main()
