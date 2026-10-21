"""Create an offline HTML viewer with optional lossless embedded PLY data."""

import argparse
import base64
import gzip
import hashlib
import html
import json
import math
import os
import pathlib
import re


def inspectPly(_path):
    from ply_io import layout
    points, _, _, rgb = layout(_path)
    return {"file": _path.name, "points": points, "bytes": _path.stat().st_size, "colour_mode": "rgb" if rgb else "intensity"}


def createShareViewer(_ply, _output, _poses=None, _embed=True, _map_metadata=None):
    source = pathlib.Path(_ply).resolve()
    output = pathlib.Path(_output).resolve()
    if output.exists():
        raise FileExistsError(output)
    metadata = inspectPly(source)
    metadata.update(chunk_bytes=4 * 1024 * 1024, embedded=_embed, focus=None)
    expected_hash = None
    if _poses is not None:
        poses = json.loads(pathlib.Path(_poses).read_text(encoding="utf-8"))
        saved_map = _map_metadata or poses.get("map")
        if saved_map is None:
            raise ValueError("Provide verified map metadata when using a poses-only manifest")
        if saved_map["points"] != metadata["points"]:
            raise ValueError("Map point count differs from the poses manifest")
        expected_hash = saved_map["sha256"]
        metadata["voxel_m"] = saved_map["voxel_m"]
        stations = [[scan["transform_map_from_scan"][axis][3] for axis in range(3)] for scan in poses["scans"]]
        if stations:
            minimum = [min(point[axis] for point in stations) for axis in range(3)]
            maximum = [max(point[axis] for point in stations) for axis in range(3)]
            metadata["focus"] = {"target": [(a + b) / 2 for a, b in zip(minimum, maximum)],
                                 "distance": max(10, 1.7 * math.dist(minimum, maximum))}
    assets = pathlib.Path(__file__).resolve().parents[1] / "web"
    document = (assets / "viewer.html").read_text(encoding="utf-8")
    document = document.replace('<link rel="stylesheet" href="viewer.css"><script src="viewer.js" defer></script>',
                                "<style>" + (assets / "viewer.css").read_text(encoding="utf-8") + "</style>")
    document = document.replace("Full-resolution point cloud</h1>", html.escape(source.name) + "</h1>")
    before, after = document.split("</body>", 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(output.suffix + ".pending")
    initial_stat = source.stat()
    digest = hashlib.sha256()
    with pending.open("xb") as destination:
        try:
            destination.write(before.encode("utf-8"))
            with source.open("rb") as stream:
                index = 0
                while block := stream.read(metadata["chunk_bytes"]):
                    digest.update(block)
                    if _embed:
                        destination.write(f'<script id="ply-block-{index}" type="application/octet-stream">'.encode("ascii"))
                        destination.write(base64.b64encode(gzip.compress(block, compresslevel=6, mtime=0)))
                        destination.write(b"</script>\n")
                    index += 1
            metadata["sha256"] = digest.hexdigest()
            final_stat = source.stat()
            if final_stat.st_size != initial_stat.st_size or final_stat.st_mtime_ns != initial_stat.st_mtime_ns:
                raise ValueError("The input PLY changed while creating the viewer")
            if expected_hash and expected_hash != metadata["sha256"]:
                raise ValueError("Map SHA-256 differs from the poses manifest")
            payload = json.dumps(metadata, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            destination.write(('<script id="shared-map" type="application/json">' + payload + "</script>\n").encode("utf-8"))
            for name in ("viewer.js", "share_viewer.js"):
                script = (assets / name).read_text(encoding="utf-8").replace("</script", "<\\/script")
                destination.write(("<script>" + script + "</script>\n").encode("utf-8"))
            license_text = (assets.parent / "LICENSE").read_text(encoding="utf-8").replace("--", "- -")
            destination.write(("<!--\n" + license_text + "-->\n").encode("utf-8"))
            destination.write(("</body>" + after).encode("utf-8"))
        except BaseException:
            destination.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        os.link(pending, output)
    finally:
        pending.unlink(missing_ok=True)
    return {**metadata, "html": str(output), "html_bytes": output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ply", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--poses", type=pathlib.Path, help="Published relative-poses JSON for checksum verification and initial framing")
    parser.add_argument("--external", action="store_true", help="Create a small viewer; recipients select a separate PLY instead of embedding it")
    args = parser.parse_args()
    print(json.dumps(createShareViewer(args.ply, args.output, args.poses, not args.external), indent=2))


if __name__ == "__main__":
    main()
