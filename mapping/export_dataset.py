"""Export a registered map and one pose manifest without copying source scans."""

import hashlib
import json
import os
import pathlib
import tempfile
import zipfile

from result_layout import posesPath
from portable_paths import relativePath, resolvePath


def poseManifest(_registration, _paths):
    return {"schema_version": 2, "units": "metres", "source_files_included": False,
            "source_path_base": "JSON directory",
            "coordinate_frame": _registration["coordinate_frame"],
            "transform_convention": "column vector: p_map = T_map_scan @ [x, y, z, 1]; rigid SE(3), no scale",
            "independently_validated": False,
            "pipeline_id": _registration.get("pipeline_id", "legacy"),
            "registration_mode": _registration.get("registration_mode", "individual_scans"),
            "internal_scan_poses_fixed": _registration.get("internal_scan_poses_fixed", False),
            "final_refinement_applied": _registration.get("final_refinement_applied"),
            "reference": _registration.get("reference"),
            "native_backend": _registration.get("native_backend"),
            "registration_voxel_m": _registration["registration_voxel_m"],
            "scans": [{"index": record["index"], "file": path, "name": pathlib.Path(record["source_path"]).name,
                       "points": record["original_points"],
                       "sha256": record["source_sha256"], "transform_map_from_scan": record["transform_map_from_scan"]}
                      for record, path in zip(_registration["scans"], _paths)]}


def sourcePaths(_registration, _directory):
    return [relativePath(record["source_path"], _directory) for record in _registration["scans"]]


def saveLocalPoses(_registration, _report):
    from studio_worker import saveJson
    saveJson(_report / "poses.json", poseManifest(_registration, sourcePaths(_registration, _report)))


def saveRegistrationPoses(_registration, _target, _name="reference_map"):
    manifest = poseManifest(_registration, sourcePaths(_registration, _target.parent))
    manifest.update(schema_version=3, result_name=_name)
    pending = _target.with_suffix(".json.pending")
    with pending.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")
    try:
        os.link(pending, _target)
    finally:
        pending.unlink(missing_ok=True)
    return _target


def mapManifest(_registration, _metadata, _map_path):
    manifest = poseManifest(_registration, sourcePaths(_registration, _map_path.parent))
    manifest["map"] = {**_metadata, "file": _map_path.name}
    return manifest


def saveMapPoses(_registration, _metadata, _map_path, _target=None):
    target = _target or posesPath(_map_path)
    manifest = poseManifest(_registration, sourcePaths(_registration, target.parent))
    manifest["map"] = {**_metadata, "file": relativePath(_map_path, target.parent)}
    content = json.dumps(manifest, indent=2, allow_nan=False) + "\n"
    #Codex: Publish a complete JSON atomically without replacing an existing manifest.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", suffix=".pending", delete=False) as stream:
        pending = pathlib.Path(stream.name)
        try:
            stream.write(content)
        except Exception:
            stream.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        os.link(pending, target)
    finally:
        pending.unlink(missing_ok=True)
    return target


def exportDataset(_records, _destination, _progress):
    if _records.is_file():
        poses = json.loads(_records.read_text(encoding="utf-8"))
        metadata = poses["map"]
        map_path = (_records.parent / metadata["file"]).resolve(strict=True)
        manifest_name = _records.name
        for scan in poses["scans"]:
            source = resolvePath(scan["file"], _records.parent)
            scan["file"] = relativePath(source, _destination.parent)
    else:
        #Codex: Old completed jobs can still export from their original working records.
        registration = json.loads((_records / "registration.json").read_text(encoding="utf-8"))
        metadata = json.loads((_records / "map.json").read_text(encoding="utf-8"))
        map_path = (_records / metadata["file"]).resolve(strict=True)
        poses = mapManifest(registration, metadata, _destination.parent / map_path.name)
        manifest_name = posesPath(map_path).name
    if _destination.exists():
        raise FileExistsError(f"Dataset already exists: {_destination}")
    pending = _destination.with_suffix(".zip.pending")
    if pending.exists():
        #Codex: Only the unpublished archive owned by this export is disposable on retry.
        pending.unlink()
    try:
        with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            _progress(f"Exporting registered map: {map_path.name}")
            digest = hashlib.sha256()
            with map_path.open("rb") as stream, archive.open(map_path.name, "w", force_zip64=True) as output:
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != metadata["sha256"]:
                raise ValueError(f"File changed since registration: {map_path}")
            archive.writestr(manifest_name, json.dumps(poses, indent=2, allow_nan=False) + "\n")
        os.link(pending, _destination)
    finally:
        pending.unlink(missing_ok=True)
    return {"status": "completed", "stage": "Map and transform JSON ready (original scans not copied)", "output_path": str(_destination),
            "bytes": _destination.stat().st_size, "scans": len(poses["scans"])}
