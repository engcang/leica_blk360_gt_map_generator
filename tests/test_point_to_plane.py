"""Shared fine engine must retain Open3D's geometry and CPU-independent results."""
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
import sys,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import open3d as o3d
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mapping'))
from point_to_plane import PointToPlaneTarget


class PointToPlaneTests(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(29)
        uv=rng.uniform(-2,2,(1500,2))
        p=np.concatenate([np.column_stack((uv,np.full(len(uv),-1))),np.column_stack((uv[:,0],np.full(len(uv),2),uv[:,1])),np.column_stack((np.full(len(uv),2),uv))])
        n=np.repeat([[0,0,1],[0,1,0],[1,0,0]],len(uv),axis=0)
        self.target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(p));self.target.normals=o3d.utility.Vector3dVector(n)
        self.source=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(p+[.002,-.001,.001]))

    def testStockOpen3dEquivalenceAndImmutableInputs(self):
        r=o3d.pipelines.registration
        source=np.asarray(self.source.points).copy();target=np.asarray(self.target.points).copy()
        reference=r.registration_icp(self.source,self.target,.03,np.eye(4),r.TransformationEstimationPointToPlane(r.TukeyLoss(.01)),
                                    r.ICPConvergenceCriteria(relative_fitness=1e-6,relative_rmse=1e-6,max_iteration=30))
        actual=PointToPlaneTarget(self.target,4).align(self.source,np.eye(4))
        np.testing.assert_allclose(actual.transformation,reference.transformation,rtol=0,atol=1e-10)
        self.assertEqual(actual.fitness,reference.fitness)
        np.testing.assert_array_equal(np.asarray(self.source.points),source)
        np.testing.assert_array_equal(np.asarray(self.target.points),target)

    def testCpuBudgetsPreserveTransformAndCorrespondences(self):
        results=[PointToPlaneTarget(self.target,n).align(self.source,np.eye(4)) for n in (1,4,28)]
        for value in results[1:]:
            np.testing.assert_array_equal(value.transformation,results[0].transformation)
            np.testing.assert_array_equal(value.correspondence_set,results[0].correspondence_set)
            self.assertTrue(value.converged)

    def testEmptySupportDoesNotConverge(self):
        initial=np.eye(4);initial[0,3]=20
        result=PointToPlaneTarget(self.target,1).align(self.source,initial)
        self.assertFalse(result.converged);self.assertEqual(len(result.correspondence_set),0)

    def testIterationLimitIsNotReportedAsConvergence(self):
        result=PointToPlaneTarget(self.target,1).align(self.source,np.eye(4),iterations=1,relative_fitness=0,relative_rmse=0)
        self.assertFalse(result.converged)

    def testStationaryPoseConvergesDespiteBoundaryFitnessChanges(self):
        matcher=PointToPlaneTarget(self.target,1)
        query=matcher.correspondences;calls=0
        def boundary_changes(source,cap):
            nonlocal calls
            pairs,fitness,rmse=query(source,cap);calls+=1
            return pairs,fitness-(calls%2)/len(source.points),rmse
        with patch.object(matcher,'correspondences',side_effect=boundary_changes):
            result=matcher.align(self.source,np.eye(4))
        self.assertTrue(result.converged)
        self.assertEqual(result.convergence_reason,'stationary_pose')
        np.testing.assert_allclose(result.transformation[:3,3],[-.002,.001,-.001],atol=1e-10)

    def testZeroTukeySupportCannotReportConvergence(self):
        xy=np.random.default_rng(4).uniform(-1,1,(2000,2))
        points=np.column_stack((xy,np.zeros(len(xy))))
        target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        target.normals=o3d.utility.Vector3dVector(np.tile([0.,0.,1.],(len(points),1)))
        result=PointToPlaneTarget(target,1).align(points+[0,0,.02],np.eye(4),[.03],[.005])
        self.assertEqual(result.fitness,1)
        self.assertFalse(result.converged)
        self.assertEqual(result.robust_support,0)

    def testInsufficientMemoryStopsWithoutSampling(self):
        with patch('point_to_plane.availableMemory',return_value=1024):
            with self.assertRaisesRegex(MemoryError,'1 cm target'):
                PointToPlaneTarget(self.target,1)

    def testTargetQueryIndexIsReused(self):
        matcher=PointToPlaneTarget(self.target,1);tree=matcher.tree
        first=matcher.align(self.source,np.eye(4));second=matcher.align(self.source,np.eye(4))
        self.assertIs(matcher.tree,tree)
        np.testing.assert_array_equal(first.transformation,second.transformation)

    def testExcludedPointBoundsPreserveEveryExactCorrespondence(self):
        matcher=PointToPlaneTarget(self.target,4)
        rng=np.random.default_rng(31)
        original=np.vstack((np.asarray(self.target.points)[::7],rng.uniform(-8,8,(3000,3))))
        # Include exact gate boundaries, a later return from outside the gate,
        # accumulated small motion, and an unrelated point ordering.
        for n,(shift,cap) in enumerate([([0,0,0],.3),([.001,0,0],.3),([.002,0,0],.1),
                                       ([-2,0,0],.8),([0,0,0],.03)]):
            points=original+shift
            if n==4:points=points[::-1]
            cloud=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
            pairs,fitness,rmse=matcher.correspondences(cloud,cap)
            distances,indices=matcher.tree.query(points,distance_upper_bound=cap,workers=1)
            valid=distances<cap
            expected=np.column_stack((np.flatnonzero(valid),indices[valid]))
            np.testing.assert_array_equal(pairs,expected)
            self.assertEqual(fitness,float(np.count_nonzero(valid))/len(points))
            self.assertEqual(rmse,float(np.sqrt(np.mean(distances[valid]**2))) if valid.any() else 0.)

    def testBoundsSkipOnlyProvablyExcludedQueriesAndAllowReentry(self):
        matcher=PointToPlaneTarget(self.target,1)
        real_tree=matcher.tree
        class Queries:
            n=real_tree.n
            def __init__(self):self.sizes=[]
            def query(self,points,**options):
                self.sizes.append(len(points));return real_tree.query(points,**options)
        recorded=Queries();matcher.tree=recorded
        cloud=o3d.geometry.PointCloud(o3d.utility.Vector3dVector([[20.,0,0]]))
        self.assertEqual(len(matcher.correspondences(cloud,.03)[0]),0)
        calls=len(recorded.sizes)
        cloud.translate([-.001,0,0])
        self.assertEqual(len(matcher.correspondences(cloud,.03)[0]),0)
        self.assertEqual(len(recorded.sizes),calls)
        cloud.points=o3d.utility.Vector3dVector(np.asarray(self.target.points)[:1])
        self.assertEqual(len(matcher.correspondences(cloud,.03)[0]),1)

    def testBoundsRetainStrictGatesAndTiesAtLargeCoordinates(self):
        points=np.array([[1e6,0.,0.],[1e6,0.,0.],[1e6,1.,0.]])
        target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        target.normals=o3d.utility.Vector3dVector(np.tile([0.,0.,1.],(3,1)))
        matcher=PointToPlaneTarget(target,2)
        for delta in (.03,np.nextafter(.03,0),np.nextafter(.03,1),0.):
            p=points+[0,0,delta];cloud=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(p))
            actual=matcher.correspondences(cloud,.03)[0]
            d,i=matcher.tree.query(p,distance_upper_bound=.03)
            np.testing.assert_array_equal(actual,np.column_stack((np.flatnonzero(d<.03),i[d<.03])))

    def testCoarseBoundsPreserveExactQueriesAcrossMotionAndGates(self):
        rng=np.random.default_rng(73)
        points=rng.uniform(-3,3,(60_000,3))
        points[:,2]=.7*points[:,0]-.2*points[:,1]+rng.normal(0,.002,len(points))
        target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        target.normals=o3d.utility.Vector3dVector(np.tile([0.,0.,1.],(len(points),1)))
        matcher=PointToPlaneTarget(target,4)
        self.assertTrue(matcher._prepare_bounds())
        original=np.vstack((points[::13],rng.uniform(-8,8,(4000,3))))
        for shift,cap in ((0.,.8),(.003,.3),(.01,.1),(3.,.8),(0.,.03)):
            moved=original+[0,0,shift]
            cloud=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(moved))
            pairs,fitness,rmse=matcher.correspondences(cloud,cap)
            d,i=matcher.tree.query(moved,distance_upper_bound=cap,workers=1)
            valid=d<cap
            np.testing.assert_array_equal(pairs,np.column_stack((np.flatnonzero(valid),i[valid])))
            self.assertEqual(fitness,float(np.mean(valid)))
            self.assertEqual(rmse,float(np.sqrt(np.mean(d[valid]**2))) if valid.any() else 0.)

    def testCoarseBoundsKeepCornersTiesAndLargeNegativeCoordinates(self):
        corner=np.array([[-1e6-.1,-.1,-.1],[-1e6,0.,0.],[-1e6+.1,.1,.1]])
        points=np.tile(corner,(20_000,1))
        target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
        target.normals=o3d.utility.Vector3dVector(np.tile([0.,0.,1.],(len(points),1)))
        matcher=PointToPlaneTarget(target,2)
        self.assertTrue(matcher._prepare_bounds())
        for cap in (.03,.1,.8):
            for delta in (cap,np.nextafter(cap,0),np.nextafter(cap,1),0.,20.):
                moved=corner+[0,0,delta]
                cloud=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(moved))
                pairs,_,_=matcher.correspondences(cloud,cap)
                d,i=matcher.tree.query(moved,distance_upper_bound=cap,workers=1)
                np.testing.assert_array_equal(pairs,np.column_stack((np.flatnonzero(d<cap),i[d<cap])))

    def testCoarseIndexAndMemoryFallbackPreserveSolver(self):
        target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.tile(np.asarray(self.target.points),(12,1))))
        target.normals=o3d.utility.Vector3dVector(np.tile(np.asarray(self.target.normals),(12,1)))
        reference=PointToPlaneTarget(target,1)
        accelerated=PointToPlaneTarget(target,4)
        with patch('point_to_plane.availableMemory',return_value=1024):
            self.assertFalse(reference._prepare_bounds())
        self.assertTrue(accelerated._prepare_bounds())
        a=reference.align(self.source,np.eye(4));b=accelerated.align(self.source,np.eye(4))
        np.testing.assert_array_equal(a.transformation,b.transformation)
        np.testing.assert_array_equal(a.correspondence_set,b.correspondence_set)
        self.assertEqual(a.converged,b.converged)
        self.assertEqual(a.iterations,b.iterations)

    def testParallelBatchesPreserveSourceOrderAndNearestTies(self):
        rng=np.random.default_rng(56)
        points=np.vstack((np.asarray(self.target.points),rng.uniform(-8,8,(40_000,3))))
        matcher=PointToPlaneTarget(self.target,4)
        for cap in (.03,.8,np.inf):
            expected=matcher.tree.query(points,distance_upper_bound=cap,workers=1)
            actual=matcher._query(matcher.tree,points,distance_upper_bound=cap)
            for a,b in zip(actual,expected):
                np.testing.assert_array_equal(a,b)

    def testOptionalGridFallsBackOutsideIntegerRange(self):
        target=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.full((50_000,3),1e19)))
        target.normals=o3d.utility.Vector3dVector(np.tile([0.,0.,1.],(50_000,1)))
        matcher=PointToPlaneTarget(target,1)
        self.assertFalse(matcher._prepare_bounds())


if __name__=='__main__':unittest.main()
