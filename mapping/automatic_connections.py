"""Find cross-group overlaps and verify them with independent scan-pair agreement."""

import json

import numpy as np

from register_scans import edgeError, registerPair, saveJson, seedPoses
from studio_worker import connectedGroups


def componentPoses(_edges, _count):
    groups = [[index - 1 for index in group] for group in connectedGroups(_edges, _count)]
    poses = {}
    membership = {}
    for number, group in enumerate(groups):
        indices = {index: local for local, index in enumerate(group)}
        edges = [{**edge, "source": indices[edge["source"]], "target": indices[edge["target"]]}
                 for edge in _edges if edge["accepted"] and edge["source"] in indices and edge["target"] in indices]
        local, _ = seedPoses(edges, len(group))
        poses.update(zip(group, local))
        membership.update((index, number) for index in group)
    return poses, membership


def transformError(_first, _second):
    difference = np.linalg.inv(_first) @ _second
    angle = np.degrees(np.arccos(np.clip((np.trace(difference[:3, :3]) - 1) / 2, -1, 1)))
    return float(np.linalg.norm(difference[:3, 3])), float(angle)


def consistentClusters(_candidates, _poses):
    transforms = [_poses[edge["target"]] @ np.array(edge["transform_target_from_source"]) @ np.linalg.inv(_poses[edge["source"]])
                  for edge in _candidates]
    neighborhoods = []
    for first in transforms:
        support = []
        for index, second in enumerate(transforms):
            translation, rotation = transformError(first, second)
            if translation <= 0.10 and rotation <= 1.0:
                support.append(index)
        neighborhoods.append(support)
    #Codex: Suppress duplicate hypotheses; support must come from different scan pairs.
    clusters = []
    used = set()
    for support in sorted(neighborhoods, key=lambda _support: sum(_candidates[index]["harmonic_overlap_12cm"] for index in _support), reverse=True):
        if used.intersection(support):
            continue
        used.update(support)
        edges = [_candidates[index] for index in support]
        clusters.append({"edges": edges, "support": len(edges),
                         "score": sum(edge["harmonic_overlap_12cm"] for edge in edges)})
    return clusters


def selectConnections(_edges, _candidates, _count):
    poses, membership = componentPoses(_edges, _count)
    grouped = {}
    for edge in _candidates:
        if not edge["accepted"]:
            continue
        first, second = membership[edge["source"]], membership[edge["target"]]
        if first == second:
            continue
        #Codex: Orient all measurements consistently, including interleaved groups.
        oriented = edge
        if first > second:
            oriented = {**edge, "source": edge["target"], "target": edge["source"],
                        "transform_target_from_source": np.linalg.inv(edge["transform_target_from_source"]).tolist()}
        grouped.setdefault(tuple(sorted((first, second))), []).append(oriented)
    hypotheses = []
    audit = []
    for groups, candidates in sorted(grouped.items()):
        clusters = consistentClusters(candidates, poses)
        best = clusters[0]
        runner_up = clusters[1]["score"] if len(clusters) > 1 else 0.0
        accepted = best["support"] >= 2 and best["score"] > 1.5 * runner_up
        audit.append({"groups": list(groups), "support": best["support"], "score": best["score"],
                      "runner_up_score": runner_up, "accepted": accepted,
                      "reason": "consistent_pair_support" if accepted else "insufficient_support_or_competing_placement",
                      "pairs": [[edge["source"] + 1, edge["target"] + 1] for edge in best["edges"]]})
        if accepted:
            hypotheses.append({**best, "audit": audit[-1]})
    selected = []
    originals = {(min(edge["source"], edge["target"]), max(edge["source"], edge["target"])): edge for edge in _candidates}
    for hypothesis in sorted(hypotheses, key=lambda _item: _item["score"], reverse=True):
        current, components = componentPoses([*_edges, *selected], _count)
        candidates = [originals[tuple(sorted((edge["source"], edge["target"])))] for edge in hypothesis["edges"]]
        first = candidates[0]
        if components[first["source"]] == components[first["target"]]:
            #Codex: A third group must also close consistently with bridges already chosen.
            if any(translation > 0.10 or rotation > 1.0 for translation, rotation in (edgeError(edge, current) for edge in candidates)):
                hypothesis["audit"]["accepted"] = False
                hypothesis["audit"]["reason"] = "inconsistent_with_existing_connections"
                continue
        for edge in candidates:
            selected.append({**edge, "automatic_connection": True, "independent_pair_support": hypothesis["support"]})
    return selected, audit


def discoverConnections(_clouds, _edges, _output, _progress):
    existing = {(edge["source"], edge["target"]): edge for edge in _edges}
    for path in (_output / "pairs").glob("*.json"):
        edge = json.loads(path.read_text(encoding="utf-8"))
        existing[(edge["source"], edge["target"])] = edge
    edges = list(existing.values())
    groups = connectedGroups(edges, len(_clouds))
    if len(groups) == 1:
        return edges
    _, membership = componentPoses(edges, len(_clouds))
    pairs = sorted([(i, j) for i in range(len(_clouds)) for j in range(i + 1, len(_clouds)) if membership[i] != membership[j]],
                   key=lambda _pair: (_pair[1] - _pair[0], _pair))
    directory = _output / "auto_pairs"
    directory.mkdir(exist_ok=True)
    candidates = []
    for number, (i, j) in enumerate(pairs):
        _progress(f"Finding connections between groups: {number + 1}/{len(pairs)} (rows {i + 1}:{j + 1})")
        path = directory / f"{i:02d}_{j:02d}.json"
        if path.exists():
            edge = json.loads(path.read_text(encoding="utf-8"))
        elif (i, j) in existing:
            edge = existing[(i, j)]
            saveJson(path, edge)
        else:
            edge = registerPair(_clouds[i], _clouds[j], i, j)
            saveJson(path, edge)
        candidates.append(edge)
    selected, audit = selectConnections(edges, candidates, len(_clouds))
    #Codex: Unverified global matches stay outside pairs/ and cannot seed the pose graph.
    for edge in selected:
        saveJson(_output / "pairs" / f"{edge['source']:02d}_{edge['target']:02d}.json", edge)
        existing[(edge["source"], edge["target"])] = edge
    edges = list(existing.values())
    saveJson(_output / "automatic_connections.json", {"schema_version": 1, "groups_before": groups,
                                                     "groups_after": connectedGroups(edges, len(_clouds)),
                                                     "searched_pairs": len(candidates), "verified_pairs": len(selected),
                                                     "translation_gate_m": 0.10, "rotation_gate_deg": 1.0,
                                                     "minimum_pair_support": 2, "hypothesis_score_ratio": 1.5, "hypotheses": audit})
    _progress(f"Verified {len(selected)} automatic connections across {len(groups)} groups")
    return edges
