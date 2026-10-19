"""Search merged components, then verify bridges on independent original scan pairs."""

import hashlib
import json
import numpy as np
import open3d as o3d

from automatic_connections import componentPoses, selectConnections, transformError
from native_backend import align, PIPELINE_ID
from register_scans import refine, registerPair
from json_io import saveJson
from studio_worker import connectedGroups


def componentKey(indices, poses, records):
    payload = {"version": 1, "pipeline": PIPELINE_ID,
               "scans": [(i, records[i]["source_sha256"], np.asarray(poses[i]).tolist()) for i in indices]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def scanPrior(source, target, poses, chunk_transform):
    # T_targetScan_sourceScan = inverse(T_B_targetScan) * T_B_A * T_A_sourceScan.
    value = np.linalg.inv(poses[target]) @ chunk_transform @ poses[source]
    if source > target:
        return target, source, np.linalg.inv(value)
    return source, target, value


def aggregate(indices, poses, clouds, directory):
    directory.mkdir(parents=True, exist_ok=True)
    result = {}
    for level, voxel in (("coarse", .30), ("fine", .08)):
        path = directory / (level + ".xyz")
        if path.is_file():
            xyz = np.fromfile(path, dtype="<f4").reshape(-1, 3)
            cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(xyz.astype(float)))
        else:
            points = [np.asarray(clouds[i][level].points) @ poses[i][:3, :3].T + poses[i][:3, 3] for i in indices]
            cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.concatenate(points))).voxel_down_sample(voxel)
            pending = path.with_suffix(".pending")
            np.asarray(cloud.points, dtype="<f4").tofile(pending)
            pending.replace(path)
        result[level] = cloud
        result[level + "_path"] = path
    return result


def verifyBridge(first, second, poses, transform, clouds, progress):
    ranked = []
    evaluate = o3d.pipelines.registration.evaluate_registration
    for source in first:
        for target in second:
            i, j, prior = scanPrior(source, target, poses, transform)
            forward = evaluate(clouds[i]["coarse"], clouds[j]["coarse"], .30, prior).fitness
            backward = evaluate(clouds[j]["coarse"], clouds[i]["coarse"], .30, np.linalg.inv(prior)).fitness
            overlap = 2 * forward * backward / max(1e-12, forward + backward)
            if overlap >= .08:
                ranked.append((overlap, i, j, prior))
    candidates = []
    for _, i, j, prior in sorted(ranked, key=lambda item: (-item[0], item[1], item[2]))[:12]:
        progress(f"Verifying chunk bridge on original scans {i + 1}:{j + 1}")
        edge = registerPair(clouds[i], clouds[j], i, j, prior)
        translation, rotation = transformError(prior, edge["transform_target_from_source"])
        edge["chunk_prior_error_m"] = translation
        edge["chunk_prior_error_deg"] = rotation
        edge["accepted"] = bool(edge["accepted"] and translation <= .10 and rotation <= 1.)
        edge["chunk_connection"] = True
        candidates.append(edge)
    return candidates


def discoverChunkConnections(clouds, edges, records, output, progress, on_groups=None, priors=None):
    directory = output / "chunk_search"
    directory.mkdir(exist_ok=True)
    attempts = []
    while True:
        groups = [[row - 1 for row in group] for group in connectedGroups(edges, len(clouds))]
        if len(groups) <= 1:
            break
        poses, _ = componentPoses(edges, len(clouds))
        keys = [componentKey(group, poses, records) for group in groups]
        pairs = [(a, b) for a in range(len(groups)) for b in range(a + 1, len(groups))
                 if len(groups[a]) > 1 or len(groups[b]) > 1]
        pairs.sort(key=lambda pair: (min(abs(i - j) for i in groups[pair[0]] for j in groups[pair[1]]), pair))
        merged = False
        for number, (a, b) in enumerate(pairs, 1):
            progress(f"Matching merged chunks: {number}/{len(pairs)} ({len(groups)} groups remain)")
            cache = directory / (keys[a] + "_" + keys[b] + ".json")
            if cache.is_file():
                attempt = json.loads(cache.read_text(encoding="utf-8"))
            else:
                source = aggregate(groups[a], poses, clouds, directory / keys[a])
                target = aggregate(groups[b], poses, clouds, directory / keys[b])
                initial = align("kiss", source["coarse_path"], target["coarse_path"], resolution=.30)
                attempt = {"source_rows": [i + 1 for i in groups[a]], "target_rows": [i + 1 for i in groups[b]],
                           "initial": initial, "candidates": []}
                if initial["valid"] and initial["inliers"] >= 10:
                    result = refine(source, target, initial["transform_target_from_source"])
                    attempt["alignment"] = result
                    if (result["converged"] and result["harmonic_overlap_12cm"] >= .10
                            and min(result["forward_overlap_12cm"], result["backward_overlap_12cm"]) >= .05
                            and result["inlier_rmse_m"] < .08 and result["relative_tilt_deg"] < 25):
                        attempt["candidates"] = verifyBridge(groups[a], groups[b], poses,
                            np.array(result["transform_target_from_source"]), clouds, progress)
                del source, target
                saveJson(cache, attempt)
            from manual_placement import compatibleWithPlacement
            candidates = [compatibleWithPlacement(edge, priors or {}) for edge in attempt["candidates"]]
            candidates = [edge for edge in candidates if edge["accepted"]]
            # Preserve competing scan-only placements as evidence against ambiguous bridges.
            by_pair = {(edge["source"], edge["target"]): edge for edge in candidates}
            conflict = False
            for i in groups[a]:
                for j in groups[b]:
                    key = tuple(sorted((i, j)))
                    path = output / "auto_pairs" / f"{key[0]:02d}_{key[1]:02d}.json"
                    if not path.is_file():
                        continue
                    old = compatibleWithPlacement(json.loads(path.read_text(encoding="utf-8")), priors or {})
                    if not old["accepted"]:
                        continue
                    if key in by_pair:
                        distance, angle = transformError(old["transform_target_from_source"], by_pair[key]["transform_target_from_source"])
                        conflict |= distance > .10 or angle > 1.
                    else:
                        by_pair[key] = old
            selected, audit = selectConnections(edges, list(by_pair.values()), len(clouds)) if not conflict else ([], [])
            # A chunk proposal needs at least two verified original pairs, never just a merged-cloud score.
            verified = {(edge["source"], edge["target"]) for edge in candidates}
            if sum((edge["source"], edge["target"]) in verified for edge in selected) < 2:
                selected = []
            attempts.append({"cache": cache.name, "selected_pairs": len(selected), "hypotheses": audit,
                             "conflicting_scan_placement": bool(conflict)})
            if selected:
                existing = {(edge["source"], edge["target"]): edge for edge in edges}
                for edge in selected:
                    saveJson(output / "pairs" / f"{edge['source']:02d}_{edge['target']:02d}.json", edge)
                    existing[(edge["source"], edge["target"])] = edge
                edges = list(existing.values())
                merged = True
            saveJson(output / "chunk_connections.json", {"schema_version": 1,
                "groups": connectedGroups(edges, len(clouds)), "attempts": attempts,
                "minimum_original_pair_support": 2, "translation_gate_m": .10, "rotation_gate_deg": 1.})
            if merged:
                if on_groups:
                    on_groups(edges)
                break
        if not merged:
            break
    return edges
