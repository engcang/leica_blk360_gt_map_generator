"""Use disconnected chunks without assigning them an invented shared frame."""

import json
from pathlib import Path

from json_io import saveJson
from result_products import registrationFromManifest


def loadChunkReference(path, key=None):
    from register_scans import hashFile
    document = json.loads(path.read_text(encoding="utf-8"))
    chunks = document.get("chunks", [])
    if document.get("status") != "disconnected_partial_result" or not chunks:
        raise ValueError("Select a BLK Studio partial_chunks.json")
    if len({chunk["key"] for chunk in chunks}) != len(chunks):
        raise ValueError("Duplicate chunk identities")
    if key is not None:
        chunks = [chunk for chunk in chunks if chunk["key"] == key]
        if len(chunks) != 1:
            raise ValueError("Chunks changed; select the chunk again")
    scans, groups = [], []
    for chunk in chunks:
        if not chunk.get("scans"):
            raise ValueError("Empty reference chunk")
        indices = []
        for scan in chunk["scans"]:
            index = len(scans)
            indices.append(index)
            scans.append({"index": index, "file": scan["source_path"], "name": scan["name"],
                "sha256": scan["source_sha256"], "points": scan["original_points"],
                "transform_map_from_scan": scan["transform_chunk_from_scan"]})
        groups.append({"key": chunk["key"], "indices": indices, "number": chunk["number"]})
    reference = registrationFromManifest({"units": "metres", "result_name": "chunk_reference",
        "coordinate_frame": f"chunk_{groups[0]['number']}_frame",
        "registration_voxel_m": .01, "scans": scans, "reference_chunks": groups}, path)
    return reference, hashFile(path)


def finalizeChunk(config, report, progress):
    from export_dataset import saveLocalPoses, saveRegistrationPoses
    from register_scans import hashFile
    from result_products import verifySources
    from review_registration import createReview

    source = Path(config["chunk_source"])
    result, digest = loadChunkReference(source, config["chunk_key"])
    if digest != config["chunk_source_sha256"]:
        raise ValueError("Chunks changed; select the chunk again")
    target = Path(config["poses_path"])
    if target.exists():
        raise FileExistsError(f"Poses already exist: {target}")
    result.update(schema_version=2, registration_mode="selected_chunk_fixed",
        internal_scan_poses_fixed=True, final_refinement_applied=False,
        constraints=[], independently_validated=False,
        reference={"chunks_sha256": digest, "chunk_key": config["chunk_key"],
                   "poses_preserved_exactly": True})
    # Keep each accepted transform exactly as reviewed, including a nonzero anchor.
    # A 1 cm output product is a map resolution, not a claim of new ICP refinement.
    verifySources(result, progress)
    saveJson(report / "registration.json", result)
    saveLocalPoses(result, report)
    progress("Creating review for the selected chunk")
    createReview(result, report, Path(config["review_directory"]))
    if hashFile(source) != digest:
        raise ValueError("Chunks changed during finalization; no poses were published")
    saveRegistrationPoses(result, target, config["name"])
    return {"status": "completed", "stage": "Selected chunk finalized; current scan poses preserved",
        "poses_path": str(target), "output_path": config["output_path"],
        "review_available": True, "review_path": str(target.parent / "result_review_and_manage.html"),
        "groups": [list(range(1, len(result["scans"]) + 1))]}
