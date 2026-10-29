"""Disconnected GT fragments must still agree with continuous measured motion."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_temporal import screen_temporal
from trajectory_validation import confirm_partial_views
from test_trajectory_validation import candidate, plane_quality, source


def continuous_source(times):
    matrices = np.tile(np.eye(4), (len(times), 1, 1))
    matrices[:, 0, 3] = times
    def pose(stamps):
        result = np.tile(np.eye(4), (len(stamps), 1, 1))
        result[:, 0, 3] = stamps
        return result
    return SimpleNamespace(times=np.asarray(times), matrices=matrices, max_gap=.5, pose=pose)


class BridgeTests(unittest.TestCase):
    def test_missing_gt_does_not_hide_a_wrong_corridor(self):
        estimate = continuous_source(np.arange(0, 10, .1))
        times = {i:i*.1 for i in [*range(20), *range(35, 55), *range(70, 90)]}
        poses = {i:estimate.pose([t])[0] for i,t in times.items()}
        for i in range(35,55): poses[i][1,3] += 67
        kept, edges = screen_temporal(poses, times, estimate)
        self.assertEqual(set(poses)-set(kept), set(range(35,55)))
        self.assertTrue(edges)

    def test_actual_odometry_gap_is_not_bridged(self):
        estimate = continuous_source(np.r_[np.arange(0,2,.1), np.arange(8,10,.1)])
        times = {0:1.,1:1.1,2:8.,3:8.1}
        poses = {i:estimate.pose([t])[0] for i,t in times.items()}
        poses[2][1,3] += 10; poses[3][1,3] += 10
        kept, edges = screen_temporal(poses, times, estimate)
        self.assertEqual(len(kept),4); self.assertFalse(edges)

    def test_low_overlap_count_cannot_outvote_stronger_map_evidence(self):
        estimate = continuous_source(np.arange(0,10,.1))
        times = {i:i*.1 for i in [*range(10), *range(25,60), *range(75,85)]}
        poses = {i:estimate.pose([t])[0] for i,t in times.items()}
        qualities = {i:dict(coverage_20cm=.95,inlier_distance_mse_m2=.001) for i in poses}
        for i in range(25,60):
            poses[i][1,3] += 6
            qualities[i] = dict(coverage_20cm=.1,inlier_distance_mse_m2=.01)
        kept, _ = screen_temporal(poses, times, estimate, qualities=qualities)
        self.assertEqual(set(poses)-set(kept),set(range(25,60)))

    def test_long_gap_allows_accumulated_drift_instead_of_fixed_half_metre(self):
        estimate = continuous_source(np.arange(0,121,.1))
        times = {0:0.,1:.1,2:120.,3:120.1}
        poses = {i:estimate.pose([t])[0] for i,t in times.items()}
        poses[2][1,3] += 1.; poses[3][1,3] += 1.
        kept, edges = screen_temporal(poses, times, estimate)
        self.assertEqual(len(kept),4);self.assertFalse(edges)

    def test_matching_low_overlap_neighbors_cannot_bootstrap_wrong_location(self):
        rows = [candidate(i,plane_quality(coverage=.05),offset=20) for i in range(3)]
        reference = candidate(4,plane_quality())
        self.assertEqual(confirm_partial_views(rows,source(),reference=reference),[])
        self.assertTrue(all(not q['accepted'] for q in rows))


if __name__ == '__main__':
    unittest.main()
