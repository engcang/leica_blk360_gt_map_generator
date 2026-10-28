"""C++ tiled queries with caller-owned buffers and bounded immutable tile storage."""
import ctypes as ct
import hashlib
import os
from pathlib import Path
import time
import weakref

import numpy as np

from parallel_runtime import cacheBudget


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


def loadLibrary():
    lib = ct.CDLL(str(libraryPath().resolve()))
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
    if lib.blk_correspondence_abi() != 1:
        raise RuntimeError('Incompatible native correspondence library')
    return lib


def pointer(array):
    return None if array is None else array.ctypes.data


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

        def check(self, result):
            if result:
                raise RuntimeError(self.lib.blk_correspondence_error().decode())

        def certify(self, points, previous, cap, distances, indices, certified, missing):
            reference, candidates, candidate_ids, second = previous
            self.lib.blk_certify(self.handle, pointer(points), pointer(reference), pointer(candidates),
                                pointer(candidate_ids), pointer(second), len(points), cap,
                                pointer(distances), pointer(indices), pointer(certified), pointer(missing))

        def close(self):
            if hasattr(self, '_release'):
                self._release()
                self.native_tiles.clear()
                self.native_bytes = 0
            super().close()

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
            for tile in sorted(needed):
                if tile not in self.native_tiles:
                    started = time.perf_counter()
                    raw = np.memmap(self.matcher.folder/(self.keys[tile]+'.bin'), mode='r', dtype='<f4').reshape(-1, 3)
                    self.check(self.lib.blk_index_add(self.handle, tile, pointer(raw)))
                    size = len(raw)*80
                    if hasattr(self.lib, 'blk_index_bytes'):
                        size = int(self.lib.blk_index_bytes(self.handle, tile))
                    self.tile_sizes[tile] = size
                    self.native_tiles[tile] = [raw, None, size]
                    self.native_bytes += size
                    self.statistics['tree_builds'] += 1
                    self.statistics['tree_preparation_s'] += time.perf_counter()-started
                else:
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
            plan = self._plan(points, cap)
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
