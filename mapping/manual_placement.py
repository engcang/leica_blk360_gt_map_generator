"""Validated user XYZ/yaw placements used as local registration priors, never final poses."""
import hashlib
import json
import numpy as np
import open3d as o3d
from automatic_connections import transformError
from json_io import saveJson
from register_scans import registerPair


def placementMatrix(value):
    pose = np.asarray(value, dtype=float)
    if pose.shape != (4,) or not np.isfinite(pose).all() or np.any(np.abs(pose) > 1e6):
        raise ValueError("Initial placement must contain finite X, Y, Z metres and yaw radians")
    x, y, z, yaw = pose
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, -s, 0, x], [s, c, 0, y], [0, 0, 1, z], [0, 0, 0, 1]])


def collectPriors(config, records):
    priors, membership = {}, {}
    guesses = config.get("initial_guesses", [])
    if guesses:
        if len(guesses) != len(records):
            raise ValueError("Initial placement count no longer matches the selected scans")
        for i, value in enumerate(guesses):
            if value is not None:
                priors[i], membership[i] = placementMatrix(value), i
    chunks = config.get("manual_chunks", [])
    for chunk in chunks:
        value = config.get("chunk_initial_guesses", {}).get(chunk["key"])
        if value is None:
            continue
        transform = placementMatrix(value)
        for scan in chunk["scans"]:
            i = int(scan["row"]) - 1
            if i < 0 or i >= len(records) or records[i]["source_sha256"] != scan["source_sha256"]:
                raise ValueError("Chunk placement refers to changed or different source scans")
            base = np.asarray(scan["transform_chunk_from_scan"], dtype=float)
            if base.shape != (4, 4) or not np.isfinite(base).all():
                raise ValueError("Invalid chunk-local pose")
            priors[i], membership[i] = transform @ base, chunk["key"]
    return priors, membership


def applyPlacements(config, clouds, records, edges, output, progress):
    priors, membership = collectPriors(config, records)
    if len(priors) < 2:
        return edges
    directory = output / "manual_pairs"
    directory.mkdir(exist_ok=True)
    (output / "pairs").mkdir(exist_ok=True)
    existing = {(e["source"], e["target"]): e for e in edges}
    pairs = [(i, j) for i in sorted(priors) for j in sorted(priors) if i < j and membership[i] != membership[j]]
    audit = []
    evaluate = o3d.pipelines.registration.evaluate_registration
    for number, (i, j) in enumerate(pairs, 1):
        progress(f"Refining manual placements: {number}/{len(pairs)} (rows {i + 1}:{j + 1})")
        prior = np.linalg.inv(priors[j]) @ priors[i]
        identity = {"version": 1, "sources": [records[i]["source_sha256"], records[j]["source_sha256"]],
                    "initial": prior.tolist()}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        cached = directory / f"{i:02d}_{j:02d}_{key}.json"
        if cached.exists():
            edge = json.loads(cached.read_text(encoding="utf-8"))
        else:
            forward = evaluate(clouds[i]["coarse"], clouds[j]["coarse"], .8, prior).fitness
            backward = evaluate(clouds[j]["coarse"], clouds[i]["coarse"], .8, np.linalg.inv(prior)).fitness
            overlap = 2 * forward * backward / max(1e-12, forward + backward)
            edge = {"source": i, "target": j, "accepted": False, "manual_placement": True,
                    "initial_transform": prior.tolist(), "transform_target_from_source": prior.tolist(),
                    "harmonic_overlap_12cm": 0., "score": 0., "converged": False, "reason": "No overlap near the manual placement"}
            if overlap >= .05:
                edge.update(registerPair(clouds[i], clouds[j], i, j, prior))
                distance, angle = transformError(prior, edge["transform_target_from_source"])
                edge.update(manual_placement=True, initial_correction_m=distance, initial_correction_deg=angle)
                edge["accepted"] = bool(edge["accepted"] and distance <= 1.0 and angle <= 15.0)
                edge["reason"] = "Verified manual initialization" if edge["accepted"] else "Overlap, convergence, or initial-correction check failed"
            saveJson(cached, edge)
        print(f"Manual pair {i + 1}:{j + 1}: accepted {edge['accepted']}; {edge['reason']}", flush=True)
        audit.append(edge)
        # Remove an incompatible old pair even if this manual attempt was rejected.
        existing[(i, j)] = edge
        saveJson(output / "pairs" / f"{i:02d}_{j:02d}.json", edge)
    saveJson(output / "manual_placement_report.json", {"usage": "initialization_only", "pairs": audit,
        "accepted_pairs": sum(e["accepted"] for e in audit), "max_correction_m": 1., "max_correction_deg": 15.})
    return list(existing.values())


def compatibleWithPlacement(edge, priors):
    i, j = edge["source"], edge["target"]
    if not edge["accepted"] or i not in priors or j not in priors:
        return edge
    prior = np.linalg.inv(priors[j]) @ priors[i]
    distance, angle = transformError(prior, edge["transform_target_from_source"])
    if distance > 1. or angle > 15.:
        return {**edge, "accepted": False, "reason": "Conflicts with manual placement"}
    return edge
