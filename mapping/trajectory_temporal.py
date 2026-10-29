"""Check fitted poses against time-ordered, short-interval relative motion."""
import numpy as np
from scipy.spatial.transform import Rotation


def motion_error(previous, current, previous_estimate, current_estimate):
    expected = previous @ np.linalg.inv(previous_estimate) @ current_estimate
    distance = float(np.linalg.norm(current[:3, 3]-expected[:3, 3]))
    angle = float(np.rad2deg(Rotation.from_matrix(expected[:3, :3].T@current[:3, :3]).magnitude()))
    return distance, angle


def motion_limits(previous_estimate, current_estimate, elapsed):
    travel = float(np.linalg.norm(current_estimate[:3, 3]-previous_estimate[:3, 3]))
    elapsed = abs(elapsed)
    if elapsed <= .5+1e-6:
        return .15+.03*travel+.005*elapsed, 3.+.1*elapsed
    return .25+.10*travel+.02*elapsed, 3.+.5*elapsed


def continuous_estimate(estimate, a, b):
    trajectory = getattr(estimate, 'frontend', estimate)
    times = getattr(trajectory, 'times', None)
    if times is None:
        return True
    lo, hi = sorted((a, b))
    first = max(0, int(np.searchsorted(times, lo, side='right'))-1)
    last = min(len(times)-1, int(np.searchsorted(times, hi, side='left')))
    return bool(np.all(np.diff(times[first:last+1]) <= trajectory.max_gap+1e-6))


def bridge_limits(estimate, a, b):
    """Use measured travel, including loops, when allowing drift across a gap."""
    trajectory = getattr(estimate, 'frontend', estimate)
    times = getattr(trajectory, 'times', None)
    if times is None:
        F = estimate.pose([a,b])
        travel = float(np.linalg.norm(F[1,:3,3]-F[0,:3,3]))
    else:
        if not hasattr(trajectory, '_motion_arc'):
            trajectory._motion_arc = np.r_[0., np.cumsum(np.linalg.norm(
                np.diff(trajectory.matrices[:,:3,3],axis=0),axis=1))]
        travel = float(abs(np.diff(np.interp([a,b],times,trajectory._motion_arc))[0]))
    elapsed = abs(b-a)
    if elapsed <= .5+1e-6:
        return .15+.03*travel+.005*elapsed, 3.+.1*elapsed
    return .25+.10*travel+.02*elapsed, 3.+.5*elapsed


def screen_temporal(poses, timestamps, estimate, gap_s=.5, qualities=None):
    """Find the best-supported chronological chain in each connected interval.

    Missing fitted scans do not break the odometry connection. Only a real
    input-trajectory gap does. Competing chains are scored by held-out map
    support, never by whether a pose came from the saved baseline.
    """
    indices = sorted(poses, key=lambda i: timestamps[i])
    if not indices:
        return {}, []
    times = np.array([timestamps[i] for i in indices])
    if np.any(np.diff(times) <= 0):
        raise ValueError('Fitted pose timestamps must strictly increase')
    frontend = estimate.pose(times)
    fitted = np.array([poses[i] for i in indices])
    corrections = fitted @ np.linalg.inv(frontend)
    trajectory = getattr(estimate, 'frontend', estimate)
    input_times = getattr(trajectory, 'times', None)
    if input_times is None:
        # A provider without its sampling support cannot certify a gap bridge.
        cuts = np.r_[0, np.flatnonzero(np.diff(times)>gap_s+1e-6)+1, len(indices)]
        arc = np.r_[0., np.cumsum(np.linalg.norm(np.diff(frontend[:, :3, 3], axis=0), axis=1))]
    else:
        input_times = np.asarray(input_times)
        breaks = input_times[1:][np.diff(input_times)>getattr(trajectory, 'max_gap', gap_s)+1e-6]
        labels = np.searchsorted(breaks, times, side='right')
        cuts = np.r_[0, np.flatnonzero(np.diff(labels))+1, len(indices)]
        path = np.r_[0., np.cumsum(np.linalg.norm(np.diff(trajectory.matrices[:, :3, 3], axis=0), axis=1))]
        arc = np.interp(times, input_times, path)
    weights = np.ones(len(indices))
    if qualities is not None:
        # Out-of-map validation points keep their 20 cm capped cost. A long
        # low-overlap false fragment must not outvote a well-supported chain.
        weights = np.array([max(0., float(qualities[i].get('coverage_20cm', 1.)) *
            (1.-min(.04, float(qualities[i].get('inlier_distance_mse_m2', 0.)))/.04))
            for i in indices])
    rejected, edges = set(), []
    def check(a, b):
        predicted = corrections[a,:3,:3]@frontend[b,:3,3]+corrections[a,:3,3]
        distance = float(np.linalg.norm(fitted[b,:3,3]-predicted))
        cosine = (np.sum(corrections[a,:3,:3]*corrections[b,:3,:3])-1)*.5
        angle = float(np.rad2deg(np.arccos(np.clip(cosine,-1,1))))
        elapsed = abs(times[b]-times[a])
        if elapsed <= gap_s+1e-6:
            limit = motion_limits(frontend[a],frontend[b],elapsed)
        else:
            # Allow accumulated odometry drift across missing fits, rather than
            # either ignoring the bridge or imposing the short-step 50 cm cap.
            limit = (.25+.10*abs(arc[b]-arc[a])+.02*elapsed, 3.+.5*elapsed)
        return distance <= limit[0] and angle <= limit[1], (distance,angle), limit

    for lo,hi in zip(cuts[:-1],cuts[1:]):
        boundaries = [i for i in range(lo+1,hi) if not check(i-1,i)[0]]
        if not boundaries:
            continue
        runs = [int(lo),*boundaries,int(hi)]
        best_count, best, best_failures = -1., set(), []
        # A short bad endpoint must not invalidate the rest of a long good run.
        for begin,end in zip(runs[:-1],runs[1:]):
            seed = (begin+end-1)//2
            kept, failures = {seed}, []
            for direction in (range(seed+1,hi),range(seed-1,lo-1,-1)):
                anchor = seed
                for i in direction:
                    valid,error,limit = check(anchor,i)
                    if valid:
                        kept.add(i);anchor=i
                    else:
                        failures.append(dict(index=indices[i],anchor_index=indices[anchor],disagreement_m_deg=list(error),limits_m_deg=list(limit)))
            score = float(weights[list(kept)].sum())
            if score>best_count+1e-9:
                best_count,best,best_failures=score,kept,failures
            elif abs(score-best_count)<=1e-9:
                best &= kept
                best_failures.extend(failures)
        removed = {indices[i] for i in range(lo,hi) if i not in best}
        rejected.update(removed)
        edges.extend(e for e in best_failures if e['index'] in removed)
    return {i: T for i, T in poses.items() if i not in rejected}, edges
