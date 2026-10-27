"""Refresh chunk presentation from saved poses, without changing chunk identity."""

import argparse
import json
import math
from pathlib import Path
import re
import shutil

from review_registration import createReview
from portable_paths import resolvePath


def writeChunkReview(output, records, progress=print):
    output, records = Path(output), Path(records)
    manifest = json.loads((output / "partial_chunks.json").read_text(encoding="utf-8"))
    web = Path(__file__).resolve().parents[1] / "web"
    assets = output / "html_source"
    display = dict(manifest, chunks=[])
    for chunk in manifest["chunks"]:
        progress(f"Preparing alignment images for chunk {chunk['number']}/{len(manifest['chunks'])}")
        destination = assets / "chunks" / chunk["key"]
        view = output / chunk["viewer"]
        document = view.read_text(encoding="utf-8")
        match = re.search(r'(<script id="shared-map" type="application/json">)(.*?)(</script>)', document)
        if match:
            metadata = json.loads(match[2])
            positions = [[scan["transform_chunk_from_scan"][axis][3] for axis in range(3)] for scan in chunk["scans"]]
            minimum = [min(p[axis] for p in positions) for axis in range(3)]
            maximum = [max(p[axis] for p in positions) for axis in range(3)]
            focus = dict(target=[(a+b)/2 for a,b in zip(minimum, maximum)],
                         distance=max(10, 1.7*math.dist(minimum, maximum)))
            if metadata.get("focus") != focus:
                metadata["focus"] = focus
                encoded = json.dumps(metadata, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                pending = view.with_suffix(".html.pending")
                pending.write_text(document[:match.start(2)] + encoded + document[match.end(2):], encoding="utf-8")
                pending.replace(view)
        summary_path = destination / "html_source" / "review_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
        if summary is None or any(not (summary_path.parent / name).is_file() for name in summary["images"]):
            scans = [{"index": scan["row"] - 1, "name": scan["name"], "source_path": str(resolvePath(scan["source_path"], output)),
                      "transform_map_from_scan": scan["transform_chunk_from_scan"]} for scan in chunk["scans"]]
            summary = createReview({"scans": scans}, records, destination, _write_page=False,
                                   _station_numbers=[scan["row"] for scan in chunk["scans"]], _quality={})
        prefix = f"html_source/chunks/{chunk['key']}/html_source/"
        display["chunks"].append(dict(chunk, alignment=dict(summary,
            images=[prefix + name for name in summary["images"]])))
    shutil.copyfile(web / "result_manager.css", assets / "result_manager.css")
    shutil.copyfile(web / "chunks_review.css", assets / "chunks_review.css")
    shutil.copyfile(web / "chunks_review.js", assets / "chunks_review.js")
    payload = json.dumps(display, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    document = (web / "chunks_review.html").read_text(encoding="utf-8").replace("__CHUNKS_JSON__", payload)
    pending = output / "chunks_review.html.pending"
    pending.write_text(document, encoding="utf-8")
    pending.replace(output / "chunks_review.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("records", type=Path)
    args = parser.parse_args()
    writeChunkReview(args.output, args.records)
