"""Local submap fitting after forward/reverse tracking; no feature-search fallback."""
import numpy as np
import time
from scipy.spatial.transform import Rotation
from trajectory_geometry import voxel
from trajectory_io import transform
from trajectory_validation import jointly_observable, VALIDATION_PROTOCOL
from parallel_runtime import cacheBudget


class LocalSubmaps:
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
        from trajectory_recovery import choose_seed
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
            seen = set()
            # A drifted global alignment must not replace a nearby trusted end
            # anchor. Try both ends independently.
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
        return None
