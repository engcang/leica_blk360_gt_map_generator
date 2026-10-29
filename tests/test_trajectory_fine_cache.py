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
from unittest.mock import patch

import numpy as np
import open3d as o3d

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_fine import FineMatcher, TiledMapIndex, array_digest
from point_to_plane import PointToPlaneTarget, IndexedPointToPlaneTarget


class FineCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.indices = []
        uv = np.random.default_rng(12).uniform(-2, 2, (600, 2))
        points = np.concatenate([np.column_stack((uv, np.full(len(uv), -1.))),
            np.column_stack((uv[:,0], np.full(len(uv), 2.), uv[:,1])),
            np.column_stack((np.full(len(uv), 2.), uv))])
        self.cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        self.cloud.normals = o3d.utility.Vector3dVector(np.repeat([[0,0,1],[0,1,0],[1,0,0]], len(uv), axis=0))
        self.source = points + [.002, -.001, .001]

    def tearDown(self):
        for index in self.indices:
            index.close()
        self.tmp.cleanup()

    def matcher(self, cloud=None, caching=True):
        m = FineMatcher.__new__(FineMatcher)
        cloud = cloud if cloud is not None else self.cloud
        points, normals = np.asarray(cloud.points).astype('<f4'), np.asarray(cloud.normals)
        m.target_digest = array_digest(points, normals)
        m.folder = self.folder/m.target_digest
        m.folder.mkdir(exist_ok=True)
        normal_folder = m.folder/'normals-4cm-40-v1'
        normal_folder.mkdir(exist_ok=True)
        m.index = dict(tile_m=8., tiles={})
        tiles = np.floor(points/8).astype(int)
        for tile in np.unique(tiles, axis=0):
            key = '_'.join(map(str, tile))
            selected = np.all(tiles == tile, axis=1)
            points[selected].tofile(m.folder/(key+'.bin'))
            np.save(normal_folder/(key+'.npy'), normals[selected])
            m.index['tiles'][key] = int(selected.sum())
        m.workers = 2
        m.map_index = TiledMapIndex(m)
        self.indices.append(m.map_index)
        m.target = IndexedPointToPlaneTarget(m.map_index, m.workers)
        m.target_uses = 0; m.target_points = len(points)
        m.result_cache = self.folder/'requests' if caching else None
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
        self.assertEqual(m.map_index.statistics['tree_builds'], 0)
        self.assertEqual(m.target_uses, 7)

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
        path = self.folder/'requests'/(info['request_key']+'.json')
        path.write_text('{truncated')
        restored = self.matcher().align(source, np.eye(4), iterations=1)
        self.assertFalse(restored[1]['result_reused'])
        np.testing.assert_array_equal(restored[0], T)
        self.assertEqual(json.loads(path.read_text())['fine']['converged'], False)

    def test_changed_correspondence_gates_cannot_reuse_old_fit(self):
        matcher = self.matcher()
        matcher.align(self.source, np.eye(4))
        _, info = matcher.align(self.source, np.eye(4), distances=(.1,))
        self.assertFalse(info['result_reused'])
        _, cached = matcher.align(self.source, np.eye(4), distances=(.1,))
        self.assertTrue(cached['result_reused'])

    def test_information_capture_is_bound_into_request_cache(self):
        plain = self.matcher().align(self.source,np.eye(4))
        actual = self.matcher().align(self.source,np.eye(4),capture_information=True)
        self.assertFalse(actual[1]['result_reused'])
        self.assertNotEqual(plain[1]['request_key'],actual[1]['request_key'])
        self.assertEqual(actual[1]['optimizer_information']['observable_directions'],6)
        repeated = self.matcher().align(self.source,np.eye(4),capture_information=True)
        self.assertTrue(repeated[1]['result_reused'])
        self.assertEqual(actual[1]['optimizer_information'],repeated[1]['optimizer_information'])

    def test_read_only_previous_cache_reuses_only_exact_requests(self):
        m = self.matcher()
        expected, first = m.align(self.source, np.eye(4))
        original = {p:p.read_bytes() for p in m.result_cache.glob('*.json')}
        m.result_sources = (m.result_cache,)
        m.result_cache = self.folder/'new-requests'
        with patch.object(PointToPlaneTarget, 'align', side_effect=AssertionError('Repeated old fit')):
            actual, info = m.align(self.source, np.eye(4))
        np.testing.assert_array_equal(actual, expected)
        self.assertTrue(info['result_reused'])
        changed = np.eye(4); changed[0,3] = .001
        _, info = m.align(self.source, changed)
        self.assertFalse(info['result_reused'])
        self.assertEqual({p:p.read_bytes() for p in original}, original)
        self.assertEqual(len(list(m.result_cache.glob('*.json'))), 1)

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
