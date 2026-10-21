"""Bounded parallel scan reduction for the two-stage voxel map export."""

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import hashlib
import multiprocessing
import os
import pathlib
import tempfile

from parallel_runtime import availableMemory, cpuCount


def mergeWorkers(_counts):
    cpus = cpuCount()
    requested = int(os.environ.get("BLK_MERGE_WORKERS", cpus))
    if requested < 1:
        raise ValueError("BLK_MERGE_WORKERS must be at least 1")
    # Spawn/import costs outweigh parallelism for small exports.
    if "BLK_MERGE_WORKERS" not in os.environ and sum(_counts) < 1_000_000:
        return 1
    # Full clouds, voxel hash tables, and the native runtime are private to each worker.
    per_scan = max(_counts, default=0) * 320 + 256 * 1024**2
    memory_limit = max(1, int(availableMemory() * 0.6) // per_scan)
    return max(1, min(len(_counts), cpus, requested, memory_limit))


def sourceStamp(_path):
    stat = _path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino


def workerThreads():
    # Scan workers already run concurrently; avoid nested native thread pools.
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "1"


def reduceScan(_task):
    import numpy as np
    import open3d as o3d
    from ply_io import colourValues, readPly

    index, record, voxel, directory, expected_stamp = _task
    path = pathlib.Path(record["source_path"])
    if sourceStamp(path) != expected_stamp:
        raise ValueError(f"Source changed during merge: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != record["source_sha256"]:
        raise ValueError(f"Source changed since registration: {path}")
    raw = readPly(path)
    xyz = np.column_stack([raw[axis] for axis in "xyz"]).astype(np.float64)
    if not np.isfinite(xyz).all():
        raise ValueError(f"Non-finite source coordinates: {path}")
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz))
    del xyz
    rgb = "red" in raw.dtype.names
    values = colourValues(raw).astype(np.float64) / (255 if rgb else 65535)
    cloud.colors = o3d.utility.Vector3dVector(values if rgb else np.column_stack([values] * 3))
    del values
    cloud.transform(np.asarray(record["transform_map_from_scan"], dtype=np.float64))
    if not np.isfinite(np.asarray(cloud.points)).all():
        raise ValueError(f"Non-finite transformed coordinates: {path}")
    reduced = cloud.voxel_down_sample(voxel)
    original_count = len(raw)
    del cloud, raw
    if sourceStamp(path) != expected_stamp:
        raise ValueError(f"Source changed during merge: {path}")
    # Float64 staging preserves centroids/colours and avoids pickling large arrays.
    np.save(pathlib.Path(directory) / f"{index}-xyz.npy", np.asarray(reduced.points))
    np.save(pathlib.Path(directory) / f"{index}-colour.npy", np.asarray(reduced.colors))
    return index, original_count, len(reduced.points)


def parallelScans(_tasks, _workers):
    if _workers == 1:
        for task in _tasks:
            yield reduceScan(task)
        return
    # Spawn works on Windows and avoids forking an initialized Open3D runtime.
    with ProcessPoolExecutor(max_workers=_workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=workerThreads) as executor:
        tasks = iter(_tasks)
        pending = set()
        try:
            for _ in range(_workers):
                task = next(tasks, None)
                if task is not None:
                    pending.add(executor.submit(reduceScan, task))
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    yield future.result()
                    task = next(tasks, None)
                    if task is not None:
                        pending.add(executor.submit(reduceScan, task))
        finally:
            for future in pending:
                future.cancel()


def mergedCloud(_records, _voxel, _directory, _progress):
    import numpy as np
    import open3d as o3d
    from ply_io import layout

    if not _records or not np.isfinite(_voxel) or _voxel <= 0:
        raise ValueError("Expected scans and a positive voxel size")
    layouts, stamps = [], []
    for record in _records:
        path = pathlib.Path(record["source_path"])
        stamps.append(sourceStamp(path))
        info = layout(path)
        if record.get("original_points", info[0]) != info[0]:
            raise ValueError(f"Original scan changed since registration: {path}")
        layouts.append(info)
    rgb = layouts[0][3]
    if any(info[3] != rgb for info in layouts):
        raise ValueError("Cannot mix RGB and intensity scans in a map")
    workers = mergeWorkers([info[0] for info in layouts])
    _progress(f"Merging {len(_records)} scans with {workers} worker{'s' if workers != 1 else ''}")
    with tempfile.TemporaryDirectory(prefix=".merge-", dir=_directory) as temporary:
        directory = pathlib.Path(temporary)
        tasks = [(i, record, _voxel, temporary, stamps[i]) for i, record in enumerate(_records)]
        counts = [0] * len(_records)
        for finished, (index, original, reduced) in enumerate(parallelScans(tasks, workers), 1):
            counts[index] = reduced
            name = pathlib.Path(_records[index]["source_path"]).name
            _progress(f"Merged scan {finished}/{len(_records)}: {name} ({original:,} → {reduced:,} points)")
        # Fill one native cloud in scan order without concatenating all scan arrays.
        total = sum(counts)
        _progress(f"Combining {total:,} scan voxels")
        cloud = o3d.geometry.PointCloud()
        cloud.points = o3d.utility.Vector3dVector(np.empty((total, 3), dtype=np.float64))
        cloud.colors = o3d.utility.Vector3dVector(np.empty((total, 3), dtype=np.float64))
        for suffix, destination in (("xyz", np.asarray(cloud.points)), ("colour", np.asarray(cloud.colors))):
            start = 0
            for index, count in enumerate(counts):
                path = directory / f"{index}-{suffix}.npy"
                values = np.load(path, mmap_mode="r")
                destination[start:start + count] = values
                del values
                path.unlink()
                start += count
        del destination
        _progress("Reducing combined map to 1 cm voxels" if _voxel == 0.01 else f"Reducing combined map to {_voxel:g} m voxels")
        reduced = cloud.voxel_down_sample(_voxel)
        del cloud
        for record, stamp in zip(_records, stamps):
            path = pathlib.Path(record["source_path"])
            if sourceStamp(path) != stamp:
                raise ValueError(f"Source changed during merge: {path}")
    return reduced, rgb
