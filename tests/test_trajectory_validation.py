"""Independent held-out geometry, partial FOV, and joint-observability checks."""
from pathlib import Path
import sys
import unittest
import tempfile
import lzma
from unittest.mock import patch
import yaml
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_geometry import correspondence_quality
from trajectory_recovery import Recovery
from trajectory_io import ScanSource, Trajectory, Scan, PoseGap
from trajectory_validation import confirm_partial_views, jointly_observable, joint_information


def plane_quality(axis=None, coverage=1., T=None, span=6.):
    rng = np.random.default_rng(101)
    points, normals = [], []
    for direction in (range(3) if axis is None else [axis]):
        p = rng.uniform(-span/2, span/2, (400, 3)); p[:, direction] = span/2
        n = np.zeros_like(p); n[:, direction] = 1
        points.append(p); normals.append(n)
    p, n = np.concatenate(points), np.concatenate(normals)
    dist = np.r_[np.zeros(len(p)), np.full(round(len(p)/coverage)-len(p), np.inf)]
    return correspondence_quality(p, n, np.zeros(len(p)), dist, np.eye(4) if T is None else T)


def candidate(index, quality, offset=0.):
    T = np.eye(4); T[0, 3] = index*.03+offset
    return dict(quality, index=index, timestamp=10.+index*.1,
                T_map_from_base=T.tolist(), fine=dict(converged=True))


def source(count=10):
    def pose(stamps):
        result = np.tile(np.eye(4), (len(stamps), 1, 1))
        result[:, 0, 3] = (np.asarray(stamps)-10)*.3
        return result
    return SimpleNamespace(timestamps=10+np.arange(count)*.1, pose=pose)


class ContextValidationTests(unittest.TestCase):
    def test_partial_fov_needs_two_independent_neighbors_not_minimum_coverage(self):
        q = plane_quality(coverage=.20)
        self.assertTrue(q['geometry_supported']); self.assertFalse(q['accepted'])
        self.assertEqual(q['observable_directions'], 6)
        rows = [candidate(i, q) for i in range(3)]
        self.assertEqual(confirm_partial_views(rows[:2], source()), [])
        self.assertEqual(set(confirm_partial_views(rows, source())), {0, 1, 2})
        for row in rows:
            self.assertTrue(row['accepted'])
            self.assertEqual(row['acceptance_scope'], 'independent-full-rank-scans')
            self.assertEqual(len(row['context_validation']['witness_indices']), 2)
            self.assertNotIn(row['index'], row['context_validation']['witness_indices'])

    def test_wrong_corridor_or_timestamp_cannot_be_a_partial_view_witness(self):
        q = plane_quality(coverage=.5)
        rows = [candidate(0, q), candidate(1, q, offset=6), candidate(2, q)]
        self.assertEqual(confirm_partial_views(rows, source()), [])
        self.assertTrue(all(not r['accepted'] for r in rows))
        rows = [candidate(i, q) for i in range(3)]
        rows[1]['timestamp'] += .1
        self.assertEqual(confirm_partial_views(rows, source()), [])
        self.assertEqual(rows[1]['reason'], 'scan_timestamp_mismatch')

    def test_weak_or_context_promoted_rank_deficient_scans_cannot_self_certify(self):
        q = plane_quality(coverage=.5)
        weak = plane_quality(axis=2)
        rows = [candidate(0, q), candidate(1, weak), candidate(2, weak)]
        for row in rows[1:]:
            row.update(accepted=True, reason='independent_submap_geometry')
        self.assertEqual(confirm_partial_views(rows, source()), [])
        self.assertFalse(rows[0]['accepted'])

    def test_rejected_motion_or_nullspace_fit_cannot_be_recycled_as_context(self):
        q = plane_quality(coverage=.5)
        for reason in ('temporal_inconsistency', 'tracking_motion_inconsistent',
                       'unconstrained_submap_departure', 'isolated_large_correction_quarantined'):
            with self.subTest(reason=reason):
                rows = [candidate(i, q) for i in range(3)]
                rows[1].update(accepted=False, reason=reason)
                self.assertEqual(confirm_partial_views(rows, source()), [])
                self.assertTrue(all(not r['accepted'] for r in rows))

    def test_individual_refit_cannot_drift_in_submap_constrained_nullspace(self):
        qualities = [plane_quality(axis=i) for i in range(3)]
        joint = jointly_observable(qualities, [np.eye(4)]*3, np.eye(4), shared_transform_fitted=True)
        recovery = Recovery.__new__(Recovery)
        recovery.seed_certificates = {}; recovery.context_certificates = {}
        members = [dict(index=i, timestamp=10.+i*.1, T_map_from_base=np.eye(4).tolist()) for i in range(3)]
        recovery.remember_certificate(0, dict(anchor_timestamp=10., joint=joint, members=members, training_quality=plane_quality(), optimizer_information=dict(observable_directions=6)))
        small = np.eye(4); small[0, 3] = .001
        good = candidate(0, qualities[2])
        recovery.certify_submap_pose(small, good)
        self.assertTrue(good['accepted'])
        displaced = np.eye(4); displaced[0, 3] = .05
        bad = candidate(0, qualities[2])
        recovery.certify_submap_pose(displaced, bad)
        self.assertFalse(bad['accepted'])
        self.assertEqual(bad['reason'], 'unconstrained_submap_departure')

    def test_unweighted_geometry_cannot_replace_rank_missing_in_actual_optimizer(self):
        quality = plane_quality()
        joint = jointly_observable([quality]*3, [np.eye(4)]*3, np.eye(4), shared_transform_fitted=True)
        recovery = Recovery.__new__(Recovery)
        recovery.seed_certificates = {}; recovery.context_certificates = {}
        members = [dict(index=i, timestamp=10.+i*.1, T_map_from_base=np.eye(4).tolist()) for i in range(3)]
        recovery.remember_certificate(0, dict(anchor_timestamp=10., joint=joint, members=members,
            training_quality=quality, optimizer_information=dict(observable_directions=3)))
        candidate_quality = candidate(0, plane_quality(axis=2))
        recovery.certify_submap_pose(np.eye(4), candidate_quality)
        self.assertFalse(candidate_quality['accepted'])

    def test_tartanstar_validation_uses_decoded_end_column_anchor_not_metadata_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); lidar = root/'lidar'; lidar.mkdir()
            height, width, count = 16, 32, 3
            metadata = {'range_dtype':'<u4', 'sensor_info':{
                'lidar_data_format':{'pixels_per_column':height, 'columns_per_frame':width},
                'beam_intrinsics':{'beam_azimuth_angles':[0]*height,
                    'beam_altitude_angles':np.linspace(-30, 30, height).tolist(),
                    'beam_to_lidar_transform':np.eye(4).ravel().tolist()}}}
            (lidar/'meta.yaml').write_text(yaml.safe_dump(metadata))
            start = 10.+np.arange(count)*.1
            ranges = lzma.compress(np.full((height, width), 5000, dtype='<u4').tobytes())
            clocks = [lzma.compress(np.linspace(t, t+.09, width).astype('<f8').tobytes()) for t in start]
            (lidar/'rng').write_bytes(ranges*count); (lidar/'tcol').write_bytes(b''.join(clocks))
            np.savez(lidar/'meta.npz', ts=start, rng_offset=np.arange(count+1)*len(ranges),
                     ts_offset=np.r_[0, np.cumsum([len(c) for c in clocks])])
            pose_rows = [[t, 0, 0, 0, 0, 0, 0, 1] for t in np.arange(10., 10.5, .1)]
            np.savetxt(root/'estimate.tum', pose_rows)
            config = dict(path=str(root), format='tartanstar')
            estimate = Trajectory(dict(path=str(root/'estimate.tum')))
            scan_source = ScanSource(config, estimate)
            scans = [scan_source.scan(i) for i in range(count)]
            self.assertTrue(all(abs(s.timestamp-scan_source.timestamps[s.index]-.09) < 1e-10 for s in scans))
            def rows():
                return [dict(plane_quality(coverage=.5), index=s.index, timestamp=s.timestamp,
                             T_map_from_base=s.frontend_T.tolist(), fine=dict(converged=True)) for s in scans]
            with patch.object(scan_source, '_decode', side_effect=AssertionError('Decoded anchors should be cached')):
                self.assertEqual(set(confirm_partial_views(rows(), scan_source)), {0, 1, 2})
            resumed = ScanSource(config, estimate)
            with patch.object(resumed, '_decode', wraps=resumed._decode) as decoder:
                self.assertEqual(set(confirm_partial_views(rows(), resumed)), {0, 1, 2})
                self.assertEqual([call.args[0] for call in decoder.call_args_list], ['tcol']*3)
                confirm_partial_views(rows(), resumed)
                self.assertEqual(decoder.call_count, 3)
            shifted = rows(); shifted[1]['timestamp'] = float(start[1])
            self.assertEqual(confirm_partial_views(shifted, scan_source), [])
            self.assertEqual(shifted[1]['reason'], 'scan_timestamp_mismatch')
            recovery = Recovery.__new__(Recovery)
            recovery.source = scan_source; recovery.rows_by_index = {q['index']:q for q in rows()}
            recovery.fit_cache = {}; recovery.latest_attempt = {}; recovery.log = lambda _:None
            recovery.fine = object()
            with patch.object(recovery, 'fit_continued', return_value=(np.eye(4), rows()[0])) as fitting:
                recovery.fit(scans[0], np.eye(4), 'timestamp-regression')
                fitting.assert_called_once()
            wrong_anchor = Scan(scans[0].index, float(start[0]), scans[0].points, scans[0].frontend_T)
            with self.assertRaisesRegex(ValueError, 'timestamp mismatch'):
                recovery.fit(wrong_anchor, np.eye(4), 'timestamp-regression')

    def test_unavailable_anchor_does_not_abort_other_independent_candidates(self):
        q = plane_quality(coverage=.5)
        rows = [candidate(i, q) for i in range(4)]
        scan_source = source()
        decoded = []
        def anchor(index):
            decoded.append(index)
            if index == 3:
                raise PoseGap('No valid LiDAR column timestamps')
            return scan_source.timestamps[index]
        scan_source.anchor_timestamp = anchor
        self.assertEqual(set(confirm_partial_views(rows, scan_source)), {0, 1, 2})
        self.assertEqual(rows[3]['reason'], 'scan_timestamp_unavailable')
        self.assertFalse(rows[3]['accepted'])
        # Rejected rows without any fitted geometric evidence do not require
        # decoding a scan that was already declared unavailable.
        rejected = dict(index=4, timestamp=10.4, accepted=False, reason='scan_unavailable')
        before = list(decoded)
        self.assertEqual(confirm_partial_views([rejected], scan_source), [])
        self.assertEqual(decoded, before)

    def test_distributed_support_is_required_even_with_full_overlap(self):
        quality = plane_quality(span=.1)
        self.assertFalse(quality['geometry_supported'])
        self.assertEqual(quality['reason'], 'concentrated_support')
        self.assertEqual(quality['coverage_20cm'], 1.)

    def test_complementary_planes_require_an_actual_shared_geometric_fit(self):
        qualities = [plane_quality(axis=i) for i in range(3)]
        self.assertTrue(all(q['observable_directions'] == 3 for q in qualities))
        poses = [np.eye(4)]*3
        self.assertFalse(jointly_observable(qualities, poses, poses[0],
                                           shared_transform_fitted=False)['accepted'])
        joint = jointly_observable(qualities, poses, poses[0], shared_transform_fitted=True)
        self.assertTrue(joint['accepted']); self.assertEqual(joint['observable_directions'], 6)
        same_plane = jointly_observable([qualities[0]]*3, poses, poses[0], shared_transform_fitted=True)
        self.assertFalse(same_plane['accepted']); self.assertEqual(same_plane['observable_directions'], 3)

    def test_information_is_rebased_to_shared_origin_without_slam_information(self):
        rng = np.random.default_rng(103)
        p, n = rng.normal(size=(100, 3)), rng.normal(size=(100, 3))
        n /= np.linalg.norm(n, axis=1)[:, None]
        anchor = np.eye(4); origin = np.eye(4); origin[:3, 3] = [3, -4, 2]
        old = np.column_stack([np.cross((p-origin[:3, 3])/5, n), n])
        expected = np.column_stack([np.cross(p/5, n), n])
        actual = joint_information([dict(points=len(p), scaled_information_matrix=old.T@old/len(p))],
                                   [origin], anchor)
        np.testing.assert_allclose(actual, expected.T@expected/len(p), atol=1e-14)


if __name__ == '__main__':
    unittest.main()
