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

    def test_streamed_dense_queries_preserve_neighbors_and_selection(self):
        rng = np.random.default_rng(52)
        points = np.concatenate([rng.uniform(.1, 7.9, (6000,3))+[x,0,0] for x in (0,16,32)])
        index = self.index(points)
        query = points+.002
        expected = index.query(query, k=2, distance_upper_bound=.8)
        index._clear_native()
        index.cache_limit = 6000*80+1024  # one tile fits, the whole submap does not
        selected = np.arange(0, len(query), 2)
        actual = index.query(query, k=2, distance_upper_bound=.8, selected=selected)
        for a,b in zip(actual,expected):
            np.testing.assert_array_equal(a[selected],b[selected])
        self.assertTrue(np.isinf(actual[0][1::2]).all())
        self.assertLessEqual(index.native_bytes, index.cache_limit)

    def test_streamed_correspondence_buffers_keep_rows_and_missing_matches(self):
        rng = np.random.default_rng(53)
        points = np.concatenate([rng.uniform(.1,7.9,(6000,3))+[x,0,0] for x in (0,16,32)])
        index = self.index(points, np.tile([0.,0.,1.],(len(points),1)))
        source = reference.o3d.geometry.PointCloud(reference.o3d.utility.Vector3dVector(
            np.vstack((points+.002, [[100.,0.,0.]]))))
        results = []
        for budget in (1024**3, 6000*80+1024):
            index._clear_native(); index.cache_limit = budget
            target = reference.IndexedPointToPlaneTarget(index,2)
            for _ in range(2):
                pairs,fitness,rmse = target.correspondences(source,.8)
                pairs = np.asarray(pairs).copy()
                results.append((pairs, np.asarray(target.cloud.points)[pairs[:,1]].copy(),fitness,rmse))
        for actual in results[1:]:
            for a,b in zip(actual,results[0]):
                np.testing.assert_array_equal(a,b)

    def test_live_memory_changes_and_new_source_sizes_reset_fused_state(self):
        rng = np.random.default_rng(54)
        points = np.concatenate([rng.uniform(.1,7.9,(6000,3))+[x,0,0] for x in (0,16,32)])
        index = self.index(points, np.tile([0.,0.,1.],(len(points),1)))
        xyz = index.gather(np.arange(index.n))
        tree = reference.cKDTree(xyz)
        target = reference.IndexedPointToPlaneTarget(index,2)
        # Switch between native and streamed paths on the same target, then
        # replace the source. Stale buffers must never certify a wrong neighbor.
        for budget,shift,stride in ((1024**3,.002,1),(6000*80+1024,.03,1),
                                   (1024**3,.04,1),(1024**3,.002,3)):
            index.cache_limit = budget
            moved = np.vstack((points[::stride]+[shift,0,0],[[100.,0.,0.]]))
            source = reference.o3d.geometry.PointCloud(reference.o3d.utility.Vector3dVector(moved))
            pairs,fitness,rmse = target.correspondences(source,.8)
            d,ids = tree.query(moved,distance_upper_bound=.8)
            valid = d < .8
            pairs = np.asarray(pairs)
            np.testing.assert_array_equal(pairs[:,0],np.flatnonzero(valid))
            np.testing.assert_array_equal(np.asarray(target.cloud.points)[pairs[:,1]],xyz[ids[valid]])
            self.assertEqual(fitness,float(valid.mean()))
            self.assertEqual(rmse,float(np.sqrt(np.mean(d[valid]**2))))

    def test_seeded_search_updates_neighbors_across_ties_and_tile_boundaries(self):
        points = np.array([[7.5,1,1],[8.0,1,1],[8.5,1,1],[9.,1,1]])
        index = self.index(points,np.tile([0.,0.,1.],(len(points),1)))
        target = reference.IndexedPointToPlaneTarget(index,2)
        for x,cap in ((7.75,.8),(8.25,.8),(8.3,.8),(9.1,.05),(8.8,.3),(7.9,.8)):
            moved = np.array([[x,1,1]])
            source = reference.o3d.geometry.PointCloud(reference.o3d.utility.Vector3dVector(moved))
            pairs,fitness,rmse = target.correspondences(source,cap)
            d,ids = index.query(moved,distance_upper_bound=cap)
            valid = d < cap
            pairs = np.asarray(pairs)
            np.testing.assert_array_equal(pairs[:,0],np.flatnonzero(valid))
            np.testing.assert_array_equal(np.asarray(target.cloud.points)[pairs[:,1]],index.gather(ids[valid]))
            self.assertEqual(rmse,float(d[0]) if valid[0] else 0.)

    def test_absent_match_halo_does_not_expand_the_fitting_gate(self):
        index = self.index([[7.75,1,1],[8.25,1,1]],[[0,0,1],[1,0,0]])
        target = reference.IndexedPointToPlaneTarget(index,2)
        target._query_state.stats = dict(queried_points=0)
        # Start with two equidistant neighbors outside the fitting gate, then
        # move a formerly absent point inside it, across a tile boundary.
        for x,cap in ((8.,.2),(8.001,.2),(8.051,.2),(8.001,.3),(8.,.2)):
            moved = np.array([[x,1,1]])
            source = reference.o3d.geometry.PointCloud(reference.o3d.utility.Vector3dVector(moved))
            pairs,fitness,rmse = target.correspondences(source,cap)
            d,ids = index.query(moved,distance_upper_bound=cap)
            valid = d < cap
            pairs = np.asarray(pairs)
            np.testing.assert_array_equal(pairs[:,0],np.flatnonzero(valid))
            np.testing.assert_array_equal(np.asarray(target.cloud.points)[pairs[:,1]],index.gather(ids[valid]))
            self.assertEqual(fitness,float(valid.mean()))
            self.assertEqual(rmse,float(d[0]) if valid[0] else 0.)
        if getattr(index,'match_halo',0):
            self.assertGreater(target._query_state.stats['absent_correspondences_reused'],0)

    def test_packed_tree_keeps_exact_bounds_and_neighbors_for_degenerate_maps(self):
        rng = np.random.default_rng(81)
        for origin in (-4096.,0.,4096.):
            # Duplicates, thin planes, and large offsets exercise leaf packing,
            # exact float32 bounds, boundary ties, and sparse tile owners.
            points = rng.uniform(0,2,(4000,3)).astype(np.float32)+origin
            points[:,2] = origin
            points[100:200] = points[:100]
            index = self.index(points)
            xyz = index.gather(np.arange(index.n))
            tree = reference.cKDTree(xyz)
            queries = np.vstack((xyz[::17]+[.0001,.0002,.01],
                                 xyz[::31]+[0,0,2],xyz[:10]))
            for cap in (.01,.05,.8):
                for k in (1,2,20):
                    actual = index.query(queries,k=k,distance_upper_bound=cap)
                    # The tiled reference has an explicit ordering for equal
                    # neighbors; a standalone cKDTree may choose another tie.
                    expected = reference.TiledMapIndex.query(index,queries,k=k,distance_upper_bound=cap)
                    np.testing.assert_array_equal(actual[0],tree.query(queries,k=k,distance_upper_bound=cap)[0])
                    for a,b in zip(actual,expected):
                        np.testing.assert_array_equal(a,b)


if __name__ == '__main__':
    unittest.main()
