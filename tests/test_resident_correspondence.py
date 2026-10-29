"""Resident C++ matching must preserve PLY precision, tie choices and graph weights."""
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
import numpy as np
import open3d as o3d
from scipy.spatial import cKDTree
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mapping'))
from native_correspondence import enabled, loadLibrary, finitePoints
from point_to_plane import PointToPlaneTarget
from fine_registration import FineTargetCache, informationFromCorrespondences


def cloud(points,normals=None):
    result=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    if normals is not None:
        result.normals=o3d.utility.Vector3dVector(normals)
    return result


@unittest.skipUnless(enabled() and hasattr(loadLibrary(),'blk_resident_matches_run'),'Build native correspondence library')
class ResidentTests(unittest.TestCase):
    def test_finite_validation_catches_nan_and_both_infinities_without_temporary_masks(self):
        lib=loadLibrary()
        for count in (0,1,32769):
            points=np.zeros((count,3),dtype=np.float64)
            self.assertTrue(finitePoints(lib,points,4))
            if count:
                for bad in (np.nan,np.inf,-np.inf):
                    points[-1,2]=bad
                    self.assertFalse(finitePoints(lib,points,4))
                points[-1,2]=0.
                self.assertTrue(finitePoints(lib,points,4))

    def test_exact_queries_with_double_precision_ties_changed_gates_and_point_order(self):
        rng=np.random.default_rng(101)
        # Non-float32 centroids, exact duplicate points, and large world offsets.
        for origin in (0.,1e6):
            points=rng.normal(size=(3000,3))+origin
            points[100:200]=points[:100]
            target=PointToPlaneTarget(cloud(points,np.tile([0,0,1],(len(points),1))),4,native=True)
            reference=cKDTree(points)
            for shift,cap,reverse in ((.001,.03,False),(.002,.03,False),(.25,.03,False),(.25,.4,False),(.003,.03,True),(0.,.03,True)):
                query=np.vstack((points[::3]+shift,[[origin+30,origin,origin]]))
                if reverse: query=query[::-1].copy()
                pairs,fitness,rmse=target.correspondences(cloud(query),cap)
                d,ids=reference.query(query,distance_upper_bound=cap)
                valid=d<cap
                np.testing.assert_array_equal(pairs,np.column_stack((np.flatnonzero(valid),ids[valid])))
                self.assertEqual(fitness,float(valid.mean()))
                self.assertEqual(rmse,float(np.sqrt(np.mean(d[valid]**2))) if valid.any() else 0.)

    def test_boundary_ties_empty_support_and_source_resize(self):
        points=np.array([[-1.,0,0],[1.,0,0],[3.,0,0],[1.,0,0]])
        target=PointToPlaneTarget(cloud(points,np.tile([0,0,1],(4,1))),2,native=True)
        reference=cKDTree(points)
        for query,cap in (([[0.,0,0]],1.),([[0.,0,0]],1.1),([[.01,0,0]],1.1),
                          ([[10.,0,0]],.03),([[10.001,0,0]],.03),([[1.,0,0],[2.,0,0]],1.1),([],1.)):
            query=np.asarray(query).reshape(-1,3)
            pairs,fitness,rmse=target.correspondences(cloud(query),cap)
            d,ids=reference.query(query,distance_upper_bound=cap);valid=d<cap
            np.testing.assert_array_equal(pairs,np.column_stack((np.flatnonzero(valid),ids[valid])))
            self.assertEqual(rmse,float(np.sqrt(np.mean(d[valid]**2))) if valid.any() else 0.)

    def test_full_solver_and_information_are_bitwise_equal_across_workers_and_concurrent_fits(self):
        rng=np.random.default_rng(42);points=rng.normal(size=(12000,3))
        normals=rng.normal(size=points.shape);normals/=np.linalg.norm(normals,axis=1)[:,None]
        target_cloud=cloud(points,normals);source=cloud(np.vstack((points+[.002,-.001,.001],points[:200]+20)))
        baseline=PointToPlaneTarget(target_cloud,1).align(source,np.eye(4),capture_information=True)
        expected=informationFromCorrespondences(target_cloud,baseline.correspondence_set)
        for workers in (1,4):
            target=PointToPlaneTarget(target_cloud,workers,native=True)
            with ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda _:target.align(source,np.eye(4),capture_information=True),range(2)))
            for result in results:
                np.testing.assert_array_equal(result.transformation,baseline.transformation)
                np.testing.assert_array_equal(result.correspondence_set,baseline.correspondence_set)
                np.testing.assert_array_equal(informationFromCorrespondences(target_cloud,result.correspondence_set),expected)
                self.assertEqual(result.optimizer_information,baseline.optimizer_information)
                self.assertEqual(result.inlier_rmse,baseline.inlier_rmse)
                self.assertEqual(result.iterations,baseline.iterations)
                self.assertGreater(result.query_statistics['exact_correspondences_reused'],0)

    def test_target_cache_is_bounded_and_keeps_in_use_indices_valid(self):
        target=cloud(np.eye(3),np.eye(3));cache=FineTargetCache()
        with patch('fine_registration.availableMemory',return_value=1024**3):
            a=cache.get(target,'a');b=cache.get(target,'b')
            self.assertIs(a,cache.get(target,'a'))
            cache.get(target,'c')
            self.assertEqual(len(cache.targets),2)
            self.assertNotIn(('b',b.workers),cache.targets)
            # Eviction only drops the cache's reference, never an active fit.
            self.assertEqual(b.correspondences(target,.03)[1],1.)
        with patch('fine_registration.availableMemory',return_value=100):
            cache.get(target,'d')
            self.assertFalse(cache.targets)  # No optional retention under low RAM.
        with patch('point_to_plane.availableMemory',return_value=100):
            with self.assertRaises(MemoryError): cache.get(target,'e')
        cache.clear();self.assertFalse(cache.targets)

    def test_backend_opt_out_remains_available(self):
        with patch.dict(os.environ,{'BLK_CORRESPONDENCE_BACKEND':'python'}):
            target=PointToPlaneTarget(cloud(np.eye(3),np.eye(3)),1,native=True)
            self.assertIsNone(target._native)
            self.assertEqual(target.correspondences(target.cloud,.03)[1],1.)


if __name__=='__main__': unittest.main()
