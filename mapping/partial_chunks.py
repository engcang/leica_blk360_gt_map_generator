"""Publish inspectable disconnected components without inventing a common map frame."""

import datetime
import hashlib
import json
import pathlib
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from automatic_connections import componentPoses
from json_io import saveJson
from chunk_placements import refreshChunkPlacements
from portable_paths import rebaseChunks
from register_scans import readPly, writePly
from ply_io import colourValues
from share_map import createShareViewer
from studio_worker import connectedGroups


def chunkKey(indices, poses, records):
    value = {"version": 1, "scans": [{"row": i + 1, "hash": records[i]["source_sha256"],
                                     "pose": np.asarray(poses[i]).tolist()} for i in indices]}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def writeText(path, text):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(text, encoding="utf-8")
    pending.replace(path)


def createChunkReview(records, edges, output, report, progress, pose_method=None):
    output.mkdir(parents=True, exist_ok=True)
    assets = output / "html_source" / "chunks"
    assets.mkdir(parents=True, exist_ok=True)
    poses, _ = componentPoses(edges, len(records))
    groups = connectedGroups(edges, len(records))
    chunks = []
    for number, rows in enumerate(groups, 1):
        indices = [row - 1 for row in rows]
        key = chunkKey(indices, poses, records)
        destination = assets / key
        destination.mkdir(exist_ok=True)
        metadata = destination / "chunk.json"
        progress(f"Preparing chunk review {number}/{len(groups)} ({len(rows)} scans)")
        if metadata.is_file() and (destination / "view.html").is_file() and (destination / "stations.png").is_file():
            chunk = rebaseChunks([json.loads(metadata.read_text(encoding="utf-8"))], destination, output)[0]
        else:
            limit = min(250000, max(1, 2000000 // len(rows)))
            points, intensities, plotted = [], [], []
            for index in indices:
                record = records[index]
                path = pathlib.Path(record["source_path"])
                before = path.stat()
                if before.st_size != record["source_bytes"] or before.st_mtime_ns != record["source_mtime_ns"]:
                    raise ValueError(f"Source changed before chunk review: {path}")
                raw = readPly(path)
                stride = max(1, int(np.ceil(len(raw) / limit)))
                xyz = np.column_stack([raw[axis][::stride] for axis in ("x", "y", "z")]).astype(float)
                xyz = xyz @ poses[index][:3, :3].T + poses[index][:3, 3]
                points.append(xyz)
                intensities.append(colourValues(raw[::stride]).copy())
                plotted.append(xyz[::max(1, len(xyz) // 20000)])
                del raw
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"Source changed during chunk review: {path}")
            xyz = np.concatenate(points)
            intensity = np.concatenate(intensities)
            scans = [{"row": i + 1, "name": records[i]["name"], "source_path": records[i]["source_path"],
                      "source_sha256": records[i]["source_sha256"], "original_points": records[i]["original_points"],
                      "transform_chunk_from_scan": poses[i].tolist()} for i in indices]
            chunk = {"key": key, "rows": rows, "anchor_row": rows[0], "points": len(xyz), "scans": scans,
                     "pose_method": "Accepted scan-pair spanning tree; no final 1 cm refinement",
                     "sample_limit_per_scan": limit, "independently_validated": False}
            # The preview PLY is temporary. Only the self-contained viewer and local poses are published.
            (destination / "view.html").unlink(missing_ok=True)
            with tempfile.TemporaryDirectory(prefix="chunk-preview-", dir=report) as temporary:
                ply = pathlib.Path(temporary) / "sampled_chunk.ply"
                writePly(ply, xyz, intensity)
                createShareViewer(ply, destination / "view.html")
            document = (destination / "view.html").read_text(encoding="utf-8")
            document = document.replace("the saved PLY retains every point", "the original scan files retain every point")
            writeText(destination / "view.html", document)
            figure, axes = plt.subplots(1, 2, figsize=(13, 5), layout="constrained")
            for local, index in enumerate(indices):
                color = plt.get_cmap("tab20")(local % 20)
                position = poses[index][:3, 3]
                for axis, dimensions in zip(axes, [(0, 1), (0, 2)]):
                    x, y = dimensions
                    sample = plotted[local]
                    axis.scatter(sample[:, x], sample[:, y], s=.3, color=color, alpha=.25, rasterized=True)
                    axis.scatter(position[x], position[y], color=color, edgecolors="black", s=40)
                    axis.annotate(f"{index + 1}: {records[index]['name']}", (position[x], position[y]),
                                  xytext=(5, 8 + 8 * (local % 2)), textcoords="offset points", fontsize=8)
                    axis.set_aspect("equal", adjustable="datalim")
                    axis.grid(alpha=.15)
            axes[0].set(xlabel="Chunk X (m)", ylabel="Chunk Y (m)", title="Top view / original registration row numbers")
            axes[1].set(xlabel="Chunk X (m)", ylabel="Chunk Z (m)", title="Side view / not a shared building frame")
            figure.savefig(destination / "stations.png", dpi=140)
            plt.close(figure)
            saveJson(metadata, rebaseChunks([chunk], output, destination)[0])
        # Prioritize the canvas when opened inside the chunk review.
        view_path = destination / "view.html"
        document = view_path.read_text(encoding="utf-8")
        if 'id="chunk-embed-layout"' not in document:
            layout = '''<style id="chunk-embed-layout">
.chunk-embedded main{height:100vh;display:flex;flex-direction:column;padding:10px}
.chunk-embedded h1,.chunk-embedded main>p,.chunk-embedded .load-controls{display:none}
.chunk-embedded #densityStatus,.chunk-embedded #error{display:block;font-size:12px}
.chunk-embedded .controls{gap:8px;margin:4px 0;font-size:12px;flex:none}
.chunk-embedded select,.chunk-embedded button{padding:6px 8px}
.chunk-embedded main>div[style]{flex:1;min-height:220px}
.chunk-embedded #cloud{height:100%;min-height:0}
</style><script>if(window.parent!==window)document.body.classList.add('chunk-embedded');</script>'''
            writeText(view_path, document.replace("</body>", layout + "</body>"))
        if pose_method is not None:
            chunk["pose_method"] = pose_method
        chunk = rebaseChunks([chunk], output, output)[0]
        chunks.append({**chunk, "number": number, "viewer": f"html_source/chunks/{key}/view.html",
                       "image": f"html_source/chunks/{key}/stations.png"})
    manifest = {"schema_version": 1, "status": "disconnected_partial_result", "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "shared_coordinate_frame": False, "final_refinement_applied": False,
                "transform_convention": "p_chunk = T_chunk_scan @ [x,y,z,1]; metres; each chunk anchored at its first row",
                "groups": groups, "chunks": chunks}
    refreshChunkPlacements(output, chunks)
    saveJson(output / "partial_chunks.json", manifest)
    from chunk_review import writeChunkReview
    writeChunkReview(output, report, progress)
    return {"groups": groups, "chunk_count": len(groups), "chunks_review_path": str(output / "chunks_review.html"),
            "chunks_manifest_path": str(output / "partial_chunks.json")}
