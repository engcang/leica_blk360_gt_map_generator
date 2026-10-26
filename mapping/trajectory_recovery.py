"""Bidirectional 1 cm recovery with independent geometry and temporal checks."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import json
from pathlib import Path
import time

import numpy as np
from scipy.spatial.transform import Rotation

from trajectory_fine import FineMatcher
from trajectory_io import digest, save_json, transform, PoseGap
from trajectory_geometry import Matcher, voxel
from trajectory_temporal import screen_temporal, motion_limits
from parallel_runtime import cacheBudget
from point_to_plane import FINE_ENGINE, RECOVERY_DISTANCES, RECOVERY_ITERATIONS


def pose(row):
    T=np.eye(4); T[:3,3]=row[1:4]; T[:3,:3]=Rotation.from_quat(row[4:8]).as_matrix()
    return T


def difference(a,b):
    return (float(np.linalg.norm(a[:3,3]-b[:3,3])),
            float(np.rad2deg(Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).magnitude())))


def partition(scan):
    p=voxel(scan.points,.01)
    return p[::2],voxel(p[1::2],.10)


def capped_distance_cost(quality):
    coverage = quality.get('coverage_20cm', 0.)
    return coverage*quality.get('inlier_distance_mse_m2', .04)+(1-coverage)*.04


def choose_seed(hypotheses):
    """Prefer clearly better held-out support; abstain on competing alignments."""
    if not hypotheses:
        return None
    ordered = sorted(hypotheses, key=lambda h: np.mean(h['costs']))
    best = ordered[0]
    if not best['accepted']:
        return None
    for other in ordered[1:]:
        dm, da = difference(best['T'], other['T'])
        if dm <= .15 and da <= 3:
            continue
        a, b = np.asarray(best['costs']), np.asarray(other['costs'])
        # Same withheld scans, with out-of-gate points charged at 20 cm.
        # An inlier-only average must not reward losing difficult surfaces.
        if not (np.mean(a) <= .8*np.mean(b) and np.all(a <= b*1.1+1e-6)):
            return None
    return best['T']


class Recovery:
    def __init__(self, baseline, output, cache, source, workers, progress):
        self.baseline, self.output = Path(baseline), Path(output)
        self.cache, self.source, self.progress = Path(cache), source, progress
        self.rows = [json.loads(x) for x in (self.baseline/'quality.jsonl').read_text().splitlines()]
        if (len({q['index'] for q in self.rows}) != len(self.rows) or
                np.any(np.diff([q['timestamp'] for q in self.rows]) <= 0) or
                np.any(np.diff([q['index'] for q in self.rows]) <= 0)):
            raise ValueError('Baseline scan indices and timestamps must strictly increase')
        self.rows_by_index = {q['index']:q for q in self.rows}
        self.output.mkdir(parents=True, exist_ok=True)
        self.original_poses = np.loadtxt(self.baseline/'candidate_trajectory.tum', ndmin=2) if (self.baseline/'candidate_trajectory.tum').stat().st_size else np.empty((0, 8))
        self.A = np.asarray(json.loads((self.baseline/'initial_alignment.json').read_text())['T_map_from_frontend'])
        self.scans = OrderedDict()
        self.scan_cache_limit = cacheBudget(.03)
        self.scan_cache_bytes = 0
        self.poses, self.quality = {}, {}
        self.quarantined, self.global_attempts = [], 0
        self.latest_attempt, self.fit_cache, self.seed_cache = {}, {}, {}
        self.kiss_targets = {}
        self.seed_hypotheses = []
        self.conflicted = set()
        self.provenance = dict(protocol='studio-recovery-v8', input=self.source.provenance,
            baseline_trajectory_sha256=digest(self.baseline/'candidate_trajectory.tum'),
            baseline_quality_sha256=digest(self.baseline/'quality.jsonl'),
            map=json.loads((cache/'map.json').read_text()),
            registration='Multiscale KISS / Open3D point-to-plane + Tukey 1cm', fine_engine=FINE_ENGINE,
            seeds=dict(window_radii_s=[1., 3.], kiss_resolutions_m=[.3, .5, .08],
                       trusted_end_guesses=True, anchor_withheld=True, retry_partial_fits=True,
                       direct_fine_before_coarse=True, compare_coarse_candidates=True,
                       selection='withheld-capped-distance', competing_cost_ratio=.8,
                       retry_conflicting_tracks=True, iterations_per_stage=180),
            validation=dict(map_voxel_m=.01, planar_neighbors=20, planar_radius_m=.3,
                            temporal_screen='supported-temporal-chain-v2'),
            target_normals='original-map-4cm-40-with-tile-halo-v1',
            correspondence_distances=list(RECOVERY_DISTANCES), iterations_per_stage=RECOVERY_ITERATIONS,
            relative_fitness=1e-6, relative_rmse=1e-6, solver_threads=1,
            thresholds=dict(coverage_20cm=.65, plane_rms_m=.08, observable_directions=6,
                            correction_m=.5, correction_deg=10, agreement_m=.15, agreement_deg=3))
        receipt = self.output/'provenance.json'
        if receipt.exists() and json.loads(receipt.read_text()) != self.provenance:
            raise ValueError('Recovery resume engine, inputs, or settings changed; choose a new output folder')
        save_json(receipt, self.provenance)
        self.matcher = Matcher(self.cache, workers)
        self.fine = FineMatcher(self.cache, workers, result_cache=self.output/'fine_requests')
        attempts = self.output/'attempts.jsonl'
        valid_lines = []
        if attempts.exists():
            for line in attempts.read_text().splitlines(keepends=True):
                try:
                    q = json.loads(line)
                except ValueError:
                    break
                if not line.endswith('\n'):
                    break
                valid_lines.append(line)
                if q.get('stage') in ('submap_seed', 'submap_seed_failed') and q.get('scope') in ('regional', 'global'):
                    self.global_attempts += 1
                if 'fit_key' in q:
                    self.fit_cache[q['fit_key']] = q
                if q.get('stage') == 'seed_complete':
                    self.seed_cache[(q['index'], q.get('allow_global', True))] = q.get('T_map_from_base')
            attempts.write_text(''.join(valid_lines))
        self.attempt_file = attempts.open('a', buffering=1)
        save_json(self.output/'initial_alignment.json', {'T_map_from_frontend':self.A.tolist()})

    def scan(self,index):
        if index in self.scans:
            self.scans.move_to_end(index); return self.scans[index]
        found=list(self.source.iter_scans(scan_indices=[index]))
        if not found:return None
        s=found[0]
        limit = min(self.scan_cache_limit, cacheBudget(.03))
        while self.scans and (len(self.scans)>=32 or self.scan_cache_bytes+s.points.nbytes>limit):
            _, old = self.scans.popitem(last=False); self.scan_cache_bytes -= old.points.nbytes
        if s.points.nbytes <= limit:
            self.scans[index]=s; self.scan_cache_bytes += s.points.nbytes
        return s

    def log(self,row):
        self.attempt_file.write(json.dumps(row,allow_nan=False)+'\n')

    def fit(self,s,initial,stage):
        import hashlib
        if abs(s.timestamp-self.rows_by_index[s.index]['timestamp']) > 1e-6:
            raise ValueError(f'Scan/pose timestamp mismatch at index {s.index}')
        key = f'{stage}:{s.index}:' + hashlib.sha256(np.asarray(initial, dtype='<f8').tobytes()).hexdigest()
        if key in self.fit_cache:
            q = dict(self.fit_cache[key])
            self.latest_attempt[s.index] = q
            return np.asarray(q['T_map_from_base']), q
        train,held=partition(s)
        try:
            T,info=self.fine.align(train,initial)
        except (RuntimeError,ValueError) as exc:
            T,info=initial.copy(),dict(converged=False,reason='registration_error',detail=str(exc))
        q=self.fine.quality(held,T)
        q.update(index=s.index,timestamp=s.timestamp,stage=stage,fine=info,
                 correction_m_deg=difference(T,initial),T_map_from_base=T.tolist())
        # A solver failure never becomes a new tracking anchor merely because
        # the initial pose happens to pass the coarse geometry check.
        if not info.get('converged'):
            q.update(accepted=False,reason='fine_registration_failed')
        q["fit_key"] = key
        self.log(q)
        self.fit_cache[key] = q
        self.latest_attempt[s.index]=q
        return T,q

    def keep_baseline(self):
        accepted=[q for q in self.rows if q['accepted']]
        if len(accepted)!=len(self.original_poses):raise ValueError('Baseline pose/quality mismatch')
        for k,(q,row) in enumerate(zip(accepted,self.original_poses)):
            if abs(row[0]-q['timestamp'])>1e-6:raise ValueError('Baseline timestamp mismatch')
            T=pose(row); isolated=False
            # A lone accepted pose inside a rejection run must not seed both
            # directions after a large unverified correction (e.g. 90deg).
            if k and k+1<len(accepted):
                isolated=(q['timestamp']-accepted[k-1]['timestamp']>.5 and
                          accepted[k+1]['timestamp']-q['timestamp']>.5)
            elif k and q['timestamp']-accepted[k-1]['timestamp']>.5:
                isolated=True
            if isolated and k:
                prev=self.original_poses[k-1]; F=self.source.pose([prev[0],row[0]])
                delta=difference(T,pose(prev)@np.linalg.inv(F[0])@F[1])
                if delta[0]>1 or delta[1]>15:
                    self.quarantined.append(q['index']);continue
            self.poses[q['index']]=T
            self.quality[q['index']]=dict(q,stage='baseline_retained',baseline_reused=True)
        self.baseline_temporal_removed = sorted(self.screen_poses('baseline_temporal_audit'))

    def screen_poses(self, stage):
        times = {q['index']: q['timestamp'] for q in self.rows}
        screened, edges = screen_temporal(self.poses, times, self.source)
        all_edges = list(edges)
        while edges:
            screened, edges = screen_temporal(screened, times, self.source)
            all_edges.extend(edges)
        removed = set(self.poses)-set(screened)
        for index in sorted(removed):
            self.quality[index] = dict(self.quality.get(index, {}), index=index,
                timestamp=times[index], accepted=False, reason='temporal_inconsistency', stage=stage)
        self.poses = screened
        if hasattr(self, 'attempt_file'):
            self.log(dict(stage=stage, rejected_indices=sorted(removed), edges=all_edges))
        return removed

    def submap_seed(self,index,allow_global=True):
        key = (index, allow_global)
        if key in self.seed_cache:
            value = self.seed_cache[key]
            return np.asarray(value) if value is not None else None
        result = self._submap_seed(index,allow_global)
        self.seed_cache[key] = result.tolist() if result is not None else None
        self.log(dict(stage='seed_complete', index=index, allow_global=allow_global, T_map_from_base=self.seed_cache[key]))
        return result

    def seed_guesses(self, anchor):
        guesses = [('initial_alignment', self.A @ anchor.frontend_T)]
        for label, candidates in (
            ('before_anchor', [i for i in self.poses if i < anchor.index]),
            ('after_anchor', [i for i in self.poses if i > anchor.index])):
            if not candidates:
                continue
            i = max(candidates) if label == 'before_anchor' else min(candidates)
            previous = self.scan(i)
            if previous is not None:
                guesses.append((label, self.poses[i] @ np.linalg.inv(previous.frontend_T) @ anchor.frontend_T))
        return guesses

    def seed_window(self, index, radius):
        anchor = self.scan(index)
        if anchor is None:
            self.log(dict(stage='submap_skipped', index=index, radius_s=radius,
                          reason='anchor_scan_unavailable'))
            return None
        times = self.source.timestamps
        nearby = np.flatnonzero(abs(times-times[index]) <= radius)
        count = 11 if radius <= 1 else 21
        nearby = nearby[np.unique(np.linspace(0, len(nearby)-1, min(count, len(nearby))).astype(int))]
        scans = [self.scan(int(i)) for i in nearby]
        scans = [s for s in scans if s is not None]
        if len(scans) < 5:
            return None
        ts = np.array([s.timestamp for s in scans])
        ps = np.array([s.frontend_T[:3, 3] for s in scans])
        if np.any(np.diff(ts) > .5) or np.any(np.linalg.norm(np.diff(ps, axis=0), axis=1)/np.diff(ts) > 8):
            self.log(dict(stage='submap_skipped', index=index, radius_s=radius, reason='unsupported_relative_motion'))
            return None
        # Withhold the anchor and entire validation scans from seed fitting.
        others = [s for s in scans if s.index != index]
        train_scans, validators = others[::2], others[1::2]
        if len(train_scans) < 2 or len(validators) < 2:
            return None
        train = voxel(np.concatenate([transform(voxel(s.points, .01),
            np.linalg.inv(anchor.frontend_T) @ s.frontend_T) for s in train_scans]), .01)
        return anchor, train, validators

    def validate_seed(self, anchor, train, validators, initial, scope, radius, resolution=None):
        self.progress(f'Validating scan {anchor.index}: {2*radius:g} s submap, {scope}, 1 cm fitting')
        self.log(dict(stage='submap_seed_started', index=anchor.index, scope=scope,
                      radius_s=radius, resolution_m=resolution, started_unix=time.time()))
        try:
            T, info = self.fine.align(train, initial, iterations=180)
        except (RuntimeError, ValueError) as exc:
            self.log(dict(stage='submap_seed_failed', index=anchor.index, scope=scope,
                          radius_s=radius, resolution_m=resolution, reason=str(exc)))
            return None
        _, held = partition(anchor)
        aq = self.fine.quality(held, T)
        costs = [capped_distance_cost(aq)]
        context = []
        for label, ids in (('before', [i for i in self.poses if i < anchor.index]),
                           ('after', [i for i in self.poses if i > anchor.index])):
            if not ids:
                continue
            i = max(ids) if label == 'before' else min(ids)
            stamp = self.rows_by_index[i]['timestamp']
            if abs(stamp-anchor.timestamp) <= 3:
                F = self.source.pose([stamp])[0]
                context.append(difference(T, self.poses[i]@np.linalg.inv(F)@anchor.frontend_T))
        consistent = all(dm <= .5 and da <= 10 for dm, da in context)
        checks = []
        if aq['accepted'] and consistent:
            for s in validators[::max(1, len(validators)//3)][:3]:
                guess = T @ np.linalg.inv(anchor.frontend_T) @ s.frontend_T
                costs.append(capped_distance_cost(self.fine.quality(partition(s)[1], guess)))
                fitted, q = self.fit(s, guess, 'submap_heldout')
                checks.append(dict(q, agreement_m_deg=difference(fitted, guess)))
        good = sum(q['accepted'] and q['agreement_m_deg'][0] <= .15 and
                   q['agreement_m_deg'][1] <= 3 for q in checks)
        plausible = bool(aq['accepted'] and good >= 2 and consistent)
        ok = bool(info.get('converged') and plausible)
        if plausible:
            self.seed_hypotheses.append(dict(T=T, costs=costs, accepted=ok))
        self.log(dict(stage='submap_seed', index=anchor.index, scope=scope, radius_s=radius,
                      resolution_m=resolution, T_map_from_base=T.tolist(), accepted=ok,
                      anchor_quality=aq, checks=checks, fine=info, heldout_capped_costs=costs,
                      nearby_anchor_disagreement_m_deg=context))
        return T if ok else None

    def _submap_seed(self, index, allow_global=True):
        # Try both local windows before constructing expensive whole-map
        # descriptors. The dense global search is the final fallback.
        for radius in (1., 3.):
            window = self.seed_window(index, radius)
            if window is None:
                continue
            anchor, train, validators = window
            guesses = self.seed_guesses(anchor)
            self.seed_hypotheses = []
            # A drifted global alignment must not replace a nearby trusted end
            # anchor. Try both ends independently before feature relocalization.
            for label, initial in guesses:
                q = self.matcher.quality(partition(anchor)[1], initial)
                if q.get('coverage_20cm', 0) < .3 or q.get('plane_rms_m', 1) >= .12:
                    continue
                self.validate_seed(anchor, train, validators, initial, label, radius)
            for label, initial in guesses:
                coarse = self.matcher.icp(voxel(train, .1), initial)
                q = self.matcher.quality(partition(anchor)[1], coarse)
                if q.get('coverage_20cm', 0) < .3 or q.get('plane_rms_m', 1) >= .12:
                    continue
                self.validate_seed(anchor, train, validators, coarse, label+'_coarse', radius)
            selected = choose_seed(self.seed_hypotheses)
            if selected is not None:
                return selected
            if self.seed_hypotheses:
                self.log(dict(stage='ambiguous_submap_seed', index=index, accepted=False))
        if not allow_global:
            return None
        for resolution in (.3, .5, .08):
            if resolution not in self.kiss_targets:
                self.kiss_targets[resolution] = voxel(self.fine.kiss_points, resolution)
            target = self.kiss_targets[resolution]
            for radius in (1., 3.):
                window = self.seed_window(index, radius)
                if window is None:
                    continue
                anchor, train, validators = window
                guesses = self.seed_guesses(anchor)
                src = voxel(train, resolution)
                moved = transform(src, guesses[-1][1])
                lo, hi = moved.min(0)-5, moved.max(0)+5
                regional = target[np.all((target >= lo) & (target <= hi), axis=1)]
                targets = [('regional', regional), ('global', target)] if 80 <= len(regional) < len(target)*.9 else [('global', target)]
                self.seed_hypotheses = []
                for label, target_points in targets:
                    self.global_attempts += 1
                    self.progress(f'Relocalizing scan {index}: {2*radius:g} s submap, KISS {resolution:g} m, {label}')
                    try:
                        initial = self.fine.native.kiss(src, target_points, resolution, self.fine.workers)
                    except (RuntimeError, ValueError) as exc:
                        self.log(dict(stage='submap_seed_failed', index=index, scope=label,
                                      radius_s=radius, resolution_m=resolution, reason=str(exc)))
                        continue
                    self.validate_seed(anchor, train, validators, initial, label, radius, resolution)
                selected = choose_seed(self.seed_hypotheses)
                if selected is not None:
                    return selected
                if self.seed_hypotheses:
                    self.log(dict(stage='ambiguous_submap_seed', index=index, accepted=False))
        return None

    def propagate(self,indices,seed_index,seed_T,direction):
        result={}
        seed=self.scan(seed_index)
        if seed is None:return result
        last_T,last_F,last_time=seed_T,seed.frontend_T,seed.timestamp
        lost=0
        last_progress = 0.
        for n,index in enumerate(indices):
            s=self.scan(index)
            if s is None:continue
            if time.monotonic()-last_progress >= 5:
                self.progress(f'Recovering {direction}: scan {index}, {len(result)} supported poses')
                last_progress = time.monotonic()
            initial=last_T@np.linalg.inv(last_F)@s.frontend_T
            T,q=self.fit(s,initial,direction)
            dm,da=difference(T,initial)
            # Large independent corrections need the submap verification path;
            # elapsed time since an old anchor must not excuse a 90deg jump.
            limit_m, limit_deg = motion_limits(last_F, s.frontend_T, s.timestamp-last_time)
            supported=q['accepted'] and dm<=limit_m and da<=limit_deg
            if supported:
                last_T,last_F,last_time=T,s.frontend_T,s.timestamp
                result[index]=(T,q)
            # Partial overlap at a stale pose must not postpone relocalization
            # through thousands of rejected scans.
            lost=0 if supported else lost+1
            if lost>=10:
                self.log(dict(stage='tracking_lost',index=index,direction=direction,
                    reason='Ten consecutive unsupported fits; use another anchor/submap, not an old seed'))
                break
        return result

    def checkpoint(self):
        pose_tmp=self.output/'candidate_trajectory.tum.tmp'
        quality_tmp=self.output/'quality.jsonl.tmp'
        with pose_tmp.open('w') as f,quality_tmp.open('w') as g:
            for original in self.rows:
                index=original['index'];q=dict(self.quality.get(index,self.latest_attempt.get(index,original)))
                if q.get('accepted') and index not in self.poses:
                    dm, da = q.get('correction_m_deg', [0, 0])
                    q['reason'] = ('unverified_large_correction' if dm > .5 or da > 10
                                   else 'unverified_submap_seed')
                q['accepted']=index in self.poses
                if index in self.conflicted:
                    q.update(accepted=False, reason='seed_disagreement')
                if index in self.quarantined and index not in self.poses:
                    q.update(reason='isolated_large_correction_quarantined',stage='quarantined')
                if index in self.poses:
                    T=self.poses[index];row=np.r_[original['timestamp'],T[:3,3],Rotation.from_matrix(T[:3,:3]).as_quat()]
                    f.write(' '.join(f'{x:.9f}' for x in row)+'\n')
                g.write(json.dumps(q,allow_nan=False)+'\n')
        pose_tmp.replace(self.output/'candidate_trajectory.tum');quality_tmp.replace(self.output/'quality.jsonl')

    def merge_direction(self,dest,new):
        for i,value in new.items():
            if i in self.conflicted:continue
            if i in dest:
                dm,da=difference(dest[i][0],value[0])
                if dm>.15 or da>3:
                    self.conflicted.add(i);dest.pop(i)
                    self.log(dict(stage='seed_disagreement',index=i,accepted=False,translation_m=dm,rotation_deg=da))
                    continue
            dest[i]=value

    def run(self, local_only=False):
        started=time.monotonic();self.keep_baseline();baseline_count=len(self.poses)
        self.checkpoint()
        for allow_global in ((False,) if local_only else (False, True)):
            blocks=[];block=[]
            for q in self.rows:
                if q['index'] not in self.poses:block.append(q['index'])
                elif block:blocks.append(block);block=[]
            if block:blocks.append(block)
            suspect = set(self.baseline_temporal_removed)
            blocks.sort(key=lambda b: (not bool(suspect.intersection(b)), -len(b), b[0]))
            original_anchors=dict(self.poses)
            for block_number,indices in enumerate(blocks):
                lo,hi=indices[0],indices[-1]
                before=[i for i in original_anchors if i<lo];after=[i for i in original_anchors if i>hi]
                forward={};backward={}
                if before:
                    i=max(before);forward=self.propagate(indices,i,original_anchors[i],'forward_fine')
                if after:
                    i=min(after);backward=self.propagate(list(reversed(indices)),i,original_anchors[i],'reverse_fine')
                disputed = set(self.conflicted).intersection(indices)
                for i in set(forward).intersection(backward):
                    dm, da = difference(forward[i][0], backward[i][0])
                    if dm > .15 or da > 3:
                        disputed.add(i)
                self.conflicted.update(disputed)
                for i in disputed:
                    forward.pop(i, None);backward.pop(i, None)
                verified_indices = set()
                # If no baseline end anchor exists, independently localize the end
                # before attempting reverse tracking. Also restart inside long gaps.
                remaining=[i for i in indices if i not in forward and i not in backward]
                # Partial fits also get independent seeds; good residuals at a
                # wrong local minimum must not suppress relocalization.
                search_needed = remaining
                if search_needed:
                    # End/middle/start first, then other stable time segments. A
                    # failed seed does not abandon the rest of a long interval.
                    seeds=list(dict.fromkeys([search_needed[-1],search_needed[len(search_needed)//2],search_needed[0]]))
                    last_stamp=self.rows_by_index[search_needed[0]]['timestamp']
                    for i in search_needed:
                        stamp=self.rows_by_index[i]['timestamp']
                        if stamp-last_stamp>=30:
                            seeds.append(i);last_stamp=stamp
                    seeds=list(dict.fromkeys(seeds))
                    for seed_index in seeds:
                        if seed_index in forward or seed_index in backward:continue
                        T=self.submap_seed(seed_index, allow_global=allow_global)
                        if T is None:continue
                        s=self.scan(seed_index)
                        # validate_seed may have moved the target to a held-out scan;
                        # obtain the anchor's own final fit and validation again.
                        T,q=self.fit(s,T,'verified_submap_anchor')
                        if not q['accepted']:continue
                        q.update(index=s.index,timestamp=s.timestamp,stage='verified_submap_seed')
                        new_forward={seed_index:(T,q),**self.propagate([i for i in indices if i>seed_index],seed_index,T,'submap_forward')}
                        new_backward={seed_index:(T,q),**self.propagate([i for i in reversed(indices) if i<seed_index],seed_index,T,'submap_reverse')}
                        covered=set(new_forward)|set(new_backward)
                        resolved=covered.intersection(disputed)-verified_indices
                        for i in resolved:
                            forward.pop(i,None);backward.pop(i,None)
                            self.conflicted.discard(i)
                        if resolved:
                            self.log(dict(stage='conflict_relocalized',seed_index=seed_index,indices=sorted(resolved)))
                        self.merge_direction(forward,new_forward)
                        self.merge_direction(backward,new_backward)
                        verified_indices.update(covered)
                for i in sorted(set(forward)|set(backward)):
                    if i in self.conflicted:continue
                    a,b=forward.get(i),backward.get(i)
                    if a and b:
                        dm,da=difference(a[0],b[0])
                        if dm>.15 or da>3:
                            self.conflicted.add(i)
                            self.log(dict(stage='direction_disagreement',index=i,accepted=False,translation_m=dm,rotation_deg=da));continue
                        chosen=min((a,b),key=lambda x:x[1]['plane_rms_m'])
                        chosen[1]['direction_agreement_m_deg']=[dm,da]
                    else:chosen=a or b
                    self.poses[i]=chosen[0];self.quality[i]=dict(chosen[1],baseline_reused=False)
                self.screen_poses('recovery_temporal_audit')
                self.checkpoint()
                self.progress(f'Recovered {len(self.poses)-baseline_count} poses; interval {block_number+1}/{len(blocks)}')
                print(f'{self.source.sequence}: block {block_number+1}/{len(blocks)}, {len(self.poses)-baseline_count} recovered',flush=True)
        self.source.verify_unchanged();self.fine.verify_unchanged()
        if digest(self.baseline/'candidate_trajectory.tum')!=self.provenance['baseline_trajectory_sha256']:
            raise ValueError('Baseline changed during recovery')
        self.attempt_file.flush()
        # Preserve baseline skipped-input facts; recovery does not invent deskew.
        save_json(self.output/'input_quality.json',json.loads((self.baseline/'input_quality.json').read_text()))
        save_json(self.output/'status.json',dict(status='candidate_complete',processed=len(self.rows),
            pending_global_search=bool(local_only),
            accepted=len(self.poses),rejected=len(self.rows)-len(self.poses),baseline_retained=baseline_count,
            recovered=len(self.poses)-baseline_count,quarantined_indices=self.quarantined,
            baseline_temporal_rejected=self.baseline_temporal_removed,
            elapsed_s=time.monotonic()-started,elapsed_scope='current_recovery_invocation',global_attempts=self.global_attempts,
            has_invalid_intervals=len(self.poses)<len(self.rows),certified_gt=False))


    def close(self):
        self.attempt_file.close()
        self.fine.native.close()
