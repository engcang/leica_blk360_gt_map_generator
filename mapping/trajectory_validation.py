"""Independent map evidence for partial views and jointly fitted local windows.

These are explicit engineering defaults, not sensor-noise-calibrated GT bounds.
Relative SLAM motion only tests agreement; it never contributes information to
an observability matrix or supplies a missing fitted degree of freedom.
"""
import itertools
import numpy as np
from scipy.spatial.transform import Rotation

from trajectory_temporal import motion_limits
from trajectory_io import PoseGap

VALIDATION_PROTOCOL = 'independent-map-context-v2'
CONTEXT_RADIUS_S = 1.
CONTEXT_MIN_SCANS = 2
LOW_OVERLAP_CONTEXT = .65
MIN_SUPPORT_POINTS = 80
MIN_SUPPORT_CELLS = 6
SUPPORT_CELL_M = .5
MIN_PRINCIPAL_SPAN_M = .5
INFORMATION_RELATIVE_EIGENVALUE = 1e-3
INFORMATION_ABSOLUTE_EIGENVALUE = 1e-5


def observability(information):
    values, vectors = np.linalg.eigh(information)
    threshold = max(values[-1]*INFORMATION_RELATIVE_EIGENVALUE,
                    INFORMATION_ABSOLUTE_EIGENVALUE)
    strong = values > threshold
    return values, vectors[:, ~strong], int(np.count_nonzero(strong))


def raw_geometry_supported(q):
    """Use raw measurements, never another contextual acceptance decision."""
    return bool(q.get('geometry_supported') and q.get('fine', {}).get('converged')
                and q.get('reason') not in ('unverified_large_correction',
                    'temporal_inconsistency', 'fine_registration_failed',
                    'seed_disagreement', 'scan_timestamp_mismatch', 'tracking_motion_inconsistent',
                    'unconstrained_submap_departure', 'isolated_large_correction_quarantined',
                    'scan_timestamp_unavailable'))


def motion_agrees(a, b, source):
    stamps = [a['timestamp'], b['timestamp']]
    if stamps[0] == stamps[1] or a['index'] == b['index']:
        return False
    F = source.pose(stamps)
    A, B = np.asarray(a['T_map_from_base']), np.asarray(b['T_map_from_base'])
    expected = A @ np.linalg.inv(F[0]) @ F[1]
    distance = float(np.linalg.norm(B[:3, 3]-expected[:3, 3]))
    angle = float(np.rad2deg(Rotation.from_matrix(expected[:3, :3].T@B[:3, :3]).magnitude()))
    limit = motion_limits(F[0], F[1], stamps[1]-stamps[0])
    return distance <= limit[0] and angle <= limit[1]


def confirm_partial_views(rows, source):
    """Confirm low-coverage full-rank fits using two independent nearby fits.

    Each witness has full-rank raw map geometry itself. Contextually promoted
    rank-deficient poses cannot serve as witnesses. No refitting is performed.
    """
    changed = []
    if hasattr(source, 'timestamps'):
        for q in rows:
            if not raw_geometry_supported(q) or q.get('observable_directions') != 6:
                continue
            index = q['index']
            if index < 0 or index >= len(source.timestamps):
                q.update(accepted=False, reason='scan_timestamp_mismatch')
                continue
            try:
                expected = (source.anchor_timestamp(index) if hasattr(source, 'anchor_timestamp')
                            else source.timestamps[index])
            except PoseGap:
                q.update(accepted=False, reason='scan_timestamp_unavailable')
                continue
            if abs(q['timestamp']-expected) > 1e-6:
                q.update(accepted=False, reason='scan_timestamp_mismatch')
    eligible = [q for q in rows if raw_geometry_supported(q)
                and q.get('observable_directions') == 6]
    eligible.sort(key=lambda q: q['timestamp'])
    stamps = np.array([q['timestamp'] for q in eligible])
    for q in eligible:
        if q.get('accepted') or q.get('reason') != 'partial_view_needs_context':
            continue
        lo, hi = np.searchsorted(stamps, [q['timestamp']-CONTEXT_RADIUS_S,
                                          q['timestamp']+CONTEXT_RADIUS_S], side='left')
        witnesses = [r for r in eligible[lo:hi+1] if r['index'] != q['index']
                     and 0 < abs(r['timestamp']-q['timestamp']) <= CONTEXT_RADIUS_S
                     and motion_agrees(q, r, source)]
        witnesses.sort(key=lambda r: (abs(r['timestamp']-q['timestamp']), r['index']))
        for a, b in itertools.combinations(witnesses[:8], CONTEXT_MIN_SCANS):
            if not motion_agrees(a, b, source):
                continue
            q.update(accepted=True, reason='independent_neighbor_geometry',
                     acceptance_scope='independent-full-rank-scans',
                     context_validation=dict(protocol=VALIDATION_PROTOCOL,
                         witness_indices=[a['index'], b['index']],
                         witness_timestamps=[a['timestamp'], b['timestamp']],
                         shared_transform_fitted=False))
            changed.append(q['index'])
            break
    return changed


def joint_information(qualities, poses, anchor):
    """Rebase independently withheld information to one fitted map-frame origin."""
    total = np.zeros((6, 6)); count = 0
    for q, T in zip(qualities, poses):
        shift = (np.asarray(T)[:3, 3]-np.asarray(anchor)[:3, 3])/5.
        x, y, z = shift
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        change = np.eye(6); change[3:, :3] = -skew
        points = q['points']
        total += points*(change.T @ np.asarray(q['scaled_information_matrix']) @ change)
        count += points
    return total/max(count, 1)


def jointly_observable(qualities, poses, anchor, *, shared_transform_fitted, minimum_scans=CONTEXT_MIN_SCANS+1):
    """Weak poses require an actual common-transform geometric fit first."""
    if not shared_transform_fitted or len(qualities) < minimum_scans:
        return dict(accepted=False, reason='shared_geometric_fit_required')
    if not all(q.get('geometry_supported') for q in qualities):
        return dict(accepted=False, reason='insufficient_independent_geometry')
    information = joint_information(qualities, poses, anchor)
    values, weak, rank = observability(information)
    return dict(accepted=rank == 6, reason='joint_geometry_pass' if rank == 6 else 'joint_weak_directions',
                protocol=VALIDATION_PROTOCOL, shared_transform_fitted=True,
                observable_directions=rank, scaled_information_matrix=information.tolist(),
                scaled_information_eigenvalues=values.tolist(), weak_directions=weak.T.tolist())
