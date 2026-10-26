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
from collections import OrderedDict

import numpy as np
import open3d as o3d
import scipy
from scipy.spatial import cKDTree

from trajectory_geometry import ply_vertices, voxel, correspondence_quality
from trajectory_io import digest, save_json, transform
from trajectory_native import NativeSession
from parallel_runtime import cacheBudget, threadBudget
from point_to_plane import FINE_ENGINE, RECOVERY_DISTANCES, RECOVERY_ITERATIONS, PointToPlaneTarget


TILE_M = 8.


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
        self.target = None
        self.target_pose = None
        self.target_uses = 0
        self.target_points = 0
        self.target_digest = None
        self.result_cache = Path(result_cache) if result_cache is not None else None
        self.tile_cache = OrderedDict()
        self.cache_bytes = 0
        self.cache_limit = cacheBudget(.1)

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

    def crop(self,source,pose,padding=.85,with_normals=False):
        moved = transform(voxel(source,.08),pose)
        tiles = np.unique(np.floor(moved/TILE_M).astype(int),axis=0)
        keys = set()
        for offset in itertools.product((-1,0,1),repeat=3):
            for tile in tiles+offset:
                key = '_'.join(str(x) for x in tile)
                if key in self.index['tiles']:
                    keys.add(key)
        tree = cKDTree(moved)
        chunks, normals = [], []
        for key in sorted(keys):
            points = self._tile(key)
            dist,_ = tree.query(points,distance_upper_bound=padding,workers=self.workers)
            valid = np.isfinite(dist)
            if not valid.any():
                continue
            chunks.append(points[valid])
            if with_normals:
                normals.append(self.tile_normals(key)[valid])
        points = np.concatenate(chunks) if chunks else np.empty((0,3),dtype=np.float32)
        if not with_normals:
            return points
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points.astype(float)))
        if normals:
            cloud.normals = o3d.utility.Vector3dVector(np.concatenate(normals))
        return cloud

    def align(self,points,initial,iterations=RECOVERY_ITERATIONS):
        started = time.perf_counter()
        if len(points)<80:
            return initial.copy(),dict(converged=False,reason='insufficient_source')
        reuse = False
        if self.target_pose is not None and self.target_uses < 5:
            angle = np.arccos(np.clip((np.trace(self.target_pose[:3,:3].T@initial[:3,:3])-1)/2,-1,1))
            reuse = np.linalg.norm(initial[:3,3]-self.target_pose[:3,3])<.2 and angle<np.deg2rad(3)
        if not reuse:
            target = self.crop(points,initial,with_normals=True)
            if len(target.points)<80:
                self.target_pose = None
                return initial.copy(),dict(converged=False,reason='insufficient_fine_target',target_points=len(target.points))
            self.target = None  # Release the old normals/tree before allocating the next.
            self.target = PointToPlaneTarget(target, self.workers)
            if self.result_cache is not None:
                self.target_digest = array_digest(np.asarray(target.points), np.asarray(target.normals))
            self.target_pose = initial.copy(); self.target_uses = 0
            self.target_points = len(target.points)
        prepared = time.perf_counter()
        source = voxel(points,.01)
        cache_file = None
        if self.result_cache is not None:
            request = dict(version=1, engine=FINE_ENGINE, target=self.target_digest,
                source_and_initial=array_digest(source, initial), iterations=iterations,
                distances=list(RECOVERY_DISTANCES), relative_fitness=1e-6, relative_rmse=1e-6,
                solver_threads=1, numpy=np.__version__, scipy=scipy.__version__, open3d=o3d.__version__)
            request_key = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
            cache_file = self.result_cache/(request_key+'.json')
            try:
                cached = json.loads(cache_file.read_text())
                T = np.asarray(cached['transformation'], dtype=float)
                info = cached['fine']
                if cached['request'] == request and T.shape == (4,4) and np.isfinite(T).all() and isinstance(info['converged'], bool):
                    # Preserve target-reuse lifetime even when the solver is skipped.
                    self.target_uses += 1
                    return T,dict(info, result_reused=True, request_key=request_key,
                        cached_fitting_s=info['fitting_s'], target_reused=bool(reuse),
                        query_statistics=dict(queried_points=0, excluded_by_bound=0, unbounded_queries=0),
                        target_preparation_s=prepared-started, fitting_s=time.perf_counter()-prepared)
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Incomplete/corrupt regenerable cache entries are recomputed.
        result = self.target.align(source, initial, RECOVERY_DISTANCES,
                                   iterations=iterations)
        self.target_uses += 1
        T = result.transformation
        info = dict(engine=FINE_ENGINE, converged=bool(result.converged and np.isfinite(T).all()),
                      convergence_reason=result.convergence_reason, robust_support=result.robust_support,
                      correspondences=len(result.correspondence_set), iterations=result.iterations,
                      target_preparation_s=prepared-started, fitting_s=time.perf_counter()-prepared,
                      query_statistics=result.query_statistics,
                      target_points=self.target_points,target_reused=bool(reuse),
                      source_voxel_m=.01,target_voxel_m=.01)
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
        tree = self.target.tree
        target = np.asarray(self.target.cloud.points)
        dist, nearest = tree.query(moved, distance_upper_bound=.2, workers=self.workers)
        valid = np.isfinite(dist)
        p = moved[valid]
        centers = target[nearest[valid]]
        if len(centers) < 80:
            return dict(accepted=False, reason='insufficient_support', points=len(centers))
        d, ids = tree.query(centers, k=20, workers=self.workers)
        delta = target[ids].copy()
        delta -= delta.mean(axis=1)[:, None]
        eigen, vectors = np.linalg.eigh(np.einsum('nki,nkj->nij', delta, delta)/20)
        good = (d[:, -1] < .3) & (eigen[:, 1] > 1e-6) & (eigen[:, 0] < .15*eigen[:, 1])
        normals = vectors[good, :, 0]
        residual = np.einsum('ij,ij->i', p[good]-centers[good], normals)
        quality = correspondence_quality(p[good], normals, residual, dist, T)
        quality['validation_map_voxel_m'] = .01
        return quality
