"""Native queries match the reference at boundaries, ties and low RAM budgets."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]/'mapping')]
import test_trajectory_map_index as reference
from native_correspondence import libraryPath, nativeIndexClass


@unittest.skipUnless(libraryPath().is_file(), 'Build blk_correspondence and set BLK_CORRESPONDENCE_LIBRARY')
class NativeMapTests(reference.PersistentMapTests):
    def index(self, *args, **kwargs):
        original = reference.TiledMapIndex
        with patch.object(reference, 'TiledMapIndex', nativeIndexClass(original)):
            return super().index(*args, **kwargs)

    def test_dynamic_memory_eviction_never_changes_neighbors(self):
        rng = np.random.default_rng(10)
        index = self.index(np.concatenate((rng.uniform(.1,1,(500,3)),rng.uniform(16.1,17,(500,3)))))
        query = np.array([[.3,.3,.3],[16.3,16.3,16.3]])
        expected = index.query(query, k=20, distance_upper_bound=.8)
        self.assertGreater(index.native_bytes, 0)
        with patch('native_correspondence.cacheBudget', return_value=1), patch('trajectory_fine.cacheBudget', return_value=1):
            actual = index.query(query, k=20, distance_upper_bound=.8)
        for a,b in zip(actual, expected):
            np.testing.assert_array_equal(a,b)
        self.assertEqual(index.native_bytes, 0)
        self.assertEqual(index.cache_bytes, 0)
        self.assertFalse(index.native_tiles)

    def test_close_releases_indices_normals_and_query_plan_idempotently(self):
        points = np.array([[0.,0,0],[.01,.01,.01]])
        index = self.index(points, np.tile([0.,0.,1.], (2,1)))
        _, ids = index.query(points, distance_upper_bound=.1)
        index.gather(ids, normals=True)
        self.assertTrue(index.native_tiles)
        index.close(); index.close()
        self.assertFalse(index.native_tiles)
        self.assertEqual(index.native_bytes, 0)
        self.assertIsNone(index.matcher)

    def test_owner_distance_prunes_only_provably_farther_neighbor_tiles(self):
        index = self.index([[7.9,1,1],[8.1,1,1]])
        expected = reference.cKDTree(index.gather(np.arange(index.n)))
        for query in ([[7.9,1,1]], [[8.,1,1]]):
            a = index.query(query, distance_upper_bound=.8)
            b = expected.query(query, distance_upper_bound=.8)
            for x,y in zip(a,b):
                np.testing.assert_array_equal(x,y)

    def test_open3d_buffers_are_reused(self):
        points = np.random.default_rng(20).uniform(0, 2, (1000,3))
        index = self.index(points, np.tile([0.,0.,1.],(len(points),1)))
        target = reference.IndexedPointToPlaneTarget(index, 2)
        source = reference.o3d.geometry.PointCloud(reference.o3d.utility.Vector3dVector(points+.002))
        target.correspondences(source, .1)
        before = np.asarray(target.cloud.points).ctypes.data
        normal_before = np.asarray(target.cloud.normals).ctypes.data
        source.translate([.0001, 0, 0])
        target.correspondences(source, .1)
        self.assertEqual(np.asarray(target.cloud.points).ctypes.data, before)
        self.assertEqual(np.asarray(target.cloud.normals).ctypes.data, normal_before)


if __name__ == '__main__':
    unittest.main()
