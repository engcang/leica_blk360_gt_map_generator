"""Evaluate fixed scan poses without changing registration or connection decisions."""

import argparse
import functools
import json
import os
import pathlib

os.environ.setdefault("OMP_NUM_THREADS", "8")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
from scipy.spatial import cKDTree


def distanceStats(_distances):
    if not len(_distances):
        return {"mean_m": None, "rmse_m": None, "p50_m": None, "p95_m": None}
    return {"mean_m": float(np.mean(_distances)),
            "rmse_m": float(np.sqrt(np.mean(np.square(_distances)))),
            "p50_m": float(np.median(_distances)),
            "p95_m": float(np.percentile(_distances, 95))}


def directionalMetrics(_source,
                       _target,
                       _transform,
                       _thresholds):
    source_points = np.asarray(_source.points) @ _transform[:3, :3].T + _transform[:3, 3]
    target_points = np.asarray(_target.points)
    #Codex: One exact, parallel bounded query supplies all four thresholds.
    distances, neighbors = cKDTree(target_points).query(source_points,
                                                       distance_upper_bound=max(_thresholds),
                                                       workers=min(8, os.cpu_count() or 1))
    indices = np.flatnonzero(np.isfinite(distances))
    target_indices = neighbors[indices]
    delta = source_points[indices] - target_points[target_indices]
    distances = distances[indices]
    normal_valid = np.zeros(len(indices), dtype=bool)
    plane_distances = np.zeros(len(indices))
    if _source.has_normals() and _target.has_normals():
        source_normals = np.asarray(_source.normals)[indices] @ _transform[:3, :3].T
        target_normals = np.asarray(_target.normals)[target_indices]
        source_lengths = np.linalg.norm(source_normals, axis=1)
        target_lengths = np.linalg.norm(target_normals, axis=1)
        usable = (source_lengths > 1e-12) & (target_lengths > 1e-12)
        source_normals[usable] /= source_lengths[usable, None]
        target_normals[usable] /= target_lengths[usable, None]
        normal_valid = usable & (np.abs(np.sum(source_normals * target_normals, axis=1)) > 0.9)
        plane_distances = np.abs(np.sum(delta * target_normals, axis=1))
    results = []
    for threshold in _thresholds:
        selected = distances < threshold
        residuals = plane_distances[selected & normal_valid]
        count = int(np.count_nonzero(selected))
        results.append({"total_points": len(_source.points),
                        "inlier_count": count,
                        "inlier_ratio": count / len(_source.points) if len(_source.points) else None,
                        **distanceStats(distances[selected]),
                        "point_to_plane": {"count": len(residuals), **distanceStats(residuals)}})
    return results


def evaluatePair(_source,
                 _target,
                 _transform,
                 _source_index,
                 _target_index):
    thresholds = [0.01, 0.02, 0.03, 0.05]
    forward = directionalMetrics(_source, _target, _transform, thresholds)
    backward = directionalMetrics(_target, _source, np.linalg.inv(_transform), thresholds)
    measures = []
    for threshold, first, second in zip(thresholds, forward, backward):
        sufficient = first["inlier_count"] > 0 and second["inlier_count"] > 0
        measures.append({"threshold_m": threshold,
                         "status": "available" if sufficient else "insufficient_overlap",
                         "inlier_chamfer_m": (first["mean_m"] + second["mean_m"]) / 2 if sufficient else None,
                         "forward": first,
                         "backward": second})
    #Codex: Keep primary-threshold fields for existing review plots and readers.
    primary = measures[2]
    plane = primary["forward"]["point_to_plane"]
    return {"source": _source_index,
            "target": _target_index,
            "thresholds": measures,
            "forward_overlap_3cm": primary["forward"]["inlier_ratio"],
            "backward_overlap_3cm": primary["backward"]["inlier_ratio"],
            "forward_nn_rmse_m": primary["forward"]["rmse_m"],
            "point_to_plane_samples": plane["count"],
            "point_to_plane_p50_m": plane["p50_m"],
            "point_to_plane_p95_m": plane["p95_m"]}


def qualityDocument(_pairs):
    return {"schema_version": 2,
            "usage": "diagnostic_only",
            "evaluation_voxel_m": 0.01,
            "primary_threshold_m": 0.03,
            "thresholds_m": [0.01, 0.02, 0.03, 0.05],
            "distance_unit": "m",
            "description": "Internal consistency at fixed final poses, not independent GT accuracy. All points of the 1 cm working clouds are queried. Distances at or above each threshold are excluded, not capped. Each direction is normalized by its own inlier count; inlier Chamfer is the mean of the two directional mean Euclidean distances (not squared). Empty sets have null statistics. Coverage uses all source points in that direction. Low residuals alone do not establish correct alignment or sufficient overlap.",
            "point_to_plane_description": "Absolute residuals for every distance-gated correspondence with valid unit normals and absolute normal dot product above 0.9. No additional residual clipping or Tukey weighting is used for evaluation. Counts may differ from point-to-point inlier counts.",
            "connection_scope": "Retained registration constraints only. These diagnostics do not change poses, connection acceptance, optimizer weights, or exported geometry.",
            "pairs": _pairs}


def writeQualityPage(_quality,
                     _destination):
    _destination.mkdir(parents=True, exist_ok=True)
    #Codex: Escape script delimiters in scan names; render all data with textContent.
    payload = json.dumps(_quality, allow_nan=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    document = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Registration quality</title>
<style>body{font:16px system-ui;margin:24px auto;padding:0 20px;max-width:1600px;color:#183344;background:#f6f8fa}
p{line-height:1.5;max-width:1100px}a{color:#08687a}label{display:inline-block;margin:8px 20px 8px 0}select,input{font:inherit;padding:6px}
.scroll{overflow:auto}table{border-collapse:collapse;background:white;width:100%}th,td{padding:10px;text-align:left;border-bottom:1px solid #dde3e8}th{white-space:nowrap}td{white-space:pre-line;min-width:120px}
th{background:#e9eff3}small{display:block;color:#526777}.missing{color:#9b3b16}</style>
<h1>Registration quality</h1><p><a href="../result_review_and_manage.html#alignment" target="_top">Alignment images and station positions</a> &middot; <a id="download" download="quality.json">Download quality JSON</a></p>
<p>Diagnostics only: these values do not change poses, registration decisions, or the saved map. They measure internal consistency, not independent GT accuracy.</p>
<p>Distances at or above the selected threshold are excluded. Inlier Chamfer averages the two directional mean distances. Read it together with both coverage ratios and counts: a small matching patch can have a low error. Coverage is a distance-based proxy, not a visibility mask.</p>
<label>Distance threshold <select id="threshold"><option value="0.01">1 cm</option><option value="0.02">2 cm</option><option value="0.03" selected>3 cm</option><option value="0.05">5 cm</option></select></label>
<label>Sort <select id="sort"><option value="order">Scan order</option><option value="error">Highest Chamfer first</option><option value="coverage">Lowest coverage first</option></select></label>
<label>Scan filter <input id="filter" type="search" placeholder="Scan name or row number"></label>
<p id="summary"></p><div class="scroll"><table><thead><tr><th>Scan pair (A / B)</th><th>Inlier Chamfer<small>mm, bidirectional</small></th>
<th>A → B coverage<small>inliers / all A points</small></th><th>B → A coverage<small>inliers / all B points</small></th>
<th>A → B distance<small>mean / RMSE / p95, mm</small></th><th>B → A distance<small>mean / RMSE / p95, mm</small></th>
<th>A → B plane residual<small>p50 / p95, mm; count</small></th><th>B → A plane residual<small>p50 / p95, mm; count</small></th></tr></thead><tbody id="pairs"></tbody></table></div>
<p>Point-to-plane statistics additionally require valid normals with |normal dot product| &gt; 0.9. They use every accepted correspondence, without robust weights. N/A means no usable correspondences; it does not mean zero error. Only retained scan connections are evaluated, using 1 cm working clouds at fixed final poses.</p>
<script id="quality-data" type="application/json">PAYLOAD</script><script>
const quality = JSON.parse(document.getElementById('quality-data').textContent);
document.getElementById('download').href = URL.createObjectURL(new Blob([JSON.stringify(quality, null, 2) + '\\n'], {type: 'application/json'}));
const mm = value => value === null ? 'N/A' : (value * 1000).toFixed(2);
const count = value => value.toLocaleString('en-US');
function render() {
    const threshold = Number(document.getElementById('threshold').value);
    const query = document.getElementById('filter').value.toLowerCase();
    const mode = document.getElementById('sort').value;
    const rows = quality.pairs.map(pair => ({pair, metric: pair.thresholds.find(item => item.threshold_m === threshold)}))
        .filter(item => `${item.pair.source + 1} ${item.pair.source_name} ${item.pair.target + 1} ${item.pair.target_name}`.toLowerCase().includes(query));
    if (mode === 'order') rows.sort((a, b) => a.pair.source - b.pair.source || a.pair.target - b.pair.target);
    if (mode === 'error') rows.sort((a, b) => (b.metric.inlier_chamfer_m ?? Infinity) - (a.metric.inlier_chamfer_m ?? Infinity));
    if (mode === 'coverage') rows.sort((a, b) => Math.min(a.metric.forward.inlier_ratio ?? 0, a.metric.backward.inlier_ratio ?? 0) - Math.min(b.metric.forward.inlier_ratio ?? 0, b.metric.backward.inlier_ratio ?? 0));
    const body = document.getElementById('pairs');
    body.replaceChildren();
    const coverage = item => `${item.inlier_ratio === null ? 'N/A' : (item.inlier_ratio * 100).toFixed(2) + '%'}\\n${count(item.inlier_count)} / ${count(item.total_points)}`;
    const distances = item => [item.mean_m, item.rmse_m, item.p95_m].map(mm).join(' / ');
    const plane = item => `${mm(item.point_to_plane.p50_m)} / ${mm(item.point_to_plane.p95_m)}\\nn=${count(item.point_to_plane.count)}`;
    for (const {pair, metric} of rows) {
        const row = document.createElement('tr');
        if (metric.status === 'insufficient_overlap') row.className = 'missing';
        const values = [`${pair.source + 1}: ${pair.source_name ?? 'Scan A'}\\n${pair.target + 1}: ${pair.target_name ?? 'Scan B'}`, mm(metric.inlier_chamfer_m), coverage(metric.forward), coverage(metric.backward), distances(metric.forward), distances(metric.backward), plane(metric.forward), plane(metric.backward)];
        for (const value of values) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
        body.append(row);
    }
    document.getElementById('summary').textContent = `${rows.length} of ${quality.pairs.length} scan pairs shown. Display units: mm. JSON distances: metres. Threshold: ${threshold * 100} cm.`;
}
for (const id of ['threshold', 'sort', 'filter']) document.getElementById(id).addEventListener('input', render);
render();</script></html>
""".replace("PAYLOAD", payload)
    pending = _destination / "quality.html.pending"
    pending.write_text(document, encoding="utf-8")
    pending.replace(_destination / "quality.html")


def main():
    parser = argparse.ArgumentParser(description="Recompute quality from saved final poses without re-registering or merging scans")
    parser.add_argument("records", type=pathlib.Path)
    args = parser.parse_args()
    from register_scans import makeCloud, saveJson
    from review_registration import createReview
    records = args.records.resolve()
    result = json.loads((records / "registration.json").read_text(encoding="utf-8"))
    if result.get("registration_voxel_m") != 0.01:
        raise ValueError("Quality evaluation requires saved 1 cm registration clouds")
    scans = result["scans"]

    @functools.lru_cache(maxsize=2)
    def loadCloud(_index):
        with np.load(records / "cache" / f"{scans[_index]['index']:02d}_1cm.npz") as data:
            return makeCloud(data["points"], data["normals"])

    edges = [edge for edge in result["constraints"] if edge["retained"]]
    pairs = []
    for number, edge in enumerate(edges):
        i, j = edge["source"], edge["target"]
        transform = np.linalg.inv(np.asarray(scans[j]["transform_map_from_scan"])) @ np.asarray(scans[i]["transform_map_from_scan"])
        pair = evaluatePair(loadCloud(i), loadCloud(j), transform, i, j)
        pair.update(source_name=scans[i]["name"], target_name=scans[j]["name"])
        pairs.append(pair)
        print(f"Evaluated {number + 1}/{len(edges)}: {scans[i]['name']} / {scans[j]['name']}", flush=True)
    saveJson(records / "quality.json", qualityDocument(pairs))
    loadCloud.cache_clear()
    createReview(result, records)


if __name__ == "__main__":
    main()
