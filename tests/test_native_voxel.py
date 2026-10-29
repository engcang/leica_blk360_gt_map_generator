"""Native voxel selection preserves source points, boundary rounding and order."""
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mapping'))
from native_correspondence import libraryPath, voxelIndices
from trajectory_geometry import voxel


def reference(points,size):
    finite=points[np.isfinite(points).all(axis=1)]
    cells=np.floor(finite/size).astype(np.int64)
    ids=np.unique(cells,axis=0,return_index=True)[1]
    return finite[ids].astype(np.float32)


@unittest.skipUnless(libraryPath().is_file(),'Native correspondence library required')
class NativeVoxelTests(unittest.TestCase):
    def test_first_source_point_and_lexicographic_order_are_thread_independent(self):
        rng=np.random.default_rng(31)
        for dtype in (np.float32,np.float64):
            points=rng.uniform(-2,2,(100000,3)).astype(dtype)
            points[1000:2000]=points[:1000]+dtype(.00001)
            for size in (.01,.03,.1):
                expected=reference(points,size)
                for workers in (1,4):
                    ids=voxelIndices(points,size,workers)
                    self.assertIsNotNone(ids)
                    np.testing.assert_array_equal(points[ids].astype(np.float32),expected)
                np.testing.assert_array_equal(voxel(points,size),expected)

    def test_float32_and_float64_voxel_boundaries_match_numpy_division(self):
        for dtype in (np.float32,np.float64):
            values=np.arange(-100,101,dtype=dtype)*dtype(.01)
            values=np.concatenate((values,np.nextafter(values,dtype(-np.inf)),np.nextafter(values,dtype(np.inf))))
            points=np.column_stack((values,np.roll(values,1),np.roll(values,2)))
            ids=voxelIndices(points,.01,2)
            np.testing.assert_array_equal(points[ids].astype(np.float32),reference(points,.01))

    def test_nonfinite_rows_are_excluded_and_large_coordinates_use_reference(self):
        points=np.tile([[np.nan,0,0],[0,np.inf,0],[0,0,-np.inf]],(12000,1))
        self.assertEqual(voxel(points,.01).shape,(0,3))
        points[:2]=[[1e6,0,0],[-1e6,0,0]]
        self.assertIsNone(voxelIndices(points,.01))
        np.testing.assert_array_equal(voxel(points,.01),reference(points,.01))

    def test_scalar_dtype_promotion_uses_reference(self):
        points=np.ones((40000,3),dtype=np.float32)
        np.testing.assert_array_equal(voxel(points,np.float64(.01)),reference(points,np.float64(.01)))


if __name__=='__main__':
    unittest.main()
