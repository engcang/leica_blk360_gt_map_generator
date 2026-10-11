"""Render scan locations and inferred height bands without modifying map geometry."""

import argparse
import csv
import json
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from register_scans import readPly, saveJson
from result_layout import reviewForRecords


def sampleScan(_record, _records, _limit):
    cached = _records / "cache" / f"{_record['index']:02d}.npz"
    if cached.exists():
        with np.load(cached) as data:
            points = data["fine"]
            normals = data["fine_normals"]
            step = max(1, int(np.ceil(len(points) / _limit)))
            return points[::step].astype(float), normals[::step].astype(float)
    raw = readPly(pathlib.Path(_record["source_path"]))
    step = max(1, int(np.ceil(len(raw) / _limit)))
    points = np.column_stack([raw[axis][::step] for axis in ("x", "y", "z")]).astype(float)
    return points, None


def plotFrame(_points, _normals, _poses):
    up = np.array([0.0, 0.0, 1.0])
    method = "Map Z axis (no reliable near-horizontal floor patch available)"
    if _normals is not None:
        candidate = (np.abs(_normals[:, 2]) > 0.97) & (_points[:, 2] < -0.3) & (_points[:, 2] > -3.5)
        points = _points[candidate]
        if len(points) >= 100:
            counts, bins = np.histogram(points[:, 2], bins=np.arange(-3.5, -0.2, 0.1))
            height = bins[np.argmax(counts)] + 0.05
            patch = points[np.abs(points[:, 2] - height) < 0.2]
            if len(patch) >= 100:
                centered = patch - np.median(patch, axis=0)
                _, values, directions = np.linalg.svd(centered, full_matrices=False)
                normal = directions[-1]
                if normal[2] < 0:
                    normal *= -1
                if normal[2] > 0.97 and values[1] > 1 and values[2] < values[1] * 0.1:
                    up = _poses[0][:3, :3] @ normal
                    method = "Near-horizontal floor patch in the first scan; plot only, not surveyed leveling"
    up /= np.linalg.norm(up)
    across = np.array([1.0, 0.0, 0.0]) - up * up[0]
    across /= np.linalg.norm(across)
    rotation = np.vstack([across, np.cross(up, across), up])
    origin = _poses[0][:3, 3]
    positions = np.array([pose[:3, 3] - origin for pose in _poses]) @ rotation.T
    if len(positions) > 1 and np.linalg.norm(np.ptp(positions[:, :2], axis=0)) > 0.5:
        _, _, directions = np.linalg.svd(positions[:, :2] - positions[:, :2].mean(axis=0), full_matrices=False)
        along = directions[0]
        movement = positions[-1, :2] - positions[0, :2]
        if along @ movement < 0:
            along *= -1
        yaw = np.array([[along[0], along[1], 0], [-along[1], along[0], 0], [0, 0, 1]])
        rotation = yaw @ rotation
    return rotation, origin, method


def inferLevels(_positions, _span=0.8):
    groups = []
    for index in np.argsort(_positions[:, 2], kind="stable"):
        index = int(index)
        if not groups or _positions[index, 2] - _positions[groups[-1][0], 2] > _span:
            groups.append([index])
        else:
            groups[-1].append(index)
    return [{"id": index + 1, "stations": sorted(group),
             "scanner_height_m": float(np.median(_positions[group, 2]))}
            for index, group in enumerate(groups)]


def decorateAxis(_axis, _x, _y):
    _axis.set_xlabel(_x)
    _axis.set_ylabel(_y)
    _axis.set_aspect("equal", adjustable="datalim")
    _axis.grid(alpha=0.18)
    _axis.set_axisbelow(True)


def labelStations(_axis, _positions, _indices, _colors, _axes=(0, 1), _names=None):
    x, y = _axes
    for index in _indices:
        point = _positions[index]
        _axis.scatter(point[x], point[y], s=48, c=[_colors[index]], edgecolors="#243746", linewidths=0.7, zorder=4)
        label = str(index + 1) if _names is None else pathlib.Path(_names[index]).stem
        _axis.annotate(label, (point[x], point[y]),
                       xytext=(5, 8 + 9 * (index % 2)), textcoords="offset points", fontsize=9,
                       bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none", "pad": 1},
                       arrowprops={"arrowstyle": "-", "color": "#687681", "linewidth": 0.5}, zorder=5)


def saveFigure(_figure, _path):
    pending = _path.with_suffix(".png.pending")
    try:
        _figure.savefig(pending, format="png", dpi=160, bbox_inches="tight", facecolor="white")
        pending.replace(_path)
    finally:
        plt.close(_figure)


def createReview(_result, _records, _destination=None):
    review = _destination or reviewForRecords(_records)
    destination = review / "html_source"
    destination.mkdir(parents=True, exist_ok=True)
    scans = _result["scans"]
    if not scans:
        raise ValueError("No registered scans to review")
    poses = [np.asarray(scan["transform_map_from_scan"], dtype=float) for scan in scans]
    if any(pose.shape != (4, 4) or not np.isfinite(pose).all() for pose in poses):
        raise ValueError("Invalid scan poses for review")
    #Codex: Bounded display samples avoid loading every raw scan or the merged map into memory.
    limit = max(1000, min(80000, 2000000 // len(scans)))
    first, normals = sampleScan(scans[0], _records, limit)
    rotation, origin, method = plotFrame(first, normals, poses)
    positions = (np.array([pose[:3, 3] for pose in poses]) - origin) @ rotation.T
    levels = inferLevels(positions)
    clouds = []
    for index, (scan, pose) in enumerate(zip(scans, poses)):
        points = first if index == 0 else sampleScan(scan, _records, limit)[0]
        transformed = (points @ pose[:3, :3].T + pose[:3, 3] - origin) @ rotation.T
        clouds.append(transformed[np.isfinite(transformed).all(axis=1)])
    colors = plt.get_cmap("tab20" if len(scans) <= 20 else "turbo")(np.linspace(0, 1, len(scans)))
    quality_path = _records / "quality.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else {}
    if quality.get("schema_version") == 2:
        from registration_quality import writeQualityPage
        writeQualityPage(quality, destination)
        saveJson(destination / "quality.json", quality)
    figure, axes = plt.subplots(2, 2, figsize=(15, 10), layout="constrained")
    figure.suptitle(f"Registration review | {len(scans)} scans | colors identify scans", fontsize=17)
    for index, cloud in enumerate(clouds):
        axes[0, 0].scatter(cloud[:, 0], cloud[:, 1], s=0.3, color=colors[index], alpha=0.25, rasterized=True)
        axes[0, 1].scatter(cloud[:, 0], cloud[:, 2], s=0.3, color=colors[index], alpha=0.25, rasterized=True)
    for axis, dimensions in ((axes[0, 0], (0, 1)), (axes[0, 1], (0, 2))):
        labelStations(axis, positions, range(len(scans)), colors, dimensions)
        axis.plot(positions[:, dimensions[0]], positions[:, dimensions[1]], "--", color="#334155", linewidth=0.8, alpha=0.6)
    axes[0, 0].set_title("Top view / station numbers")
    axes[0, 1].set_title("Side view / station numbers")
    decorateAxis(axes[0, 0], "Plot X (m)", "Plot Y (m)")
    decorateAxis(axes[0, 1], "Plot X (m)", "Relative scanner height (m)")
    numbers = np.arange(1, len(scans) + 1)
    axes[1, 0].plot(numbers, positions[:, 2], color="#64748b", linewidth=1)
    axes[1, 0].scatter(numbers, positions[:, 2], c=colors, edgecolors="#243746", zorder=3)
    for level in levels:
        height = level["scanner_height_m"]
        axes[1, 0].axhline(height, linestyle=":", color="#94a3b8", linewidth=0.8)
        axes[1, 0].text(1, height + 0.1, f"L{level['id']} (inferred)", fontsize=9)
    axes[1, 0].set(title="Height by acquisition order / dashed path is not a trajectory", xlabel="Station number", ylabel="Relative scanner height (m)")
    axes[1, 0].set_xticks(numbers[::max(1, len(scans) // 30)])
    axes[1, 0].grid(alpha=0.18)
    for key, label in (("point_to_plane_p50_m", "Per-pair median"), ("point_to_plane_p95_m", "Per-pair 95th percentile")):
        values = [pair[key] * 1000 for pair in quality.get("pairs", []) if pair.get(key) is not None and np.isfinite(pair[key])]
        if values:
            axes[1, 1].hist(values, bins=min(30, max(5, len(values) // 3)), alpha=0.65, label=label)
    axes[1, 1].set(title="Internal residuals / not independent GT accuracy", xlabel="Absolute point-to-plane residual (mm)", ylabel="Scan pairs")
    if axes[1, 1].has_data():
        axes[1, 1].legend()
    else:
        axes[1, 1].text(0.5, 0.5, "Residual statistics unavailable", ha="center", transform=axes[1, 1].transAxes)
    saveFigure(figure, destination / "alignment_review.png")
    files = ["alignment_review.png"]
    xy_limits = [positions[:, :2].min(axis=0) - 8, positions[:, :2].max(axis=0) + 8]
    for level in levels:
        figure, axis = plt.subplots(figsize=(12, 8), layout="constrained")
        height = level["scanner_height_m"]
        for index, cloud in enumerate(clouds):
            sliced = cloud[np.abs(cloud[:, 2] - height) <= 0.75]
            if len(sliced):
                axis.scatter(sliced[:, 0], sliced[:, 1], s=1, color=colors[index], alpha=0.65, rasterized=True)
        labelStations(axis, positions, level["stations"], colors, _names=[scan["name"] for scan in scans])
        axis.set_title(f"Inferred level L{level['id']} | scanner height {height:+.2f} m\nWall slice within 0.75 m of this height; colors identify source scans")
        decorateAxis(axis, "Plot X (m)", "Plot Y (m)")
        axis.set_aspect("equal", adjustable="box")
        axis.set_xlim(xy_limits[0][0], xy_limits[1][0])
        axis.set_ylim(xy_limits[0][1], xy_limits[1][1])
        figure.text(0.5, 0.005, "Station context: 8 m beyond the station bounds. Overview includes all sampled observations.", ha="center", fontsize=9)
        name = f"level_{level['id']:02d}.png"
        saveFigure(figure, destination / name)
        files.append(name)
    stations = []
    for index, (scan, pose, point) in enumerate(zip(scans, poses, positions)):
        level_id = next(level["id"] for level in levels if index in level["stations"])
        stations.append({"station": index + 1, "name": scan["name"], "inferred_level": level_id,
                         "map_xyz_m": pose[:3, 3].tolist(), "plot_xyz_m": point.tolist(),
                         "color": matplotlib.colors.to_hex(colors[index])})
    summary = {"schema_version": 1, "plot_only_rotation": rotation.tolist(), "plot_origin_map_m": origin.tolist(),
               "plot_frame_note": method, "level_group_max_span_m": 0.8, "wall_slice_half_width_m": 0.75,
               "level_note": "Inferred scanner-height bands, lowest first. Not building floor numbers. Tripod height, stairs, ramps and plot leveling can affect assignments.",
               "geometry_note": "Review transforms and display sampling do not modify saved PLY coordinates or registration poses.",
               "path_note": "Dashed lines show acquisition order between stationary scans, not a measured moving trajectory.",
               "level_plot_xy_limits_m": [limit.tolist() for limit in xy_limits],
               "sample_limit_per_scan": limit, "sampled_points": sum(len(cloud) for cloud in clouds),
               "quality_description": quality.get("description", "Residual statistics unavailable"),
               "stations": stations, "levels": levels, "images": files}
    saveJson(destination / "review_summary.json", summary)
    with (destination / "station_positions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["station", "scan", "inferred_level", "map_x_m", "map_y_m", "map_z_m", "plot_x_m", "plot_y_m", "plot_z_m"])
        for station in stations:
            writer.writerow([station["station"], station["name"], station["inferred_level"], *station["map_xyz_m"], *station["plot_xyz_m"]])
    from result_page import writeReviewPage
    writeReviewPage(review)
    print(f"Created review images for {len(scans)} scans and {len(levels)} inferred height bands: {review}", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description="Create review images from saved registration poses; no re-registration")
    parser.add_argument("records", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    records = args.records.resolve()
    createReview(json.loads((records / "registration.json").read_text(encoding="utf-8")), records, args.output)


if __name__ == "__main__":
    main()
