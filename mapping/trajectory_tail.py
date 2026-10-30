"""Independent end-of-sequence initialization for reverse tracking."""
from dataclasses import dataclass
import hashlib
import json
import time

import numpy as np
from scipy.spatial.transform import Rotation

from parallel_runtime import cacheBudget
from trajectory_geometry import voxel
from trajectory_io import transform, save_json, digest
from trajectory_validation import jointly_observable, confirm_partial_views, context_map_supported, map_rooted


SUBMAP_PROTOCOL = 'shared-kiss-selective-support-v1'
WINDOW_RADII = (1., 2., 3.)
KISS_RESOLUTIONS = (.3, .5, .08)
SUPPORT_AGREEMENT = (.10, 2.)
STABILITY_LIMIT = (.01, .1)


def delta(a, b):
    return (float(np.linalg.norm(a[:3, 3]-b[:3, 3])),
            float(np.rad2deg(Rotation.from_matrix(a[:3, :3].T@b[:3, :3]).magnitude())))


def within(a, b, limits):
    return all(v <= limit for v, limit in zip(delta(a, b), limits))


@dataclass
class Window:
    anchor: object
    training: list
    validators: list
    members: list
    radius: float
    coarse: np.ndarray

    def relative(self, scan):
        return np.linalg.inv(self.anchor.frontend_T)@scan.frontend_T

    def points(self, scans, size):
        return voxel(np.concatenate([transform(voxel(s.points, size), self.relative(s))
                                     for s in scans]), size)


class TailInitializer:
    def kiss_request(self, source, target, resolution):
        from native_backend import executablePath
        if not hasattr(self, 'kiss_engine_digest'):
            self.kiss_engine_digest = digest(executablePath())
        request = dict(engine=self.kiss_engine_digest, resolution=resolution,
                       source_sha256=hashlib.sha256(np.asarray(source, dtype='<f4').tobytes()).hexdigest(),
                       target_sha256=hashlib.sha256(np.asarray(target, dtype='<f4').tobytes()).hexdigest())
        key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        destination = self.output/'kiss_requests'/(key+'.json')
        try:
            saved = json.loads(destination.read_text())
            T = np.asarray(saved['transformation'])
            if saved['request'] == request and T.shape == (4, 4) and np.isfinite(T).all():
                return T, True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        T = self.fine.native.kiss(source, target, resolution, self.fine.workers)
        destination.parent.mkdir(exist_ok=True)
        save_json(destination, dict(request=request, transformation=T.tolist()))
        return T, False

    def tail_window(self, index, radius):
        key = ('tail', index, radius)
        if key in getattr(self, 'window_cache', {}):
            self.window_cache.move_to_end(key)
            return self.window_cache[key]
        anchor = self.scan(index)
        if anchor is None:
            self.log(dict(stage='submap_skipped', index=index, radius_s=radius,
                          reason='anchor_scan_unavailable'))
            return None
        # The selected trajectory timestamps are decoded scan anchors. Metadata
        # timestamps may denote the start, rather than end, of a rotating scan.
        members = [q['index'] for q in self.rows if abs(q['timestamp']-anchor.timestamp) <= radius]
        if len(members) < 5:
            return None
        cap = int(10*radius)+1
        ids = np.unique(np.linspace(0, len(members)-1, min(cap, len(members))).astype(int))
        scans = [self.scan(members[i]) for i in ids]
        scans = [s for s in scans if s is not None and s.index != index]
        if len(scans) < 4:
            return None
        stamps = np.array([s.timestamp for s in scans])
        travel = np.linalg.norm(np.diff([s.frontend_T[:3, 3] for s in scans], axis=0), axis=1)
        turns = [Rotation.from_matrix(a.frontend_T[:3, :3].T@b.frontend_T[:3, :3]).magnitude()
                 for a, b in zip(scans, scans[1:])]
        # Limit internal distortion before asking a rigid matcher to explain it.
        if (np.any(np.diff(stamps) > .5) or np.any(travel/np.diff(stamps) > 8)
                or travel.sum() > 12 or sum(turns) > np.deg2rad(120)):
            self.log(dict(stage='submap_skipped', index=index, radius_s=radius,
                          reason='unsupported_relative_motion'))
            return None
        training, validators = scans[::2], scans[1::2]
        window = Window(anchor, training, validators, members, radius, np.empty((0, 3)))
        # KISS needs no dense 1 cm concatenation. Dense points are assembled only
        # for the optional support fit; individual scan fitting stays at 1 cm.
        window.coarse = window.points(training, .08)
        if len(window.coarse) < 80:
            return None
        size = window.coarse.nbytes+anchor.points.nbytes+sum(s.points.nbytes for s in scans)
        if size < cacheBudget(.02):
            self.window_cache.clear()
            self.window_cache[key] = window
        return window

    def seed_pose(self, index, initial):
        # Reuse the exact individually validated fit, not a second fit starting
        # at its result (which would change the request and waste iterations).
        saved = self.seed_fits.get(index)
        if saved is not None and np.array_equal(saved[0], initial):
            return saved
        scan = self.scan(index)
        return self.fit(scan, initial, 'verified_submap_anchor')

    def _tail_support_fit(self, window, initial):
        """Fit one shared transform and remove each contributing scan in turn."""
        started = time.perf_counter()
        scans = window.training
        # Select complementary map geometry, avoiding temporal subsampling that
        # can repeatedly pick the same wall. This only selects support scans;
        # every selected support fit still uses its full 1 cm cloud.
        if len(scans) > 6:
            candidates = []
            for scan in scans:
                q = self.fine.quality(window.points([scan], .1), initial)
                H = np.asarray(q.get('scaled_information_matrix', np.zeros((6, 6))))
                candidates.append((scan, H))
            selected, information = [], np.eye(6)*1e-8
            while candidates and len(selected) < 6:
                k = max(range(len(candidates)), key=lambda k: np.linalg.slogdet(information+candidates[k][1])[1])
                scan, H = candidates.pop(k)
                selected.append(scan); information += H
            scans = selected
        result = dict(protocol=SUBMAP_PROTOCOL, accepted=False,
                      training_indices=[s.index for s in scans], leave_one_out=[])
        if len(scans) < 4:
            result['reason'] = 'insufficient_independent_training_scans'
            result['elapsed_s'] = time.perf_counter()-started
            self.log(dict(stage='tail_support_fit', index=window.anchor.index, **result))
            return None, result
        points = window.points(scans, .01)
        T, info = self.fine.align(points, initial, capture_information=True)
        training = self.fine.quality(voxel(points, .1), T)
        if (not info.get('converged') and info.get('convergence_reason') == 'iteration_limit'
                and context_map_supported(training)):
            T, info = self.fine.align(points, T, iterations=180, capture_information=True)
            training = self.fine.quality(voxel(points, .1), T)
        result.update(fine=info, training_quality=training,
                      optimizer_information=info.get('optimizer_information', {}))
        ok = (info.get('converged') and context_map_supported(training)
              and training.get('observable_directions') == 6
              and result['optimizer_information'].get('observable_directions') == 6)
        if ok:
            for excluded in scans:
                subset = window.points([s for s in scans if s.index != excluded.index], .01)
                # Start from the same unrefined proposal. Starting every ablation
                # at T could conceal reliance on the removed scan's constraints.
                fitted, fit = self.fine.align(subset, initial, capture_information=True)
                quality = self.fine.quality(voxel(subset, .1), fitted)
                if (not fit.get('converged') and fit.get('convergence_reason') == 'iteration_limit'
                        and context_map_supported(quality)):
                    fitted, fit = self.fine.align(subset, fitted, iterations=180, capture_information=True)
                    quality = self.fine.quality(voxel(subset, .1), fitted)
                stable = bool(fit.get('converged') and context_map_supported(quality)
                    and quality.get('observable_directions') == 6
                    and fit.get('optimizer_information', {}).get('observable_directions') == 6
                    and within(T, fitted, STABILITY_LIMIT))
                result['leave_one_out'].append(dict(excluded_index=excluded.index, accepted=stable,
                    difference_m_deg=delta(T, fitted), observable_directions=quality.get('observable_directions'),
                    optimizer_observable_directions=fit.get('optimizer_information', {}).get('observable_directions')))
                if not stable:
                    ok = False
                    break
        result.update(accepted=bool(ok), reason='stable_shared_fit' if ok else 'unstable_or_weak_shared_fit',
                      elapsed_s=time.perf_counter()-started)
        self.log(dict(stage='tail_support_fit', index=window.anchor.index, **result))
        return (T if ok else None), result

    def validate_tail_seed(self, window, initial, scope, resolution, *, allow_shared_support=True):
        """One KISS proposal -> individual fine fits; shared fitting is conditional."""
        started = time.perf_counter()
        anchor = window.anchor
        # KISS's 30/50 cm feature pose is a proposal, not a 1 cm ICP seed.
        # Refine only the training submap here; the anchor and witnesses remain
        # wholly withheld and still need independent original-map fine fits.
        if self.matcher is None:
            from trajectory_geometry import Matcher
            self.matcher = Matcher(self.cache, self.fine.workers)
        initial = self.matcher.icp(window.coarse, initial)
        preliminary = self.fine.quality(self.prepared_scan(anchor)[1], initial)
        if (preliminary.get('coverage_20cm', 0) < .20
                or preliminary.get('plane_rms_m', 1) > .06
                or preliminary.get('inlier_precision_10cm', 0) < .60):
            self.log(dict(stage='tail_candidate', index=anchor.index, scope=scope,
                radius_s=window.radius, resolution_m=resolution, accepted=False,
                reason='poor_direct_tls_proposal', anchor_quality=preliminary,
                elapsed_s=time.perf_counter()-started))
            return None
        validators = window.validators
        ids = np.unique(np.linspace(0, len(validators)-1, min(3, len(validators))).astype(int))
        checks = [anchor]+[validators[i] for i in ids]
        stage = 'local_capture_individual' if scope.startswith('local_capture_') else 'kiss_individual'
        fits = [self.fit(anchor, initial, stage)]
        anchor_quality = fits[0][1]
        if not (anchor_quality.get('geometry_supported') and anchor_quality.get('fine', {}).get('converged')):
            self.log(dict(stage='tail_candidate', index=anchor.index, scope=scope,
                radius_s=window.radius, resolution_m=resolution, accepted=False,
                reason='anchor_fine_validation_failed', checks=[anchor_quality],
                elapsed_s=time.perf_counter()-started))
            return None
        fits.extend(self.fit(s, initial@window.relative(s), stage) for s in checks[1:])
        confirm_partial_views([q for _, q in fits], self.source)
        # Fine correction common to the scans need not remain within 15 cm of
        # a coarse KISS proposal. Compare the independently refined transforms.
        full = [(s, T, q) for s, (T, q) in zip(checks, fits)
                if q.get('accepted') and (map_rooted(q) or context_map_supported(q))]
        reference = min(full, key=lambda x:x[2]['plane_rms_m']) if full else None
        T = reference[1]@np.linalg.inv(window.relative(reference[0])) if reference else initial
        good = [bool((map_rooted(q) or context_map_supported(q)) and q.get('fine', {}).get('converged')
                and within(fitted, T@window.relative(s), (.15, 3.)))
                for s, (fitted, q) in zip(checks, fits)]
        weak = any(ok and q.get('observable_directions', 0) < 6 for ok, (_, q) in zip(good, fits))
        enough = good[0] and fits[0][1].get('accepted') and sum(
            bool(ok and q.get('accepted')) for ok, (_, q) in zip(good[1:], fits[1:])) >= 2
        certificate = None
        if allow_shared_support and not enough and weak and good[0] and sum(good[1:]) >= 2:
            try:
                shared, support = self._tail_support_fit(window, initial)
            except (ValueError, RuntimeError) as error:
                shared, support = None, {}
                self.log(dict(stage='submap_support_failed', index=anchor.index, reason=str(error)))
            if shared is not None:
                # Checks remain wholly excluded from both KISS and shared fit.
                fits = [self.fit(s, shared@window.relative(s), 'submap_support_member') for s in checks]
                predicted = [self.fine.quality(self.prepared_scan(s)[1], shared@window.relative(s)) for s in checks]
                eligible = [j for j, (s, (fitted, q)) in enumerate(zip(checks, fits))
                    if q.get('geometry_supported') and q.get('fine', {}).get('converged')
                    and predicted[j].get('geometry_supported')
                    and within(fitted, shared@window.relative(s), SUPPORT_AGREEMENT)]
                joint = jointly_observable([predicted[j] for j in eligible],
                                           [shared@window.relative(checks[j]) for j in eligible],
                                           shared, shared_transform_fitted=True)
                if joint['accepted'] and 0 in eligible:
                    certificate = dict(protocol=SUBMAP_PROTOCOL, anchor_timestamp=anchor.timestamp,
                        joint=joint, training_quality=support['training_quality'],
                        optimizer_information=support['optimizer_information'], stability=support,
                        training_indices=[s.index for s in window.training],
                        members=[dict(index=s.index, timestamp=s.timestamp,
                            T_map_from_base=(shared@window.relative(s)).tolist()) for j, s in enumerate(checks) if j in eligible])
                    for fitted, q in fits:
                        self.certify_tail_pose(fitted, q, certificate)
                    T = shared
        confirmed = [j for j, (s, (fitted, q)) in enumerate(zip(checks, fits))
                     if q.get('accepted') and (map_rooted(q) or context_map_supported(q))
                     and (allow_shared_support or map_rooted(q))
                     and within(fitted, T@window.relative(s), (.15, 3.))]
        accepted = 0 in confirmed and len(confirmed) >= 3
        reason = 'individual_scans_verified' if accepted else 'insufficient_independent_verified_scans'
        # Compare candidates on precisely the same withheld scans, using a
        # common rigid transform rather than separately optimized residuals.
        predicted = [self.fine.quality(self.prepared_scan(s)[1], T@window.relative(s)) for s in checks]
        costs = [q.get('coverage_20cm', 0)*q.get('inlier_distance_mse_m2', .04)+
                 (1-q.get('coverage_20cm', 0))*.04 for q in predicted]
        # Only close temporal anchors constrain a new global localization.
        for index, pose in self.poses.items():
            if abs(self.rows_by_index[index]['timestamp']-anchor.timestamp) <= 3:
                scan = self.scan(index)
                if scan is not None and not within(T, pose@np.linalg.inv(scan.frontend_T)@anchor.frontend_T, (.5, 10.)):
                    accepted = False
                    reason = 'nearby_anchor_disagreement'
        self.log(dict(stage='tail_candidate', index=anchor.index, scope=scope, radius_s=window.radius,
            resolution_m=resolution, accepted=bool(accepted), T_map_from_base=T.tolist(),
            checks=[q for _, q in fits], heldout_capped_costs=costs,
            shared_support_used=certificate is not None, reason=reason, elapsed_s=time.perf_counter()-started))
        if accepted:
            hypothesis = dict(T=T, costs=costs, accepted=True, certificate=certificate,
                              fits=[fits[j] for j in confirmed], checks=[checks[j] for j in confirmed], window=window)
            self.seed_hypotheses.append(hypothesis)
            return hypothesis
        return None

    def initialize_from_anchors(self, index):
        """Find an independently supported island without feature or dense submap search.

        Ten bad scans stop tracking, but do not prove that the entire interval
        is unobservable. Revisit its interior from the verified endpoints before
        spending time on dense shared fitting. No partial-view promotion here.
        """
        from trajectory_recovery import choose_seed
        for radius in (1., 3.):
            window = self.tail_window(index, radius)
            if window is None:
                continue
            guesses = [(label, T) for label, T in self.seed_guesses(window.anchor)
                       if label != 'initial_alignment']
            self.seed_hypotheses = []
            seen = []
            for label, initial in guesses:
                if any(within(initial, previous, (.01, .1)) for previous in seen):
                    continue
                seen.append(initial)
                self.validate_tail_seed(window, initial, 'local_capture_'+label, .1,
                                        allow_shared_support=False)
            # Compare both endpoint hypotheses before accepting either one.
            selected = choose_seed(self.seed_hypotheses)
            if selected is not None:
                return self._accept_tail_hypothesis(selected)
        return None

    def initialize_tail(self, index, allow_global=True):
        from trajectory_recovery import choose_seed
        for radius in WINDOW_RADII:
            window = self.tail_window(index, radius)
            if window is None:
                continue
            guesses = self.seed_guesses(window.anchor)
            self.seed_hypotheses = []
            if not allow_global:
                # Try bounded drift capture before feature matching. Only the
                # training scans rank candidates; every returned anchor still
                # needs three independently fitted TLS-supported scans.
                if self.matcher is None:
                    from trajectory_geometry import Matcher
                    self.matcher = Matcher(self.cache, self.fine.workers)
                seen = []
                for label, guess in guesses:
                    if any(within(guess, previous, (.5, 3.)) for previous in seen):
                        continue
                    seen.append(guess)
                    for cost, proposal in self.matcher.capture_candidates(window.coarse, guess):
                        self.validate_tail_seed(window, proposal, 'local_capture_'+label, .4)
                    selected = choose_seed(self.seed_hypotheses)
                    if selected is not None:
                        return self._accept_tail_hypothesis(selected)
            for resolution in KISS_RESOLUTIONS:
                target = self.kiss_targets.get(resolution)
                if target is None:
                    target = voxel(self.fine.kiss_points, resolution)
                    self.kiss_targets[resolution] = target
                source = voxel(window.coarse, resolution)
                groups = []
                if not allow_global:
                    for margin in (5., 15.):
                        scopes = []
                        for _, guess in guesses:
                            moved = transform(source, guess)
                            ids = np.flatnonzero(np.all((target >= moved.min(0)-margin) &
                                                       (target <= moved.max(0)+margin), axis=1))
                            if len(ids) >= 80:
                                for k, (label, previous) in enumerate(scopes):
                                    if len(np.intersect1d(ids, previous, assume_unique=True)) >= .9*max(len(ids), len(previous)):
                                        scopes[k] = (label, np.union1d(ids, previous))
                                        break
                                else:
                                    scopes.append((f'regional_{margin:g}m', ids))
                        groups.append(scopes)
                else:
                    groups = [[('global', np.arange(len(target)))]]
                # Stop once the smallest successful region/window is verified.
                for scopes in groups:
                    for label, ids in scopes:
                        key = (index, radius, resolution, hashlib.sha256(ids.tobytes()).hexdigest(),
                               hashlib.sha256(np.asarray([T for _, T in guesses], dtype='<f8').tobytes()).hexdigest())
                        if key in self.kiss_attempts:
                            continue
                        self.kiss_attempts.add(key)
                        self.global_attempts += 1
                        self.progress(f'Relocalizing scan {index}: {2*radius:g} s submap, KISS {resolution:g} m, {label}')
                        started = time.perf_counter()
                        try:
                            initial, reused = self.kiss_request(source, target[ids], resolution)
                        except (RuntimeError, ValueError) as exc:
                            self.log(dict(stage='kiss_search', index=index, scope=label, radius_s=radius,
                                resolution_m=resolution, accepted=False, elapsed_s=time.perf_counter()-started, reason=str(exc)))
                            continue
                        self.log(dict(stage='kiss_search', index=index, scope=label, radius_s=radius,
                            resolution_m=resolution, accepted=True, result_reused=reused, elapsed_s=time.perf_counter()-started))
                        self.validate_tail_seed(window, initial, label, resolution)
                    selected = choose_seed(self.seed_hypotheses)
                    if selected is not None:
                        return self._accept_tail_hypothesis(selected)
        return None

    def _accept_tail_hypothesis(self, selected):
        h = next(h for h in self.seed_hypotheses if h['T'] is selected)
        index = h['window'].anchor.index
        if h['certificate']:
            self.remember_certificate(index, h['certificate'])
        for s, (fitted, q) in zip(h['checks'], h['fits']):
            self.seed_fits[s.index] = (fitted, q)
        return self.seed_fits[index][0]
