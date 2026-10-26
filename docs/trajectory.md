# Trajectory inputs and recovery

- **Workflow:** last app tab, **Generate trajectory**.
  - Fixed reference: an already merged TLS PLY, in its original map frame.
  - Output poses: TLS-map-from-body, using the first estimate's body frame.
  - Input files are opened read-only; result/cache directories must be separate from scan inputs.
  - No ROS or TartanStar SDK installation required.

## Estimated trajectories

- **TUM:** whitespace-separated `timestamp x y z qx qy qz qw`.
  - Pose convention: world-from-body by default; select **Invert input poses** for body-from-world.
  - Position scale: `1` for metres, `0.001` for millimetres.
  - Timestamp units: seconds, milliseconds, microseconds, or nanoseconds.
  - Offset: added after unit conversion, in seconds.
- **CSV:** header row plus eight selected column names.
  - UI order: time, x, y, z, qx, qy, qz, qw.
  - Example header: `timestamp,x,y,z,qx,qy,qz,qw`.
  - Custom delimiter: trajectory configuration field `delimiter`; default comma.
- **KITTI:** twelve values per row, the first three rows of a homogeneous transform.
  - Select a separate timestamp file containing one timestamp per matrix.
- **Pose frame conversion:** optional `T_pose_from_body` matrix in each trajectory's format settings.
  - Applied on the right: `T_world_from_body = T_world_from_pose @ T_pose_from_body`.
  - Frontend/backend poses must describe the same physical body and estimated world frame.
  - The same TLS-from-estimate alignment applies to every estimate; curves are not fitted independently.
- **Time support:** strictly increasing timestamps and finite, normalized quaternions.
  - Default maximum interpolation gap: `0.5` seconds.
  - Exact measured endpoints remain valid; unsupported intervals are skipped.

## Scan sequences

- **Timestamped folder:** numeric filenames such as `1791826637.500000.npy`.
  - PLY/PCD: XYZ point clouds.
  - NPY: numeric `N × 3` XYZ array.
  - NPZ: `points` array (`N × 3`); optional `timestamps` array (`N`).
  - BIN: little-endian float32 XYZI by default; `scans.bin_columns: 3` selects XYZ.
  - Without per-point timestamps: scans are treated as already deskewed at the filename timestamp.
- **Manifest JSON:** arbitrary scan filenames and explicit timestamps.
  - Paths resolve relative to the manifest; absolute paths also supported.

  ```json
  {
    "scans": [
      {"timestamp": 100.0, "path": "scans/frame000.ply"},
      {"timestamp": 100.1, "path": "scans/frame001.npz"},
      {"timestamp": 100.2, "path": "scans/frame002.bin"}
    ]
  }
  ```

- **TartanStar:** select an extracted sequence folder containing `lidar/`.
  - Reads `meta.yaml`, `meta.npz`, `rng`, and `tcol` directly.
  - Requires lossless uint32 ranges; verifies compressed frame widths and metadata offsets.
  - Uses original column timestamps, Ouster beam calibration, and OS-DOME → base_link calibration.
  - Other sensor configurations: select a custom calibration file.
- **Scan calibration:** `T_body_from_sensor`, applied before deskew.
  - JSON: a 4 × 4 matrix or `{"T_body_from_sensor": [[...], ...]}`.
  - URDF: defaults `sensor_frame: "os_dome_lidar"`, `body_frame: "base_link"`.
  - Change frame names through `scans.calibration` in additional settings.
- **Additional settings:** merged with the form's scan settings.
  - Per-point timestamps are absolute by default; set `point_times_relative` for offsets from the scan timestamp.
  - `point_scale`: source XYZ units to metres; default `1`.
  - Deskew: interpolated translation and quaternion SLERP from the first estimate, anchored at each scan timestamp; TartanStar anchors at its last valid column.

  ```json
  {
    "max_pose_gap_s": 0.5,
    "scans": {
      "min_range_m": 1,
      "max_range_m": 60,
      "point_scale": 1,
      "time_offset_s": 0,
      "point_times_relative": false,
      "bin_columns": 4
    }
  }
  ```

## Registration and recovery

- **Initialization:** KISS at 30/50 cm, with complete scans withheld for validation.
  - Optional JSON: `{"T_map_from_frontend": [[...], ...]}`.
  - The merged TLS map is already transformed; scanner poses are never applied again.
- **Localization:** point-to-plane ICP with a 5 cm Huber scale against the fixed 10 cm TLS normal index.
  - Correspondence gates: 80 / 40 / 20 cm; up to 15 iterations per stage.
  - Retain accepted baseline poses after temporal checks; 1 cm recovery processes rejected intervals.
  - Independent held-out scan points for quality checks.
  - Eligibility: at least 80 planar correspondences, 20 cm coverage ≥ 65%, plane RMS < 8 cm, all six directions observable.
  - Rotation/translation information scaled with a 5 m lever arm; weak eigenvectors retained in `quality.jsonl`.
- **Recovery:** shared Open3D point-to-plane ICP + Tukey engine on 1 cm source points and original TLS point tiles.
  - Same engine as Register, chunk merging, and Additional registration.
  - Correspondence gates: 80 / 30 / 10 cm; Tukey scales: 10 / 10 / 3.33 cm.
  - Iteration limits per stage: 60 for individual scans, 180 for submap seeds; stop early on convergence.
  - Convergence: stable fitness/RMSE, or three stable pose updates below 10 µm / 1 µrad with stable RMSE; require positive Tukey support.
  - Target normals: 4 cm radius, up to 40 neighbors; cached on original tiles with neighbors across tile boundaries.
  - Held-out validation: original 1 cm target, 20-neighbor planar support; the same coverage, residual, and six-direction thresholds.
  - Bidirectional propagation from retained anchors.
  - Relative-motion gate: starts at 15 cm / 3°, with bounded distance/time allowance up to 50 cm / 10°.
  - Audit existing and recovered poses in both time directions; remove inconsistent runs before fitting and map export.
  - Exact scan-index/timestamp checks before accepting or exporting poses.
  - Relocalization: 2 s and 6 s submaps, with guesses from both retained ends and the initial alignment.
  - KISS fallback: regional/full-map matching at 30 / 50 / 8 cm, followed by the same 1 cm refinement.
  - Try direct fine fitting and both local window sizes first; visit all local recovery intervals before global search.
  - Compare direct and coarse-initialized candidates on the same withheld scans, including unmatched points in the cost; reject unresolved alternatives.
  - Withhold the anchor and entire validation scans from submap fitting; require agreement from at least two other scans.
  - Partial geometric fits still receive independent seed attempts; low residual alone does not suppress relocalization.
  - Retry conflicting forward/reverse tracks with an independently validated submap; reject unresolved conflicts between seeds.
  - Parallel normal estimation and correspondence queries; serial Open3D optimizer/reductions; normals/search index reused for nearby scans.
  - Reuse exact distance bounds between iterations; skip a query only when the point must remain outside the gate.
  - For difficult overlaps, an auxiliary cell-cover index supplies conservative distance bounds; all retained correspondences still query the original 1 cm target exactly.
  - Large nearest-neighbor searches use queued 8,192-point batches within the CPU budget, retaining original correspondence and solver reduction order.
  - Seed progress includes the scan, submap duration, and search scope; fit logs record preparation/fitting timings.
- **CPU threads:** Auto uses the available logical CPUs, respecting process affinity and `BLK_CPU_THREADS`.
  - Leave the UI field blank; configuration `workers: 0` or omitted selects Auto.
  - An explicit count limits parallel work; the effective count is recorded in `execution.jsonl`.
  - CPU affinity and Linux container quotas are respected; no machine-specific CPU count.
  - Independent queries, normal estimation, and map layers run in parallel; sequential pose propagation and optimizer reductions keep their order.
- **Memory:** live Windows/Linux available RAM and Linux cgroup limits determine cache budgets and map concurrency.
  - TLS tile cache: up to 10% of available RAM; scan cache: up to 3%, at most 32 scans.
  - Evict the least recently used TLS tiles individually; optional search indexes fall back to the original exact search when memory is limited.
  - Lower RAM reduces cache retention or concurrent map layers, without sampling fitting points.
  - A single target that cannot fit reports the memory requirement before allocating its search index.
  - Dense KISS searches share a process lock; their native descriptor allocations are released after each request.
- **Resume:** input/map hashes, source file statistics, saved fitting requests, and atomic trajectory checkpoints.
  - Interrupted trailing log records are discarded; completed fits remain reusable.
  - Reuse identical submap ICP requests across candidate checks and restarts; hash source points, initial pose, target points/normals, solver settings, and library versions.
  - Recompute held-out geometry and temporal checks for each candidate; cached fitting results do not bypass acceptance checks.
  - Changed inputs/settings require a new output folder.
  - Earlier Nano-GICP recovery journals remain viewable; use a new output folder for the unified engine.
  - Recovery v8 records fine-map validation, temporal chain selection, competing seeds, and cached original-map normals; use a new output folder for older protocols.
  - CLI `--local-only`: save local recovery first; resume the same command without this flag for global search.
  - Existing handoff baselines can be imported without changing their files.
- **Validity:** accepted poses only in the TUM trajectory; rejected poses remain in quality records.
  - Weak directions remain unresolved; no invented poses or interpolation across missing GT.
  - Residuals and agreement with SLAM are internal diagnostics, not independent absolute accuracy measurements.

## Outputs and viewer

- **Trajectory:** `recovery/candidate_trajectory.tum`.
- **Diagnostics:** `baseline/`, `recovery/quality.jsonl`, `recovery/attempts.jsonl`, `input_quality.json`, `provenance.json`.
- **Live job state:** monitored jobs read `job_status.json`; a saved viewer can remain available while recovery is running or queued.
  - Saved-result imports without live status are labeled `saved`; only a completed worker is labeled `completed`.
  - Background controllers register configurations through `/api/trajectory/monitor` and refresh status timestamps; missing or stale progress is shown as interrupted.
- **Browser links:** keep the complete local URL, including `#` and its session key, when opening it in another browser.
  - `execution.jsonl`: source/native hashes and package versions for each run or resume.
- **Maps:** `tls_gt_10cm.ply`, `estimate_0_10cm.ply`, additional `estimate_N_10cm.ply` when supplied.
  - Every accepted scan contributes to the generated ground-truth trajectory map; estimates use their supported scans.
  - Accepted scan indices select the original scans directly; no nearest-timestamp reassociation.
  - Sensor-to-body calibration and available point timestamps are applied before reconstruction.
  - Each accepted pose supplies the full rotation and translation: `p_map = R_gt * p_body + t_gt`.
  - Estimate interpolation: linear translation and quaternion SLERP; PLY/PCD scans without point timestamps receive a rigid transform only.
  - Fixed frontend deskew for all layers; actual scan points, one representative per 10 cm voxel.
  - Read each scan once; build map layers in parallel while retaining scan order and voxel representatives.
  - No ICP-inlier filtering, projection onto TLS surfaces, or synthetic gap filling.
- **Viewer:** open the saved result folder in the app.
  - Each result row provides **Open output folder** and **Open trajectory viewer**.
  - **Scanned GT map:** scanner reference at 10 cm only; original 1 cm input retained for registration.
  - **Generated ground-truth trajectory map:** dataset scans transformed by accepted GT poses, at 10 cm.
  - Generated ground-truth trajectory and available estimated trajectories/maps.
  - Shared map frame, disconnected time gaps, start/end markers, saved view settings.
  - PLY range streaming and GPU buffers of at most 12 MiB for map XYZ.
  - Display maps and trajectory SHA-256 recorded in `products.json` / `viewer.json`.
- **Command line:** same worker as the app, with explicit configuration and report paths.

  ```bash
  .venv/bin/python mapping/trajectory_worker.py --config trajectory_config.json --report progress.json
  ```

  - Add `--inspect` to validate inputs without generating a result.
  - Use the native executable installed by the launcher, or set `BLK_REGISTRATION_EXECUTABLE`.
