"""Register placed chunks as rigid bodies; never optimize their member scan poses."""

import functools
import hashlib
import json
import pathlib

import numpy as np
import open3d as o3d

from automatic_connections import componentPoses, transformError
from chunk_connections import aggregate, componentKey
from fine_registration import cloudFingerprint, refinePair
from json_io import saveJson
from manual_placement import placementMatrix
from native_backend import PIPELINE_ID, align, backendInfo
from register_scans import (edgeError, loadClouds, makeCloud, normals, readPly,
                            refine, saveCache, seedPoses)
from registration_quality import evaluatePair, qualityDocument
from studio_worker import connectedGroups

MODE = "rigid_chunks_v1"


def validateChunks(config, records):
    chunks = config.get("manual_chunks", [])
    guesses = config.get("chunk_initial_guesses", {})
    if not chunks or set(guesses) != {chunk["key"] for chunk in chunks}:
        raise ValueError("Chunk selection changed; reopen Arrange chunks")
    groups, bases, placed = [], {}, {}
    for number, chunk in enumerate(chunks):
        indices = []
        for scan in chunk["scans"]:
            index = int(scan["row"]) - 1
            if index in bases or not 0 <= index < len(records):
                raise ValueError("Chunk membership is duplicated or outside the selected scans")
            if records[index]["source_sha256"] != scan["source_sha256"]:
                raise ValueError("Chunk sources changed; original scan hashes no longer match")
            matrix = np.asarray(scan["transform_chunk_from_scan"], dtype=float)
            if (matrix.shape != (4, 4) or not np.isfinite(matrix).all()
                    or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-8)
                    or not np.allclose(matrix[:3, :3].T @ matrix[:3, :3], np.eye(3), atol=1e-6)
                    or not np.isclose(np.linalg.det(matrix[:3, :3]), 1., atol=1e-6)):
                raise ValueError("Chunk-local transform must be rigid SE(3)")
            bases[index] = matrix
            indices.append(index)
        if not indices:
            raise ValueError("An empty chunk cannot be registered")
        groups.append(indices)
        if guesses[chunk["key"]] is not None:
            placed[number] = placementMatrix(guesses[chunk["key"]])
    if set(bases) != set(range(len(records))):
        raise ValueError("Chunk manifest must include every input scan exactly once")
    return groups, bases, placed


def composeScans(groups, bases, chunk_poses):
    poses = {}
    for chunk, indices in enumerate(groups):
        poses.update({i: chunk_poses[chunk] @ bases[i] for i in indices})
    return poses


def optimizeChunks(edges, count, directory, stage):
    """Optimize each connected chunk component and discard inconsistent loop edges."""
    registration = o3d.pipelines.registration
    kept = [dict(edge) for edge in edges if edge["accepted"]]
    while True:
        final = {}
        rejected = set()
        groups = connectedGroups(kept, count)
        for group_number, rows in enumerate(groups):
            ids = [row - 1 for row in rows]
            local_ids = {index: i for i, index in enumerate(ids)}
            local = [{**e, "source": local_ids[e["source"]], "target": local_ids[e["target"]]}
                     for e in kept if e["source"] in local_ids and e["target"] in local_ids]
            poses, tree = seedPoses(local, len(ids))
            graph = registration.PoseGraph()
            for pose in poses:
                graph.nodes.append(registration.PoseGraphNode(pose))
            for edge in local:
                key = edge["source"], edge["target"]
                if key not in tree and (edgeError(edge, poses)[0] > .10 or edgeError(edge, poses)[1] > 1.):
                    rejected.add((ids[key[0]], ids[key[1]]))
                    continue
                graph.edges.append(registration.PoseGraphEdge(*key,
                    np.asarray(edge["transform_target_from_source"]), np.asarray(edge["information"]),
                    uncertain=key not in tree))
            if graph.edges:
                registration.global_optimization(graph,
                    registration.GlobalOptimizationLevenbergMarquardt(),
                    registration.GlobalOptimizationConvergenceCriteria(),
                    registration.GlobalOptimizationOption(max_correspondence_distance=.03 if stage == "fine" else .12,
                        edge_prune_threshold=.25, preference_loop_closure=1., reference_node=0))
            o3d.io.write_pose_graph(str(directory / f"{stage}_graph_{group_number}.json"), graph)
            result = [np.asarray(node.pose) for node in graph.nodes]
            retained = {(e.source_node_id, e.target_node_id) for e in graph.edges}
            for edge in local:
                key = edge["source"], edge["target"]
                distance, angle = edgeError(edge, result)
                if key not in retained or distance > .10 or angle > 1.:
                    rejected.add((ids[key[0]], ids[key[1]]))
            final.update(zip(ids, result))
        if not rejected:
            return final, kept
        kept = [e for e in kept if (e["source"], e["target"]) not in rejected]


def verifyFixedBridge(first, second, bases, transform, clouds):
    """Evaluate at most twelve scan pairs at fixed poses; do not run scan ICP."""
    boxes = {}
    for index in first + second:
        xyz = np.asarray(clouds[index]["coarse"].points)
        matrix = (transform @ bases[index]) if index in first else bases[index]
        xyz = xyz @ matrix[:3, :3].T + matrix[:3, 3]
        boxes[index] = (xyz.min(axis=0), xyz.max(axis=0))
    ranked = []
    for i in first:
        for j in second:
            low = np.maximum(boxes[i][0], boxes[j][0])
            high = np.minimum(boxes[i][1], boxes[j][1])
            if np.any(high + .24 < low):
                continue
            intersection = float(np.prod(np.maximum(.01, high - low + .24)))
            ranked.append((intersection, i, j))
    evaluate = o3d.pipelines.registration.evaluate_registration
    verified, audit = [], []
    for _, i, j in sorted(ranked, reverse=True)[:12]:
        prior = np.linalg.inv(bases[j]) @ transform @ bases[i]
        forward = evaluate(clouds[i]["fine"], clouds[j]["fine"], .12, prior)
        backward = evaluate(clouds[j]["fine"], clouds[i]["fine"], .12, np.linalg.inv(prior))
        overlap = 2 * forward.fitness * backward.fitness / max(1e-12, forward.fitness + backward.fitness)
        accepted = (overlap >= .25 and len(forward.correspondence_set) >= 20
                    and len(backward.correspondence_set) >= 20
                    and max(forward.inlier_rmse, backward.inlier_rmse) < .08)
        audit.append({"source": i, "target": j, "accepted": bool(accepted),
                      "harmonic_overlap_12cm": overlap, "optimization": "none; fixed-pose validation"})
        if accepted:
            verified.append((i, j))
        if len(verified) >= min(2, len(first) * len(second)):
            break
    return verified, audit


def runRigidChunks(config, records, report, progress, publish):
    groups, bases, placed = validateChunks(config, records)
    directory = report / "rigid_chunks" / MODE
    directory.mkdir(parents=True, exist_ok=True)
    clouds = loadClouds(report, len(records))
    keys = [componentKey(group, bases, records) for group in groups]
    merged = []
    for i, group in enumerate(groups):
        progress(f"Preparing rigid chunk {i + 1}/{len(groups)} ({len(group)} fixed scans)")
        merged.append(aggregate(group, bases, clouds, directory / keys[i]))

    @functools.lru_cache(maxsize=2)
    def scanFine(index):
        path = report / "cache" / f"{index:02d}_1cm.npz"
        if path.is_file():
            with np.load(path) as data:
                return makeCloud(data["points"], data["normals"])
        raw = readPly(pathlib.Path(records[index]["source_path"]))
        cloud = makeCloud(np.column_stack([raw[x] for x in ("x", "y", "z")])).voxel_down_sample(.01)
        normals(cloud, .04)
        saveCache(path, points=np.asarray(cloud.points), normals=np.asarray(cloud.normals))
        return cloud

    @functools.lru_cache(maxsize=2)
    def chunkFine(index):
        path = directory / keys[index] / "merged_1cm.npz"
        if path.is_file():
            with np.load(path) as data:
                cloud = makeCloud(data["points"], data["normals"])
        else:
            points = []
            for number, scan in enumerate(groups[index]):
                progress(f"Preparing 1 cm rigid chunk {index + 1}: scan {number + 1}/{len(groups[index])}")
                points.append(np.asarray(scanFine(scan).points) @ bases[scan][:3, :3].T + bases[scan][:3, 3])
            cloud = makeCloud(np.concatenate(points)).voxel_down_sample(.01)
            normals(cloud, .04)
            saveCache(path, points=np.asarray(cloud.points), normals=np.asarray(cloud.normals))
        return cloud, cloudFingerprint(cloud)

    pairs = [(i, j) for i in range(len(groups)) for j in range(i + 1, len(groups))]
    edges = []
    for number, (i, j) in enumerate(pairs, 1):
        progress(f"Registering rigid chunks: {number}/{len(pairs)} (chunks {i + 1}:{j + 1}; internal scans fixed)")
        initial = np.linalg.inv(placed[j]) @ placed[i] if i in placed and j in placed else None
        identity = {"mode": MODE, "source": keys[i], "target": keys[j],
                    "initial": None if initial is None else initial.tolist()}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        path = directory / (digest + ".json")
        if path.is_file():
            edges.append(json.loads(path.read_text(encoding="utf-8")))
            continue
        edge = {"source": i, "target": j, "accepted": False, "score": 0.,
                "manual_placement": initial is not None, "reason": "No verified chunk overlap"}
        valid = True
        if initial is None:
            seed = align("kiss", merged[i]["coarse_path"], merged[j]["coarse_path"], resolution=.30)
            valid = seed["valid"] and seed["inliers"] >= 10
            initial = np.asarray(seed["transform_target_from_source"])
        else:
            evaluate = o3d.pipelines.registration.evaluate_registration
            forward = evaluate(merged[i]["coarse"], merged[j]["coarse"], .8, initial).fitness
            backward = evaluate(merged[j]["coarse"], merged[i]["coarse"], .8, np.linalg.inv(initial)).fitness
            valid = 2 * forward * backward / max(1e-12, forward + backward) >= .05
        if valid:
            match = refine(merged[i], merged[j], initial)
            edge.update(match)
            correction = transformError(initial, np.asarray(match["transform_target_from_source"]))
            edge["accepted"] = bool(match["converged"] and match["harmonic_overlap_12cm"] >= .10
                and min(match["forward_overlap_12cm"], match["backward_overlap_12cm"]) >= .05
                and match["inlier_rmse_m"] < .08 and match["relative_tilt_deg"] < 25
                and (not edge["manual_placement"] or (correction[0] <= 1. and correction[1] <= 15.)))
            if edge["accepted"]:
                information = o3d.pipelines.registration.get_information_matrix_from_point_clouds(
                    merged[i]["fine"], merged[j]["fine"], .12, np.asarray(match["transform_target_from_source"]))
                edge.update(information=information.tolist(), reason="Chunk Nano-GICP accepted")
        saveJson(path, edge)
        edges.append(edge)
    poses, coarse = optimizeChunks(edges, len(groups), directory, "coarse")
    fine = []
    for number, edge in enumerate(coarse, 1):
        i, j = edge["source"], edge["target"]
        progress(f"Refining rigid chunks at 1 cm: {number}/{len(coarse)} (chunks {i + 1}:{j + 1})")
        initial = np.linalg.inv(poses[j]) @ poses[i]
        source, source_key = chunkFine(i)
        target, target_key = chunkFine(j)
        match, reused = refinePair(source, target, initial, directory / f"fine_{keys[i]}_{keys[j]}.json", source_key, target_key)
        transform = np.asarray(match["transform"])
        correction = transformError(initial, transform)
        if correction[0] > .05 or correction[1] > .5 or match["correspondences"] < 20 or match["fitness"] < .05:
            continue
        progress(f"Validating rigid chunk bridge {i + 1}:{j + 1} on at most 12 fixed scan pairs (no scan ICP)")
        verified, audit = verifyFixedBridge(groups[i], groups[j], bases, transform, clouds)
        if len(verified) < min(2, len(groups[i]) * len(groups[j])):
            continue
        fine.append({**edge, "transform_target_from_source": match["transform"],
                     "information": match["information"], "verification_pairs": verified,
                     "verification_audit": audit, "fine_cached": reused})
    chunk_poses, retained = optimizeChunks(fine, len(groups), directory, "fine")
    # PGO can adjust a bridge: validate the actual final transform, not only the ICP proposal.
    while True:
        checked = []
        for edge in retained:
            i, j = edge["source"], edge["target"]
            verified, audit = verifyFixedBridge(groups[i], groups[j], bases,
                np.linalg.inv(chunk_poses[j]) @ chunk_poses[i], clouds)
            if len(verified) >= min(2, len(groups[i]) * len(groups[j])):
                checked.append({**edge, "verification_pairs": verified, "final_verification_audit": audit})
        if len(checked) == len(retained):
            retained = checked
            break
        chunk_poses, retained = optimizeChunks(checked, len(groups), directory, "fine")
    scan_poses = composeScans(groups, bases, chunk_poses)
    audit = {"mode": MODE, "internal_scan_poses_fixed": True, "chunk_count": len(groups),
             "coarse_attempts": edges, "chunk_constraints": retained,
             "chunk_poses": [{"key": chunk["key"], "transform_component_from_chunk": chunk_poses[i].tolist()}
                             for i, chunk in enumerate(config["manual_chunks"])]}
    saveJson(directory / "result.json", audit)
    chunkFine.cache_clear()
    del merged
    # Synthetic edges only encode rigid geometry for the partial-chunk viewer.
    virtual = []
    for group in groups:
        for index in group[1:]:
            first, second = sorted((group[0], index))
            virtual.append({"source": first, "target": second, "accepted": True, "score": 1.,
                "transform_target_from_source": (np.linalg.inv(scan_poses[second]) @ scan_poses[first]).tolist()})
    for edge in retained:
        first, second = sorted((groups[edge["source"]][0], groups[edge["target"]][0]))
        virtual.append({"source": first, "target": second, "accepted": True, "score": edge["score"],
            "transform_target_from_source": (np.linalg.inv(scan_poses[second]) @ scan_poses[first]).tolist()})
    saveJson(directory / "viewer_edges.json", virtual)
    if len(connectedGroups(retained, len(groups))) > 1:
        publish(virtual)
        return None
    # Preserve the original first scan as the exported map frame.
    origin = np.linalg.inv(scan_poses[0])
    final = [origin @ scan_poses[i] for i in range(len(records))]
    membership = {scan: chunk for chunk, group in enumerate(groups) for scan in group}
    validation_pairs = set()
    for path in (report / "pairs").glob("*.json"):
        edge = json.loads(path.read_text(encoding="utf-8"))
        i, j = edge["source"], edge["target"]
        if edge["accepted"] and membership[i] == membership[j]:
            validation_pairs.add((min(i,j), max(i,j)))
    for edge in retained:
        validation_pairs.update(tuple(sorted(pair)) for pair in edge["verification_pairs"])
    constraints, quality = [], []
    for number, (i, j) in enumerate(sorted(validation_pairs), 1):
        progress(f"Evaluating fixed scan poses: {number}/{len(validation_pairs)} (rows {i + 1}:{j + 1}; no scan registration)")
        transform = np.linalg.inv(final[j]) @ final[i]
        first, second = scanFine(i), scanFine(j)
        pair = evaluatePair(first, second, transform, i, j)
        pair.update(source_name=records[i]["name"], target_name=records[j]["name"])
        quality.append(pair)
        info = o3d.pipelines.registration.get_information_matrix_from_point_clouds(first, second, .03, transform)
        constraints.append({"source": i, "target": j, "transform_target_from_source": transform.tolist(),
            "information": info.tolist(), "accepted": True, "score": 1., "retained": True,
            "tree_edge": False, "fixed_pose_evaluation": True})
    result = {"schema_version": 2, "pipeline_id": PIPELINE_ID, "native_backend": backendInfo(),
              "registration_mode": MODE, "internal_scan_poses_fixed": True,
              "global_alignment": "User chunk placements; KISS-Matcher only for unplaced chunks",
              "local_alignment": "Rigid-chunk Nano-GICP and 1 cm robust ICP; chunk pose graph",
              "registration_voxel_m": .01, "coarse_voxel_m": .30, "local_voxel_m": .08,
              "status": "experimental_reference_map", "independently_validated": False,
              "coordinate_frame": pathlib.Path(records[0]["name"]).stem + "_scanner_frame", "units": "metres",
              "transform_convention": "p_map = T_map_chunk @ T_chunk_scan @ p_scan; internal scan transforms fixed",
              "scans": [{**record, "transform_map_from_scan": pose.tolist()} for record, pose in zip(records, final)],
              "constraints": constraints, "chunk_constraints": retained,
              "chunks": [{"key": chunk["key"], "rows": [i+1 for i in groups[number]],
                          "transform_map_from_chunk": (origin @ chunk_poses[number]).tolist(),
                          "scan_local_transforms": {str(i): bases[i].tolist() for i in groups[number]}}
                         for number, chunk in enumerate(config["manual_chunks"])]}
    saveJson(report / "registration.json", result)
    saveJson(report / "quality.json", qualityDocument(quality))
    from export_dataset import saveLocalPoses
    saveLocalPoses(result, report)
    if config.get("refine_individual_scans", False):
        if len(connectedGroups(constraints, len(records))) != 1:
            raise ValueError("Optional scan refinement needs connected original scan constraints. Disable it to keep rigid chunks.")
        _, tree = seedPoses(constraints, len(records))
        for edge in constraints:
            edge["tree_edge"] = (edge["source"], edge["target"]) in tree
        scanFine.cache_clear()
        progress("Optional individual-scan refinement requested; internal chunk geometry may change")
        from register_scans import refineGraph
        result = refineGraph(result, report, progress)
        result.update(registration_mode="rigid_chunks_then_individual_scans", internal_scan_poses_fixed=False)
        result["initial_rigid_chunks"] = result.pop("chunks")
        result["initial_chunk_constraints"] = result.pop("chunk_constraints")
        saveJson(report / "registration.json", result)
        saveLocalPoses(result, report)
    return result
