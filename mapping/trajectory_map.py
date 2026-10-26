"""Streaming TLS map preparation and 10 cm display map reconstruction."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

import numpy as np
from scipy.spatial import cKDTree

from trajectory_geometry import ply_vertices, voxel
from trajectory_io import digest, save_json, transform, PoseGap
from parallel_runtime import memoryWorkers


def write_ply(path, points):
    path = Path(path)
    temporary = path.with_suffix('.ply.tmp')
    dtype = np.dtype([(k, '<f4') for k in 'xyz']+[('intensity', '<u2')])
    with temporary.open('wb') as f:
        header = ('ply\nformat binary_little_endian 1.0\nelement vertex '+str(len(points))+
                  '\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n')
        f.write(header.encode('ascii'))
        for start in range(0, len(points), 1_000_000):
            block = points[start:start+1_000_000]
            out = np.empty(len(block), dtype=dtype)
            for j, key in enumerate('xyz'):
                out[key] = block[:, j]
            out['intensity'] = 65535
            out.tofile(f)
    temporary.replace(path)


def prepare_map(source, cache, workers, progress):
    source, cache = Path(source).resolve(strict=True), Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    stat = [source.stat().st_size, source.stat().st_mtime_ns]
    receipt = cache/'map.json'
    if receipt.exists():
        binding = json.loads(receipt.read_text())
        if binding['source'] != str(source) or binding['source_stat'] != stat:
            raise ValueError('Map cache belongs to a different or changed TLS map; choose a new cache folder')
        if all((cache/f).is_file() for f in ('points.npy', 'normals.npy', 'normal_valid.npy', 'tls_10cm.ply')):
            return binding
    progress('Preparing TLS map at 10 cm')
    vertices = ply_vertices(source)
    parts = []
    for start in range(0, len(vertices), 1_000_000):
        part = vertices[start:start+1_000_000]
        parts.append(voxel(np.column_stack([part[k] for k in 'xyz']), .1))
        if len(parts) >= 16:
            parts = [voxel(np.concatenate(parts), .1)]
    points = voxel(np.concatenate(parts), .1)
    del parts, vertices
    if len(points) < 80:
        raise ValueError('TLS map has fewer than 80 usable points')
    progress(f'Computing TLS normals: {len(points):,} points')
    tree = cKDTree(points)
    normals, good = np.empty_like(points), np.zeros(len(points), bool)
    for start in range(0, len(points), 50_000):
        stop = min(start+50_000, len(points))
        d, ids = tree.query(points[start:stop], k=20, workers=workers)
        delta = points[ids].astype(float)
        delta -= delta.mean(axis=1)[:, None]
        eig, vec = np.linalg.eigh(np.einsum('nki,nkj->nij', delta, delta)/20)
        normals[start:stop] = vec[:, :, 0]
        good[start:stop] = (d[:, -1] < .6) & (eig[:, 1] > 1e-5) & (eig[:, 0] < .15*eig[:, 1])
    np.save(cache/'points.npy', points)
    np.save(cache/'normals.npy', normals)
    np.save(cache/'normal_valid.npy', good)
    write_ply(cache/'tls_10cm.ply', points)
    binding = dict(source=str(source), source_sha256=digest(source), source_stat=stat,
                   voxel_m=.1, frame='TLS map', protocol='studio-trajectory-v1')
    if stat != [source.stat().st_size, source.stat().st_mtime_ns]:
        raise ValueError('TLS map changed during preparation')
    save_json(receipt, binding)
    return binding


def reconstruct_maps(source, selections, output, progress, estimates=(), alignment=None, indices=None, workers=1, pose_timestamps=None):
    """All selected real scan points, with the same frontend deskew for every layer."""
    keys = list(selections)+[f'estimate_{n}' for n in range(len(estimates))]
    parts = {key: [] for key in keys}
    counts = {key: 0 for key in keys}
    indices = sorted(set().union(*(poses.keys() for poses in selections.values()))) if indices is None else indices
    input_gaps = []
    def append_scan(key, pose, points):
        # Each layer has its own accumulator; scans retain their original order.
        parts[key].append(voxel(transform(points, pose), .1))
        counts[key] += 1
        if len(parts[key]) >= 32:
            parts[key] = [voxel(np.concatenate(parts[key]), .1)]

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(keys)))) as pool:
        for n, index in enumerate(indices):
            try:
                scan = source.scan(index)
            except PoseGap as error:
                input_gaps.append(dict(scan_index=int(index), timestamp=float(source.timestamps[index]), reason=str(error)))
                continue
            poses = {key: values[index] for key, values in selections.items() if index in values}
            for key in poses:
                expected = (pose_timestamps or {}).get(key, {}).get(index)
                if expected is not None and abs(scan.timestamp-expected) > 1e-6:
                    raise ValueError(f'Scan/pose timestamp mismatch at index {index}')
            for k, estimate in enumerate(estimates):
                try:
                    poses[f'estimate_{k}'] = alignment@estimate.pose([scan.timestamp])[0]
                except PoseGap:
                    continue
            # Compaction needs extra coordinate/key/sort arrays. Limit concurrent
            # layers from live free RAM; representatives and scan order are unchanged.
            peak_points = max([len(scan.points)] + [sum(len(p) for p in parts[key])+len(scan.points)
                              for key in poses if len(parts[key]) >= 31])
            active = memoryWorkers(workers, len(poses), peak_points * 128)
            if active > 1 and len(poses) > 1:
                items = list(poses.items())
                for start in range(0, len(items), active):
                    futures = [pool.submit(append_scan, key, pose, scan.points) for key, pose in items[start:start+active]]
                    for future in futures:
                        future.result()
            else:
                for key, pose in poses.items():
                    append_scan(key, pose, scan.points)
            if n % 100 == 0:
                progress(f'Reconstructing 10 cm maps: {n+1}/{len(indices)} scans')
    products = {}
    for key, chunks in parts.items():
        if chunks:
            p = voxel(np.concatenate(chunks), .1)
            path = output/(key+'_10cm.ply')
            write_ply(path, p)
            products[key] = dict(path=str(path), points=len(p), scans=counts[key], voxel_m=.1, sha256=digest(path), bounds=[p.min(0).tolist(), p.max(0).tolist()])
    source.verify_unchanged()
    return products, input_gaps
