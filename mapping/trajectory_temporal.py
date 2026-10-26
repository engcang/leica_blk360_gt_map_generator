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
    return min(.5, .15+.03*travel+.005*abs(elapsed)), min(10., 3.+.1*abs(elapsed))


def screen_temporal(poses, timestamps, estimate, gap_s=.5):
    """Find the best-supported chronological chain in each connected interval.

    Neither endpoint is assumed correct. Competing chains are tested from each
    locally consistent run; equal-support ambiguity remains rejected.
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
    rejected, edges = set(), []
    def check(a, b):
        predicted = corrections[a,:3,:3]@frontend[b,:3,3]+corrections[a,:3,3]
        distance = float(np.linalg.norm(fitted[b,:3,3]-predicted))
        cosine = (np.sum(corrections[a,:3,:3]*corrections[b,:3,:3])-1)*.5
        angle = float(np.rad2deg(np.arccos(np.clip(cosine,-1,1))))
        limit = motion_limits(frontend[a],frontend[b],times[b]-times[a])
        return distance <= limit[0] and angle <= limit[1], (distance,angle), limit

    cuts = np.r_[0,np.flatnonzero(np.diff(times)>gap_s+1e-6)+1,len(indices)]
    for lo,hi in zip(cuts[:-1],cuts[1:]):
        boundaries = [i for i in range(lo+1,hi) if not check(i-1,i)[0]]
        if not boundaries:
            continue
        runs = [int(lo),*boundaries,int(hi)]
        best_count, best, best_failures = -1, set(), []
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
            if len(kept)>best_count:
                best_count,best,best_failures=len(kept),kept,failures
            elif len(kept)==best_count:
                best &= kept
                best_failures.extend(failures)
        removed = {indices[i] for i in range(lo,hi) if i not in best}
        rejected.update(removed)
        edges.extend(e for e in best_failures if e['index'] in removed)
    return {i: T for i, T in poses.items() if i not in rejected}, edges
