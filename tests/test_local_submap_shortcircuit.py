"""Skip only work that cannot change a local submap acceptance decision."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_local_submaps import LocalSubmaps


def geometry(supported=True):
    return dict(geometry_supported=supported, observable_directions=6, points=100,
                scaled_information_matrix=np.eye(6).tolist(), coverage_20cm=.9,
                inlier_distance_mse_m2=.001)


class SubmapShortCircuitTests(unittest.TestCase):
    def setup_seed(self, *, converged=True, context=(), predictions=(True, True, True)):
        recovery = LocalSubmaps()
        anchor = SimpleNamespace(index=5, timestamp=5., frontend_T=np.eye(4))
        validators = [SimpleNamespace(index=i, timestamp=float(i), frontend_T=np.eye(4)) for i in (6, 7, 8)]
        train = np.full((80, 3), -1.)
        recovery.seed_context = Mock(return_value=list(context))
        recovery.progress = Mock(); recovery.log = Mock(); recovery.seed_hypotheses = []
        def quality(points, _):
            marker = int(points[0, 0])
            return geometry(predictions[marker-6] if marker >= 6 else True)
        recovery.fine = SimpleNamespace(
            align=Mock(return_value=(np.eye(4), dict(converged=converged,
                optimizer_information=dict(observable_directions=6)))),
            quality=Mock(side_effect=quality))
        recovery.prepared_scan = lambda s: (None, np.full((80, 3), float(s.index)))
        recovery.fit = Mock(side_effect=lambda s, T, stage: (T, dict(geometry(), index=s.index,
            timestamp=s.timestamp, fine=dict(converged=True))))
        return recovery, anchor, train, validators

    def validate(self, parts):
        recovery, anchor, train, validators = parts
        with patch('trajectory_local_submaps.voxel', side_effect=lambda p, _:p):
            return recovery.validate_seed(anchor, train, validators, np.eye(4), 'before_anchor', 1.)

    def test_nonconverged_fit_never_queries_validation_or_fits_witnesses(self):
        parts = self.setup_seed(converged=False)
        self.assertIsNone(self.validate(parts))
        parts[0].fine.quality.assert_not_called(); parts[0].fit.assert_not_called()

    def test_rejected_temporal_agreement_precedes_geometric_validation(self):
        other = np.eye(4); other[0, 3] = .501
        parts = self.setup_seed(context=[other])
        self.assertIsNone(self.validate(parts))
        parts[0].fine.quality.assert_not_called(); parts[0].fit.assert_not_called()

    def test_failed_seed_keeps_independent_fits_needed_by_later_tracking(self):
        parts = self.setup_seed(predictions=(False, False, True))
        self.assertIsNone(self.validate(parts))
        self.assertEqual(parts[0].fit.call_count, 3)
        self.assertEqual(parts[0].fine.quality.call_count, 5)

    def test_one_bad_prediction_does_not_suppress_two_good_witnesses(self):
        parts = self.setup_seed(predictions=(False, True, True))
        np.testing.assert_array_equal(self.validate(parts), np.eye(4))
        self.assertEqual(parts[0].fit.call_count, 3)
        self.assertEqual(len(parts[0].seed_hypotheses[0]['costs']), 4)

    def test_success_keeps_all_witnesses_and_candidate_comparison_costs(self):
        parts = self.setup_seed()
        np.testing.assert_array_equal(self.validate(parts), np.eye(4))
        self.assertEqual(parts[0].fit.call_count, 3)
        self.assertEqual(len(parts[0].seed_hypotheses[0]['certificate']['members']), 4)

    def test_disjoint_anchor_constraints_skip_dense_submap_work(self):
        recovery, anchor, *_ = self.setup_seed()
        recovery.scan = Mock(return_value=anchor)
        other = np.eye(4); other[0, 3] = 1.001
        recovery.seed_context.return_value = [np.eye(4), other]
        recovery.seed_window = Mock(side_effect=AssertionError('Impossible seed built a dense window'))
        self.assertIsNone(recovery._submap_seed(anchor.index))
        recovery.seed_window.assert_not_called()


if __name__ == '__main__':
    unittest.main()
