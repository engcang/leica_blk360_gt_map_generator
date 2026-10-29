"""Open3D point-to-plane/Tukey fitting with reusable parallel target queries."""
import copy
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import numpy as np
import open3d as o3d
from scipy.spatial import cKDTree

from parallel_runtime import threadBudget, availableMemory

FINE_ENGINE = 'open3d-point-to-plane-tukey-v4'
RECOVERY_DISTANCES = (.8, .3, .1)
RECOVERY_ITERATIONS = 60

_estimator_lock = threading.Lock()


class PointToPlaneTarget:
    def __init__(self, target, workers=None, *, native=False):
        self.workers = threadBudget(workers)
        count = len(target.points) if isinstance(target, o3d.geometry.PointCloud) else len(target)
        existing_normals = isinstance(target, o3d.geometry.PointCloud) and target.has_normals()
        required = count * (40 if existing_normals else 128)
        available = availableMemory()
        if available and required > available * .8:
            raise MemoryError(f'1 cm target needs approximately {required/1024**3:.2f} GiB of additional memory; '
                              f'{available/1024**3:.2f} GiB is available. Free memory before retrying.')
        if isinstance(target, o3d.geometry.PointCloud):
            self.cloud = target
        else:
            self.cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(target, dtype=float)))
        if not self.cloud.has_normals():
            o3d.utility.set_max_threads(self.workers)
            self.cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=.04, max_nn=40))
        self._native = None
        if native:
            from native_correspondence import ResidentIndex
            self._native = ResidentIndex.create(np.asarray(self.cloud.points),self.workers)
        self.tree = None if self._native else cKDTree(np.asarray(self.cloud.points))
        self._query_state = threading.local()
        self._bound_tree = None
        self._queries = ThreadPoolExecutor(max_workers=self.workers)
        weakref.finalize(self, self._queries.shutdown, wait=False)

    def _query(self, tree, points, **options):
        if self.workers == 1 or len(points) < 32_768:
            return tree.query(points, workers=self.workers, **options)
        # Spatially ordered scans otherwise give each native thread one large
        # region. Small queued batches share expensive regions across the budget.
        def batch(start):
            return tree.query(points[start:start+8192], workers=1, **options)
        results = list(self._queries.map(batch, range(0, len(points), 8192)))
        return tuple(np.concatenate([result[k] for result in results]) for k in (0, 1))

    def _prepare_bounds(self):
        """Cover every target point with a cell; this index never supplies matches."""
        if self._bound_tree is not None:
            return True
        count = self.tree.n
        available = availableMemory()
        # Optional acceleration must fit alongside the unchanged fine target.
        if count < 50_000 or (available and count*96 > available*.2):
            return False
        points = np.asarray(self.cloud.points)
        cell = .1
        try:
            extent = float(np.max(np.abs(points)))
            if extent/cell >= 2**62:
                return False  # Keep the original index outside the integer grid range.
            grid = np.floor(points/cell).astype(np.int64)
            if grid.min() >= -(1 << 20) and grid.max() < (1 << 20):
                shifted = grid+(1 << 20)
                keys = (shifted[:, 0] << 42) | (shifted[:, 1] << 21) | shifted[:, 2]
                _, chosen = np.unique(keys, return_index=True)
                centers = (grid[chosen]+.5)*cell
            else:
                centers = (np.unique(grid, axis=0)+.5)*cell
            bound_tree = cKDTree(centers)
        except MemoryError:
            return False
        # Account for rounding when forming cells, including large coordinates.
        self._bound_radius = np.sqrt(3)*cell/2 + 128*np.finfo(float).eps*(1+extent)
        self._bound_tree = bound_tree
        return True

    def _lower_bounds(self, points):
        # Every fine target point lies within _bound_radius of an indexed center.
        # Triangle inequality therefore gives a lower bound on its distance.
        distance = self._query(self._bound_tree, points)[0]
        return np.maximum(0., distance-self._bound_radius)

    def correspondences(self, source, cap):
        points = np.asarray(source.points)
        previous = getattr(self._query_state, 'bounds', None)
        if self._native is not None:
            from native_correspondence import ResidentMatches
            state = getattr(self._query_state,'resident_matches',None)
            if state is None or state.count != len(points):
                state = ResidentMatches(self._native,len(points))
                self._query_state.resident_matches = state
                previous = None
            pairs,fitness,rmse,counts = state.match(points,cap,reset=previous is None)
            self._query_state.bounds = True
            stats = getattr(self._query_state,'stats',None)
            if stats is not None:
                for key,value in counts.items():
                    stats[key] = stats.get(key,0)+value
            return o3d.utility.Vector2iVector(pairs),fitness,rmse
        if previous is None or len(previous[0]) != len(points):
            reference = points.copy()
            lower = np.zeros(len(points))
            selected = np.arange(len(points))
            if self._bound_tree is None and len(points) >= 32_768 and self.tree.n >= 50_000:
                sample = points[::max(1, len(points)//256)][:256]
                distance = self.tree.query(sample, distance_upper_bound=cap, workers=1)[0]
                if np.mean(~np.isfinite(distance)) >= .1:
                    self._prepare_bounds()
        else:
            reference, lower = previous
            # Distance to a fixed set is 1-Lipschitz. A point whose previous
            # exact distance minus its displacement still exceeds the gate
            # cannot contribute a correspondence. Preserve a numerical margin.
            bound = lower-np.linalg.norm(points-reference, axis=1)
            margin = 128*np.finfo(float).eps*(1+cap+np.linalg.norm(points, axis=1)+
                                              np.linalg.norm(reference, axis=1)+lower)
            selected = np.flatnonzero(~np.isfinite(bound) | (bound <= cap+margin))
        distances = np.full(len(points), np.inf)
        indices = np.full(len(points), self.tree.n, dtype=np.intp)
        stats = getattr(self._query_state, 'stats', None)
        if stats is not None:
            stats['excluded_by_bound'] += len(points)-len(selected)
        bounded = self._bound_tree is not None
        if bounded and len(selected):
            candidate = points[selected]
            bound = self._lower_bounds(candidate)
            margin = 128*np.finfo(float).eps*(1+cap+np.linalg.norm(candidate, axis=1)+bound)
            reference[selected], lower[selected] = candidate, bound
            uncertain = bound <= cap+margin
            if stats is not None:
                stats['coarse_bound_points'] = stats.get('coarse_bound_points', 0)+len(selected)
                stats['excluded_by_coarse'] = stats.get('excluded_by_coarse', 0)+int(np.count_nonzero(~uncertain))
            selected = selected[uncertain]
        if stats is not None:
            stats['queried_points'] += len(selected)
        if len(selected):
            d, ids = self._query(self.tree, points[selected], distance_upper_bound=cap)
            distances[selected], indices[selected] = d, ids
            reference[selected] = points[selected]
            finite = np.isfinite(d)
            lower[selected[finite]] = d[finite]
            missing = selected[~finite]
            if len(missing):
                if bounded:
                    lower[missing] = np.maximum(cap, lower[missing])
                elif len(missing) >= max(4096, len(points)*.1) and self._prepare_bounds():
                    lower[missing] = np.maximum(cap, self._lower_bounds(points[missing]))
                else:
                    # Small/full-overlap fits avoid building an auxiliary index.
                    lower[missing] = self._query(self.tree, points[missing])[0]
                    if stats is not None:
                        stats['unbounded_queries'] += len(missing)
        self._query_state.bounds = reference, lower
        valid = distances < cap
        pairs = np.column_stack((np.flatnonzero(valid), indices[valid])).astype(np.int32)
        fitness = len(pairs)/len(points) if len(points) else 0.
        rmse = float(np.sqrt(np.mean(distances[valid]**2))) if len(pairs) else 0.
        return o3d.utility.Vector2iVector(pairs), fitness, rmse

    def align(self, source, initial, distances=(.03,), tukey_scales=None, iterations=30,
              relative_fitness=1e-6, relative_rmse=1e-6, capture_information=False):
        if not distances or any(not np.isfinite(c) or c <= 0 for c in distances):
            raise ValueError('Positive correspondence distances are required')
        scales = tukey_scales if tukey_scales is not None else [min(.1, cap/3) for cap in distances]
        if len(scales) != len(distances) or any(not np.isfinite(k) or k <= 0 for k in scales):
            raise ValueError('One positive Tukey scale is required per distance')
        original = source if isinstance(source, o3d.geometry.PointCloud) else o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(source, dtype=float)))
        T = np.asarray(initial, dtype=float).copy()
        self._query_state.bounds = None
        self._query_state.stats = dict(queried_points=0, excluded_by_bound=0, unbounded_queries=0)
        used_iterations = 0
        convergence_reason = 'iteration_limit'
        robust_support = 0
        for cap, scale in zip(distances, scales):
            moved = copy.deepcopy(original)
            moved.transform(T)
            pairs, fitness, rmse = self.correspondences(moved, cap)
            estimator = o3d.pipelines.registration.TransformationEstimationPointToPlane(
                o3d.pipelines.registration.TukeyLoss(scale))
            converged = False
            robust_support = None
            stable_steps = 0
            for _ in range(iterations):
                # Queries/normal estimation are parallel. Stable source order and
                # a serial Open3D Hessian reduction make CPU budgets reproducible.
                with _estimator_lock:
                    o3d.utility.set_max_threads(1)
                    try:
                        update = estimator.compute_transformation(moved, self.cloud, pairs)
                    finally:
                        o3d.utility.set_max_threads(self.workers)
                if not np.isfinite(update).all():
                    raise ValueError('Open3D returned a non-finite point-to-plane update')
                before = T[:3, 3].copy()
                T = update @ T
                step_m = float(np.linalg.norm(T[:3, 3]-before))
                step_rotation = float(np.linalg.norm(update[:3, :3]-np.eye(3))/np.sqrt(2))
                moved.transform(update)
                previous_fitness, previous_rmse = fitness, rmse
                pairs, fitness, rmse = self.correspondences(moved, cap)
                used_iterations += 1
                stable_rmse = abs(previous_rmse-rmse) < relative_rmse
                stable_steps = stable_steps+1 if step_m < 1e-5 and step_rotation < 1e-6 and stable_rmse else 0
                metric_convergence = abs(previous_fitness-fitness) < relative_fitness and stable_rmse
                if metric_convergence or stable_steps >= 3:
                    matches = np.asarray(pairs)
                    if len(matches):
                        normals = np.asarray(self.cloud.normals)[matches[:, 1]]
                        residual = np.einsum('ij,ij->i', np.asarray(moved.points)[matches[:, 0]]-
                                             np.asarray(self.cloud.points)[matches[:, 1]], normals)
                        robust_support = int(np.count_nonzero((abs(residual) < scale) & (np.linalg.norm(normals, axis=1) > .5)))
                    else:
                        robust_support = 0
                    converged = robust_support >= 6
                    convergence_reason = ('fitness_rmse' if metric_convergence else 'stationary_pose') if converged else 'insufficient_robust_support'
                    break
            else:
                convergence_reason = 'iteration_limit'
        information = None
        if capture_information:
            matches = np.asarray(pairs)
            p = np.asarray(moved.points)[matches[:, 0]]
            n = np.asarray(self.cloud.normals)[matches[:, 1]]
            residual = np.einsum('ij,ij->i', p-np.asarray(self.cloud.points)[matches[:, 1]], n)
            positive = (abs(residual) < scale) & (np.linalg.norm(n, axis=1) > .5)
            weights = (1-(residual[positive]/scale)**2)**2
            J = np.column_stack((np.cross((p[positive]-T[:3, 3])/5., n[positive]), n[positive]))
            H = J.T @ (weights[:, None]*J)/max(1, len(weights))
            eigenvalues = np.linalg.eigvalsh(H)
            rank = int(np.count_nonzero(eigenvalues > max(float(eigenvalues[-1])*1e-3, 1e-5)))
            information = dict(scaled_information_matrix=H.tolist(), eigenvalues=eigenvalues.tolist(),
                               observable_directions=rank, positive_points=int(positive.sum()),
                               tukey_scale=float(scale), rotation_scale_m=5.)
        return SimpleNamespace(transformation=T, fitness=fitness, inlier_rmse=rmse,
                               correspondence_set=pairs, iterations=used_iterations, converged=converged,
                               convergence_reason=convergence_reason, robust_support=robust_support,
                               query_statistics=dict(self._query_state.stats), optimizer_information=information)


class IndexedPointToPlaneTarget(PointToPlaneTarget):
    """Run the same Open3D solver against an exact, externally indexed map.

    Only matching original target points are copied into Open3D. Their source
    order and normals are unchanged, so compacting target indices does not
    change the serial point-to-plane Hessian reduction.
    """
    def __init__(self, index, workers=None):
        self.workers = threadBudget(workers)
        self.index = index
        self._query_state = threading.local()
        self.cloud = o3d.geometry.PointCloud()

    def correspondences(self, source, cap):
        started = time.perf_counter()
        points = np.asarray(source.points)
        count = len(points)
        previous = getattr(self._query_state, 'bounds', None)
        if getattr(self.index, 'fused_correspondences', False):
            state = getattr(self._query_state, 'native_matches', None)
            if state is None or state.count != count:
                if state is not None:
                    state.close()
                state = self.index.new_match_state(count)
                self._query_state.native_matches = state
                previous = None
            if len(self.cloud.points) != count:
                self.cloud.points = o3d.utility.Vector3dVector(np.zeros((count,3)))
                self.cloud.normals = o3d.utility.Vector3dVector(np.zeros((count,3)))
            result = state.match(points, cap, np.asarray(self.cloud.points), np.asarray(self.cloud.normals),
                                 reset=not isinstance(previous, str))
            if result is not None:
                pairs, fitness, rmse, measurements = result
                self._query_state.bounds = 'native'
                stats = getattr(self._query_state, 'stats', None)
                if stats is not None:
                    for key, value in measurements.items():
                        stats[key] = stats.get(key,0)+value
                    stats['query_and_gather_s'] = stats.get('query_and_gather_s',0.)+time.perf_counter()-started
                return o3d.utility.Vector2iVector(pairs), fitness, rmse
            if isinstance(previous, str):
                previous = None
        distances = np.full(count, np.inf)
        indices = np.full(count, self.index.n, dtype=np.int64)
        certified = np.zeros(count, dtype=bool)
        missing = np.zeros(count, dtype=bool)
        if previous is None or len(previous[0]) != count:
            reference = points.copy()
            candidates = np.full_like(points, np.nan)
            candidate_ids = indices.copy()
            second_lower = np.zeros(count)
        else:
            reference, candidates, candidate_ids, second_lower = previous
            if hasattr(self.index, 'certify'):
                self.index.certify(points, previous, cap, distances, indices, certified, missing)
            else:
                movement = np.linalg.norm(points-reference, axis=1)
                candidate_distance = np.linalg.norm(points-candidates, axis=1)
                margin = 128*np.finfo(float).eps*(1+cap+second_lower+
                           np.linalg.norm(points, axis=1)+np.linalg.norm(reference, axis=1))
                present = candidate_ids < self.index.n
                # Every competing point stays at least d2 - movement away.
                # Strict inequality excludes ties, including at tile boundaries.
                certified = present & (candidate_distance+movement < second_lower-margin)
                keep = certified & (candidate_distance < cap)
                distances[keep], indices[keep] = candidate_distance[keep], candidate_ids[keep]
                missing = ~present & (second_lower-movement > cap+margin)
        selected = np.flatnonzero(~certified & ~missing)
        inplace = getattr(self.index, 'inplace_correspondences', False)
        tie_count = 0
        if len(selected):
            # Retain full source row identities for the reusable tile plan;
            # querying a differently sized subset would rebuild it each step.
            all_distances, all_ids = self.index.query(points, k=2, distance_upper_bound=cap, selected=selected)
            # Native buffers include unselected rows as infinities. Keep their
            # original row identities and update state directly in C++.
            d, ids = (all_distances, all_ids) if inplace else (all_distances[selected], all_ids[selected])
            tied = np.isfinite(d[:, 1]) & (d[:, 0] == d[:, 1])
            if tied.any():
                # cKDTree may order equal neighbors differently for k=1/k=2.
                # Keep the established k=1 choice; ties are never certified.
                tied_rows = np.flatnonzero(tied) if inplace else selected[tied]
                tie_distances, tie_ids = self.index.query(points, distance_upper_bound=cap, selected=tied_rows)
                d[tied, 0], ids[tied, 0] = tie_distances[tied_rows], tie_ids[tied_rows]
                tie_count = int(tied.sum())
            if inplace:
                self.index.update_correspondences(points, selected, d, ids, cap,
                    (reference, candidates, candidate_ids, second_lower), distances, indices)
            else:
                distances[selected], indices[selected] = d[:, 0], ids[:, 0]
                reference[selected] = points[selected]
                candidate_ids[selected] = ids[:, 0]
                candidates[selected] = np.nan
                second_lower[selected] = np.minimum(d[:, 1], cap)
        valid = distances < cap
        rows = np.flatnonzero(valid)
        if hasattr(self.index, 'gather_into'):
            # Retain Open3D's buffers across ICP iterations. Source row order,
            # matching coordinates, normals and serial reduction stay unchanged.
            if len(self.cloud.points) != count:
                self.cloud.points = o3d.utility.Vector3dVector(np.zeros((count, 3)))
                self.cloud.normals = o3d.utility.Vector3dVector(np.zeros((count, 3)))
            xyz, normals = np.asarray(self.cloud.points), np.asarray(self.cloud.normals)
            self.index.gather_into(indices, xyz, normals)
            if inplace:
                pairs = self.index.pack_correspondences(rows, xyz, candidates)
            else:
                candidates[valid] = xyz[valid]
                pairs = np.column_stack((rows, rows)).astype(np.int32)
        else:
            xyz, normals = self.index.gather(indices[valid], normals=True)
            candidates[valid] = xyz
            self.cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz))
            self.cloud.normals = o3d.utility.Vector3dVector(normals)
            pairs = np.column_stack((rows, np.arange(len(rows)))).astype(np.int32)
        self._query_state.bounds = reference, candidates, candidate_ids, second_lower
        stats = getattr(self._query_state, 'stats', None)
        if stats is not None:
            stats['queried_points'] += len(selected)+tie_count
            stats['exact_correspondences_reused'] = stats.get('exact_correspondences_reused', 0)+int((certified & valid).sum())
            stats['absent_correspondences_reused'] = stats.get('absent_correspondences_reused', 0)+int((missing | (certified & ~valid)).sum())
            stats['tie_queries'] = stats.get('tie_queries', 0)+tie_count
            stats['query_and_gather_s'] = stats.get('query_and_gather_s', 0.) + time.perf_counter()-started
        fitness = len(pairs)/len(points) if len(points) else 0.
        rmse = float(np.sqrt(np.mean(distances[valid]**2))) if len(pairs) else 0.
        return o3d.utility.Vector2iVector(pairs), fitness, rmse
