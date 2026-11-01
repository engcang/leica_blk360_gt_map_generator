"""A partial local minimum cannot be certified by a low inlier-only residual."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_ambiguity import audit, conflicting_basin, needs_audit


def quality(coverage=.71, mse=.003, rms=.045):
    return dict(accepted=True, geometry_supported=True, context_map_supported=True,
                coverage_20cm=coverage, inlier_distance_mse_m2=mse, plane_rms_m=rms,
                inlier_precision_10cm=.89, points=1000)


class AmbiguityTests(unittest.TestCase):
    def test_lower_residual_with_less_map_support_does_not_win(self):
        self.assertFalse(conflicting_basin(quality(), quality(.50,.0001,.01), .3, 0))

    def test_small_pose_change_is_not_a_competing_basin(self):
        self.assertFalse(conflicting_basin(quality(), quality(.95,.001,.02), .02, .1))

    def test_normal_fit_skips_extra_search(self):
        q=quality(.98,.001,.02)
        self.assertFalse(needs_audit(q))
        self.assertIs(audit(None,None,None,None,q),q)

    def test_held_out_conflict_stops_search_and_blocks_neighbor_promotion(self):
        T=np.eye(4);alternative=T.copy();alternative[0,3]=.3
        matcher=SimpleNamespace(icp=lambda points,initial: alternative)
        held=np.zeros((100,3));calls=[]
        fine=SimpleNamespace(_ambiguity_matcher=matcher,
            quality=lambda points,transform: calls.append(points) or quality(.84,.003,.05))
        q=quality()
        with patch('trajectory_ambiguity.voxel',side_effect=lambda points,size: points):
            audit(fine,np.ones((100,3)),held,T,q)
        self.assertFalse(q['accepted']);self.assertFalse(q['geometry_supported'])
        self.assertFalse(q['context_map_supported']);self.assertFalse(q['needs_context'])
        self.assertEqual(q['reason'],'ambiguous_tls_alignment')
        self.assertEqual(len(calls),1);self.assertIs(calls[0],held)
        self.assertEqual(q['local_basin_validation']['candidates'],1)


if __name__=='__main__':unittest.main()
