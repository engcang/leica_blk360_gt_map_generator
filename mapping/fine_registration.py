"""Reuse fine ICP work without reducing point density or convergence accuracy."""

import hashlib
import json
import time
import threading
from collections import OrderedDict

from parallel_runtime import configureThreads, cpuCount, availableMemory

configureThreads()

import numpy as np
import open3d as o3d

from json_io import saveJson
from point_to_plane import FINE_ENGINE, PointToPlaneTarget

o3d.utility.set_max_threads(cpuCount())

FINE_SETTINGS = {"version": 2, "engine": FINE_ENGINE, "distance_m": 0.03, "tukey_m": 0.01,
                 "relative_fitness": 1e-6, "relative_rmse": 1e-6, "iterations": 30}


def cloudFingerprint(_cloud):
    digest = hashlib.sha256()
    for values in (np.asarray(_cloud.points), np.asarray(_cloud.normals)):
        digest.update(str(values.shape).encode("ascii"))
        if values.size:
            digest.update(memoryview(values).cast("B"))
    return digest.hexdigest()


def informationFromCorrespondences(_target, _correspondences):
    """Accumulate Open3D's point-to-point information from ICP's final matches."""
    points = np.asarray(_target.points)
    pairs = np.asarray(_correspondences)
    second = np.zeros((3, 3))
    first = np.zeros(3)
    for start in range(0, len(pairs), 262144):
        selected = points[pairs[start:start + 262144, 1]]
        second += selected.T @ selected
        first += selected.sum(axis=0)
    x, y, z = first
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    information = np.zeros((6, 6))
    information[:3, :3] = np.trace(second) * np.eye(3) - second
    information[:3, 3:] = cross
    information[3:, :3] = cross.T
    information[3:, 3:] = len(pairs) * np.eye(3)
    return information


class FineTargetCache:
    """Per-registration target indices; bounded retention, safe concurrent fits."""
    def __init__(self):
        self.targets = OrderedDict()
        self.lock = threading.Lock()

    def get(self, cloud, key):
        workers = cpuCount()
        identity = (key,workers)
        with self.lock:
            if identity in self.targets:
                target,cost = self.targets.pop(identity)
                self.targets[identity] = (target,cost)
                return target
            cost = len(cloud.points)*96  # Retained cloud, index and possible tie index.
            available = availableMemory()
            budget = available*.15 if available else 0
            while self.targets and (len(self.targets) >= 2 or
                    cost+sum(item[1] for item in self.targets.values()) > budget):
                self.targets.popitem(last=False)
            target = PointToPlaneTarget(cloud,workers,native=True)
            if cost <= budget:
                self.targets[identity] = (target,cost)
            return target

    def clear(self):
        with self.lock:
            self.targets.clear()


def refinePair(_source, _target, _initial, _cache, _source_key, _target_key, target_cache=None):
    identity = {"source": _source_key, "target": _target_key,
                "initial": np.asarray(_initial).tolist(), "settings": FINE_SETTINGS,
                "open3d": o3d.__version__}
    if _cache.is_file():
        try:
            cached = json.loads(_cache.read_text(encoding="utf-8"))
            result = cached["result"]
            valid = (cached["identity"] == identity
                     and np.asarray(result["transform"]).shape == (4, 4)
                     and np.asarray(result["information"]).shape == (6, 6)
                     and np.isfinite(result["transform"]).all()
                     and np.isfinite(result["information"]).all()
                     and 0 <= result["fitness"] <= 1
                     and 0 <= result["correspondences"] <= len(_source.points)
                     and result["icp_seconds"] >= 0 and result["total_seconds"] >= 0)
            if valid:
                return result, True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    start = time.perf_counter()
    target = (target_cache.get(_target,_target_key) if target_cache is not None
              else PointToPlaneTarget(_target,cpuCount(),native=True))
    try:
        match = target.align(_source, _initial, [FINE_SETTINGS["distance_m"]], [FINE_SETTINGS["tukey_m"]],
                             iterations=FINE_SETTINGS["iterations"], relative_fitness=FINE_SETTINGS["relative_fitness"],
                             relative_rmse=FINE_SETTINGS["relative_rmse"])
    finally:
        # Keep the target index, not the previous source's per-iteration buffers.
        target._query_state.__dict__.clear()
    icp_seconds = time.perf_counter() - start
    # Keep repeated target indices: each correspondence contributes to the graph weight.
    information = informationFromCorrespondences(_target, match.correspondence_set)
    result = {"transform": match.transformation.tolist(), "fitness": match.fitness,
              "correspondences": len(match.correspondence_set), "information": information.tolist(),
              "icp_seconds": icp_seconds, "total_seconds": time.perf_counter() - start}
    _cache.parent.mkdir(parents=True, exist_ok=True)
    saveJson(_cache, {"identity": identity, "result": result})
    return result, False
