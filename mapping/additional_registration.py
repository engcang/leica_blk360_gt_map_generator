"""Extend a saved reference map without changing any of its original scan poses."""

import pathlib
import json
import numpy as np

from result_products import loadRegistration


def loadReference(config):
    path = pathlib.Path(config["reference_poses"]).resolve(strict=True)
    if json.loads(path.read_text(encoding="utf-8")).get("status") == "disconnected_partial_result":
        from chunk_result import loadChunkReference
        reference, digest = loadChunkReference(path)
    else:
        reference, digest = loadRegistration(path)
    if digest != config["reference_sha256"]:
        raise ValueError("Reference poses changed since selection; select the reference again")
    if config.get("refine_individual_scans"):
        raise ValueError("Additional registration cannot refine the fixed reference scans")
    expected = [pathlib.Path(scan["source_path"]).resolve(strict=True) for scan in reference["scans"]]
    paths = [pathlib.Path(path).resolve(strict=True) for path in config["paths"]]
    if paths[:len(expected)] != expected or len(paths) <= len(expected) or len(set(paths)) != len(paths):
        raise ValueError("Reference sources changed or no distinct new scans were selected")
    if pathlib.Path(config["poses_path"]).resolve().parent == pathlib.Path(config["reference_poses"]).resolve().parent:
        raise ValueError("Use a separate output folder for the extended result")
    return reference


def prepareAdditional(config, records, reference):
    count = len(reference["scans"])
    for old, record in zip(reference["scans"], records):
        if old["source_sha256"] != record["source_sha256"] or old["original_points"] != record["original_points"]:
            raise ValueError(f"Original reference PLY changed: {old['source_path']}")
    reference_groups = reference.get("reference_chunks", [{"key": "reference-" + config["reference_sha256"],
                                                          "indices": list(range(count))}])
    fixed_count = len(reference_groups[0]["indices"])
    config["reference_fixed_poses"] = [scan["transform_map_from_scan"] for scan in reference["scans"][:fixed_count]]
    config["reference_coordinate_frame"] = reference["coordinate_frame"]
    if not config.get("manual_chunks"):
        guesses = config["additional_initial_guesses"]
        if len(guesses) != len(records) - count + len(reference_groups) or guesses[0] != [0,0,0,0]:
            raise ValueError("Additional placement selection changed")
        chunks = []
        for number, rows in enumerate([g["indices"] for g in reference_groups] + [[i] for i in range(count, len(records))]):
            key = reference_groups[number]["key"] if number < len(reference_groups) else "new-" + str(rows[0])
            chunks.append({"key": key, "rows": [i+1 for i in rows], "scans": [
                {"row": i+1, "name": records[i]["name"], "source_sha256": records[i]["source_sha256"],
                 "transform_chunk_from_scan": reference["scans"][i]["transform_map_from_scan"] if i < count else np.eye(4).tolist()}
                for i in rows]})
        config["manual_chunks"] = chunks
        config["chunk_initial_guesses"] = {chunk["key"]: guess for chunk, guess in zip(chunks, guesses)}
    # Resuming from regrouped chunks is allowed only if the entire reference is still rigid.
    from rigid_chunks import validateChunks
    groups, bases, _ = validateChunks(config, records)
    for original in reference_groups:
        indices = original["indices"]
        group = next(group for group in groups if indices[0] in group)
        if not set(indices).issubset(group):
            raise ValueError("Reference scans must stay in a single rigid chunk")
        first = np.asarray(reference["scans"][indices[0]]["transform_map_from_scan"])
        for i in indices:
            actual = first @ np.linalg.inv(bases[indices[0]]) @ bases[i]
            if not np.allclose(actual, reference["scans"][i]["transform_map_from_scan"], atol=1e-8, rtol=0):
                raise ValueError("Reference internal transforms changed; refusing to alter the original map")


def anchorReference(config, scan_poses):
    origin = np.linalg.inv(scan_poses[0])
    fixed = config.get("reference_fixed_poses", [])
    if fixed:
        origin = np.asarray(fixed[0]) @ origin
    final = [origin @ scan_poses[i] for i in range(len(scan_poses))]
    for i, pose in enumerate(fixed):
        if not np.allclose(final[i], pose, atol=1e-8, rtol=0):
            raise ValueError("Reference geometry changed during registration; result will not be published")
        final[i] = np.asarray(pose, dtype=float)  # Publish the exact original values.
    return origin, final
