"""C++ tiled queries with caller-owned buffers and bounded immutable tile storage."""
import ctypes as ct
import hashlib
import os
from pathlib import Path
import time
import weakref
from functools import lru_cache

import numpy as np

from parallel_runtime import cacheBudget, threadBudget


def libraryPath():
    name = 'blk_correspondence.dll' if os.name == 'nt' else 'libblk_correspondence.so'
    return Path(os.environ.get('BLK_CORRESPONDENCE_LIBRARY', Path(__file__).with_name('bin') / name))


def enabled():
    setting = os.environ.get('BLK_CORRESPONDENCE_BACKEND', 'auto')
    if setting not in ('auto', 'cpp', 'python'):
        raise ValueError('BLK_CORRESPONDENCE_BACKEND must be auto, cpp, or python')
    if setting == 'cpp' and not libraryPath().is_file():
        raise FileNotFoundError(f'Build blk_correspondence first: {libraryPath()}')
    return setting == 'cpp' or (setting == 'auto' and libraryPath().is_file())


def backendInfo():
    if not enabled():
        return dict(backend='scipy-tiled-map-v1')
    return dict(backend='native-tiled-map-v1', sha256=hashlib.sha256(libraryPath().read_bytes()).hexdigest())


def loadLibrary(path=None):
    lib = ct.CDLL(str(Path(path or libraryPath()).resolve()))
    pointer, integer = ct.c_void_p, ct.c_int64
    signatures = dict(
        blk_correspondence_abi=(ct.c_int, []),
        blk_correspondence_error=(ct.c_char_p, []),
        blk_index_create=(pointer, [pointer, pointer, integer, ct.c_double, ct.c_int]),
        blk_index_destroy=(None, [pointer]),
        blk_index_add=(ct.c_int, [pointer, integer, pointer]),
        blk_index_remove=(None, [pointer, integer]),
        blk_index_normals=(None, [pointer, integer, pointer]),
        blk_index_query=(ct.c_int, [pointer, pointer, integer, ct.c_int, ct.c_double, pointer, pointer, pointer]),
        blk_index_gather=(ct.c_int, [pointer, pointer, integer, pointer, pointer]),
        blk_certify=(None, [pointer, pointer, pointer, pointer, pointer, pointer, integer, ct.c_double,
                            pointer, pointer, pointer, pointer]))
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    if hasattr(lib, 'blk_index_bytes'):
        lib.blk_index_bytes.restype = ct.c_uint64
        lib.blk_index_bytes.argtypes = [pointer, integer]
    if hasattr(lib, 'blk_matches_search_halo'):
        lib.blk_matches_search_halo.restype = ct.c_double
        lib.blk_matches_search_halo.argtypes = []
    optional = dict(
        blk_points_finite=[pointer, integer, ct.c_int],
        blk_index_plan=[pointer, pointer, integer, ct.c_double, pointer],
        blk_plan_reusable=[pointer, pointer, pointer, integer, ct.c_double, ct.c_double],
        blk_index_gather_plan=[pointer, pointer, integer, pointer],
        blk_index_add_batch=[pointer, pointer, pointer, integer],
        blk_voxel_rows=[pointer, integer, ct.c_int, ct.c_double, ct.c_int, pointer, pointer])
    for name, args in optional.items():
        if hasattr(lib, name):
            function = getattr(lib, name)
            function.restype, function.argtypes = ct.c_int, args
    for name, args in dict(
        blk_correspondence_update=[pointer, pointer, pointer, integer, pointer, pointer, integer,
                                   ct.c_double, pointer, pointer, pointer, pointer, pointer, pointer],
        blk_correspondence_pack=[pointer, pointer, integer, pointer, pointer, pointer]).items():
        if hasattr(lib, name):
            function = getattr(lib, name)
            function.restype, function.argtypes = None, args
    fused = dict(
        blk_resident_create=(pointer, [pointer, integer, ct.c_int]),
        blk_resident_destroy=(None, [pointer]),
        blk_resident_matches_create=(pointer, [pointer, integer]),
        blk_resident_matches_destroy=(None, [pointer]),
        blk_resident_matches_run=(ct.c_int, [pointer, pointer, ct.c_double, ct.c_int,
                                           pointer, pointer, pointer, pointer]),
        blk_matches_create=(pointer, [pointer, integer]),
        blk_matches_destroy=(None, [pointer]),
        blk_matches_set_search_halo=(ct.c_int, [pointer, ct.c_double]),
        blk_matches_run=(ct.c_int, [pointer, pointer, ct.c_double, ct.c_int,
                                   pointer, pointer, pointer, pointer, pointer, pointer]),
        blk_matches_patch_ties=(ct.c_int, [pointer, pointer, pointer, pointer, integer,
                                          pointer, pointer, pointer]))
    for name, (result, args) in fused.items():
        if hasattr(lib, name):
            function = getattr(lib, name)
            function.restype, function.argtypes = result, args
    if lib.blk_correspondence_abi() != 1:
        raise RuntimeError('Incompatible native correspondence library')
    return lib


def pointer(array):
    return None if array is None else array.ctypes.data


def finitePoints(lib, points, workers):
    if hasattr(lib,'blk_points_finite'):
        return bool(lib.blk_points_finite(pointer(points),len(points),workers))
    return bool(np.isfinite(points).all())


@lru_cache(maxsize=4)
def voxelLibrary(path):
    return loadLibrary(path)


def voxelIndices(points, size, workers=None):
    if (points.dtype not in (np.dtype('float32'),np.dtype('float64'))
            or points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(size) or size <= 0
            or not enabled()):
        return None
    if (np.empty(0,dtype=points.dtype)/size).dtype != points.dtype:
        return None
    lib = voxelLibrary(str(libraryPath().resolve()))
    if not hasattr(lib,'blk_voxel_rows'):
        return None
    data = np.ascontiguousarray(points)
    ids = np.empty(len(data),dtype=np.int64)
    used = np.empty(1,dtype=np.int64)
    result = lib.blk_voxel_rows(pointer(data),len(data),int(data.dtype==np.float32),size,
                                threadBudget(workers),pointer(ids),pointer(used))
    if result:
        raise RuntimeError(lib.blk_correspondence_error().decode())
    return ids[:used[0]] if used[0]>=0 else None


class ResidentIndex:
    """Zero-copy float64 target index; each fitting thread owns its match state."""
    def __init__(self, points, workers, lib):
        self.points, self.workers, self.lib = points, workers, lib
        self.handle = lib.blk_resident_create(pointer(points),len(points),workers)
        if not self.handle:
            raise MemoryError(lib.blk_correspondence_error().decode())
        self._release = weakref.finalize(self,lib.blk_resident_destroy,self.handle)
        self.reference = None
        # Ties are uncommon; do not build or retain a second index unless needed.
        import threading
        self._reference_lock = threading.Lock()

    def resolve_ties(self, points, cap):
        from scipy.spatial import cKDTree
        with self._reference_lock:
            if self.reference is None:
                self.reference = cKDTree(self.points)
        return self.reference.query(points,distance_upper_bound=cap,workers=self.workers)

    @classmethod
    def create(cls, points, workers):
        if not enabled():
            return None
        lib = loadLibrary()
        if not hasattr(lib,'blk_resident_matches_run'):
            return None  # Installed older libraries retain the SciPy path.
        if len(points) >= np.iinfo(np.int32).max or not finitePoints(lib,points,workers):
            raise ValueError('Finite PLY target within Open3D correspondence capacity required')
        return cls(points,workers,lib)


class ResidentMatches:
    def __init__(self, index, count):
        self.index, self.count = index, count
        self.handle = index.lib.blk_resident_matches_create(index.handle,count)
        if not self.handle:
            raise MemoryError(index.lib.blk_correspondence_error().decode())
        self._release = weakref.finalize(self,index.lib.blk_resident_matches_destroy,self.handle)
        self.distances = np.empty(count,dtype=np.float64)
        self.ids = np.empty(count,dtype=np.int32)
        self.ties = np.empty(count,dtype=np.uint8)
        self.statistics = np.empty(4,dtype=np.int64)

    def match(self, points, cap, reset):
        if (points.shape != (self.count,3) or points.dtype != np.float64
                or not points.flags.c_contiguous or not finitePoints(self.index.lib,points,self.index.workers)
                or not np.isfinite(cap) or cap <= 0):
            raise ValueError('Finite contiguous XYZ points and positive distance required')
        index = self.index
        result = index.lib.blk_resident_matches_run(self.handle,pointer(points),cap,int(reset),
            pointer(self.distances),pointer(self.ids),pointer(self.ties),pointer(self.statistics))
        if result:
            raise RuntimeError(index.lib.blk_correspondence_error().decode())
        if self.statistics[3]:
            rows = np.flatnonzero(self.ties)
            self.distances[rows], self.ids[rows] = index.resolve_ties(points[rows],cap)
        valid = self.distances < cap
        pairs = np.column_stack((np.flatnonzero(valid),self.ids[valid])).astype(np.int32)
        rmse = float(np.sqrt(np.mean(self.distances[valid]**2))) if len(pairs) else 0.
        queried,reused,absent,ties = map(int,self.statistics)
        return pairs,len(pairs)/self.count if self.count else 0.,rmse,dict(
            queried_points=queried+ties,exact_correspondences_reused=reused,
            excluded_by_bound=absent,tie_queries=ties)


class NativeMatches:
    """Own correspondence scratch buffers across iterations, with exact tie fallback."""
    def __init__(self, index, count):
        self.index, self.count = index, count
        self.handle = index.lib.blk_matches_create(index.handle, count)
        if not self.handle:
            raise MemoryError(index.lib.blk_correspondence_error().decode())
        self._release = weakref.finalize(self, index.lib.blk_matches_destroy, self.handle)
        if hasattr(index.lib,'blk_matches_set_search_halo'):
            index.check(index.lib.blk_matches_set_search_halo(self.handle,index.match_halo))
        self.distances = np.empty(count, dtype=np.float64)
        self.pairs = np.empty((count,2), dtype=np.int32)
        self.ties = np.empty(count, dtype=np.uint8)
        self.statistics = np.empty(5, dtype=np.int64)

    def close(self):
        self._release()

    def match(self, points, cap, xyz, normals, reset):
        index = self.index
        if not self._release.alive:
            raise RuntimeError('Native correspondence state is closed')
        if (points.shape != (self.count,3) or not points.flags.c_contiguous
                or points.dtype != np.float64 or not finitePoints(index.lib,points,index.workers)
                or not np.isfinite(cap) or cap <= 0):
            raise ValueError('Finite contiguous XYZ points and positive distance required')
        started = time.perf_counter()
        if cap >= 2*index.tile_m:
            return None
        plan = index._native_plan(points, cap+index.match_halo)
        index.statistics['query_plan_s'] += time.perf_counter()-started
        if not index._native_prepare(plan, normals=True):
            return None  # The streamed reference path remains available under low RAM.
        started = time.perf_counter()
        index.check(index.lib.blk_matches_run(self.handle, pointer(points), cap, int(reset),
            pointer(xyz), pointer(normals), pointer(self.distances), pointer(self.pairs),
            pointer(self.ties), pointer(self.statistics)))
        index.statistics['tree_query_s'] += time.perf_counter()-started
        valid, queried, reused, absent, ties = map(int, self.statistics)
        index.statistics['queried_tile_points'] += queried
        if ties:
            rows = np.flatnonzero(self.ties)
            d, ids = index.reference_query(points, cap, rows)
            d, ids = np.ascontiguousarray(d[rows]), np.ascontiguousarray(ids[rows])
            index.check(index.lib.blk_matches_patch_ties(self.handle, pointer(rows), pointer(ids), pointer(d),
                len(rows), pointer(xyz), pointer(normals), pointer(self.distances)))
        pairs = self.pairs[:valid]
        # Preserve NumPy's reduction order and convergence metrics exactly.
        rmse = float(np.sqrt(np.mean(self.distances[pairs[:,0]]**2))) if valid else 0.
        return pairs, valid/self.count if self.count else 0., rmse, dict(
            queried_points=queried+ties, exact_correspondences_reused=reused,
            absent_correspondences_reused=absent, tie_queries=ties)


def nativeIndexClass(base):
    # Lazy construction avoids a circular import of the reference implementation.
    class NativeTiledMapIndex(base):
        backend = 'native-tiled-map-v1'

        def __init__(self, matcher):
            super().__init__(matcher)
            self.lib = loadLibrary()
            self.fingerprint = hashlib.sha256(libraryPath().read_bytes()).hexdigest()
            self.handle = self.lib.blk_index_create(pointer(self.coordinates), pointer(self.offsets),
                                                     len(self.keys), self.tile_m, self.workers)
            if not self.handle:
                raise MemoryError(self.lib.blk_correspondence_error().decode())
            self._release = weakref.finalize(self, self.lib.blk_index_destroy, self.handle)
            self.native_tiles = {}  # Own every array referenced by C++ until eviction.
            self.tile_sizes = {}
            self.native_bytes = 0
            self.native_plan = None
            self.inplace_correspondences = (hasattr(self.lib, 'blk_correspondence_update')
                                           and hasattr(self.lib, 'blk_correspondence_pack'))
            self.fused_correspondences = all(hasattr(self.lib, name) for name in (
                'blk_matches_create','blk_matches_destroy','blk_matches_run','blk_matches_patch_ties'))
            self.match_halo = self.lib.blk_matches_search_halo() if hasattr(self.lib,'blk_matches_search_halo') else 0.
            self.match_states = weakref.WeakSet()

        def new_match_state(self, count):
            state = NativeMatches(self, count)
            self.match_states.add(state)
            return state

        def reference_query(self, points, cap, rows):
            result = super().query(points, 1, cap, selected=rows)
            self.trees.clear(); self.normal_maps.clear(); self.cache_bytes = 0
            return result

        def check(self, result):
            if result:
                raise RuntimeError(self.lib.blk_correspondence_error().decode())

        def certify(self, points, previous, cap, distances, indices, certified, missing):
            reference, candidates, candidate_ids, second = previous
            self.lib.blk_certify(self.handle, pointer(points), pointer(reference), pointer(candidates),
                                pointer(candidate_ids), pointer(second), len(points), cap,
                                pointer(distances), pointer(indices), pointer(certified), pointer(missing))

        def update_correspondences(self, points, selected, query_distances, query_ids, cap,
                                   previous, distances, indices):
            reference, candidates, candidate_ids, second = previous
            stride = query_distances.strides[0]//query_distances.itemsize
            if (query_distances.shape != query_ids.shape or query_distances.shape[1] != 2
                    or query_distances.strides[1] != 8 or query_ids.strides != query_distances.strides):
                raise ValueError('Two-neighbor query buffers with matching strides required')
            self.lib.blk_correspondence_update(self.handle, pointer(points), pointer(selected), len(selected),
                pointer(query_distances), pointer(query_ids), stride, cap,
                pointer(reference), pointer(candidates), pointer(candidate_ids), pointer(second),
                pointer(distances), pointer(indices))

        def pack_correspondences(self, rows, xyz, candidates):
            pairs = np.empty((len(rows),2), dtype=np.int32)
            self.lib.blk_correspondence_pack(self.handle, pointer(rows), len(rows),
                                             pointer(xyz), pointer(candidates), pointer(pairs))
            return pairs

        def close(self):
            if hasattr(self, '_release'):
                for state in self.match_states:
                    state.close()
                self.match_states.clear()
                self._release()
                self.native_tiles.clear()
                self.native_bytes = 0
                self.native_plan = None
            super().close()

        def _native_plan(self, points, cap):
            if not (hasattr(self.lib, 'blk_index_plan') and hasattr(self.lib, 'blk_plan_reusable')):
                return self._plan(points, cap)
            previous = self.native_plan
            if previous is not None and len(previous[0]) == len(points) and cap <= previous[1]:
                reference, radius, tiles = previous
                if self.lib.blk_plan_reusable(self.handle, pointer(points), pointer(reference),
                                              len(points), cap, radius):
                    self.statistics['plan_reuses'] += 1
                    return tiles
            radius = cap+.2
            needed = np.empty(len(self.keys), dtype=np.uint8)
            self.check(self.lib.blk_index_plan(self.handle, pointer(points), len(points), radius, pointer(needed)))
            tiles = np.flatnonzero(needed)
            self.native_plan = points.copy(), radius, tiles
            self.statistics['plan_builds'] += 1
            return tiles

        def _clear_native(self):
            for tile in list(self.native_tiles):
                self.lib.blk_index_remove(self.handle, tile)
                self.statistics['tree_evictions'] += 1
            self.native_tiles.clear()
            self.native_bytes = 0

        def _native_prepare(self, tiles, normals=False):
            needed = set(map(int, tiles))
            # Include point mappings, tree nodes/permutation and optional normals.
            limit = min(self.cache_limit, cacheBudget(.2)) - self.cache_bytes
            estimate = sum(self.tile_sizes.get(t, int(self.offsets[t+1]-self.offsets[t])*80)
                           for t in needed)
            if estimate > limit:
                return False  # Split queries before evicting useful native trees.
            additional = sum(self.tile_sizes.get(t, int(self.offsets[t+1]-self.offsets[t])*80)
                             for t in needed if t not in self.native_tiles)
            for tile in list(self.native_tiles):
                if self.native_bytes+additional > limit and tile not in needed:
                    self.lib.blk_index_remove(self.handle, tile)
                    self.native_bytes -= self.native_tiles.pop(tile)[2]
                    self.statistics['tree_evictions'] += 1
            missing = sorted(needed-set(self.native_tiles))
            if len(missing) > 1 and hasattr(self.lib, 'blk_index_add_batch'):
                started = time.perf_counter()
                # Owned buffers close each input immediately. Retaining one
                # memmap per tile exhausts OS file handles on long sequences.
                # The existing byte budget already includes these coordinates.
                buffers = [np.fromfile(self.matcher.folder/(self.keys[t]+'.bin'),
                                       dtype='<f4').reshape(-1,3) for t in missing]
                tiles = np.asarray(missing,dtype=np.int64)
                addresses = np.asarray([p.ctypes.data for p in buffers],dtype=np.uintp)
                self.check(self.lib.blk_index_add_batch(self.handle,pointer(tiles),pointer(addresses),len(tiles)))
                for tile,raw in zip(missing,buffers):
                    size = int(self.lib.blk_index_bytes(self.handle,tile)) if hasattr(self.lib,'blk_index_bytes') else len(raw)*80
                    self.tile_sizes[tile] = size
                    self.native_tiles[tile] = [raw,None,size]
                    self.native_bytes += size
                self.statistics['tree_builds'] += len(missing)
                self.statistics['tree_preparation_s'] += time.perf_counter()-started
            for tile in sorted(needed):
                if tile not in self.native_tiles:
                    started = time.perf_counter()
                    raw = np.fromfile(self.matcher.folder/(self.keys[tile]+'.bin'), dtype='<f4').reshape(-1, 3)
                    self.check(self.lib.blk_index_add(self.handle, tile, pointer(raw)))
                    size = len(raw)*80
                    if hasattr(self.lib, 'blk_index_bytes'):
                        size = int(self.lib.blk_index_bytes(self.handle, tile))
                    self.tile_sizes[tile] = size
                    self.native_tiles[tile] = [raw, None, size]
                    self.native_bytes += size
                    self.statistics['tree_builds'] += 1
                    self.statistics['tree_preparation_s'] += time.perf_counter()-started
                elif tile not in missing:
                    self.statistics['tree_hits'] += 1
                    self.native_tiles[tile] = self.native_tiles.pop(tile)
                if normals and self.native_tiles[tile][1] is None:
                    started = time.perf_counter()
                    directions = np.ascontiguousarray(self.matcher.tile_normals(self.keys[tile]), dtype=np.float64)
                    self.native_tiles[tile][1] = directions
                    self.lib.blk_index_normals(self.handle, tile, pointer(directions))
                    self.statistics['normal_preparation_s'] += time.perf_counter()-started
            return True

        def _stream_query(self, points, k, cap, selected):
            """Keep dense submaps in C++ when their whole tile plan exceeds RAM.

            Split query rows, never map points or search radii. Each batch loads
            every tile its original exact query can visit. Source order is
            restored before the unchanged Open3D reduction.
            """
            distances = np.full((len(points), k+1), np.inf)
            ids = np.full((len(points), k+1), self.n, dtype=np.int64)
            rows = np.arange(len(points)) if selected is None else np.asarray(selected)
            if not len(rows):
                return distances, ids
            cells = np.floor(points[rows]/self.tile_m)
            rows = rows[np.lexsort((cells[:, 2], cells[:, 1], cells[:, 0]))]
            saved_plan = self.query_plan
            # Do not let a previous reference fallback consume the native budget.
            self.trees.clear(); self.normal_maps.clear(); self.cache_bytes = 0
            def visit(rows):
                query = np.ascontiguousarray(points[rows])
                started = time.perf_counter()
                plan = self._tile_queries(query, cap)
                self.statistics['query_plan_s'] += time.perf_counter()-started
                if self._native_prepare(plan):
                    d = np.empty((len(rows), k+1)); i = np.empty((len(rows), k+1), dtype=np.int64)
                    started = time.perf_counter()
                    self.check(self.lib.blk_index_query(self.handle, pointer(query), len(rows), k+1, cap,
                                                       None, pointer(d), pointer(i)))
                    self.statistics['tree_query_s'] += time.perf_counter()-started
                    self.statistics['queried_tile_points'] += len(rows)
                    distances[rows], ids[rows] = d, i
                elif len(rows) > 4096 and len(plan) > 1:
                    middle = len(rows)//2
                    visit(rows[:middle]); visit(rows[middle:])
                else:
                    # A single tile can exceed the budget. Preserve the exact
                    # reference path for that batch, without retaining two trees.
                    self._clear_native()
                    d, i = super(NativeTiledMapIndex, self).query(query, k+1, cap)
                    distances[rows], ids[rows] = d, i
                    self.trees.clear(); self.normal_maps.clear(); self.cache_bytes = 0
            try:
                visit(rows)
            finally:
                self.query_plan = saved_plan
            return distances, ids

        def query(self, points, k=1, distance_upper_bound=np.inf, workers=None, selected=None):
            points = np.ascontiguousarray(points, dtype=np.float64)
            cap = float(distance_upper_bound)
            if points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all():
                raise ValueError('Finite Nx3 query points are required')
            if not isinstance(k, int) or k < 1 or cap <= 0 or np.isnan(cap):
                raise ValueError('Positive neighbor count and search radius are required')
            if not len(points) or not self.n or not np.isfinite(cap) or cap >= 2*self.tile_m:
                if len(points) and self.n:
                    self._clear_native()
                return super().query(points, k, cap, workers, selected)
            started = time.perf_counter()
            plan = self._native_plan(points, cap)
            self.statistics['query_plan_s'] += time.perf_counter()-started
            streamed = not self._native_prepare(plan)
            # These arrays belong to this query result; callers may retain them.
            # One extra neighbor detects ties crossing the requested boundary.
            if streamed:
                distances, ids = self._stream_query(points, k, cap, selected)
            else:
                distances = np.empty((len(points), k+1))
                ids = np.empty((len(points), k+1), dtype=np.int64)
                mask = None
                if selected is not None:
                    mask = np.zeros(len(points), dtype=np.uint8)
                    mask[selected] = 1
                started = time.perf_counter()
                self.check(self.lib.blk_index_query(self.handle, pointer(points), len(points), k+1, cap,
                                                    pointer(mask), pointer(distances), pointer(ids)))
                self.statistics['tree_query_s'] += time.perf_counter()-started
                self.statistics['queried_tile_points'] += len(points) if mask is None else int(mask.sum())
            # Preserve SciPy's established choice for exact ties, including the
            # kth-neighbor boundary. Ties are uncommon in real floating scans.
            tied = np.any(np.isfinite(distances[:, 1:]) & (distances[:, 1:] == distances[:, :-1]), axis=1)
            if tied.any():
                rows = np.flatnonzero(tied)
                d, i = super().query(points, k, cap, workers, rows)
                distances[rows, :k], ids[rows, :k] = d[rows].reshape(-1, k), i[rows].reshape(-1, k)
                # Reference tie queries must not double the retained map budget.
                self.trees.clear(); self.normal_maps.clear(); self.cache_bytes = 0
            return (distances[:, 0], ids[:, 0]) if k == 1 else (distances[:, :k], ids[:, :k])

        def gather_into(self, indices, xyz, normals=None):
            ids = np.ascontiguousarray(indices, dtype=np.int64)
            if np.any((ids < 0) | (ids > self.n)):
                raise ValueError('Invalid map neighbor index')
            valid = ids < self.n
            if hasattr(self.lib, 'blk_index_gather_plan'):
                needed = np.empty(len(self.keys), dtype=np.uint8)
                self.check(self.lib.blk_index_gather_plan(self.handle, pointer(ids), ids.size, pointer(needed)))
                tiles = np.flatnonzero(needed)
            else:
                tiles = np.unique(np.searchsorted(self.offsets[1:], ids[valid], side='right'))
            if not self._native_prepare(tiles, normals is not None):
                self._clear_native()
                values = super().gather(ids[valid], normals=normals is not None)
                if normals is None:
                    xyz[valid] = values
                else:
                    xyz[valid], normals[valid] = values
                return
            if xyz.dtype != np.float64 or not xyz.flags.c_contiguous or xyz.shape != ids.shape+(3,):
                raise ValueError('Contiguous float64 XYZ output buffer required')
            if normals is not None and (normals.dtype != np.float64 or not normals.flags.c_contiguous or normals.shape != xyz.shape):
                raise ValueError('Contiguous float64 normal output buffer required')
            started = time.perf_counter()
            self.check(self.lib.blk_index_gather(self.handle, pointer(ids), ids.size, pointer(xyz), pointer(normals)))
            self.statistics['gather_s'] += time.perf_counter()-started

        def gather(self, indices, normals=False):
            ids = np.asarray(indices, dtype=np.int64)
            if np.any((ids < 0) | (ids >= self.n)):
                raise ValueError('Cannot gather missing map neighbors')
            xyz = np.empty(ids.shape+(3,))
            directions = np.empty_like(xyz) if normals else None
            self.gather_into(ids, xyz, directions)
            return (xyz, directions) if normals else xyz

    return NativeTiledMapIndex
