"""Reject an uncertain local fit when held-out TLS evidence favors another basin."""
import time
import numpy as np
from scipy.spatial.transform import Rotation

from trajectory_geometry import Matcher, voxel


def needs_audit(quality):
    # Precise, well-supported fits take no extra queries. These limits only
    # select candidates for a comparison; they do not reject a pose by themselves.
    return (quality.get('geometry_supported', False)
            and quality.get('plane_rms_m', 0.) > .03
            and quality.get('inlier_precision_10cm', 1.) < .95)


def distance_cost(quality):
    coverage = quality.get('coverage_20cm', 0.)
    return coverage*quality.get('inlier_distance_mse_m2', .04)+(1.-coverage)*.04


def conflicting_basin(original, alternative, delta_m, delta_deg):
    # All held-out points contribute: missing map matches are charged at 20cm.
    # A smaller inlier-only RMS cannot win by dropping inconvenient surfaces.
    return bool((delta_m > .15 or delta_deg > 3.)
                and alternative.get('points', 0) >= 80
                and alternative.get('plane_rms_m', 1.) < .06
                and alternative.get('coverage_20cm', 0.) >= original.get('coverage_20cm', 0.)+.05
                and distance_cost(alternative) < .8*distance_cost(original))


def audit(fine, train, held, transform, quality):
    if not needs_audit(quality):
        return quality
    started = time.perf_counter()
    if getattr(fine, '_ambiguity_matcher', None) is None:
        fine._ambiguity_matcher = Matcher(fine.folder.parent, fine.workers)
    matcher = fine._ambiguity_matcher
    training = voxel(train, .1)
    evidence = dict(checked=True, candidates=0, rejected=False)
    for shift in ((0.,0.,0.),(.35,0.,0.),(-.35,0.,0.),(0.,.35,0.),(0.,-.35,0.)):
        initial = transform.copy(); initial[:3,3] += shift
        candidate = matcher.icp(training, initial)
        distance = float(np.linalg.norm(candidate[:3,3]-transform[:3,3]))
        angle = float(np.rad2deg(Rotation.from_matrix(candidate[:3,:3].T@transform[:3,:3]).magnitude()))
        evidence['candidates'] += 1
        if distance <= .15 and angle <= 3.:
            continue
        other = fine.quality(held, candidate)
        if conflicting_basin(quality, other, distance, angle):
            evidence.update(rejected=True, alternative_T=candidate.tolist(),
                delta_m=distance, delta_deg=angle, original_cost=distance_cost(quality),
                alternative_cost=distance_cost(other), alternative_coverage=other.get('coverage_20cm'),
                alternative_plane_rms_m=other.get('plane_rms_m'))
            quality.update(accepted=False, geometry_supported=False, needs_context=False,
                           context_map_supported=False, reason='ambiguous_tls_alignment',
                           acceptance_scope='rejected')
            break
    evidence['seconds'] = time.perf_counter()-started
    quality['local_basin_validation'] = evidence
    return quality
