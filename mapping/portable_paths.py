"""Paths in published JSON are relative to the file containing them."""

import copy
import os
from pathlib import Path


def resolvePath(value, directory):
    return (Path(directory) / value.replace("\\", "/")).resolve()


def relativePath(value, directory):
    try:
        return Path(os.path.relpath(value, directory)).as_posix()
    except ValueError as error:
        raise ValueError("Keep the output and source files on the same drive for relative paths") from error


def rebaseChunks(chunks, source_directory, destination_directory):
    chunks = copy.deepcopy(chunks)
    for chunk in chunks:
        for scan in chunk.get("scans", []):
            if isinstance(scan, dict) and "source_path" in scan:
                scan["source_path"] = relativePath(resolvePath(scan["source_path"], source_directory), destination_directory)
        for key in ("viewer", "image"):
            if key in chunk:
                chunk[key] = relativePath(resolvePath(chunk[key], source_directory), destination_directory)
    return chunks
