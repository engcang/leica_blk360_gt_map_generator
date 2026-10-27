"""Bidirectional 1 cm recovery with independent geometry and temporal checks."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import copy
import json
from pathlib import Path
import time

import numpy as np
from scipy.spatial.transform import Rotation

from trajectory_fine import FineMatcher
from trajectory_io import digest, save_json, transform, PoseGap
from trajectory_geometry import Matcher, voxel
from trajectory_temporal import screen_temporal, motion_limits
from trajectory_validation import (confirm_partial_views, jointly_observable, VALIDATION_PROTOCOL,
    LOW_OVERLAP_CONTEXT, MIN_SUPPORT_POINTS, MIN_SUPPORT_CELLS, SUPPORT_CELL_M, MIN_PRINCIPAL_SPAN_M)
from parallel_runtime import cacheBudget
from point_to_plane import FINE_ENGINE, RECOVERY_DISTANCES, RECOVERY_ITERATIONS


def save_recovery_provenance(output, current):
    receipt = output/'provenance.json'
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous != current:
            old, new = dict(previous), dict(current)
            old_backend = old.pop('correspondence', {'backend':'scipy-tiled-map-v1'})
            new_backend = new.pop('correspondence', {'backend':'scipy-tiled-map-v1'})
            # Only the verified reference -> exact native implementation change
            # can reuse checkpoints. Geometry, inputs, validation and solver
            # settings must still match, and native binary changes remain strict.
            compatible = (old == new and old_backend == {'backend':'scipy-tiled-map-v1'}
                          and new_backend.get('backend') == 'native-tiled-map-v1'
                          and len(new_backend.get('sha256', '')) == 64)
            if not compatible:
                raise ValueError('Recovery resume engine, inputs, or settings changed; choose a new output folder')
            with (output/'backend_transitions.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(changed_unix=time.time(), previous=previous,
                    correspondence=new_backend, retained='validated poses and completed attempt journals'), allow_nan=False)+'\n')
    save_json(receipt, current)


def pose(row):
    T=np.eye(4); T[:3,3]=row[1:4]; T[:3,:3]=Rotation.from_quat(row[4:8]).as_matrix()
    return T


def difference(a,b):
    return (float(np.linalg.norm(a[:3,3]-b[:3,3])),
            float(np.rad2deg(Rotation.from_matrix(a[:3,:3].T@b[:3,:3]).magnitude())))


def partition(scan):
    p=voxel(scan.points,.01)
    return p[::2],voxel(p[1::2],.10)


def fit_scan(fine, scan, initial):
    """Every accepted trajectory pose must pass the same 1 cm fit and validation."""
    started = time.perf_counter()
    train, held = partition(scan)
    partitioned = time.perf_counter()
    try:
        T, info = fine.align(train, initial)
    except (RuntimeError, ValueError) as exc:
        T, info = initial.copy(), dict(converged=False, reason='registration_error', detail=str(exc))
    fitted = time.perf_counter()
    q = fine.quality(held, T)
    q.update(index=scan.index, timestamp=scan.timestamp, fine=info,
             correction_m_deg=difference(T, initial), T_map_from_base=T.tolist(),
             partition_s=partitioned-started, validation_s=time.perf_counter()-fitted,
             total_fit_s=time.perf_counter()-started)
    if not info.get('converged'):
        q.update(accepted=False, reason='fine_registration_failed')
    return T, q


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
    def __init__(self, baseline, output, cache, source, workers, progress, *, baseline_is_fine=False, reuse_fits_from=()):
        self.baseline, self.output = Path(baseline), Path(output)
        self.cache, self.source, self.progress = Path(cache), source, progress
        self.baseline_is_fine = baseline_is_fine
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
        self.prepared = OrderedDict()
        self.prepared_bytes = 0
        self.prepared_limit = cacheBudget(.02)
        self.window_cache = OrderedDict()
        self.scan_cache_limit = cacheBudget(.03)
        self.scan_cache_bytes = 0
        self.poses, self.quality = {}, {}
        self.quarantined, self.global_attempts = [], 0
        self.latest_attempt, self.fit_cache, self.seed_cache = {}, {}, {}
        self.kiss_targets = {}
        self.seed_hypotheses = []
        self.seed_certificates, self.context_certificates = {}, {}
        self.conflicted = set()
        self.provenance = dict(protocol='studio-recovery-v12', input=self.source.provenance,
            baseline_policy='verified-1cm' if baseline_is_fine else 'refit-imported-at-1cm',
            baseline_trajectory_sha256=digest(self.baseline/'candidate_trajectory.tum'),
            baseline_quality_sha256=digest(self.baseline/'quality.jsonl'),
            map=json.loads((cache/'map.json').read_text()),
            registration='Multiscale KISS / Open3D point-to-plane + Tukey 1cm', fine_engine=FINE_ENGINE,
            seeds=dict(window_radii_s=[1., 3.], kiss_resolutions_m=[.3, .5, .08],
                       trusted_end_guesses=True, anchor_withheld=True, retry_partial_fits=True,
                       direct_fine_before_coarse=True, compare_coarse_candidates=True,
                       selection='withheld-capped-distance', competing_cost_ratio=.8,
                       retry_conflicting_tracks=True, iterations_per_stage=180,
                       schedule='all-reverse-before-submaps', iteration_limit_continuation=180,
                       exact_fit_reuse_across_stages=True, exact_window_reuse=True),
            validation=dict(map_voxel_m=.01, planar_neighbors=20, planar_radius_m=.3,
                            temporal_screen='supported-temporal-chain-v2',
                            context_protocol=VALIDATION_PROTOCOL, independent_witness_scans=2,
                            rank_deficiency_policy='full-rank-shared-fit-and-independent-joint-information',
                            shared_training_observable_directions=6, training_validation_voxel_m=.1,
                            shared_training_information='actual-final-Tukey-weighted-fit'),
            target_normals='original-map-4cm-40-with-tile-halo-v1',
            correspondence_distances=list(RECOVERY_DISTANCES), iterations_per_stage=RECOVERY_ITERATIONS,
            relative_fitness=1e-6, relative_rmse=1e-6, solver_threads=1,
            thresholds=dict(partial_view_context_below_coverage=LOW_OVERLAP_CONTEXT,
                            minimum_coverage=None, support_points=MIN_SUPPORT_POINTS,
                            support_cells=MIN_SUPPORT_CELLS, support_cell_m=SUPPORT_CELL_M,
                            second_principal_span_m=MIN_PRINCIPAL_SPAN_M,
                            weak_submap_departure_scaled_m=.01,
                            plane_rms_m=.08, joint_observable_directions=6,
                            correction_m=.5, correction_deg=10, agreement_m=.15, agreement_deg=3))
        from native_correspondence import backendInfo as correspondenceInfo
        correspondence = correspondenceInfo()
        if correspondence['backend'] == 'native-tiled-map-v1':
            self.provenance['correspondence'] = correspondence
        save_recovery_provenance(self.output, self.provenance)
        self.matcher = Matcher(self.cache, workers)
        self.fine = FineMatcher(self.cache, workers, result_cache=self.output/'fine_requests')
        # Read-only acceleration: source points, target, initial pose, solver and
        # iteration settings must match the full request hash. Validation is new.
        self.fine.result_sources = tuple(Path(p)/'fine_requests' for p in reuse_fits_from)
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
                    if q.get('certificate'):
                        self.remember_certificate(q['index'], q['certificate'])
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

    def prepared_scan(self, scan):
        """Keep the exact train/held-out split; evict using available RAM."""
        if scan.index in self.prepared:
            self.prepared.move_to_end(scan.index)
            return self.prepared[scan.index]
        result = partition(scan)
        size = sum(p.nbytes for p in result)
        limit = min(self.prepared_limit, cacheBudget(.02))
        while self.prepared and self.prepared_bytes+size > limit:
            _, old = self.prepared.popitem(last=False)
            self.prepared_bytes -= sum(p.nbytes for p in old)
        if size <= limit:
            self.prepared[scan.index] = result
            self.prepared_bytes += size
        return result

    def fit_continued(self, scan, initial):
        # The initial pass is unchanged. Only recovery gives a geometrically
        # supported iteration-limited fit more time before building submaps.
        started = time.perf_counter()
        train, held = self.prepared_scan(scan)
        prepared = time.perf_counter()
        try:
            T, info = self.fine.align(train, initial)
            q = self.fine.quality(held, T)
            if (not info.get('converged') and info.get('convergence_reason') == 'iteration_limit'
                    and q.get('geometry_supported')):
                first = info
                T, info = self.fine.align(train, T, iterations=180)
                info = dict(info, continuation_from=first, continuation_iterations_per_stage=180)
                q = self.fine.quality(held, T)
        except (RuntimeError, ValueError) as exc:
            T, info = initial.copy(), dict(converged=False, reason='registration_error', detail=str(exc))
            q = self.fine.quality(held, T)
        q.update(index=scan.index, timestamp=scan.timestamp, fine=info,
                 correction_m_deg=difference(T, initial), T_map_from_base=T.tolist(),
                 partition_s=prepared-started, total_fit_s=time.perf_counter()-started)
        if not info.get('converged'):
            q.update(accepted=False, reason='fine_registration_failed')
        return T, q

    def fit(self,s,initial,stage):
        import hashlib
        anchor_timestamp = self.source.anchor_timestamp(s.index)
        expected = (self.rows_by_index[s.index]['timestamp'] if s.index in self.rows_by_index
                    else anchor_timestamp)
        if abs(s.timestamp-expected) > 1e-6 or abs(s.timestamp-anchor_timestamp) > 1e-6:
            raise ValueError(f'Scan/pose timestamp mismatch at index {s.index}')
        key = f'{s.index}:' + hashlib.sha256(np.asarray(initial, dtype='<f8').tobytes()).hexdigest()
        if key in self.fit_cache:
            q = copy.deepcopy(self.fit_cache[key])
            q['stage'] = stage
            self.latest_attempt[s.index] = q
            return np.asarray(q['T_map_from_base']), q
        T,q=self.fit_continued(s,initial)
        q['stage']=stage
        q["fit_key"] = key
        self.log(q)
        self.fit_cache[key] = copy.deepcopy(q)
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
            verified = (self.baseline_is_fine and q.get('validation_map_voxel_m') == .01
                        and q.get('fine', {}).get('engine') == FINE_ENGINE
                        and q.get('validation_protocol') == VALIDATION_PROTOCOL
                        and q.get('fine', {}).get('converged'))
            if not verified:
                s = self.scan(q['index'])
                if s is None:
                    self.latest_attempt[q['index']] = dict(q, accepted=False, reason='scan_unavailable')
                    continue
                if k % 25 == 0:
                    self.progress(f'Refining imported baseline at 1 cm: {k+1}/{len(accepted)} poses')
                T, refined = self.fit(s, T, 'baseline_fine')
                q = refined
            if q['accepted']:
                self.poses[q['index']]=T
            self.quality[q['index']]=dict(q,stage='baseline_retained' if verified else 'baseline_fine',
                                         baseline_reused=bool(verified))
        promoted = confirm_partial_views(list(self.quality.values()), self.source)
        for index in promoted:
            self.poses[index] = np.asarray(self.quality[index]['T_map_from_base'])
        self.baseline_temporal_removed = sorted(self.screen_poses('baseline_temporal_audit'))

    def screen_poses(self, stage):
        # Context validation can invalidate an already retained timestamp.
        # Pose membership must never override that rejection at checkpoint.
        self.poses = {i:T for i,T in self.poses.items() if self.quality.get(i, {}).get('accepted', False)}
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
        self.log(dict(stage='seed_complete', index=index, allow_global=allow_global,
                      T_map_from_base=self.seed_cache[key], certificate=self.seed_certificates.get(index)))
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
        key = (index, radius)
        if hasattr(self, 'window_cache') and key in self.window_cache:
            self.window_cache.move_to_end(key)
            return self.window_cache[key]
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
        result = anchor, train, validators
        # Only the current anchor's two windows are useful across KISS scales.
        # Bound retained scans too; this cache owns references outside scan().
        limit = cacheBudget(.02)
        def size(window):
            a, p, vs = window
            return p.nbytes+a.points.nbytes+sum(s.points.nbytes for s in vs)
        if size(result) <= limit:
            while self.window_cache and (len(self.window_cache) >= 2 or
                    sum(size(w) for w in self.window_cache.values())+size(result) > limit):
                self.window_cache.popitem(last=False)
            self.window_cache[key] = result
        return result

    def remember_certificate(self, index, certificate):
        self.seed_certificates[index] = certificate
        for member in certificate['members']:
            self.context_certificates[member['index']] = dict(certificate,
                member=member, shared_anchor_index=index)

    def certify_submap_pose(self, T, q):
        certificate = self.context_certificates.get(q['index'])
        if (certificate is None or not certificate.get('training_quality', {}).get('geometry_supported')
                or certificate['training_quality'].get('observable_directions') != 6
                or certificate.get('optimizer_information', {}).get('observable_directions') != 6
                or not q.get('geometry_supported') or not q.get('fine', {}).get('converged')):
            return
        member = certificate['member']
        dm, da = difference(T, np.asarray(member['T_map_from_base']))
        if abs(q['timestamp']-member['timestamp']) > 1e-6 or dm > .15 or da > 3:
            return
        predicted = np.asarray(member['T_map_from_base'])
        increment = np.r_[5*Rotation.from_matrix(T[:3, :3]@predicted[:3, :3].T).as_rotvec(),
                          T[:3, 3]-predicted[:3, 3]]
        weak = np.asarray(q.get('weak_directions', [])).reshape(-1, 6)
        weak_departure = float(np.linalg.norm(weak@increment))
        if weak_departure > .01:
            q.update(accepted=False, reason='unconstrained_submap_departure')
            return
        # The candidate was wholly excluded from the shared-transform fit,
        # and independent withheld geometry constrains all six directions.
        q.update(accepted=True, reason='independent_submap_geometry',
                 acceptance_scope='jointly-fitted-submap',
                 context_validation=dict(protocol=VALIDATION_PROTOCOL,
                     shared_transform_fitted=True,
                     shared_anchor_index=certificate['shared_anchor_index'],
                     shared_anchor_timestamp=certificate['anchor_timestamp'],
                     witness_indices=[m['index'] for m in certificate['members'] if m['index'] != q['index']],
                     observable_directions=certificate['joint']['observable_directions'],
                     training_observable_directions=certificate['training_quality']['observable_directions'],
                     optimizer_observable_directions=certificate['optimizer_information']['observable_directions'],
                     scaled_information_eigenvalues=certificate['joint']['scaled_information_eigenvalues'],
                     shared_transform_agreement_m_deg=[dm, da],
                     weak_direction_departure_m=weak_departure, weak_direction_limit_m=.01))

    def validate_seed(self, anchor, train, validators, initial, scope, radius, resolution=None):
        self.progress(f'Validating scan {anchor.index}: {2*radius:g} s submap, {scope}, 1 cm fitting')
        self.log(dict(stage='submap_seed_started', index=anchor.index, scope=scope,
                      radius_s=radius, resolution_m=resolution, started_unix=time.time()))
        try:
            T, info = self.fine.align(train, initial, iterations=180, capture_information=True)
        except (RuntimeError, ValueError) as exc:
            self.log(dict(stage='submap_seed_failed', index=anchor.index, scope=scope,
                          radius_s=radius, resolution_m=resolution, reason=str(exc)))
            return None
        # Convergence alone does not mean the shared fit determined every DOF.
        # Check actual training geometry separately from independent validation:
        # a floor-only fit must never inherit lateral coordinates from SLAM.
        training_quality = self.fine.quality(voxel(train, .1), T)
        optimizer_information = info.get('optimizer_information', {})
        training_supported = (training_quality.get('geometry_supported')
                              and training_quality.get('observable_directions') == 6
                              and optimizer_information.get('observable_directions') == 6)
        _, held = self.prepared_scan(anchor)
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
        if training_supported and aq.get('geometry_supported') and consistent and info.get('converged'):
            for s in validators[::max(1, len(validators)//3)][:3]:
                guess = T @ np.linalg.inv(anchor.frontend_T) @ s.frontend_T
                predicted = self.fine.quality(self.prepared_scan(s)[1], guess)
                costs.append(capped_distance_cost(predicted))
                fitted, q = self.fit(s, guess, 'submap_heldout')
                checks.append(dict(q, agreement_m_deg=difference(fitted, guess),
                                   shared_transform_quality=predicted, shared_transform_pose=guess.tolist()))
        valid = [q for q in checks if q.get('geometry_supported') and q.get('fine', {}).get('converged')
                 and q['shared_transform_quality'].get('geometry_supported')
                 and q['agreement_m_deg'][0] <= .15 and q['agreement_m_deg'][1] <= 3]
        joint = jointly_observable([aq]+[q['shared_transform_quality'] for q in valid],
            [T]+[np.asarray(q['shared_transform_pose']) for q in valid], T,
            shared_transform_fitted=bool(info.get('converged')))
        plausible = bool(training_supported and len(valid) >= 2 and consistent and joint['accepted'])
        ok = bool(info.get('converged') and plausible)
        certificate = dict(anchor_timestamp=anchor.timestamp, joint=joint, training_quality=training_quality,
            optimizer_information=optimizer_information,
            members=[dict(index=anchor.index, timestamp=anchor.timestamp, T_map_from_base=T.tolist())]+
                    [dict(index=q['index'], timestamp=q['timestamp'],
                          T_map_from_base=q['shared_transform_pose']) for q in valid])
        if plausible:
            self.seed_hypotheses.append(dict(T=T, costs=costs, accepted=ok, certificate=certificate))
        # Keep the raw per-scan rank in each check; the separate joint record is
        # the evidence that allows a rank-deficient withheld pose to be used.
        for q in valid:
            q['accepted_by_joint_validation'] = ok
        self.log(dict(stage='submap_seed', index=anchor.index, scope=scope, radius_s=radius,
                      resolution_m=resolution, T_map_from_base=T.tolist(), accepted=ok,
                      anchor_quality=aq, checks=checks, fine=info, heldout_capped_costs=costs,
                      training_quality=training_quality, joint_validation=joint,
                      reason='shared_fit_weak_geometry' if not training_supported else joint['reason'],
                      nearby_anchor_disagreement_m_deg=context))
        return T if ok else None

    def _submap_seed(self, index, allow_global=True):
        # Try both local windows before constructing expensive whole-map
        # descriptors. The dense global search is the final fallback.
        local_radii = () if allow_global and (index, False) in self.seed_cache else (1., 3.)
        for radius in local_radii:
            window = self.seed_window(index, radius)
            if window is None:
                continue
            anchor, train, validators = window
            guesses = self.seed_guesses(anchor)
            self.seed_hypotheses = []
            held = self.prepared_scan(anchor)[1]
            coarse_source = voxel(train, .1)
            seen = set()
            # A drifted global alignment must not replace a nearby trusted end
            # anchor. Try both ends independently before feature relocalization.
            for label, initial in guesses:
                key = np.asarray(initial, dtype='<f8').tobytes()
                if key in seen:
                    continue
                seen.add(key)
                q = self.matcher.quality(held, initial)
                if not q.get('geometry_supported'):
                    continue
                self.validate_seed(anchor, train, validators, initial, label, radius)
            for label, initial in guesses:
                coarse = self.matcher.icp(coarse_source, initial)
                key = np.asarray(coarse, dtype='<f8').tobytes()
                if key in seen:
                    continue
                seen.add(key)
                q = self.matcher.quality(held, coarse)
                if not q.get('geometry_supported'):
                    continue
                self.validate_seed(anchor, train, validators, coarse, label+'_coarse', radius)
            selected = choose_seed(self.seed_hypotheses)
            if selected is not None:
                selected_hypothesis = next(h for h in self.seed_hypotheses if h['T'] is selected)
                self.remember_certificate(index, selected_hypothesis['certificate'])
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
                    selected_hypothesis = next(h for h in self.seed_hypotheses if h['T'] is selected)
                    self.remember_certificate(index, selected_hypothesis['certificate'])
                    return selected
                if self.seed_hypotheses:
                    self.log(dict(stage='ambiguous_submap_seed', index=index, accepted=False))
        return None

    def propagate(self,indices,seed_index,seed_T,direction):
        result={}; pending={}
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
            # SLAM is a short-interval consistency check, never map evidence.
            limit_m, limit_deg = motion_limits(last_F, s.frontend_T, s.timestamp-last_time)
            within_motion = dm <= limit_m and da <= limit_deg
            if not within_motion:
                q.update(accepted=False, reason='tracking_motion_inconsistent')
            else:
                pending[index] = (T, q)
                self.certify_submap_pose(T, q)
                if q.get('reason') == 'partial_view_needs_context':
                    # Compare decoded scan-anchor timestamps. Dataset selection
                    # timestamps can refer to the beginning of a rotating scan.
                    nearby = {i:r for i,r in self.quality.items()
                              if abs(r.get('timestamp', -1e30)-s.timestamp) <= 1}
                    nearby.update({i:r for i,r in self.latest_attempt.items()
                                   if abs(r.get('timestamp', -1e30)-s.timestamp) <= 1})
                    blocked = self.conflicted | set(self.quarantined) | {i for i, r in nearby.items()
                        if self.quality.get(i, {}).get('reason') == 'temporal_inconsistency'}
                    nearby = {i:r for i,r in nearby.items() if i not in blocked}
                    if s.index not in blocked:
                        nearby[s.index] = q
                    promoted = confirm_partial_views(list(nearby.values()), self.source)
                    for i in promoted:
                        if i in pending:
                            result[i] = pending[i]
            supported=q['accepted'] and within_motion
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
        for phase in (('tracking', 'local') if local_only else ('tracking', 'local', 'global')):
            allow_global = phase == 'global'
            blocks=[];block=[]
            for q in self.rows:
                if q['index'] not in self.poses:block.append(q['index'])
                elif block:blocks.append(block);block=[]
            if block:blocks.append(block)
            suspect = set(self.baseline_temporal_removed)
            blocks.sort(key=(lambda b: -b[-1]) if phase == 'tracking' else
                        (lambda b: (not bool(suspect.intersection(b)), -len(b), b[0])))
            for block_number,indices in enumerate(blocks):
                original_anchors=dict(self.poses)
                lo,hi=indices[0],indices[-1]
                before=[i for i in original_anchors if i<lo];after=[i for i in original_anchors if i>hi]
                forward={};backward={}
                # Fresh localization already tried every scan forward at 1 cm.
                # Imported baselines may still have gaps never tried by this engine.
                if phase == 'tracking' and before and not self.baseline_is_fine:
                    i=max(before);forward=self.propagate(indices,i,original_anchors[i],'forward_fine')
                if phase == 'tracking' and after:
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
                if search_needed and phase != 'tracking':
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
                        self.certify_submap_pose(T, q)
                        if not q['accepted']:continue
                        q.update(index=s.index,timestamp=s.timestamp,stage='verified_submap_seed')
                        new_forward={seed_index:(T,q),**self.propagate([i for i in indices if i>seed_index and (i not in forward and i not in backward or i in disputed)],seed_index,T,'submap_forward')}
                        new_backward={seed_index:(T,q),**self.propagate([i for i in reversed(indices) if i<seed_index and (i not in forward and i not in backward or i in disputed)],seed_index,T,'submap_reverse')}
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
        self.fine.close()
