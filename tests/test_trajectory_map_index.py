"""Full-resolution persistent map queries and the unchanged Open3D solver."""
import os
os.environ['OPENBLAS_NUM_THREADS'] = '1'
from collections import OrderedDict
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import open3d as o3d
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_fine import FineMatcher, TiledMapIndex
from point_to_plane import IndexedPointToPlaneTarget, PointToPlaneTarget


class PersistentMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.indices = []

    def tearDown(self):
        for index in self.indices:
            index.close()
        self.temp.cleanup()

    def index(self, points, normals=None, workers=2):
        points = np.asarray(points, dtype='<f4')
        matcher = FineMatcher.__new__(FineMatcher)
        matcher.folder = self.folder
        matcher.workers = workers
        matcher.index = dict(tile_m=8., tiles={})
        matcher.tile_cache = OrderedDict()
        matcher.cache_bytes = 0
        matcher.cache_limit = 1024**3
        normal_folder = self.folder/'normals-4cm-40-v1'
        normal_folder.mkdir(exist_ok=True)
        tiles = np.floor(points/8).astype(int)
        for tile in np.unique(tiles, axis=0):
            key = '_'.join(map(str, tile))
            selected = np.all(tiles == tile, axis=1)
            points[selected].tofile(self.folder/(key+'.bin'))
            matcher.index['tiles'][key] = int(selected.sum())
            if normals is not None:
                np.save(normal_folder/(key+'.npy'), np.asarray(normals)[selected])
        index = TiledMapIndex(matcher)
        self.indices.append(index)
        matcher.map_index = index
        matcher.target = IndexedPointToPlaneTarget(index, workers)
        return index

    def test_exact_nearest_and_neighbors_across_faces_edges_and_corners(self):
        rng = np.random.default_rng(63)
        points = np.concatenate([rng.uniform(-.4, .4, (300, 3))+center for center in
                                 ([0,0,0], [8,8,8], [-8,-8,-8], [8,0,-8])])
        index = self.index(points)
        target = index.gather(np.arange(index.n))
        reference = cKDTree(target)
        query = np.concatenate((target[::7]+[.001,-.004,.01], np.array([[200,300,-100]])))
        for cap in (.03, .2, .8, 8.1, np.inf):
            for k in (1, 20):
                actual = index.query(query, k=k, distance_upper_bound=cap)
                expected = reference.query(query, k=k, distance_upper_bound=cap)
                np.testing.assert_array_equal(actual[0], expected[0])
                np.testing.assert_array_equal(actual[1], expected[1])

    def test_adjacent_scans_reuse_index_beyond_old_five_use_and_pose_limits(self):
        rng = np.random.default_rng(9)
        index = self.index(rng.uniform(.1, 7.9, (3000,3)))
        query = rng.uniform(2,4,(100,3))
        index.query(query, distance_upper_bound=.8)
        builds = index.statistics['tree_builds']
        for frame in range(12):
            index.query(query+[frame*.1,0,0], distance_upper_bound=.8)
        self.assertEqual(index.statistics['tree_builds'], builds)
        self.assertEqual(index.statistics['tree_hits'], 12)
        self.assertEqual(builds, 1)

    def test_dynamic_memory_eviction_never_changes_neighbors(self):
        rng = np.random.default_rng(10)
        points = np.concatenate((rng.uniform(.1,1,(500,3)),rng.uniform(16.1,17,(500,3))))
        index = self.index(points)
        query = np.array([[.3,.3,.3],[16.3,16.3,16.3]])
        expected = index.query(query, k=20, distance_upper_bound=.8)
        self.assertGreater(index.cache_bytes, 0)
        with patch('trajectory_fine.cacheBudget', return_value=1):
            actual = index.query(query, k=20, distance_upper_bound=.8)
        for a,b in zip(actual, expected):
            np.testing.assert_array_equal(a,b)
        self.assertEqual(index.cache_bytes, 0)
        self.assertEqual(len(index.trees), 0)
        self.assertGreater(index.statistics['tree_evictions'], 0)

    def test_motion_halo_reuses_plan_and_rebuilds_before_missing_new_tiles(self):
        index = self.index([[7.7,0,0],[8.05,0,0],[8.45,0,0],[16.01,0,0]])
        reference = cKDTree(index.gather(np.arange(index.n)))
        for x in (7.85,8.0,8.39,16.0):
            query = np.array([[x,0,0]])
            actual = index.query(query,distance_upper_bound=.1)
            expected = reference.query(query,distance_upper_bound=.1)
            for a,b in zip(actual,expected):
                np.testing.assert_array_equal(a,b)
        self.assertEqual(index.statistics['plan_reuses'],1)
        self.assertEqual(index.statistics['plan_builds'],3)

    def test_changing_uncertain_subset_keeps_full_source_plan_reusable(self):
        rng = np.random.default_rng(74)
        points = rng.uniform(-9,9,(2500,3))
        index = self.index(points)
        xyz = index.gather(np.arange(index.n))
        query = xyz+[.001,0,0]
        reference = cKDTree(xyz)
        for rows in (np.arange(len(query)),np.arange(0,len(query),3),np.arange(0,len(query),7)):
            actual = index.query(query,k=2,distance_upper_bound=.8,selected=rows)
            expected = reference.query(query[rows],k=2,distance_upper_bound=.8)
            for a,b in zip(actual,expected):
                np.testing.assert_array_equal(a[rows],b)
            excluded = np.ones(len(query),dtype=bool)
            excluded[rows] = False
            self.assertTrue(np.isinf(actual[0][excluded]).all())
        self.assertEqual(index.statistics['plan_builds'],1)
        self.assertEqual(index.statistics['plan_reuses'],2)

    def test_parallel_tiled_queries_preserve_distances_and_neighbor_order(self):
        rng = np.random.default_rng(42)
        points = rng.uniform(-9,9,(9000,3))
        index = self.index(points,workers=4)
        reference = cKDTree(index.gather(np.arange(index.n)))
        query = points[rng.integers(0,len(points),35000)]+rng.uniform(-.1,.1,(35000,3))
        for k in (1,20):
            expected = reference.query(query,k=k,distance_upper_bound=.8)
            actual = index.query(query,k=k,distance_upper_bound=.8)
            for a,b in zip(actual,expected):
                np.testing.assert_array_equal(a,b)

    def test_owner_distance_prunes_only_provably_farther_neighbor_tiles(self):
        index = self.index([[7.9,1,1],[8.1,1,1]])
        distances, ids = index.query([[7.9,1,1]],distance_upper_bound=.8)
        self.assertLess(distances[0],1e-6)
        self.assertEqual(index.statistics['queried_tile_points'],1)
        self.assertEqual(index.statistics['tree_builds'],1)
        # Crossing the boundary still searches the other tile when necessary.
        reference = cKDTree(index.gather(np.arange(index.n)))
        actual = index.query([[8.,1,1]],distance_upper_bound=.8)
        expected = reference.query([[8.,1,1]],distance_upper_bound=.8)
        for a,b in zip(actual,expected):
            np.testing.assert_array_equal(a,b)

    def test_compact_target_preserves_scalar_solver_and_normals(self):
        rng = np.random.default_rng(11)
        uv = rng.uniform(-1,1,(1400,2))
        points = np.concatenate((np.column_stack((uv,np.zeros(len(uv)))),
                                 np.column_stack((uv[:,0],np.ones(len(uv)),uv[:,1])),
                                 np.column_stack((np.ones(len(uv)),uv)))) + [8,0,0]
        normals = np.repeat([[0,0,1],[0,1,0],[1,0,0]],len(uv),axis=0)
        index = self.index(points, normals)
        xyz, directions = index.gather(np.arange(index.n), normals=True)
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz))
        cloud.normals = o3d.utility.Vector3dVector(directions)
        source = xyz+[.008,-.006,.004]
        reference = PointToPlaneTarget(cloud, 1).align(source,np.eye(4),distances=(.1,.03))
        result = IndexedPointToPlaneTarget(index, 2).align(source,np.eye(4),distances=(.1,.03))
        np.testing.assert_array_equal(result.transformation, reference.transformation)
        self.assertEqual(result.iterations, reference.iterations)
        self.assertEqual(result.converged, reference.converged)
        self.assertEqual(result.robust_support, reference.robust_support)
        self.assertEqual(result.fitness, reference.fitness)
        self.assertEqual(result.inlier_rmse, reference.inlier_rmse)

    def test_neighbor_certificates_preserve_exact_matches_as_points_and_caps_change(self):
        rng = np.random.default_rng(57)
        points = rng.uniform(-9,9,(3000,3))
        index = self.index(points,np.tile([0.,0.,1.],(len(points),1)))
        xyz = index.gather(np.arange(index.n))
        matcher = IndexedPointToPlaneTarget(index,2)
        matcher._query_state.stats = dict(queried_points=0)
        original = np.vstack((xyz[::5],[[40.,0,0]]))
        for shift,cap in ((.001,.8),(.0011,.8),(.002,.1),(.002,.0001),(.001,.1),(.3,.8)):
            moved = original+[shift,shift*.2,0]
            cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(moved))
            pairs,fitness,rmse = matcher.correspondences(cloud,cap)
            d,ids = index.query(moved,distance_upper_bound=cap)
            valid = d < cap
            np.testing.assert_array_equal(np.asarray(pairs)[:,0],np.flatnonzero(valid))
            np.testing.assert_array_equal(np.asarray(matcher.cloud.points)[np.asarray(pairs)[:,1]],xyz[ids[valid]])
            self.assertEqual(fitness,float(np.mean(valid)))
            self.assertEqual(rmse,float(np.sqrt(np.mean(d[valid]**2))) if valid.any() else 0.)
        self.assertGreater(matcher._query_state.stats['exact_correspondences_reused'],1000)
        self.assertGreater(matcher._query_state.stats['absent_correspondences_reused'],0)

    def test_neighbor_certificates_never_reuse_equal_distance_ties(self):
        index = self.index([[7.75,1,1],[8.25,1,1]],[[0,0,1],[1,0,0]])
        matcher = IndexedPointToPlaneTarget(index,2)
        matcher._query_state.stats = dict(queried_points=0)
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector([[8.,1,1]]))
        for _ in range(3):
            matcher.correspondences(cloud,.8)
            np.testing.assert_array_equal(np.asarray(matcher.cloud.points),[[7.75,1,1]])
        self.assertEqual(matcher._query_state.stats['exact_correspondences_reused'],0)
        self.assertEqual(matcher._query_state.stats['tie_queries'],3)

    def test_optimizer_information_uses_only_actual_tukey_weighted_support(self):
        uv = np.random.default_rng(13).uniform(1,2,(1000,2))
        floor = np.column_stack((uv,np.zeros(len(uv))))
        wall_x = np.column_stack((np.full(len(uv),3.),uv))
        wall_y = np.column_stack((uv[:,0],np.full(len(uv),3.),uv[:,1]))
        points = np.vstack((floor,wall_x,wall_y))
        normals = np.repeat([[0,0,1],[1,0,0],[0,1,0]],len(uv),axis=0)
        index = self.index(points,normals)
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points.astype('<f4').astype(float)))
        cloud.normals = o3d.utility.Vector3dVector(normals)
        source = np.asarray(cloud.points).copy()
        source[len(uv):2*len(uv)] += [.07,0,0]
        source[2*len(uv):] += [0,.07,0]
        plain = PointToPlaneTarget(cloud,1).align(source,np.eye(4),distances=(.1,))
        result = PointToPlaneTarget(cloud,1).align(source,np.eye(4),distances=(.1,),capture_information=True)
        np.testing.assert_array_equal(plain.transformation,result.transformation)
        self.assertTrue(result.converged)
        info = result.optimizer_information
        self.assertEqual(info['positive_points'],len(uv))
        self.assertEqual(info['observable_directions'],3)
        self.assertEqual(info['rotation_scale_m'],5.)

    def test_validation_neighborhood_is_not_clipped_at_tile_boundary(self):
        rng = np.random.default_rng(87)
        xy = rng.uniform(-.15,.15,(1600,2))
        points = np.column_stack((xy[:,0]+8,xy[:,1],np.zeros(len(xy))))
        index = self.index(points)
        query = points[::3]
        actual = index.matcher.quality(query,np.eye(4))
        reference = FineMatcher.__new__(FineMatcher)
        reference.workers = 2
        reference.target = PointToPlaneTarget(index.gather(np.arange(index.n)),2)
        expected = reference.quality(query,np.eye(4))
        self.assertEqual(actual, expected)

    def test_empty_and_far_queries_do_not_load_distant_trees(self):
        index = self.index([[0,0,0],[.01,.01,.01]])
        self.assertEqual(index.query(np.empty((0,3)))[0].shape,(0,))
        distances,ids = index.query([[200,0,0]],distance_upper_bound=.8)
        self.assertTrue(np.isinf(distances).all())
        self.assertEqual(ids.tolist(),[index.n])
        self.assertEqual(index.statistics['tree_builds'],0)

    def test_close_releases_indices_normals_and_query_plan_idempotently(self):
        points = np.array([[0.,0,0],[.01,.01,.01]])
        index = self.index(points,np.tile([0.,0,1.],(2,1)))
        _, ids = index.query(points,distance_upper_bound=.1)
        index.gather(ids,normals=True)
        self.assertTrue(index.trees)
        self.assertTrue(index.normal_maps)
        index.close()
        index.close()
        self.assertEqual(index.cache_bytes,0)
        self.assertFalse(index.trees)
        self.assertFalse(index.normal_maps)
        self.assertIsNone(index.query_plan)
        self.assertIsNone(index.matcher)


if __name__ == '__main__':
    unittest.main()
