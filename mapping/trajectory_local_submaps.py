"""Local submap fitting after forward/reverse tracking; no feature-search fallback."""
import numpy as np
import time
import hashlib
import copy
from scipy.spatial.transform import Rotation
from trajectory_geometry import voxel
from trajectory_io import transform
from trajectory_validation import jointly_observable, VALIDATION_PROTOCOL
from parallel_runtime import cacheBudget
from trajectory_temporal import bridge_limits, continuous_estimate


class LocalSubmaps:
    def seed_context(self, anchor):
        """Connect both sides even when fitted GT is missing between them."""
        result = []
        self.seed_context_limits = []
        for before, ids in ((True, [i for i in self.poses if i < anchor.index]),
                            (False, [i for i in self.poses if i > anchor.index])):
            if not ids:
                continue
            i = max(ids) if before else min(ids)
            stamp = self.rows_by_index[i]['timestamp']
            if continuous_estimate(self.source, stamp, anchor.timestamp):
                F = self.source.pose([stamp])[0]
                result.append(self.poses[i]@np.linalg.inv(F)@anchor.frontend_T)
                limits = bridge_limits(self.source, stamp, anchor.timestamp)
                self.seed_context_limits.append((max(.5,limits[0]), max(10.,limits[1])))
        return result

    def context_limits(self, count):
        limits = getattr(self, 'seed_context_limits', [])
        return limits if len(limits) == count else [(.5,10.)]*count

    def submap_seed(self,index,allow_global=True):
        anchor = self.scan(index)
        if anchor is None:
            return None
        context = self.seed_context(anchor)
        signature = hashlib.sha256(np.asarray(context, dtype='<f8').tobytes()+
            np.asarray(self.context_limits(len(context)), dtype='<f8').tobytes()).hexdigest()
        key = (index, allow_global, signature)
        if key in self.seed_cache:
            value = self.seed_cache[key]
            if value['certificate']:
                self.remember_certificate(index, copy.deepcopy(value['certificate']))
            return np.asarray(value['T']) if value['T'] is not None else None
        result = self._submap_seed(index,allow_global)
        value = dict(T=result.tolist() if result is not None else None,
                     certificate=copy.deepcopy(self.seed_certificates.get(index)) if result is not None else None)
        self.seed_cache[key] = value
        self.log(dict(stage='seed_complete', index=index, allow_global=allow_global,
                      anchor_signature=signature,
                      T_map_from_base=value['T'], certificate=value['certificate']))
        return result

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
        # Retain the current anchor's two windows across local guesses.
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

    def certify_submap_pose(self, T, q):
        from trajectory_recovery import difference, capped_distance_cost
        if q.get('accepted'):
            return
        certificate = self.context_certificates.get(q['index'])
        if certificate and certificate.get('protocol') == 'shared-kiss-selective-support-v1':
            return self.certify_tail_pose(T, q, certificate)
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
        q.update(accepted=True, reason='independent_submap_geometry', submap_support_only=True,
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
        from trajectory_recovery import difference, capped_distance_cost
        started = time.perf_counter()
        self.progress(f'Validating scan {anchor.index}: {2*radius:g} s submap, {scope}, 1 cm fitting')
        self.log(dict(stage='submap_seed_started', index=anchor.index, scope=scope,
                      radius_s=radius, resolution_m=resolution, started_unix=time.time()))
        try:
            # Broad capture happens on the 10 cm submap. Keep the full 1 cm
            # source for the final 10 cm correspondence gate, not for three
            # repeated broad-to-fine searches over millions of points.
            T, info = self.fine.align(train, initial, iterations=60,
                                      distances=(.1,), capture_information=True)
            context = [difference(T, expected) for expected in self.seed_context(anchor)]
            if (not info.get('converged') and info.get('convergence_reason') == 'iteration_limit'
                    and all(dm <= lm and da <= la for (dm,da),(lm,la) in zip(context,self.context_limits(len(context))))
                    and info.get('optimizer_information', {}).get('observable_directions') == 6
                    and self.fine.quality(voxel(train, .1), T).get('geometry_supported')):
                first = info
                T, info = self.fine.align(train, T, iterations=180,
                                          distances=(.1,), capture_information=True)
                info = dict(info, continuation_from=first)
        except (RuntimeError, ValueError) as exc:
            self.log(dict(stage='submap_seed_failed', index=anchor.index, scope=scope,
                          radius_s=radius, resolution_m=resolution, reason=str(exc)))
            return None
        context = [difference(T, expected) for expected in self.seed_context(anchor)]
        def reject(reason, **measurements):
            self.log(dict(stage='submap_seed', index=anchor.index, scope=scope,
                radius_s=radius, resolution_m=resolution, T_map_from_base=T.tolist(),
                accepted=False, fine=info, checks=[], heldout_capped_costs=[],
                nearby_anchor_disagreement_m_deg=context, reason=reason,
                short_circuited=True, elapsed_s=time.perf_counter()-started, **measurements))
            return None
        # These conditions cannot be repaired by later validation. In particular,
        # do not fit independent witnesses after the shared proposal has failed.
        if not info.get('converged'):
            return reject('shared_fit_not_converged')
        if any(dm > lm or da > la for (dm,da),(lm,la) in zip(context,self.context_limits(len(context)))):
            return reject('nearby_anchor_disagreement')
        optimizer_information = info.get('optimizer_information', {})
        if optimizer_information.get('observable_directions') != 6:
            return reject('shared_fit_weak_geometry')
        # Convergence alone does not mean the shared fit determined every DOF.
        # Check actual training geometry separately from independent validation:
        # a floor-only fit must never inherit lateral coordinates from SLAM.
        training_quality = self.fine.quality(voxel(train, .1), T)
        training_supported = (training_quality.get('geometry_supported')
                              and training_quality.get('observable_directions') == 6
                              and optimizer_information.get('observable_directions') == 6)
        if not training_supported:
            return reject('shared_fit_weak_geometry', training_quality=training_quality)
        _, held = self.prepared_scan(anchor)
        aq = self.fine.quality(held, T)
        if not aq.get('geometry_supported'):
            return reject('insufficient_anchor_geometry', training_quality=training_quality,
                          anchor_quality=aq)
        costs = [capped_distance_cost(aq)]
        checks = []
        selected = validators[::max(1, len(validators)//3)][:3]
        valid = []
        for s in selected:
            guess = T @ np.linalg.inv(anchor.frontend_T) @ s.frontend_T
            predicted = self.fine.quality(self.prepared_scan(s)[1], guess)
            costs.append(capped_distance_cost(predicted))
            # Even a failed seed's independently fitted scan can later support
            # partial-view tracking through latest_attempt. Preserve those fits.
            fitted, q = self.fit(s, guess, 'submap_heldout')
            check = dict(q, agreement_m_deg=difference(fitted, guess),
                         shared_transform_quality=predicted, shared_transform_pose=guess.tolist())
            if (predicted.get('geometry_supported') and q.get('geometry_supported')
                    and q.get('fine', {}).get('converged')
                    and check['agreement_m_deg'][0] <= .15 and check['agreement_m_deg'][1] <= 3):
                valid.append(check)
            checks.append(check)
        joint = jointly_observable([aq]+[q['shared_transform_quality'] for q in valid],
            [T]+[np.asarray(q['shared_transform_pose']) for q in valid], T,
            shared_transform_fitted=bool(info.get('converged')))
        plausible = bool(training_supported and len(valid) >= 2 and joint['accepted'])
        ok = bool(info.get('converged') and plausible)
        certificate = dict(protocol='local-submap-v1', anchor_timestamp=anchor.timestamp, joint=joint, training_quality=training_quality,
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
                      nearby_anchor_disagreement_m_deg=context, elapsed_s=time.perf_counter()-started))
        return T if ok else None

    def _submap_seed(self, index, allow_global=True):
        # Exhaust both local windows; no feature-search fallback follows.
        from trajectory_recovery import choose_seed, difference
        anchor = self.scan(index)
        if anchor is None:
            return None
        context = self.seed_context(anchor)
        if len(context) == 2:
            dm, da = difference(*context)
            # Triangle inequalities: no pose can satisfy both temporal anchor
            # bounds when their feasible regions are disjoint.
            limits = self.context_limits(len(context))
            if dm > sum(v[0] for v in limits)+1e-9 or da > sum(v[1] for v in limits)+1e-9:
                self.log(dict(stage='submap_skipped', index=index,
                    reason='incompatible_nearby_anchors', disagreement_m_deg=[dm, da]))
                return None
        if self.matcher is None:
            from trajectory_geometry import Matcher
            self.matcher = Matcher(self.cache, self.fine.workers)
        local_radii = (1., 3.)
        for radius in local_radii:
            window = self.seed_window(index, radius)
            if window is None:
                continue
            anchor, train, validators = window
            guesses = self.seed_guesses(anchor)
            self.seed_hypotheses = []
            held = self.prepared_scan(anchor)[1]
            coarse_source = voxel(train, .1)
            proposals = []
            context = self.seed_context(anchor)
            # Compare the cheap proposals first. Do not repeat dense fitting
            # for the supplied guess and all its almost-identical refinements.
            for label, initial in guesses:
                coarse = self.matcher.icp(coarse_source, initial)
                for name, T in ((label, initial), (label+'_coarse', coarse)):
                    if any(dm > lm+.2 or da > la+3 for (dm,da),(lm,la) in zip(
                           (difference(T, expected) for expected in context), self.context_limits(len(context)))):
                        self.log(dict(stage='submap_proposal_rejected', index=index,
                            scope=name, radius_s=radius, reason='coarse_anchor_disagreement'))
                        continue
                    q = self.matcher.quality(held, T)
                    if q.get('geometry_supported'):
                        from trajectory_recovery import capped_distance_cost
                        proposals.append((capped_distance_cost(q), name, T))
            seen = []
            for _, name, T in sorted(proposals, key=lambda p:p[0]):
                if any(dm <= .01 and da <= .1 for dm, da in (difference(T, old) for old in seen)):
                    self.log(dict(stage='submap_proposal_reused', index=index, scope=name,
                                  radius_s=radius, reason='same_coarse_basin'))
                    continue
                seen.append(T)
                self.validate_seed(anchor, train, validators, T, name, radius)
            selected = choose_seed(self.seed_hypotheses)
            if selected is not None:
                selected_hypothesis = next(h for h in self.seed_hypotheses if h['T'] is selected)
                self.remember_certificate(index, selected_hypothesis['certificate'])
                return selected
            if self.seed_hypotheses:
                self.log(dict(stage='ambiguous_submap_seed', index=index, accepted=False))
        return None
