import argparse
import hashlib
import json
import os
import pathlib
import time

from parallel_runtime import configureThreads, cpuCount, orderedParallel, pairWorkers

configureThreads()

import numpy as np
import open3d as o3d

from fine_registration import FineTargetCache, cloudFingerprint, refinePair
from json_io import saveJson
from native_backend import PIPELINE_ID, align, backendInfo
from registration_quality import evaluatePair, qualityDocument


def saveCache(_path, **_arrays):
    pending = _path.with_suffix(".npz.pending")
    with pending.open("wb") as stream:
        np.savez(stream, **_arrays)
    pending.replace(_path)


def hashFile(_path):
    digest = hashlib.sha256()
    with _path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


from ply_io import readPly, colourValues, plyHeader


def makeCloud(_points, _normals=None):
    cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(_points, dtype=np.float64)))
    if _normals is not None:
        cloud.normals = o3d.utility.Vector3dVector(np.asarray(_normals, dtype=np.float64))
    return cloud


def normals(_cloud, _radius):
    _cloud.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=_radius, max_nn=40))
    _cloud.orient_normals_towards_camera_location(np.zeros(3))


def prepare(_jobs, _output):
    manifest = _output / "pipeline.json"
    identity = {"id": PIPELINE_ID, "backend": backendInfo()}
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != identity:
            raise ValueError("Registration engine changed. Start a new map name; previous caches are preserved.")
    elif (_output / "cache").exists() or (_output / "registration.json").exists():
        raise ValueError("Legacy registration cache cannot be reused by this pipeline. Start a new map name.")
    else:
        saveJson(manifest, identity)
    cache = _output / "cache"
    cache.mkdir(exist_ok=True)
    records = []
    for index, job in enumerate(_jobs):
        path = pathlib.Path(job["output_path"])
        destination = cache / f"{index:02d}.npz"
        metadata_path = cache / f"{index:02d}.json"
        if destination.exists() and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata["source_path"] == str(path) and metadata["source_bytes"] == path.stat().st_size and metadata["source_mtime_ns"] == path.stat().st_mtime_ns:
                records.append(metadata)
                continue
            raise ValueError(f"Cached source changed: {path}. Use a new output directory.")
        start = time.monotonic()
        raw = readPly(path)
        xyz = np.column_stack([raw["x"], raw["y"], raw["z"]])
        if not np.isfinite(xyz).all():
            raise ValueError("Non-finite source coordinates")
        cloud = makeCloud(xyz)
        fine = cloud.voxel_down_sample(0.08)
        coarse = cloud.voxel_down_sample(0.30)
        del cloud, xyz
        normals(fine, 0.24)
        normals(coarse, 0.75)
        saveCache(destination,
                 fine=np.asarray(fine.points).astype(np.float32),
                 fine_normals=np.asarray(fine.normals).astype(np.float32),
                 coarse=np.asarray(coarse.points).astype(np.float32),
                 coarse_normals=np.asarray(coarse.normals).astype(np.float32))
        metadata = {"index": index, "name": path.name, "source_path": str(path),
                    "source_bytes": path.stat().st_size, "source_mtime_ns": path.stat().st_mtime_ns,
                    "source_sha256": hashFile(path), "original_points": len(raw),
                    "fine_points": len(fine.points), "coarse_points": len(coarse.points),
                    "z_percentiles": np.percentile(raw["z"], [0, 1, 50, 99, 100]).tolist()}
        saveJson(metadata_path, metadata)
        records.append(metadata)
        print(f"Prepared {path.name}: {len(raw):,} -> {len(fine.points):,} / {len(coarse.points):,} points ({time.monotonic() - start:.1f}s)", flush=True)
        del raw, fine, coarse
    saveJson(_output / "inputs.json", records)
    return records


def loadClouds(_output, _count):
    clouds = []
    for index in range(_count):
        with np.load(_output / "cache" / f"{index:02d}.npz") as data:
            paths = {}
            for level in ("fine", "coarse"):
                path = _output / "cache" / f"{index:02d}_{level}.xyz"
                pending = path.with_suffix(".xyz.pending")
                np.asarray(data[level], dtype="<f4").tofile(pending)
                pending.replace(path)
                paths[level + "_path"] = path
            clouds.append({"fine": makeCloud(data["fine"], data["fine_normals"]),
                           "coarse": makeCloud(data["coarse"], data["coarse_normals"]), **paths})
    return clouds


def refine(_source, _target, _initial):
    registration = o3d.pipelines.registration
    coarse = align("nano", _source["coarse_path"], _target["coarse_path"],
                   initial=np.asarray(_initial).tolist(), max_distance=0.80)
    fine = align("nano", _source["fine_path"], _target["fine_path"],
                 initial=coarse["transform_target_from_source"], max_distance=0.16)
    transform = np.array(fine["transform_target_from_source"])
    if not np.isfinite(transform).all():
        raise ValueError("Nano-GICP returned a non-finite transform")
    forward = registration.evaluate_registration(_source["fine"], _target["fine"], 0.12, transform)
    backward = registration.evaluate_registration(_target["fine"], _source["fine"], 0.12, np.linalg.inv(transform))
    harmonic = 2 * forward.fitness * backward.fitness / max(1e-12, forward.fitness + backward.fitness)
    rmse = (forward.inlier_rmse + backward.inlier_rmse) / 2
    tilt = np.degrees(np.arccos(np.clip(transform[2, 2], -1, 1)))
    return {"transform_target_from_source": transform.tolist(), "converged": bool(fine["valid"]),
            "forward_overlap_12cm": forward.fitness, "backward_overlap_12cm": backward.fitness,
            "harmonic_overlap_12cm": harmonic, "inlier_rmse_m": rmse,
            "relative_tilt_deg": float(tilt), "translation_m": float(np.linalg.norm(transform[:3, 3])),
            "score": harmonic - 2 * rmse - (0.5 if tilt > 25 else 0)}


def registerPair(_source, _target, _i, _j, _initial=None):
    registration = o3d.pipelines.registration
    initial_method = "pose_prior"
    initial_valid = True
    global_result = None
    if _initial is None:
        initial_method = "KISS-Matcher"
        global_result = align("kiss", _source["coarse_path"], _target["coarse_path"], resolution=0.30)
        _initial = global_result["transform_target_from_source"]
        initial_valid = global_result["valid"] and global_result["inliers"] >= 10
    best = refine(_source, _target, _initial)
    best["initialization"] = initial_method
    best["local_alignment"] = "Nano-GICP"
    transform = np.array(best["transform_target_from_source"])
    information = registration.get_information_matrix_from_point_clouds(_source["fine"], _target["fine"], 0.12, transform)
    return {"source": _i, "target": _j, **best,
            "accepted": bool(initial_valid and best["converged"] and best["harmonic_overlap_12cm"] >= 0.25 and best["relative_tilt_deg"] < 25 and best["inlier_rmse_m"] < 0.08),
            "information": information.tolist(), "global_result": global_result, "pipeline_id": PIPELINE_ID}


def adjacent(_clouds, _records, _output):
    directory = _output / "pairs"
    directory.mkdir(exist_ok=True)
    results = []
    for i in range(len(_clouds) - 1):
        path = directory / f"{i:02d}_{i + 1:02d}.json"
        if path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
        else:
            start = time.monotonic()
            result = registerPair(_clouds[i], _clouds[i + 1], i, i + 1)
            saveJson(path, result)
            print(f"{_records[i]['name']} -> {_records[i + 1]['name']}: overlap {result['harmonic_overlap_12cm']:.3f}, RMSE {result['inlier_rmse_m']:.4f} m, translation {result['translation_m']:.2f} m, tilt {result['relative_tilt_deg']:.2f} deg, accepted {result['accepted']} ({time.monotonic() - start:.1f}s)", flush=True)
        results.append(result)
    return results


def chainPoses(_edges, _count):
    poses = [np.eye(4)]
    for i in range(_count - 1):
        edge = next(edge for edge in _edges if edge["source"] == i and edge["target"] == i + 1)
        if not edge["accepted"]:
            raise ValueError(f"Unresolved connection {i} -> {i + 1}; do not merge disconnected scans")
        poses.append(poses[-1] @ np.linalg.inv(edge["transform_target_from_source"]))
    return poses


def edgeError(_edge, _poses):
    predicted = np.linalg.inv(_poses[_edge["target"]]) @ _poses[_edge["source"]]
    difference = np.linalg.inv(_edge["transform_target_from_source"]) @ predicted
    angle = np.degrees(np.arccos(np.clip((np.trace(difference[:3, :3]) - 1) / 2, -1, 1)))
    return float(np.linalg.norm(difference[:3, 3])), float(angle)


def seedPoses(_edges, _count):
    parents = list(range(_count))

    def root(_index):
        while parents[_index] != _index:
            _index = parents[_index]
        return _index

    tree = []
    ordered = sorted([edge for edge in _edges if edge["accepted"]],
                     key=lambda _edge: (bool(_edge.get("manual_placement")), int(_edge["target"] == _edge["source"] + 1), _edge["score"]),
                     reverse=True)
    for edge in ordered:
        i, j = edge["source"], edge["target"]
        if root(i) == root(j):
            continue
        parents[root(i)] = root(j)
        tree.append(edge)
    if len(tree) != _count - 1:
        raise ValueError("Registration graph is disconnected; additional overlap or correspondences are required")
    poses = {0: np.eye(4)}
    while len(poses) < _count:
        for edge in tree:
            i, j = edge["source"], edge["target"]
            transform = np.array(edge["transform_target_from_source"])
            if i in poses and j not in poses:
                poses[j] = poses[i] @ np.linalg.inv(transform)
            elif j in poses and i not in poses:
                poses[i] = poses[j] @ transform
    return [poses[i] for i in range(_count)], {(edge["source"], edge["target"]) for edge in tree}


def discoverLoops(_clouds, _poses, _output):
    registration = o3d.pipelines.registration
    directory = _output / "loops"
    directory.mkdir(exist_ok=True)
    edges = []
    for i in range(len(_clouds)):
        for j in range(i + 1, len(_clouds)):
            path = directory / f"{i:02d}_{j:02d}.json"
            if path.exists():
                edge = json.loads(path.read_text(encoding="utf-8"))
                translation, rotation = edgeError(edge, _poses)
                edge["accepted"] = edge.get("converged", False) and edge["harmonic_overlap_12cm"] >= 0.25 and edge["inlier_rmse_m"] < 0.08 and edge["relative_tilt_deg"] < 25 and translation < 0.10 and rotation < 1.0
                saveJson(path, edge)
                edges.append(edge)
                continue
            initial = np.linalg.inv(_poses[j]) @ _poses[i]
            source, target = _clouds[i], _clouds[j]
            forward = registration.evaluate_registration(source["coarse"], target["coarse"], 0.45, initial)
            backward = registration.evaluate_registration(target["coarse"], source["coarse"], 0.45, np.linalg.inv(initial))
            overlap = 2 * forward.fitness * backward.fitness / max(1e-12, forward.fitness + backward.fitness)
            if overlap < 0.20:
                continue
            edge = registerPair(source, target, i, j, initial)
            translation, rotation = edgeError(edge, _poses)
            edge["initial_disagreement_m"] = translation
            edge["initial_disagreement_deg"] = rotation
            edge["accepted"] = edge["accepted"] and translation < 0.10 and rotation < 1.0
            saveJson(path, edge)
            edges.append(edge)
            print(f"Loop {i:02d}:{j:02d}: overlap {edge['harmonic_overlap_12cm']:.3f}, correction {translation:.3f} m / {rotation:.3f} deg, accepted {edge['accepted']}", flush=True)
    return edges


def optimize(_clouds, _records, _output):
    edges = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((_output / "pairs").glob("*.json"))]
    poses, tree = seedPoses(edges, len(_records))
    saveJson(_output / "initial_poses.json", [pose.tolist() for pose in poses])
    audit = []
    for edge in edges:
        translation, rotation = edgeError(edge, poses)
        audit.append({"source": edge["source"], "target": edge["target"], "pair_accepted": edge["accepted"],
                      "translation_disagreement_m": translation, "rotation_disagreement_deg": rotation})
    saveJson(_output / "independent_pair_audit.json", audit)
    loop_edges = discoverLoops(_clouds, poses, _output)
    merged = {(edge["source"], edge["target"]): edge for edge in edges if edge["accepted"] and edgeError(edge, poses)[0] < 0.10 and edgeError(edge, poses)[1] < 1.0}
    for edge in loop_edges:
        if edge["accepted"]:
            merged[(edge["source"], edge["target"])] = edge
    registration = o3d.pipelines.registration
    graph = registration.PoseGraph()
    for pose in poses:
        graph.nodes.append(registration.PoseGraphNode(pose))
    for key, edge in merged.items():
        graph.edges.append(registration.PoseGraphEdge(edge["source"],
                                                       edge["target"],
                                                       np.array(edge["transform_target_from_source"]),
                                                       np.array(edge["information"]),
                                                       uncertain=key not in tree))
    o3d.io.write_pose_graph(str(_output / "pose_graph_initial.json"), graph)
    registration.global_optimization(graph,
                                     registration.GlobalOptimizationLevenbergMarquardt(),
                                     registration.GlobalOptimizationConvergenceCriteria(),
                                     registration.GlobalOptimizationOption(max_correspondence_distance=0.12, edge_prune_threshold=0.25, preference_loop_closure=1.0, reference_node=0))
    o3d.io.write_pose_graph(str(_output / "pose_graph_optimized.json"), graph)
    final = [np.array(node.pose) for node in graph.nodes]
    kept = {(edge.source_node_id, edge.target_node_id) for edge in graph.edges}
    constraints = []
    for key, edge in merged.items():
        before = edgeError(edge, poses)
        after = edgeError(edge, final)
        constraints.append({**edge, "tree_edge": key in tree, "retained": key in kept,
                            "before_translation_m": before[0], "before_rotation_deg": before[1],
                            "after_translation_m": after[0], "after_rotation_deg": after[1]})
    result = {"schema_version": 2, "pipeline_id": PIPELINE_ID, "native_backend": backendInfo(),
              "global_alignment": "KISS-Matcher", "local_alignment": "Nano-GICP",
              "coarse_voxel_m": 0.30, "local_voxel_m": 0.08, "status": "experimental_reference_map", "independently_validated": False,
              "coordinate_frame": pathlib.Path(_records[0]["name"]).stem + "_scanner_frame", "units": "metres",
              "transform_convention": "column vector: p_map = T_map_scan @ p_scan; no scale adjustment",
              "open3d_version": o3d.__version__, "registration_voxel_m": 0.08,
              "scans": [{**record, "transform_map_from_scan": pose.tolist()} for record, pose in zip(_records, final)],
              "constraints": constraints}
    saveJson(_output / "registration.json", result)
    print(f"Optimized {len(final)} scan poses with {len(graph.edges)} retained constraints", flush=True)
    return result


def refineGraph(_result, _output, _progress=None):
    if _result.get("pipeline_id") != PIPELINE_ID:
        raise ValueError("Run KISS-Matcher / Nano-GICP registration in a new map job before 1 cm refinement")
    registration = o3d.pipelines.registration
    poses = [np.array(record["transform_map_from_scan"]) for record in _result["scans"]]
    clouds = []
    cloud_keys = []
    for number, record in enumerate(_result["scans"]):
        if _progress:
            _progress(f"Preparing 1 cm working clouds: {number + 1}/{len(_result['scans'])} ({record['name']})")
        path = _output / "cache" / f"{record['index']:02d}_1cm.npz"
        if path.exists():
            with np.load(path) as data:
                cloud = makeCloud(data["points"], data["normals"])
        else:
            raw = readPly(pathlib.Path(record["source_path"]))
            cloud = makeCloud(np.column_stack([raw["x"], raw["y"], raw["z"]])).voxel_down_sample(0.01)
            normals(cloud, 0.04)
            # Preserve precision so first-run and reloaded clouds have identical checkpoint keys.
            saveCache(path, points=np.asarray(cloud.points), normals=np.asarray(cloud.normals))
            del raw
        clouds.append(cloud)
        cloud_keys.append(cloudFingerprint(cloud))
        print(f"Fine cloud {record['name']}: {len(cloud.points):,} points", flush=True)
    graph = registration.PoseGraph()
    for pose in poses:
        graph.nodes.append(registration.PoseGraphNode(pose))
    edges = []
    retained = [edge for edge in _result["constraints"] if edge["retained"]]
    workers = pairWorkers(clouds, len(retained))
    if _progress:
        _progress(f"Refining 1 cm connections: 0/{len(retained)}; {workers} concurrent pairs, {cpuCount()} available CPU threads")
    print(f"Fine ICP parallelism: {workers} pairs; CPU budget {cpuCount()}", flush=True)

    target_cache = FineTargetCache()

    def refineEdge(_edge):
        i, j = _edge["source"], _edge["target"]
        initial = np.linalg.inv(poses[j]) @ poses[i]
        return refinePair(clouds[i], clouds[j], initial,
                          _output / "fine_pairs" / f"{i:02d}_{j:02d}.json", cloud_keys[i], cloud_keys[j], target_cache)

    for number, (edge, (match, reused)) in enumerate(zip(retained, orderedParallel(refineEdge, retained, workers))):
        i, j = edge["source"], edge["target"]
        if _progress:
            _progress(f"Refining 1 cm connections: {number + 1}/{len(retained)} completed; {workers} concurrent pairs")
        transform = np.array(match["transform"])
        refined = {"source": i, "target": j, "transform_target_from_source": match["transform"], "tree_edge": edge["tree_edge"]}
        print(f"Fine pair {i:02d}:{j:02d}: {'cached' if reused else 'computed'}, "
              f"ICP {match['icp_seconds']:.2f} s, total {match['total_seconds']:.2f} s", flush=True)
        translation, rotation = edgeError(refined, poses)
        if translation > 0.05 or rotation > 0.5 or match["correspondences"] < 20 or match["fitness"] < 0.10:
            if edge["tree_edge"]:
                raise ValueError(f"Fine registration failed the correction/overlap gate for a required connection: {i}:{j}")
            print(f"Excluded unstable fine loop {i}:{j}", flush=True)
            continue
        information = np.array(match["information"])
        refined["information"] = information.tolist()
        refined["correction_m"] = translation
        refined["correction_deg"] = rotation
        edges.append(refined)
        graph.edges.append(registration.PoseGraphEdge(i, j, transform, information, uncertain=not edge["tree_edge"]))
        print(f"Fine pair {i:02d}:{j:02d}: correction {translation:.4f} m / {rotation:.4f} deg", flush=True)
    target_cache.clear()
    if _progress:
        _progress("Optimizing the final 1 cm pose graph")
    registration.global_optimization(graph,
                                     registration.GlobalOptimizationLevenbergMarquardt(),
                                     registration.GlobalOptimizationConvergenceCriteria(),
                                     registration.GlobalOptimizationOption(max_correspondence_distance=0.03, edge_prune_threshold=0.25, preference_loop_closure=1.0, reference_node=0))
    o3d.io.write_pose_graph(str(_output / "pose_graph_fine.json"), graph)
    final = [np.array(node.pose) for node in graph.nodes]
    kept = {(edge.source_node_id, edge.target_node_id) for edge in graph.edges}
    quality = []
    for number, edge in enumerate(edges):
        i, j = edge["source"], edge["target"]
        edge["retained"] = (i, j) in kept
        edge["after_translation_m"], edge["after_rotation_deg"] = edgeError(edge, final)
        if not edge["retained"]:
            continue
        if _progress:
            _progress(f"Checking final alignment quality: {number + 1}/{len(edges)} (rows {i + 1}:{j + 1})")
        transform = np.linalg.inv(final[j]) @ final[i]
        pair = evaluatePair(clouds[i], clouds[j], transform, i, j)
        pair.update(source_name=_result["scans"][i]["name"], target_name=_result["scans"][j]["name"])
        quality.append(pair)
    saveJson(_output / "registration_coarse.json", _result)
    _result["registration_voxel_m"] = 0.01
    _result["fine_alignment"] = "Robust point-to-plane ICP + pose graph optimization"
    _result["coarse_constraints"] = _result.pop("constraints")
    _result["constraints"] = edges
    for record, pose in zip(_result["scans"], final):
        record["transform_map_from_scan"] = pose.tolist()
    saveJson(_output / "registration.json", _result)
    from export_dataset import saveLocalPoses
    saveLocalPoses(_result, _output)
    saveJson(_output / "quality.json", qualityDocument(quality))
    print(f"Fine graph complete: {len(kept)} retained constraints", flush=True)
    return _result


def writePly(_path, _xyz, _intensity):
    rgb = np.asarray(_intensity).ndim == 2
    if rgb and np.asarray(_intensity).shape != (len(_xyz), 3):
        raise ValueError("Expected Nx3 RGB values")
    header = plyHeader(len(_xyz), rgb)
    dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4")] +
                     ([(c, "u1") for c in ("red", "green", "blue")] if rgb else [("intensity", "<u2")]))
    if _path.exists():
        raise FileExistsError(_path)
    pending = _path.with_suffix(_path.suffix + ".pending")
    with pending.open("xb") as stream:
        stream.write(header.encode("ascii"))
        for start in range(0, len(_xyz), 1000000):
            end = min(start + 1000000, len(_xyz))
            block = np.empty(end - start, dtype=dtype)
            for i, name in enumerate(("x", "y", "z")):
                block[name] = _xyz[start:end, i]
            if rgb:
                for channel, name in enumerate(("red", "green", "blue")):
                    block[name] = np.clip(np.rint(_intensity[start:end, channel]), 0, 255).astype(np.uint8)
            else:
                block["intensity"] = np.clip(np.rint(_intensity[start:end]), 0, 65535).astype(np.uint16)
            stream.write(block.tobytes())
    os.link(pending, _path)
    pending.unlink()


def merge(_result, _output, _voxel, _name=None, _viewer=True, _progress=None):
    from voxel_merge import mergedCloud
    progress = _progress or (lambda message: print(message, flush=True))
    target = _output / (_name or f"reference_map_{_voxel * 100:g}cm.ply")
    if target.exists():
        raise ValueError(f"Output already exists: {target}")
    reduced, colour_mode = mergedCloud(_result["scans"], _voxel, _output, progress)
    xyz = np.asarray(reduced.points)
    intensity = np.asarray(reduced.colors) * 255 if colour_mode else np.asarray(reduced.colors)[:, 0] * 65535
    progress(f"Writing {len(xyz):,} map points")
    writePly(target, xyz, intensity)
    progress("Finalizing map")
    metadata = {"file": target.name, "points": len(xyz), "voxel_m": _voxel,
                "minimum_xyz": xyz.min(axis=0).tolist(), "maximum_xyz": xyz.max(axis=0).tolist(),
                "sha256": hashFile(target), "independently_validated": False,
                "aggregation": "Per-scan voxel centroids followed by merged voxel centroids; colour channels averaged. Original full PLYs and rigid transforms are retained."}
    saveJson(_output / "map.json", metadata)
    if _viewer:
        from view_map import createViewer
        createViewer(_output)
    print(json.dumps(metadata), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Experimental offline registration of full BLK Studio PLY scans")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--project", type=pathlib.Path)
    inputs.add_argument("--inputs", type=pathlib.Path, help="Saved inputs.json in acquisition order")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--stage", choices=["prepare", "adjacent", "pairs", "optimize", "refine", "merge"], default="adjacent")
    parser.add_argument("--pairs", help="Extra global pairs as zero-based indices, for example 3:10,5:10")
    parser.add_argument("--map-voxel", type=float, default=0.01)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.stage in ("refine", "merge"):
        if not 0.005 <= args.map_voxel <= 0.5:
            raise ValueError("Map voxel size must be between 0.005 and 0.5 metres")
        result = json.loads((args.output / "registration.json").read_text(encoding="utf-8"))
        if args.stage == "refine":
            if result["registration_voxel_m"] != 0.08:
                raise ValueError("Run optimize before refine; this result has already been refined")
            refineGraph(result, args.output)
            from review_registration import createReview
            createReview(result, args.output)
        else:
            merge(result, args.output, args.map_voxel)
        return
    if args.inputs:
        records = json.loads(args.inputs.read_text(encoding="utf-8"))
        jobs = [{"output_path": record["source_path"]} for record in records]
    elif args.project:
        project = json.loads(args.project.read_text(encoding="utf-8"))
        jobs = [job for job in project["jobs"] if job["status"] == "completed"]
    else:
        raise ValueError("Provide --project or --inputs for scan registration")
    if len(jobs) < 2:
        raise ValueError("At least two completed scans are required")
    records = prepare(jobs, args.output)
    if args.stage == "prepare":
        return
    clouds = loadClouds(args.output, len(records))
    if args.stage == "pairs":
        if not args.pairs:
            raise ValueError("Provide --pairs i:j,i:j in acquisition-order indices")
        for item in args.pairs.split(","):
            i, j = [int(value) for value in item.split(":")]
            if not 0 <= i < j < len(records):
                raise ValueError(f"Invalid scan pair: {item}")
            path = args.output / "pairs" / f"{i:02d}_{j:02d}.json"
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                result = registerPair(clouds[i], clouds[j], i, j)
                saveJson(path, result)
                print(f"Extra pair {item}: overlap {result['harmonic_overlap_12cm']:.3f}, accepted {result['accepted']}", flush=True)
        return
    if args.stage == "optimize":
        optimize(clouds, records, args.output)
        return
    edges = adjacent(clouds, records, args.output)
    poses = chainPoses(edges, len(records))
    saveJson(args.output / "chain_poses.json", [pose.tolist() for pose in poses])
    print("All consecutive scans connected. Loop checks and global optimization are still required.", flush=True)


if __name__ == "__main__":
    main()
