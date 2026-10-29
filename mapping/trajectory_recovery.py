"""Bidirectional 1 cm recovery with independent geometry and temporal checks."""
from __future__ import annotations

import argparse
from collections import OrderedDict
import copy
import json
import shutil
from pathlib import Path
import time

import numpy as np
from scipy.spatial.transform import Rotation

from trajectory_fine import FineMatcher
from trajectory_io import digest, save_json, transform, PoseGap
from trajectory_geometry import voxel
from trajectory_temporal import screen_temporal, motion_limits, bridge_limits
from trajectory_validation import (confirm_partial_views, jointly_observable, VALIDATION_PROTOCOL,
    LOW_OVERLAP_CONTEXT, MIN_SUPPORT_POINTS, MIN_SUPPORT_CELLS, SUPPORT_CELL_M, MIN_PRINCIPAL_SPAN_M,
    context_map_supported, MAX_PLANE_RMS_M, CONTEXT_MAX_PLANE_RMS_M, MIN_MAP_COVERAGE,
    validate_optimizer)
from parallel_runtime import cacheBudget
from trajectory_tail import TailInitializer, SUBMAP_PROTOCOL, WINDOW_RADII, SUPPORT_AGREEMENT
from trajectory_local_submaps import LocalSubmaps
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
        T, info = fine.align(train, initial, capture_information=True)
    except (RuntimeError, ValueError) as exc:
        T, info = initial.copy(), dict(converged=False, reason='registration_error', detail=str(exc))
    fitted = time.perf_counter()
    q = fine.quality(held, T)
    validate_optimizer(q, info)
    q.update(index=scan.index, timestamp=scan.timestamp, fine=info,
             correction_m_deg=difference(T, initial), T_map_from_base=T.tolist(),
             partition_s=partitioned-started, validation_s=time.perf_counter()-fitted,
             total_fit_s=time.perf_counter()-started)
    if not info.get('converged'):
        q.update(accepted=False, reason='fine_registration_failed')
    return T, q


def capture_scan(matcher, fine, scan, initial):
    """Cheap broad capture before retrying dense ICP after tracking is lost."""
    held = partition(scan)[1]
    predicted = fine.quality(held, initial)
    if (context_map_supported(predicted) or
            predicted.get('accepted') and predicted.get('inlier_precision_10cm',0) >= .90):
        return initial, None
    coarse = matcher.icp(voxel(scan.points,.1), initial)
    q = fine.quality(held, coarse)
    plausible = (q.get('coverage_20cm',0) >= .20 and q.get('plane_rms_m',1) < .06
                 and q.get('inlier_precision_10cm',0) >= .60)
    if not plausible:
        q.update(accepted=False, geometry_supported=False, reason='coarse_capture_failed',
                 index=scan.index, timestamp=scan.timestamp, T_map_from_base=coarse.tolist(),
                 correction_m_deg=difference(coarse, initial),
                 fine=dict(engine=FINE_ENGINE,converged=False,reason='poor_direct_tls_proposal'))
    return coarse, None if plausible else q


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


class Recovery(LocalSubmaps, TailInitializer):
    def __init__(self, baseline, output, cache, source, workers, progress, *, baseline_is_fine=False,
                 tracking_directory=None, on_checkpoint=None, revalidate_from=None):
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
        self.seed_fits = {}
        self.matcher = None
        self.tracking_directory = Path(tracking_directory) if tracking_directory else self.output/'tracking'
        self.on_checkpoint = on_checkpoint
        self.kiss_attempts = set()
        self.seed_hypotheses = []
        self.seed_certificates, self.context_certificates = {}, {}
        self.conflicted = set()
        self.revalidate_from = Path(revalidate_from) if revalidate_from is not None else None
        self.provenance = dict(protocol='studio-recovery-direct-tls-v3', input=self.source.provenance,
            baseline_policy='verified-1cm' if baseline_is_fine else 'refit-imported-at-1cm',
            baseline_trajectory_sha256=digest(self.baseline/'candidate_trajectory.tum'),
            baseline_quality_sha256=digest(self.baseline/'quality.jsonl'),
            map=json.loads((cache/'map.json').read_text()),
            registration='Forward/reverse 1cm; local submap 1cm; individual 1cm validation', fine_engine=FINE_ENGINE,
            seeds=dict(window_radii_s=[1.,3.], iterations_per_stage=60,
                       fine_distances=[.1], supported_continuation_iterations=180,
                       proposal_merge_m_deg=[.01,.1], coarse_anchor_capture_margin_m_deg=[.2,3.],
                       initial_guesses=['initial_alignment','before_anchor','after_anchor'],
                       local_coarse_voxel_m=.1, post_reverse_kiss=False,
                       tail_initialization='local-capture-coarse-fine-v2',
                       tail_local_capture_radius_m=8., reverse_restart_gap_s=30.,
                       tail_window_radii_s=list(WINDOW_RADII), tail_kiss_resolutions_m=[.3,.5,.08],
                       iteration_limit_continuation=180,
                       schedule='forward-then-reverse-then-local-submaps'),
            validation=dict(context_protocol=VALIDATION_PROTOCOL, independent_witness_scans=2,
                            temporal_protocol='drift-scaled-bridge-v3', partial_views_require_map_anchor=True,
                            shared_training_observable_directions=6,
                            shared_training_information='actual-final-Tukey-weighted-fit',
                            shared_agreement_m_deg=[.15,3.], weak_departure_m=.01),
            target_normals='original-map-4cm-40-with-tile-halo-v1',
            correspondence_distances=list(RECOVERY_DISTANCES), iterations_per_stage=RECOVERY_ITERATIONS,
            relative_fitness=1e-6, relative_rmse=1e-6, solver_threads=1,
            thresholds=dict(partial_view_context_below_coverage=LOW_OVERLAP_CONTEXT,
                            minimum_coverage=MIN_MAP_COVERAGE, context_plane_rms_m=CONTEXT_MAX_PLANE_RMS_M,
                            support_points=MIN_SUPPORT_POINTS,
                            support_cells=MIN_SUPPORT_CELLS, support_cell_m=SUPPORT_CELL_M,
                            second_principal_span_m=MIN_PRINCIPAL_SPAN_M,
                            weak_submap_departure_scaled_m=.01,
                            plane_rms_m=MAX_PLANE_RMS_M, joint_observable_directions=6,
                            correction_m=None, correction_deg=None, agreement_m=.15, agreement_deg=3))
        from native_correspondence import backendInfo as correspondenceInfo
        correspondence = correspondenceInfo()
        if correspondence['backend'] == 'native-tiled-map-v1':
            self.provenance['correspondence'] = correspondence
        if self.revalidate_from is not None:
            if self.revalidate_from.resolve() == self.output.resolve():
                raise ValueError('Revalidation must preserve the previous result in a different folder')
            previous = json.loads((self.revalidate_from/'provenance.json').read_text())
            if previous.get('validation', {}).get('context_protocol') != VALIDATION_PROTOCOL:
                raise ValueError('Previous TLS validation changed; generate fresh 1 cm fits instead of reusing their acceptance')
            for key in ('input','map','fine_engine','target_normals','baseline_trajectory_sha256','baseline_quality_sha256'):
                if previous.get(key) != self.provenance.get(key):
                    raise ValueError(f'Revalidation source changed: {key}')
            self.provenance['revalidated_source'] = dict(path=str(self.revalidate_from.resolve()),
                files={name:digest(self.revalidate_from/name) for name in
                       ('provenance.json','candidate_trajectory.tum','quality.jsonl')})
        save_recovery_provenance(self.output, self.provenance)
        self.fine = FineMatcher(self.cache, workers, result_cache=self.output/'fine_requests')
        if self.revalidate_from is not None:
            self.fine.result_sources = (self.revalidate_from/'fine_requests', self.baseline/'fine_requests')
        # Read-only acceleration: source points, target, initial pose, solver and
        # iteration settings must match the full request hash. Validation is new.
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
                if (q.get('stage') == 'kiss_search' or
                        q.get('stage') in ('submap_seed', 'submap_seed_failed') and q.get('scope') in ('regional', 'global')):
                    self.global_attempts += 1
                if 'fit_key' in q:
                    self.fit_cache[q['fit_key']] = q
                if q.get('stage') == 'seed_complete':
                    if q.get('anchor_signature'):
                        self.seed_cache[(q['index'], q.get('allow_global', True), q['anchor_signature'])] = dict(
                            T=q.get('T_map_from_base'), certificate=q.get('certificate'))
                    if q.get('certificate'):
                        self.remember_certificate(q['index'], q['certificate'])
                    if q.get('fit') and q.get('T_map_from_base'):
                        self.seed_fits[q['index']] = (np.asarray(q['T_map_from_base']), q['fit'])
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
            T, info = self.fine.align(train, initial, capture_information=True)
            q = self.fine.quality(held, T)
            if (not info.get('converged') and info.get('convergence_reason') == 'iteration_limit'
                    and q.get('geometry_supported')):
                first = info
                T, info = self.fine.align(train, T, iterations=180, capture_information=True)
                info = dict(info, continuation_from=first, continuation_iterations_per_stage=180)
                q = self.fine.quality(held, T)
        except (RuntimeError, ValueError) as exc:
            T, info = initial.copy(), dict(converged=False, reason='registration_error', detail=str(exc))
            q = self.fine.quality(held, T)
        validate_optimizer(q, info)
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
        screened, edges = screen_temporal(self.poses, times, self.source, qualities=self.quality)
        all_edges = list(edges)
        while edges:
            screened, edges = screen_temporal(screened, times, self.source, qualities=self.quality)
            all_edges.extend(edges)
        removed = set(self.poses)-set(screened)
        # Old low-overlap votes are not immutable anchors. Rebuild their link
        # to the surviving map-supported chain, including on checkpoint reuse.
        contextual = []
        unanchored = set()
        for index in screened:
            q = self.quality[index]
            if q.get('reason') == 'independent_neighbor_geometry':
                q.update(accepted=False, reason='partial_view_needs_context')
                q.pop('context_validation', None)
                contextual.append(index)
        confirm_partial_views([self.quality[i] for i in screened], self.source)
        for index in contextual:
            if not self.quality[index].get('accepted'):
                screened.pop(index)
                removed.add(index)
                unanchored.add(index)
        for index in sorted(removed):
            self.quality[index] = dict(self.quality.get(index, {}), index=index,
                timestamp=times[index], accepted=False,
                reason='unanchored_partial_view' if index in unanchored else 'temporal_inconsistency', stage=stage)
        self.poses = screened
        if hasattr(self, 'attempt_file'):
            self.log(dict(stage=stage, rejected_indices=sorted(removed), edges=all_edges))
        return removed


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

    def remember_certificate(self, index, certificate):
        self.seed_certificates[index] = certificate
        for member in certificate['members']:
            self.context_certificates[member['index']] = dict(certificate,
                member=member, shared_anchor_index=index)

    def certify_tail_pose(self, T, q, certificate=None):
        if q.get('accepted'):
            return
        if certificate is None:
            certificate = self.context_certificates.get(q['index'])
        if certificate and 'member' not in certificate:
            member = next((m for m in certificate['members'] if m['index'] == q['index']), None)
            certificate = dict(certificate, member=member, shared_anchor_index=certificate['members'][0]['index']) if member else None
        if (certificate is None or certificate.get('protocol') != SUBMAP_PROTOCOL
                or not certificate.get('stability', {}).get('accepted')
                or not certificate.get('joint', {}).get('accepted')
                or q['index'] in certificate.get('training_indices', ())
                or q.get('reason') not in ('weak_directions', 'partial_view_needs_context')
                or not context_map_supported(certificate.get('training_quality', {}))
                or certificate['training_quality'].get('observable_directions') != 6
                or certificate.get('optimizer_information', {}).get('observable_directions') != 6
                or not context_map_supported(q) or not q.get('fine', {}).get('converged')):
            return
        member = certificate['member']
        dm, da = difference(T, np.asarray(member['T_map_from_base']))
        q['submap_support_check'] = dict(anchor_index=certificate['shared_anchor_index'],
            agreement_m_deg=[dm, da], agreement_limit_m_deg=list(SUPPORT_AGREEMENT), accepted=False)
        if abs(q['timestamp']-member['timestamp']) > 1e-6 or dm > SUPPORT_AGREEMENT[0] or da > SUPPORT_AGREEMENT[1]:
            q['submap_support_check']['reason'] = 'shared_transform_disagreement'
            return
        predicted = np.asarray(member['T_map_from_base'])
        increment = np.r_[5*Rotation.from_matrix(T[:3, :3]@predicted[:3, :3].T).as_rotvec(),
                          T[:3, 3]-predicted[:3, 3]]
        weak = np.asarray(q.get('weak_directions', [])).reshape(-1, 6)
        weak_departure = float(np.linalg.norm(weak@increment))
        q['submap_support_check'].update(weak_departure_m=weak_departure, weak_limit_m=.01)
        if weak_departure > .01:
            q['submap_support_check']['reason'] = 'unconstrained_submap_departure'
            q.update(accepted=False, reason='unconstrained_submap_departure')
            return
        # The candidate was wholly excluded from the shared-transform fit,
        # and independent withheld geometry constrains all six directions.
        q.update(accepted=True, reason='independent_submap_geometry',
                 submap_support_only=True, support_anchor_index=certificate['shared_anchor_index'],
                 acceptance_scope='jointly-fitted-submap',
                 context_validation=dict(protocol=VALIDATION_PROTOCOL,
                     shared_transform_fitted=True, support_protocol=SUBMAP_PROTOCOL,
                     stability=certificate['stability'],
                     shared_anchor_index=certificate['shared_anchor_index'],
                     shared_anchor_timestamp=certificate['anchor_timestamp'],
                     witness_indices=[m['index'] for m in certificate['members'] if m['index'] != q['index']],
                     observable_directions=certificate['joint']['observable_directions'],
                     training_observable_directions=certificate['training_quality']['observable_directions'],
                     optimizer_observable_directions=certificate['optimizer_information']['observable_directions'],
                     scaled_information_eigenvalues=certificate['joint']['scaled_information_eigenvalues'],
                     shared_transform_agreement_m_deg=[dm, da],
                     weak_direction_departure_m=weak_departure, weak_direction_limit_m=.01))
        q['submap_support_check'].update(accepted=True, reason='independent_submap_geometry')

    def propagate(self,indices,seed_index,seed_T,direction):
        result={}; pending={}
        seed=self.scan(seed_index)
        if seed is None:return result
        last_T,last_F,last_time=seed_T,seed.frontend_T,seed.timestamp
        seed_quality = self.quality.get(seed_index, self.seed_fits.get(seed_index, (None, {}))[1])
        last_quality = dict(seed_quality, index=seed_index, timestamp=seed.timestamp,
                            T_map_from_base=seed_T.tolist())
        support_ancestors = set(seed_quality.get('support_ancestors', []))
        if seed_quality.get('submap_support_only'):
            support_ancestors.add(seed_index)
        lost=0
        last_progress = 0.
        for n,index in enumerate(indices):
            s=self.scan(index)
            if s is None:continue
            if time.monotonic()-last_progress >= 5:
                self.progress(f'Recovering {direction}: scan {index}, {len(result)} supported poses')
                last_progress = time.monotonic()
            initial=last_T@np.linalg.inv(last_F)@s.frontend_T
            failed = None
            if lost or abs(s.timestamp-last_time) > .5:
                if self.matcher is None:
                    from trajectory_geometry import Matcher
                    self.matcher = Matcher(self.cache,self.fine.workers)
                captured, failed = capture_scan(self.matcher,self.fine,s,initial)
            else:
                captured = initial
            if failed is None:
                T,q=self.fit(s,captured,direction)
            else:
                T,q=captured,dict(failed,stage=direction)
                self.latest_attempt[index]=q
                self.log(q)
            q['support_ancestors'] = sorted(support_ancestors)
            dm,da=difference(T,initial)
            # SLAM is a short-interval consistency check, never map evidence.
            limit_m, limit_deg = (bridge_limits(self.source, last_time, s.timestamp)
                if abs(s.timestamp-last_time) > .5 else motion_limits(last_F, s.frontend_T, s.timestamp-last_time))
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
                    promoted = confirm_partial_views(list(nearby.values()), self.source, reference=last_quality)
                    for i in promoted:
                        if i in pending:
                            result[i] = pending[i]
            supported=q['accepted'] and within_motion
            if supported:
                last_T,last_F,last_time=T,s.frontend_T,s.timestamp
                last_quality = q
                result[index]=(T,q)
                if q.get('submap_support_only'):
                    support_ancestors.add(index)
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
                    q['reason'] = 'unverified_submap_seed'
                q['accepted']=index in self.poses
                if index in self.conflicted:
                    q.update(accepted=False, reason='seed_disagreement')
                if index in self.quarantined and index not in self.poses:
                    q.update(reason='isolated_large_correction_quarantined',stage='quarantined')
                if index in self.poses:
                    T=self.poses[index];row=np.r_[original['timestamp'],T[:3,3],Rotation.from_matrix(T[:3,:3]).as_quat()]
                    q['T_map_from_base'] = T.tolist()
                    f.write(' '.join(f'{x:.9f}' for x in row)+'\n')
                g.write(json.dumps(q,allow_nan=False)+'\n')
        pose_tmp.replace(self.output/'candidate_trajectory.tum');quality_tmp.replace(self.output/'quality.jsonl')
        if self.on_checkpoint:
            self.on_checkpoint()


    def restore_accepted_checkpoint(self):
        path = self.output/'quality.jsonl'
        if not path.exists():
            return
        self.poses, self.quality = {}, {}
        for q in map(json.loads, path.read_text().splitlines()):
            index = q['index']
            self.quality[index] = q
            if q.get('reason') == 'seed_disagreement':
                self.conflicted.add(index)
            if index in self.poses or not q.get('accepted') or index in self.quarantined:
                continue
            if index not in self.rows_by_index or abs(q['timestamp']-self.source.anchor_timestamp(index)) > 1e-6:
                raise ValueError('Recovery checkpoint scan timestamp changed')
            self.poses[index] = np.asarray(q['T_map_from_base'])
            self.quality[index] = q
        self.screen_poses('resumed_temporal_audit')

    def revalidate_previous(self):
        if self.revalidate_from is None or (self.output/'quality.jsonl').exists():
            return False
        receipt = self.provenance['revalidated_source']
        if any(digest(self.revalidate_from/name) != value for name,value in receipt['files'].items()):
            raise ValueError('Previous result changed during revalidation')
        rows = [json.loads(line) for line in (self.revalidate_from/'quality.jsonl').read_text().splitlines()]
        if [q['index'] for q in rows] != [q['index'] for q in self.rows]:
            raise ValueError('Previous result has different scan selection')
        accepted = [q for q in rows if q.get('accepted')]
        values = np.loadtxt(self.revalidate_from/'candidate_trajectory.tum', ndmin=2) if accepted else np.empty((0,8))
        if len(values) != len(accepted):
            raise ValueError('Previous result pose count does not match its quality record')
        for q,row in zip(accepted,values):
            if (abs(q['timestamp']-row[0]) > 1e-6
                    or abs(q['timestamp']-self.source.anchor_timestamp(q['index'])) > 1e-6
                    or not np.allclose(pose(row),q['T_map_from_base'],atol=2e-8,rtol=0)
                    or q.get('validation_map_voxel_m') != .01
                    or q.get('fine',{}).get('engine') != FINE_ENGINE
                    or not q.get('fine',{}).get('converged')):
                raise ValueError('Previous result contains an unverified scan fit')
        self.quality = {q['index']:q for q in rows}
        self.poses = {q['index']:np.asarray(q['T_map_from_base']) for q in accepted}
        self.conflicted.clear(); self.quarantined.clear()
        removed = self.screen_poses('previous_result_revalidated')
        self.baseline_temporal_removed = sorted(set(self.baseline_temporal_removed)|removed)
        self.log(dict(stage='previous_result_revalidated', previous=len(accepted),
                      retained=len(self.poses), rejected_indices=sorted(removed)))
        return bool(removed)

    def restore_tracking(self):
        folder = self.tracking_directory
        receipt = folder/'tracking_complete.json'
        if not receipt.exists():
            # Import old geometric fits read-only; write new tracking decisions
            # to the new protocol's directory after connected-motion screening.
            folder = self.baseline.parent/'tracking'
            receipt = folder/'tracking_complete.json'
            if not receipt.exists():
                return False
        saved = json.loads(receipt.read_text())
        keys = ('input', 'map', 'baseline_trajectory_sha256', 'baseline_quality_sha256',
                'fine_engine', 'correspondence_distances', 'iterations_per_stage', 'thresholds', 'validation')
        if (saved.get('protocol') != 'forward-reverse-checkpoint-v1' or not saved.get('completed')
                or any(saved['provenance'].get(k) != self.provenance.get(k) for k in keys)):
            raise ValueError('Forward/reverse checkpoint inputs or settings changed')
        for name, key in (('quality.jsonl', 'quality_sha256'), ('candidate_trajectory.tum', 'trajectory_sha256')):
            if digest(folder/name) != saved[key]:
                raise ValueError('Forward/reverse checkpoint is incomplete or changed')
        rows = [json.loads(line) for line in (folder/'quality.jsonl').read_text().splitlines()]
        if [q['index'] for q in rows] != [q['index'] for q in self.rows]:
            raise ValueError('Forward/reverse checkpoint scan selection changed')
        values = np.loadtxt(folder/'candidate_trajectory.tum', ndmin=2) if saved['accepted'] else np.empty((0,8))
        accepted = [q for q in rows if q.get('accepted')]
        if len(accepted) != saved['accepted'] or len(values) != len(accepted):
            raise ValueError('Forward/reverse checkpoint pose count mismatch')
        allowed = {'baseline_retained', 'baseline_fine', 'forward_fine', 'reverse_fine', 'tail_seed', 'tail_forward'}
        if (saved['provenance'].get('protocol') == 'studio-recovery-connected-local-v2'
                and saved['provenance'].get('revalidated_source') == self.provenance.get('revalidated_source')):
            allowed.update(('verified_submap_seed','submap_forward','submap_reverse'))
        for q, row in zip(accepted, values):
            if (q.get('stage') not in allowed or abs(q['timestamp']-row[0]) > 1e-6
                    or abs(q['timestamp']-self.source.anchor_timestamp(q['index'])) > 1e-6
                    or not np.allclose(pose(row), q['T_map_from_base'], atol=2e-8, rtol=0)):
                raise ValueError('Forward/reverse checkpoint contains an invalid or post-reverse pose')
        self.quality = {q['index']:q for q in rows}
        self.poses = {q['index']:np.asarray(q['T_map_from_base']) for q in accepted}
        self.quarantined = saved['quarantined']
        self.conflicted = set(saved['conflicted'])
        self.baseline_temporal_removed = saved['baseline_temporal_removed']
        removed = self.screen_poses('tracking_checkpoint_temporal_audit')
        self.baseline_temporal_removed = sorted(set(self.baseline_temporal_removed)|removed)
        self.log(dict(stage='tracking_checkpoint_reused', accepted=len(self.poses), rejected_on_revalidation=len(removed)))
        same_search = (saved['provenance'].get('seeds', {}).get('tail_initialization')
                       == self.provenance['seeds']['tail_initialization'])
        # Keep verified poses, but retry missing intervals when reverse capture
        # has improved. Do not mistake an older unsuccessful search for proof
        # that the remaining scans have already had the new recovery attempt.
        return not removed and same_search

    def save_tracking(self):
        self.checkpoint()
        folder = self.tracking_directory
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('quality.jsonl', 'candidate_trajectory.tum'):
            temporary = folder/(name+'.tmp')
            shutil.copyfile(self.output/name, temporary)
            temporary.replace(folder/name)
        save_json(folder/'tracking_complete.json', dict(protocol='forward-reverse-checkpoint-v1',
            completed=True, accepted=len(self.poses), processed=len(self.rows), provenance=self.provenance,
            quality_sha256=digest(folder/'quality.jsonl'), trajectory_sha256=digest(folder/'candidate_trajectory.tum'),
            quarantined=self.quarantined, conflicted=sorted(self.conflicted),
            baseline_temporal_removed=self.baseline_temporal_removed))

    def seed_indices(self, indices, *, from_end=False):
        if not indices:
            return []
        ordered = list(reversed(indices)) if from_end else indices
        seeds = [ordered[0]]
        stamp = self.rows_by_index[ordered[0]]['timestamp']
        for index in ordered[1:]:
            t = self.rows_by_index[index]['timestamp']
            if abs(t-stamp) >= 30:
                seeds.append(index); stamp = t
        return list(dict.fromkeys(seeds+[ordered[len(ordered)//2], ordered[-1]]))

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

    def reverse_block(self, indices, after_index=None):
        """Reuse the end anchor, then independently restart long untracked gaps."""
        backward, forward = {}, {}
        if after_index is not None:
            backward = self.propagate(list(reversed(indices)), after_index,
                                      self.poses[after_index], 'reverse_fine')
        for seed_index in self.seed_indices(indices, from_end=True):
            if seed_index in backward or seed_index in forward:
                continue
            if (after_index is not None and
                    self.rows_by_index[after_index]['timestamp']-
                    self.rows_by_index[seed_index]['timestamp'] <= 3.):
                # This end anchor was just tried by reverse ICP. Independent
                # initialization is constrained to the same nearby anchor;
                # searching the whole map here cannot resolve accumulated drift.
                continue
            remaining = []
            for i in reversed(indices):
                if i > seed_index:
                    continue
                if i in backward or i in forward:
                    break
                remaining.append(i)
            if not remaining:
                continue
            # Ordinary short holes remain local-submap work. A failed reverse
            # pass must not suppress independent localization of a long gap
            # merely because some accepted pose exists later in the sequence.
            span = self.rows_by_index[remaining[0]]['timestamp']-self.rows_by_index[remaining[-1]]['timestamp']
            if (after_index is not None or backward or forward) and span < 30.:
                continue
            T = self.initialize_tail(seed_index, allow_global=False)
            if T is None:
                T = self.initialize_tail(seed_index, allow_global=True)
            if T is None:
                continue
            T, q = self.seed_pose(seed_index, T)
            self.certify_submap_pose(T, q)
            if not q.get('accepted'):
                continue
            q.update(stage='tail_seed')
            self.seed_fits[seed_index] = (T, q)
            new_backward = {seed_index:(T,q), **self.propagate(
                [i for i in reversed(indices) if i < seed_index and i not in backward],
                seed_index, T, 'reverse_fine')}
            new_forward = self.propagate(
                [i for i in indices if i > seed_index and i not in backward and i not in forward],
                seed_index, T, 'tail_forward')
            self.merge_direction(backward, new_backward)
            self.merge_direction(forward, new_forward)
            self.log(dict(stage='tail_initialized', index=seed_index,
                          reverse_recovered=len(new_backward), forward_recovered=len(new_forward)))
        return forward, backward

    def run(self, tracking_only=False):
        started=time.monotonic();self.keep_baseline();baseline_count=len(self.poses)
        tracking_ready = self.restore_tracking()
        if self.revalidate_previous():
            tracking_ready = False
        self.restore_accepted_checkpoint()
        self.checkpoint()
        phases = ('tracking',) if tracking_only else ('tracking', 'local')
        for phase in phases:
            if phase == 'tracking' and tracking_ready:
                continue
            allow_global = False
            self.window_cache.clear()
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
                if phase == 'tracking':
                    restarted, backward = self.reverse_block(indices, min(after) if after else None)
                    self.merge_direction(forward, restarted)
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
                        # Use the fitted submap as the initial pose for the
                        # anchor's own 1 cm fitting and validation.
                        T,q=self.fit(s,T,'verified_submap_anchor')
                        self.certify_submap_pose(T, q)
                        if not q['accepted']:continue
                        q.update(index=s.index,timestamp=s.timestamp,stage='verified_submap_seed')
                        self.seed_fits[seed_index] = (T, q)
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
            if phase == 'tracking':
                self.save_tracking()
        self.source.verify_unchanged();self.fine.verify_unchanged()
        if digest(self.baseline/'candidate_trajectory.tum')!=self.provenance['baseline_trajectory_sha256']:
            raise ValueError('Baseline changed during recovery')
        self.attempt_file.flush()
        # Preserve baseline skipped-input facts; recovery does not invent deskew.
        save_json(self.output/'input_quality.json',json.loads((self.baseline/'input_quality.json').read_text()))
        save_json(self.output/'status.json',dict(status='candidate_complete',processed=len(self.rows),
            pending_global_search=False, recovery_method='connected-local-submap-v2',
            accepted=len(self.poses),rejected=len(self.rows)-len(self.poses),baseline_retained=baseline_count,
            recovered=len(self.poses)-baseline_count,quarantined_indices=self.quarantined,
            baseline_temporal_rejected=self.baseline_temporal_removed,
            elapsed_s=time.monotonic()-started,elapsed_scope='current_recovery_invocation',tail_kiss_attempts=self.global_attempts,
            has_invalid_intervals=len(self.poses)<len(self.rows),certified_gt=False))


    def close(self):
        self.attempt_file.close()
        self.fine.close()
