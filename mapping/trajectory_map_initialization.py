"""Whole-sequence proposals used only after local initialization fails."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import time

import numpy as np

from parallel_runtime import memoryWorkers
from trajectory_geometry import voxel
from trajectory_io import PoseGap, digest, rigid, save_json, transform

MAP_VOXEL_M = .3
KISS_RESOLUTIONS_M = (1., .5, .3)


def training_map(source, indices, withheld, output, workers, progress):
    """Accumulate every usable training scan, keeping independent scans out."""
    selected = [int(i) for i in indices if int(i) not in withheld]
    if not selected:
        raise ValueError('No training scans remain after withholding initialization checks')
    binding = dict(protocol='whole-sequence-training-map-v1', input=source.provenance,
                   indices=selected, withheld_indices=sorted(withheld), voxel_m=MAP_VOXEL_M)
    key = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    path, receipt = output/'initialization_map.npy', output/'initialization_map.json'
    if path.exists() and receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get('binding_sha256') == key and saved.get('points_sha256') == digest(path):
            source.verify_unchanged()
            return np.load(path, mmap_mode='r'), saved

    from native_correspondence import voxelIndices
    started = time.perf_counter()
    # Each decoder/voxel task uses one native thread; large merge operations
    # run between batches. Bound pending clouds using the available CPU/RAM.
    active = memoryWorkers(workers, min(32, len(selected)), 256 << 20)

    def read(index):
        try:
            scan = source.scan(index)
        except PoseGap as error:
            return index, None, str(error)
        points = transform(scan.points, scan.frontend_T)
        ids = voxelIndices(points, MAP_VOXEL_M, workers=1)
        cloud = (points[ids].astype(np.float32) if ids is not None
                 else voxel(points, MAP_VOXEL_M))
        return index, cloud, None

    parts, skipped, used = [], [], 0
    last_progress = 0.
    with ThreadPoolExecutor(max_workers=active) as pool:
        for start in range(0, len(selected), active):
            futures = [pool.submit(read, i) for i in selected[start:start+active]]
            # Consume in timestamp order so thread scheduling cannot change
            # which original point represents a voxel.
            for future in futures:
                index, cloud, error = future.result()
                if cloud is None:
                    skipped.append(dict(index=index, reason=error))
                else:
                    parts.append(cloud)
                    used += 1
            if len(parts) >= 32:
                parts = [voxel(np.concatenate(parts), MAP_VOXEL_M)]
            if time.monotonic()-last_progress >= 5:
                progress(f'Building whole-sequence initialization map: '
                         f'{min(start+active,len(selected))}/{len(selected)} scans')
                last_progress = time.monotonic()
    source.verify_unchanged()
    if not parts:
        raise ValueError('No usable training scans for whole-map initialization')
    points = voxel(np.concatenate(parts), MAP_VOXEL_M)
    if len(points) < 80:
        raise ValueError('Whole-sequence initialization map has fewer than 80 points')
    temporary = path.with_suffix('.npy.tmp')
    with temporary.open('wb') as stream:
        np.save(stream, points, allow_pickle=False)
    temporary.replace(path)
    saved = dict(binding_sha256=key, points_sha256=digest(path),
                 voxel_m=MAP_VOXEL_M, points=len(points), training_scans=used,
                 withheld_indices=sorted(withheld), skipped=skipped,
                 preparation_s=time.perf_counter()-started, workers=active)
    save_json(receipt, saved)
    return points, saved


def initialize_from_map(source, matcher, fine, output, indices, withheld,
                        workers, progress, validate, record):
    """Return only an independently certified transform, never a KISS pose."""
    progress('Local initialization failed; trying whole-sequence map-to-map initialization')
    try:
        points, prepared = training_map(source, indices, withheld, output, workers, progress)
    except ValueError as error:
        record(dict(origin='map-to-map', validated=False, error=str(error)))
        return None
    for resolution in KISS_RESOLUTIONS_M:
        progress(f'Whole-map KISS initialization: {resolution:g} m')
        start = time.perf_counter()
        cloud, target = voxel(points, resolution), voxel(matcher.p, resolution)
        try:
            candidate = rigid(fine.native.kiss(cloud, target, resolution, workers))
        except (ValueError, RuntimeError) as error:
            record(dict(origin='map-to-map', resolution_m=resolution, validated=False,
                        error=str(error), elapsed_s=time.perf_counter()-start))
            continue
        proposal = dict(origin='map-to-map', resolution_m=resolution,
                        source_points=len(cloud), target_points=len(target),
                        training_scans=prepared['training_scans'],
                        withheld_indices=prepared['withheld_indices'],
                        kiss_s=time.perf_counter()-start,
                        T_map_from_frontend=candidate.tolist())
        # Whole-map drift can make global ICP worse. Check the KISS proposal
        # and then its coarse refinement; both need the same local 1 cm checks.
        for refine in (False, True):
            origin = f'map-to-map-{resolution:g}m'+('-coarse' if refine else '')
            try:
                if refine:
                    candidate = rigid(matcher.icp(cloud, candidate))
                verified = validate(candidate, origin)
            except (ValueError, RuntimeError) as error:
                record(dict(proposal, refinement=refine, validated=False, error=str(error)))
                continue
            if verified is not None:
                source.verify_unchanged()
                record(dict(proposal, refinement=refine, validated=True,
                            elapsed_s=time.perf_counter()-start))
                return verified
        record(dict(proposal, validated=False, elapsed_s=time.perf_counter()-start))
    return None
