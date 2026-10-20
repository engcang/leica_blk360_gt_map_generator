"""Generate optional products from published rigid poses and unchanged source PLYs."""

import json
import os
import pathlib
import re
import tempfile

import numpy as np

from share_map import createShareViewer, inspectPly


def loadRegistration(_poses):
    from register_scans import hashFile
    manifest = json.loads(_poses.read_text(encoding="utf-8"))
    if manifest.get("units") != "metres" or not manifest.get("scans"):
        raise ValueError("Expected a BLK Studio relative poses manifest in metres")
    name = manifest.get("result_name", "reference_map")
    if not isinstance(name, str) or not re.fullmatch(r"[^<>:\"/\\|?*\x00-\x1f]{1,100}", name) or name.endswith((".", " ")):
        raise ValueError("Invalid result name in poses manifest")
    scans = []
    sources = set()
    for scan in manifest["scans"]:
        pose = np.asarray(scan["transform_map_from_scan"], dtype=float)
        if (pose.shape != (4, 4) or not np.isfinite(pose).all()
                or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-8, rtol=0)
                or not np.allclose(pose[:3, :3].T @ pose[:3, :3], np.eye(3), atol=1e-6, rtol=0)
                or not np.isclose(np.linalg.det(pose[:3, :3]), 1, atol=1e-6, rtol=0)):
            raise ValueError(f"Invalid rigid transform: {scan.get('name')}")
        path = (_poses.parent / scan["file"]).resolve()
        if path in sources or not isinstance(scan["points"], int) or scan["points"] <= 0:
            raise ValueError("Duplicate source or invalid point count in poses manifest")
        if not re.fullmatch(r"[0-9a-f]{64}", scan["sha256"]):
            raise ValueError("Missing source SHA-256 in poses manifest")
        sources.add(path)
        scans.append({**scan, "source_path": str(path), "source_sha256": scan["sha256"], "original_points": scan["points"]})
    return {**manifest, "result_name": name, "scans": scans}, hashFile(_poses)


def verifySources(_registration, _progress):
    from register_scans import hashFile
    for scan in _registration["scans"]:
        path = pathlib.Path(scan["source_path"])
        _progress(f"Checking original scan: {path.name}")
        if inspectPly(path)["points"] != scan["original_points"] or hashFile(path) != scan["source_sha256"]:
            raise ValueError(f"Original scan changed since registration: {path}")


def mergeRaw(_registration, _target, _progress):
    from register_scans import hashFile, readPly
    from ply_io import layout, plyHeader
    count = sum(scan["original_points"] for scan in _registration["scans"])
    rgb = layout(pathlib.Path(_registration["scans"][0]["source_path"]))[3]
    if any(layout(pathlib.Path(scan["source_path"]))[3] != rgb for scan in _registration["scans"]):
        raise ValueError("Cannot mix RGB and intensity scans in a map")
    header = plyHeader(count, rgb)
    with _target.open("xb") as output:
        output.write(header.encode("ascii"))
        for scan in _registration["scans"]:
            path = pathlib.Path(scan["source_path"])
            initial = path.stat()
            raw = readPly(path)
            pose = np.asarray(scan["transform_map_from_scan"], dtype=float)
            _progress(f"Transforming every point: {path.name}")
            for start in range(0, len(raw), 1_000_000):
                block = raw[start:start + 1_000_000]
                xyz = np.column_stack([block[axis] for axis in ("x", "y", "z")]).astype(float)
                xyz = xyz @ pose[:3, :3].T + pose[:3, 3]
                if not np.isfinite(xyz).all() or np.abs(xyz).max() > np.finfo(np.float32).max:
                    raise ValueError(f"Non-finite transformed coordinates: {path}")
                packed = np.empty(len(block), dtype=raw.dtype)
                for axis, values in zip(("x", "y", "z"), xyz.T):
                    packed[axis] = values
                for channel in (("red", "green", "blue") if rgb else ("intensity",)):
                    packed[channel] = block[channel]
                output.write(packed.tobytes())
            del raw
            current = path.stat()
            if (initial.st_size, initial.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise ValueError(f"Source changed during merge: {path}")
    return {**inspectPly(_target), "sha256": hashFile(_target), "voxel_m": None,
            "aggregation": "Every source point transformed once; no filtering or deduplication; original colour channels unchanged; XYZ stored as float32."}


def generateProduct(_poses, _kind, _report, _progress):
    from register_scans import hashFile, merge
    from studio_worker import saveJson
    if _kind not in ("raw", "voxel", "share"):
        raise ValueError("Choose raw, voxel, or share")
    poses = _poses.resolve(strict=True)
    registration, manifest_hash = loadRegistration(poses)
    directory = poses.parent
    name = registration["result_name"]
    filename = {"raw": f"{name}_raw.ply", "voxel": f"{name}_1cm.ply", "share": f"{name}_1cm_share.html"}[_kind]
    target = directory / filename
    if target.exists():
        raise FileExistsError(f"Output already exists: {target}. Move or remove it before regenerating.")
    assets = directory / "html_source"
    assets.mkdir(exist_ok=True)
    products_path = assets / "products.json"
    products = json.loads(products_path.read_text(encoding="utf-8")) if products_path.exists() else {}
    _report.mkdir(parents=True, exist_ok=True)
    #Codex: Temporary products share the destination volume, allowing exclusive atomic publication.
    with tempfile.TemporaryDirectory(prefix=".product-", dir=directory) as temporary:
        work = pathlib.Path(temporary)
        saveJson(_report / "staging.json", {"path": str(work)})
        metadata = None
        if _kind == "raw":
            verifySources(registration, _progress)
            metadata = mergeRaw(registration, work / filename, _progress)
        else:
            cached = products.get("voxel", {})
            cached_path = directory / f"{name}_1cm.ply"
            use_cached = (_kind == "share" and cached_path.is_file()
                          and cached.get("poses_sha256") == manifest_hash
                          and hashFile(cached_path) == cached.get("sha256"))
            if use_cached:
                ply = cached_path
                metadata = cached
            else:
                verifySources(registration, _progress)
                _progress("Merging original scans into 1 cm voxels")
                merge(registration, work, 0.01, f"{name}_1cm.ply", False)
                metadata = json.loads((work / "map.json").read_text(encoding="utf-8"))
                ply = work / metadata["file"]
            if _kind == "share":
                _progress("Embedding the complete 1 cm map into a standalone HTML")
                createShareViewer(ply, work / filename, poses, _map_metadata=metadata)
                metadata = {"points": metadata["points"], "voxel_m": 0.01,
                            "sha256": hashFile(work / filename), "embedded_map_sha256": metadata["sha256"]}
        if hashFile(poses) != manifest_hash:
            raise ValueError("Poses changed during generation; no output was published")
        metadata.update(file=filename, poses_sha256=manifest_hash, bytes=(work / filename).stat().st_size)
        os.link(work / filename, target)
    products[_kind] = metadata
    saveJson(products_path, products)
    from result_page import writeProductSnapshot
    writeProductSnapshot(directory)
    result = {"status": "completed", "stage": "Product ready", "product": _kind,
              "output_path": str(target), **metadata}
    if _kind != "share":
        result["ply_path"] = str(target)
    return result
