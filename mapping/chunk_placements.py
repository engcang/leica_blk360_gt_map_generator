"""Refresh editable chunk frames without discarding earlier user placements."""

import hashlib
import json
from pathlib import Path

from json_io import saveJson


def refreshChunkPlacements(output, chunks):
    output = Path(output)
    path = output / "chunk_initial_placements.json"
    if path.exists():
        previous = path.read_bytes()
        document = json.loads(previous)
        if document.get("chunks") == chunks:
            return
        # Frames can change after merging or optimization. Do not carry old XYZ/yaw
        # values into new frames, even when the number of chunks stays the same.
        if any(pose is not None for pose in document.get("poses", {}).values()):
            history = output / "html_source" / "placement_history"
            history.mkdir(parents=True, exist_ok=True)
            archive = history / (hashlib.sha256(previous).hexdigest() + ".json")
            if not archive.exists():
                saveJson(archive, document)
    saveJson(path, {"schema_version": 1, "chunks": chunks,
                    "poses": {chunk["key"]: None for chunk in chunks}})
