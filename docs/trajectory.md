# Trajectory inputs and recovery

- **Workflow:** last app tab, **Generate trajectory**.
  - Fixed reference: an already merged TLS PLY, in its original map frame.
  - Output poses: TLS-map-from-body, using the first estimate's body frame.
  - Select several sequence folders for one scanned map; review detected scans and odometry per sequence.
  - Input files remain read-only; write `ground_truth_trajectory.tum` in each sequence folder and supporting files under `.ground_truth/`.
  - Use every scan by default (`period_s: 0`); positive intervals remain available for explicit CLI configurations and existing runs.
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
  - CLI configurations can specify frame names through `scans.calibration`.
- **CLI scan settings:** for custom formats beyond the form controls.
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
  - Select up to 24 scans evenly across the sequence; combine alternating scans in the SLAM frame for matching, and reserve the other scans for validation.
  - Match the combined sparse cloud to the TLS map; select the candidate supported by the most validation scans. No averaging of per-scan transforms.
  - Refine initialization candidates against the 10 cm index with point-to-plane ICP and a 5 cm Huber scale: 80 / 40 / 20 cm gates, up to 15 iterations per stage.
  - Optional JSON: `{"T_map_from_frontend": [[...], ...]}`.
  - The merged TLS map is already transformed; scanner poses are never applied again.
- **Localization:** shared Open3D point-to-plane ICP + Tukey on 1 cm source points and original TLS tiles for every scan.
  - Predict from the previous accepted pose and SLAM relative motion; refine and validate before adopting the next pose.
  - Imported baseline poses provide initial guesses and also undergo 1 cm refinement; coarse-only poses cannot become final output poses.
  - New forward-pass poses already verified at 1 cm are reused during recovery, followed by temporal checks.
  - Independent held-out scan points for quality checks.
  - Geometric support: at least 80 planar correspondences, plane RMS < 8 cm, at least six occupied 50 cm support cells, and at least 50 cm span along two principal directions.
  - Coverage below 65% requests additional context; it is not an overlap rejection threshold. No minimum coverage fraction is imposed on independently supported poses.
  - Partial views with six observable directions require two distinct neighboring scans within 1 s, each with its own converged full-rank map fit and consistent pairwise relative motion. Reuse existing fits rather than running extra ICP.
  - A weak single-scan direction remains provisional until an actual shared submap fit and independent withheld geometry jointly constrain all six directions. SLAM relative motion contributes no geometric information to the rank test.
  - These support and information thresholds are engineering defaults, not sensor-noise-calibrated accuracy bounds.
  - Rotation/translation information scaled with a 5 m lever arm; weak eigenvectors retained in `quality.jsonl`.
- **Recovery order:** forward 1 cm → reverse 1 cm → local submaps → final validation and export.
  - Engine: Open3D point-to-plane ICP + Tukey, shared with the registration tabs.
  - Correspondence gates: 80 / 30 / 10 cm; Tukey scales: 10 / 10 / 3.33 cm.
  - Individual fits: 60 iterations per gate; geometrically supported iteration-limit results can continue for 180 iterations per gate.
  - Reverse tracking reuses an accepted end pose. Otherwise, independently initialize end windows with KISS, then track backwards; try earlier windows if the last window fails.
    - End initialization uses 2 / 4 / 6 s windows and 30 / 50 / 8 cm KISS resolutions, nearby regions before the whole map.
    - Its independent scan checks and conditional leave-one-scan-out stability checks are retained.
  - Local submaps use 2 / 6 s windows, up to 11 / 21 scans, and the preceding/following accepted poses plus the original alignment as initial guesses.
    - Try the supplied guess and its 10 cm coarse refinement; fit the submap at 1 cm with up to 180 iterations per gate.
    - Withhold the anchor and complete validation scans; require independent geometry and consistent temporal motion.
    - Stop dependent validation once convergence, nearby-anchor agreement, or shared geometric support fails. Skip dense fitting when nearby-anchor constraints cannot both be satisfied.
    - Refine each accepted scan individually at 1 cm and propagate from verified anchors in both directions.
    - Weak individual directions require full-rank shared fitting and independent joint geometry; agreement within 15 cm / 3° and weak-direction departure within 1 cm using the 5 m rotation scale.
    - Mark support-dependent acceptance only when the individual scan did not already pass.
    - Stop after local submap recovery; no subsequent KISS search.
  - Exact scan/timestamp matching, competing-candidate checks, and temporal auditing apply before export.
  - CPU/memory-aware caches reuse original TLS tiles, normals, exact correspondence indices, partitions, and identical fitting requests.
  - C++/OpenMP correspondence queries use persistent nanoflann indices and reusable buffers; optimizer reductions remain ordered.
    - Balance query work dynamically; split oversized query batches without removing source/map points or changing search radii.
    - Account for allocated index memory and reserve normals before evicting reusable tiles.

## Staged campaigns

- **Initial fitting only:** `trajectory_worker.py --config CONFIG --report REPORT --baseline-only`.
  - Saves a completed, hashed `baseline/status.json`; reports `baseline_ready`, with recovery pending.
- **Continue recovery:** same config with `--recovery-only`.
  - Verify the completed baseline and reuse the hashed `tracking/` checkpoint when available.
  - Finish missing reverse work before local submaps; save the tracking checkpoint separately.
  - Publish a trajectory preview against the scanned map during recovery; reconstruct the generated map after recovery finishes.
- **Batch scheduling:** `scripts/run_trajectory_campaign.py --batch BATCH_DIRECTORY`.
  - Read `plan.json`; finish initial fitting before launching recovery.
  - Require `recovery_ready.json` with `validated: true` and matching source hashes.
  - Bound concurrent jobs and threads; refresh queued and running status in the app.
- **CPU threads:** Auto uses the available logical CPUs, respecting process affinity and `BLK_CPU_THREADS`.
  - Leave the UI field blank; configuration `workers: 0` or omitted selects Auto.
  - An explicit count limits parallel work; the effective count is recorded in `execution.jsonl`.
  - Execution records include the correspondence backend and native library SHA-256.
  - CPU affinity and Linux container quotas are respected; no machine-specific CPU count.
  - Independent queries, normal estimation, and map layers run in parallel; sequential pose propagation and optimizer reductions keep their order.
- **Memory:** live Windows/Linux available RAM and Linux cgroup limits determine cache budgets and map concurrency.
  - Persistent map search indices: up to 20% of available RAM; TLS point tiles: up to 10%; scan cache: up to 3%, at most 32 scans.
  - Evict the least recently used TLS tiles and indices individually; rebuilding an evicted index changes runtime only.
  - Lower RAM reduces cache retention or concurrent map layers, without sampling fitting points.
  - A single target that cannot fit reports the memory requirement before allocating its search index.
  - Dense KISS searches share a process lock; their native descriptor allocations are released after each request.
- **Resume:** input/map hashes, source file statistics, saved fitting requests, and atomic trajectory checkpoints.
  - Interrupted trailing log records are discarded; completed fits remain reusable.
  - Reuse identical submap ICP requests across candidate checks and restarts; bind source points, initial pose, original map hash, normal/index version, solver settings, and library versions.
  - Recompute held-out geometry and temporal checks for each candidate; cached fitting results do not bypass acceptance checks.
  - Changed inputs/settings require a new output folder.
  - Existing handoff baselines can be imported without changing their files.
- **Validity:** accepted poses only in the TUM trajectory; rejected poses remain in quality records.
  - Directions still weak after independent joint validation remain unresolved; no invented poses or interpolation across missing GT.
  - Residuals and agreement with SLAM are internal diagnostics, not independent absolute accuracy measurements.

## Outputs and viewer

- **Public trajectory:** `ground_truth_trajectory.tum` in the sequence folder.
  - Supporting files under `.ground_truth/`; CLI jobs without `sequence_directory` publish in their chosen output directory.
  - Internal `candidate_trajectory.tum` files remain resumable checkpoints.
- **Diagnostics:** `baseline/`, `recovery/quality.jsonl`, `recovery/attempts.jsonl`, `input_quality.json`, `provenance.json`.
- **Reusable tracking:** `tracking/quality.jsonl`, `tracking/candidate_trajectory.tum`, and their hashed completion receipt.
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

## Batch form and result browsing

- **Batch:** one scanned map, several independently configured sequence folders.
  - Automatically detect timestamped scans and conventional odometry filenames/folders.
  - Review each sequence; select missing files or change TUM/CSV/KITTI, timestamp units, and sensor frame.
  - Share the CPU budget across concurrent workers; bound concurrency using available memory.
  - Stop cancels active workers and queued sequences; completed checkpoints remain resumable.
- **Results:** choose a folder or individual TUM files explicitly; never populate the list from project history.
  - A parent result folder opens its immediate sequence results.
  - A complete saved result restores maps and the common scanner coordinate frame.
  - TUM-only inputs display trajectories; detected odometry uses a saved alignment when available, otherwise its original coordinates.
