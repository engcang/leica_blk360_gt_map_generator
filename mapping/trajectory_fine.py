"""Original 1 cm TLS tiles and the shared Open3D point-to-plane/Tukey engine."""
from __future__ import annotations

import itertools
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import weakref
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import open3d as o3d
import scipy
from scipy.spatial import cKDTree

from trajectory_geometry import ply_vertices, voxel, correspondence_quality
from trajectory_io import digest, save_json, transform
from trajectory_native import NativeSession
from parallel_runtime import cacheBudget, threadBudget, availableMemory
from point_to_plane import FINE_ENGINE, RECOVERY_DISTANCES, RECOVERY_ITERATIONS, IndexedPointToPlaneTarget


TILE_M = 8.


class TiledMapIndex:
    """Exact nearest points in the immutable original map, with reusable trees.

    A query visits every occupied tile whose box is within its search radius.
    There is no scan-dependent target crop: movement and tile boundaries cannot
    silently remove a possible correspondence. Eviction affects speed only.
    """
    def __init__(self, matcher):
        self.matcher = matcher
        self.workers = matcher.workers
        self.tile_m = float(matcher.index.get('tile_m', TILE_M))
        self.keys = sorted(matcher.index['tiles'])
        self.coordinates = np.array([[int(v) for v in key.split('_')] for key in self.keys], dtype=np.int64).reshape(-1, 3)
        self.lookup = {tuple(tile): i for i, tile in enumerate(self.coordinates)}
        counts = np.array([matcher.index['tiles'][key] for key in self.keys], dtype=np.int64)
        self.offsets = np.r_[0, np.cumsum(counts)]
        self.n = int(self.offsets[-1])
        self.trees = OrderedDict()
        self.normal_maps = {}
        self.cache_bytes = 0
        self.cache_limit = cacheBudget(.2)
        self.query_plan = None
        self.executor = ThreadPoolExecutor(max_workers=self.workers)
        weakref.finalize(self, self.executor.shutdown, wait=False)
        self.statistics = dict(tree_builds=0, tree_hits=0, tree_evictions=0,
                               tree_preparation_s=0., normal_preparation_s=0.,
                               query_plan_s=0., plan_builds=0, plan_reuses=0,
                               tree_query_s=0., gather_s=0., queried_tile_points=0)

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.trees.clear()
        self.normal_maps.clear()
        self.query_plan = None
        self.cache_bytes = 0
        self.matcher = None

    def _evict(self):
        tile, (_, size) = self.trees.popitem(last=False)
        self.normal_maps.pop(tile, None)
        self.cache_bytes -= size
        self.statistics['tree_evictions'] += 1

    def _tree(self, tile, limit=None):
        limit = min(self.cache_limit, cacheBudget(.2)) if limit is None else limit
        while self.trees and self.cache_bytes > limit:
            self._evict()
        if tile in self.trees:
            self.trees.move_to_end(tile)
            self.statistics['tree_hits'] += 1
            return self.trees[tile][0]
        started = time.perf_counter()
        estimate = int(self.offsets[tile+1]-self.offsets[tile])*64
        while self.trees and self.cache_bytes+estimate > limit:
            self._evict()
        available = availableMemory()
        if available and estimate > available*.8:
            raise MemoryError(f'Original 1 cm tile index needs about {estimate/1024**3:.2f} GiB; '
                              f'{available/1024**3:.2f} GiB is available')
        path = self.matcher.folder/(self.keys[tile]+'.bin')
        raw = np.memmap(path, mode='r', dtype='<f4').reshape(-1, 3)
        tree = cKDTree(np.asarray(raw, dtype=float))
        # Account for owned double coordinates, permutation and native nodes.
        size = tree.data.nbytes + tree.indices.nbytes + tree.size*72
        self.statistics['tree_builds'] += 1
        self.statistics['tree_preparation_s'] += time.perf_counter()-started
        while self.trees and self.cache_bytes+size > limit:
            self._evict()
        if size <= limit:
            self.trees[tile] = tree, size
            self.cache_bytes += size
        return tree

    def _tile_queries(self, points, cap):
        """Group by source tile once; only edge points visit adjacent tiles."""
        if cap >= 2*self.tile_m:
            # Broad diagnostic requests should not enumerate a cubic number
            # of empty cells; querying all occupied tiles remains exact.
            return {i: [np.arange(len(points))] for i in range(len(self.keys))}
        lower = self.coordinates.min(axis=0)*self.tile_m
        upper = (self.coordinates.max(axis=0)+1)*self.tile_m
        margin = 128*np.finfo(float).eps*(1+cap+np.maximum(abs(lower), abs(upper)))
        inside = np.flatnonzero(np.all((points >= lower-cap-margin) & (points <= upper+cap+margin), axis=1))
        if not len(inside):
            return {}
        points = points[inside]
        tiles = np.floor(points/self.tile_m).astype(np.int64)
        # Unique structured triples avoid an O(points * map_tiles) mask scan.
        if tiles.min() >= -(1 << 20) and tiles.max() < (1 << 20):
            shifted = tiles+(1 << 20)
            packed = (shifted[:, 0] << 42) | (shifted[:, 1] << 21) | shifted[:, 2]
        else:
            packed = np.ascontiguousarray(tiles).view(np.dtype((np.void, tiles.dtype.itemsize*3))).ravel()
        _, first, inverse = np.unique(packed, return_index=True, return_inverse=True)
        order = np.argsort(inverse, kind='stable')
        ends = np.r_[0, np.cumsum(np.bincount(inverse))]
        extent = int(np.ceil(cap/self.tile_m))
        offsets = tuple(itertools.product(range(-extent, extent+1), repeat=3))
        plan = {}
        for group, source_tile in enumerate(tiles[first]):
            ids = order[ends[group]:ends[group+1]]
            query = points[ids]
            margin = 128*np.finfo(float).eps*(1+cap+np.max(np.abs(query), axis=1))
            for offset in offsets:
                tile = source_tile+offset
                index = self.lookup.get(tuple(tile))
                if index is None:
                    continue
                lower, upper = tile*self.tile_m, (tile+1)*self.tile_m
                delta = np.maximum(np.maximum(lower-query, query-upper), 0.)
                # Roundoff can only add extra queries, never exclude a match.
                near = np.einsum('ij,ij->i', delta, delta) <= (cap+margin)**2
                if near.any():
                    plan.setdefault(index, []).append(inside[ids[near]])
        return plan

    def _plan(self, points, cap):
        previous = self.query_plan
        if previous is not None and len(previous[0]) == len(points) and cap <= previous[1]:
            reference, radius, plan = previous
            with np.errstate(over='ignore'):
                movement = np.max(np.linalg.norm(points-reference, axis=1), initial=0.)
            margin = 128*np.finfo(float).eps*(1+radius+np.max(np.abs(points), initial=0.))
            if movement+cap < radius-margin:
                self.statistics['plan_reuses'] += 1
                return plan
        radius = cap+.2
        plan = self._tile_queries(points, radius)
        self.query_plan = points.copy(), radius, plan
        self.statistics['plan_builds'] += 1
        return plan

    def query(self, points, k=1, distance_upper_bound=np.inf, workers=None, selected=None):
        points = np.asarray(points, dtype=float)
        if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
            raise ValueError('Finite Nx3 query points are required')
        if not isinstance(k, int) or k < 1:
            raise ValueError('A positive integer neighbor count is required')
        cap = float(distance_upper_bound)
        if cap <= 0 or np.isnan(cap):
            raise ValueError('A positive search radius is required')
        distances = np.full((len(points), k), np.inf)
        nearest = np.full((len(points), k), self.n, dtype=np.int64)
        allowed = None
        if selected is not None:
            allowed = np.zeros(len(points), dtype=bool)
            allowed[selected] = True
        if not len(points) or not self.n:
            return (distances[:, 0], nearest[:, 0]) if k == 1 else (distances, nearest)
        planned = time.perf_counter()
        if np.isfinite(cap):
            plan = self._plan(points, cap)
        else:
            # Full kNN is available for diagnostics; ICP and validation supply
            # their finite physical gates and never enumerate distant tiles.
            plan = {i: [np.arange(len(points))] for i in range(len(self.keys))}
        self.statistics['query_plan_s'] += time.perf_counter()-planned
        limit = min(self.cache_limit, cacheBudget(.2))

        def consume(tile, ids, result):
            d, local = result
            self.statistics['queried_tile_points'] += len(ids)
            d, local = np.asarray(d).reshape(-1, k), np.asarray(local).reshape(-1, k)
            global_ids = np.where(np.isfinite(d), local+self.offsets[tile], self.n)
            if k == 1:
                better = (d[:, 0] < distances[ids, 0]) | ((d[:, 0] == distances[ids, 0]) & (global_ids[:, 0] < nearest[ids, 0]))
                use = ids[better]
                distances[use, 0], nearest[use, 0] = d[better, 0], global_ids[better, 0]
            else:
                merged_d = np.concatenate((distances[ids], d), axis=1)
                merged_i = np.concatenate((nearest[ids], global_ids), axis=1)
                selected = np.lexsort((merged_i, merged_d), axis=1)[:, :k]
                distances[ids] = np.take_along_axis(merged_d, selected, axis=1)
                nearest[ids] = np.take_along_axis(merged_i, selected, axis=1)

        def work(owners):
            for tile in sorted(plan):
                ids = np.concatenate(plan[tile])
                if allowed is not None:
                    ids = ids[allowed[ids]]
                query = points[ids]
                lower = self.coordinates[tile]*self.tile_m
                upper = lower+self.tile_m
                owner = np.all((query >= lower) & (query < upper), axis=1)
                if owners:
                    ids = ids[owner]
                else:
                    ids = ids[~owner]
                    query = query[~owner]
                    delta = np.maximum(np.maximum(lower-query, query-upper), 0.)
                    bound = np.sqrt(np.einsum('ij,ij->i', delta, delta))
                    best = np.minimum(cap, distances[ids, -1])
                    margin = 128*np.finfo(float).eps*(1+cap+np.max(np.abs(query), axis=1))
                    ids = ids[bound <= best+margin]
                if not len(ids):
                    continue
                tree = self._tree(tile, limit)
                for start in range(0, len(ids), 8192):
                    yield tile, ids[start:start+8192], tree

        def search(tree, ids):
            return tree.query(points[ids], k=k, distance_upper_bound=cap, workers=1)

        started = time.perf_counter()
        preparation_before = self.statistics['tree_preparation_s']
        def run(owners):
            if self.workers == 1 or len(points) < 8192:
                for tile, ids, tree in work(owners):
                    consume(tile, ids, search(tree, ids))
                return
            # Bounded futures share expensive tile queries across the CPU
            # budget. Consume in tile/source order for reproducible ties; do
            # not eagerly retain trees for the whole map when RAM is limited.
            pending = deque()
            try:
                for tile, ids, tree in work(owners):
                    pending.append((tile, ids, self.executor.submit(search, tree, ids)))
                    if len(pending) >= 2*self.workers:
                        old_tile, old_ids, future = pending.popleft()
                        consume(old_tile, old_ids, future.result())
                while pending:
                    tile, ids, future = pending.popleft()
                    consume(tile, ids, future.result())
            finally:
                for _, _, future in pending:
                    future.cancel()
        # Start inside each query's tile. Its actual nearest distance is a
        # tighter upper bound than the ICP gate; adjacent boxes farther away
        # cannot contain a better match. Keep equality for deterministic ties.
        run(True)
        run(False)
        self.statistics['tree_query_s'] += max(0., time.perf_counter()-started-
            (self.statistics['tree_preparation_s']-preparation_before))
        return (distances[:, 0], nearest[:, 0]) if k == 1 else (distances, nearest)

    def gather(self, indices, normals=False):
        started = time.perf_counter()
        ids = np.asarray(indices, dtype=np.int64)
        if np.any((ids < 0) | (ids >= self.n)):
            raise ValueError('Cannot gather missing map neighbors')
        flat = ids.ravel()
        xyz = np.empty((len(flat), 3))
        directions = np.empty_like(xyz) if normals else None
        tile_ids = np.searchsorted(self.offsets[1:], flat, side='right')
        order = np.argsort(tile_ids, kind='stable')
        edges = np.r_[0, np.flatnonzero(np.diff(tile_ids[order]))+1, len(order)]
        for begin, end in zip(edges[:-1], edges[1:]):
            if begin == end:
                continue
            rows = order[begin:end]
            tile = tile_ids[rows[0]]
            local = flat[rows]-self.offsets[tile]
            path = self.matcher.folder/(self.keys[tile]+'.bin')
            cached = self.trees.get(tile)
            xyz[rows] = (cached[0].data if cached is not None else
                         np.memmap(path, mode='r', dtype='<f4').reshape(-1, 3))[local]
            if normals:
                mapping = self.normal_maps.get(tile)
                if mapping is None:
                    cold = not (self.matcher.folder/'normals-4cm-40-v1'/(self.keys[tile]+'.npy')).exists()
                    normal_started = time.perf_counter()
                    mapping = self.matcher.tile_normals(self.keys[tile])
                    if tile in self.trees:
                        self.normal_maps[tile] = mapping
                    if cold:
                        self.statistics['normal_preparation_s'] += time.perf_counter()-normal_started
                directions[rows] = mapping[local]
        self.statistics['gather_s'] += time.perf_counter()-started
        shape = ids.shape+(3,)
        return (xyz.reshape(shape), directions.reshape(shape)) if normals else xyz.reshape(shape)


def array_digest(*arrays):
    digest = hashlib.sha256()
    for array in arrays:
        values = np.ascontiguousarray(array, dtype='<f8')
        digest.update(str(values.shape).encode('ascii') + b'\0')
        if values.size:
            digest.update(memoryview(values).cast('B'))
    return digest.hexdigest()


def source_stat(p):
    s = p.stat()
    return [s.st_size, s.st_mtime_ns]


def prepare_fine(cache):
    """Stream original points once. Never synthesize 1cm points from the 10cm map."""
    binding = json.loads((cache/'map.json').read_text())
    source = Path(binding['source']).resolve(strict=True)
    fine = cache/'fine_1cm'
    if (fine/'index.json').exists():
        j = json.loads((fine/'index.json').read_text())
        if j['source_sha256'] != binding['source_sha256'] or j['source_stat'] != source_stat(source):
            raise ValueError('Fine TLS cache source changed')
        return j
    if fine.exists():
        raise ValueError('Incomplete final fine cache; inspect before rebuilding')
    tmp = cache/'fine_1cm.building'
    if tmp.exists():
        shutil.rmtree(tmp)  # Only this owned, locked, regenerable cache.
    tmp.mkdir()
    before = source_stat(source)
    if digest(source) != binding['source_sha256']:
        raise ValueError('Original 1cm TLS map hash changed')
    a = ply_vertices(source)
    counts = {}
    coarse = []
    for start in range(0,len(a),1_000_000):
        xyz = np.column_stack([a[k][start:start+1_000_000] for k in ('x','y','z')]).astype('<f4')
        xyz = xyz[np.isfinite(xyz).all(1)]
        tiles = np.floor(xyz/TILE_M).astype(np.int32)
        if np.any(np.abs(tiles) >= 2**20):
            raise ValueError('TLS tile index out of range')
        b = tiles.astype(np.int64)+2**20
        code = (b[:,0]<<42)|(b[:,1]<<21)|b[:,2]
        order = np.argsort(code,kind='stable')
        edges = np.r_[0,np.flatnonzero(np.diff(code[order]))+1,len(order)]
        for lo,hi in zip(edges[:-1],edges[1:]):
            key = '_'.join(str(x) for x in tiles[order[lo]])
            with (tmp/(key+'.bin')).open('ab') as f:
                xyz[order[lo:hi]].tofile(f)
            counts[key] = counts.get(key,0)+int(hi-lo)
        coarse.append(voxel(xyz,.08))
        if start % 10_000_000 == 0:
            print(f'Fine TLS tiles: {min(start+len(xyz),len(a))}/{len(a)} points',flush=True)
    np.save(tmp/'kiss_8cm.npy',voxel(np.concatenate(coarse),.08))
    if before != source_stat(source):
        raise ValueError('TLS input changed during fine map preparation')
    j = dict(source=str(source),source_sha256=binding['source_sha256'],source_stat=before,
             tile_m=TILE_M,voxel_m=.01,points=sum(counts.values()),tiles=counts)
    save_json(tmp/'index.json',j)
    tmp.rename(fine)
    return j


class FineMatcher:
    def __init__(self,cache,workers=None,result_cache=None):
        self.index = prepare_fine(cache)
        self.native = NativeSession(cache)
        self.folder = cache/'fine_1cm'
        self.kiss_points = np.load(self.folder/'kiss_8cm.npy',mmap_mode='r')
        self.workers = threadBudget(workers)
        index_type = TiledMapIndex
        from native_correspondence import enabled, nativeIndexClass
        if enabled():
            index_type = nativeIndexClass(TiledMapIndex)
        self.map_index = index_type(self)
        self.target = IndexedPointToPlaneTarget(self.map_index, self.workers)
        self.target_uses = 0
        self.target_points = self.map_index.n
        self.target_digest = hashlib.sha256(json.dumps(dict(
            source=self.index['source_sha256'], index=getattr(self.map_index, 'backend', 'exact-tiled-map-v1'),
            native_sha256=getattr(self.map_index, 'fingerprint', None),
            normals='4cm-40-v1', tile_m=self.map_index.tile_m), sort_keys=True).encode()).hexdigest()
        self.result_cache = Path(result_cache) if result_cache is not None else None
        self.tile_cache = OrderedDict()
        self.cache_bytes = 0
        self.cache_limit = cacheBudget(.1)

    def close(self):
        """Release persistent map state between baseline and recovery stages."""
        try:
            self.native.close()
        finally:
            self.map_index.close()
            self.target = None
            self.kiss_points = None
            self.tile_cache.clear()
            self.cache_bytes = 0

    def _tile(self,key):
        if key in self.tile_cache:
            self.tile_cache.move_to_end(key)
            return self.tile_cache[key]
        a = np.fromfile(self.folder/(key+'.bin'),dtype='<f4').reshape(-1,3)
        # RAM controls cache retention, never the points used for fitting.
        limit = min(getattr(self, 'cache_limit', sys.maxsize), cacheBudget(.1))
        if a.nbytes > limit:
            return a
        while self.tile_cache and self.cache_bytes+a.nbytes > limit:
            _, old = self.tile_cache.popitem(last=False)
            self.cache_bytes -= old.nbytes
        self.tile_cache[key] = a; self.cache_bytes += a.nbytes
        return a

    def tile_normals(self, key):
        """Cache original-map normals once, including neighbors across tile edges."""
        directory = self.folder/'normals-4cm-40-v1'
        directory.mkdir(exist_ok=True)
        path = directory/(key+'.npy')
        if not path.exists():
            tile = np.array([int(v) for v in key.split('_')])
            lower, upper = tile*TILE_M-.04, (tile+1)*TILE_M+.04
            core = self._tile(key)
            chunks = [core]
            for offset in itertools.product((-1, 0, 1), repeat=3):
                if offset == (0, 0, 0):
                    continue
                neighbor = '_'.join(str(v) for v in tile+offset)
                if neighbor not in self.index['tiles']:
                    continue
                p = self._tile(neighbor)
                chunks.append(p[np.all((p >= lower) & (p <= upper), axis=1)])
            cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.concatenate(chunks).astype(float)))
            o3d.utility.set_max_threads(self.workers)
            cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=.04, max_nn=40))
            temporary = path.with_suffix(f'.{os.getpid()}.tmp')
            with temporary.open('wb') as stream:
                np.save(stream, np.asarray(cloud.normals)[:len(core)])
            temporary.replace(path)
        return np.load(path, mmap_mode='r')

    def align(self,points,initial,iterations=RECOVERY_ITERATIONS,capture_information=False):
        started = time.perf_counter()
        if len(points)<80:
            return initial.copy(),dict(converged=False,reason='insufficient_source')
        reuse = self.target_uses > 0
        before = dict(self.map_index.statistics)
        prepared = time.perf_counter()
        source = voxel(points,.01)
        cache_file = None
        if self.result_cache is not None:
            request = dict(version=2, engine=FINE_ENGINE, target=self.target_digest,
                source_and_initial=array_digest(source, initial), iterations=iterations,
                distances=list(RECOVERY_DISTANCES), relative_fitness=1e-6, relative_rmse=1e-6,
                capture_information=bool(capture_information),
                solver_threads=1, numpy=np.__version__, scipy=scipy.__version__, open3d=o3d.__version__)
            request_key = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
            cache_file = self.result_cache/(request_key+'.json')
            candidates = [cache_file]+[Path(p)/(request_key+'.json') for p in getattr(self, 'result_sources', ())]
            for candidate in candidates:
                try:
                    cached = json.loads(candidate.read_text())
                    T = np.asarray(cached['transformation'], dtype=float)
                    info = cached['fine']
                    if cached['request'] == request and T.shape == (4,4) and np.isfinite(T).all() and isinstance(info['converged'], bool):
                        self.target_uses += 1
                        return T,dict(info, result_reused=True, request_key=request_key,
                            cached_fitting_s=info['fitting_s'], target_reused=bool(reuse),
                            query_statistics=dict(queried_points=0, excluded_by_bound=0, unbounded_queries=0),
                            index_statistics=dict({key: 0 for key in self.map_index.statistics},
                                                  retained_trees=len(self.map_index.trees)+len(getattr(self.map_index, 'native_tiles', {})),
                                                  retained_bytes=self.map_index.cache_bytes+getattr(self.map_index, 'native_bytes', 0)),
                            target_preparation_s=prepared-started, fitting_s=time.perf_counter()-prepared)
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Incomplete/corrupt regenerable cache entries are recomputed.
        result = self.target.align(source, initial, RECOVERY_DISTANCES,
                                   iterations=iterations, capture_information=capture_information)
        self.target_uses += 1
        T = result.transformation
        index_stats = {key: value-before[key] for key, value in self.map_index.statistics.items()}
        index_preparation_s = index_stats['tree_preparation_s']+index_stats['normal_preparation_s']
        info = dict(engine=FINE_ENGINE, converged=bool(result.converged and np.isfinite(T).all()),
                      correspondence_backend=getattr(self.map_index, 'backend', 'scipy-tiled-map-v1'),
                      convergence_reason=result.convergence_reason, robust_support=result.robust_support,
                      correspondences=len(result.correspondence_set), iterations=result.iterations,
                      target_preparation_s=prepared-started+index_preparation_s,
                      fitting_s=max(0., time.perf_counter()-prepared-index_preparation_s),
                      query_statistics=result.query_statistics,
                      target_points=self.target_points,target_reused=bool(reuse),
                      index_statistics=dict(index_stats, retained_trees=len(self.map_index.trees)+len(getattr(self.map_index, 'native_tiles', {})),
                                            retained_bytes=self.map_index.cache_bytes+getattr(self.map_index, 'native_bytes', 0)),
                      source_voxel_m=.01,target_voxel_m=.01)
        if capture_information:
            info['optimizer_information'] = result.optimizer_information
        if cache_file is not None:
            info.update(result_reused=False, request_key=request_key)
            if np.isfinite(T).all():
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                save_json(cache_file, dict(request=request, transformation=T.tolist(), fine=info))
        return T,info

    def verify_unchanged(self):
        if source_stat(Path(self.index['source'])) != self.index['source_stat']:
            raise ValueError('Original TLS map changed during registration')

    def quality(self, points, T):
        """Held-out geometry on the original 1 cm target, with planar support.

        Recompute validation normals from nearby original points. Coarse 10 cm
        normals can erase small structures, while isolated/noisy fine normals
        must not create artificial directional information.
        """
        if self.target is None:
            return dict(accepted=False, reason='insufficient_fine_target', points=0)
        moved = transform(points, T)
        indexed = hasattr(self, 'map_index')
        tree = self.map_index if indexed else self.target.tree
        target = None if indexed else np.asarray(self.target.cloud.points)
        dist, nearest = tree.query(moved, distance_upper_bound=.2, workers=self.workers)
        valid = np.isfinite(dist)
        p = moved[valid]
        centers = tree.gather(nearest[valid]) if indexed else target[nearest[valid]]
        if len(centers) < 80:
            return dict(accepted=False, reason='insufficient_support', points=len(centers))
        d, ids = tree.query(centers, k=20, distance_upper_bound=.3, workers=self.workers)
        complete = np.isfinite(d[:, -1])
        p, centers, d, ids = p[complete], centers[complete], d[complete], ids[complete]
        delta = tree.gather(ids) if indexed else target[ids].copy()
        delta -= delta.mean(axis=1)[:, None]
        eigen, vectors = np.linalg.eigh(np.einsum('nki,nkj->nij', delta, delta)/20)
        good = (d[:, -1] < .3) & (eigen[:, 1] > 1e-6) & (eigen[:, 0] < .15*eigen[:, 1])
        normals = vectors[good, :, 0]
        residual = np.einsum('ij,ij->i', p[good]-centers[good], normals)
        quality = correspondence_quality(p[good], normals, residual, dist, T)
        quality['validation_map_voxel_m'] = .01
        return quality
