<h1 align="center">BLK Studio</h1>

- Convert supported BLK360 G1 scans to full-resolution PLY through a local web app.
- Register overlapping scans, review alignment, and generate maps or standalone 3D HTML.
- Preserve original files in place; C++17 core, no ROS, MIT license.

<br>

## Install and run

- **Source:** download or clone this repository into a writable folder.
  - Optional: `git clone --recurse-submodules https://github.com/engcang/leica_blk360_gt_map_generator.git`.
  - Builds automatically initialize missing submodules at their pinned commits.
  - Source ZIPs download the same pinned sources automatically.
- **Launch:**
  - **Windows 10/11 x64:** double-click `Start-BLK-Studio.bat`.
  - **Ubuntu 22.04/24.04 x86_64:** run `bash start.sh` from the repository folder.
    - Sudo required only for missing system packages.
- **First run:**
  - Internet access, several GB of free disk space, and compilation time required.
  - Automatic tool downloads, local Python setup, app/registration builds, and runtime checks.
  - Browser opens when ready; no manual CMake or Python package installation.
- **Later runs:** reuse the installation; source changes trigger a rebuild.
- **Local installation:**
  - Original submodules: `third_party/kiss-matcher/` and `third_party/nano_gicp/`.
  - Other registration sources: `third_party/registration/`; build artifacts: `build/`.
  - Local tools and installation: `.build-tools/`, `.venv/`, and `install/`.
  - Keep these folders with the project; Python environments contain absolute paths.
  - Windows setup: no administrator rights or system PATH changes.
- **Operation:**
  - Use the same launcher each time; processing runs locally at `127.0.0.1`.
  - **Quit app** stops the process; closing the browser tab leaves it running.
  - Setup errors appear in the launcher; details: `build/setup.log`.
  - Build only: `Start-BLK-Studio.bat -NoLaunch` or `bash start.sh --no-launch`.
  - Default compilation parallelism: two jobs.

<br>

## Usage

- **Tabs:** Convert (default), View saved point clouds, Register, Additional Register, Generate trajectory.
- **Selections:**
  - Choose PLY files or a folder each time; viewing/registration selections are not restored.
  - View files in place without a library cache.
- **Scan order:**
  - Modification time, oldest first; path breaks ties.
  - New PLY conversions preserve BLK timestamps; existing PLY timestamps stay unchanged.
  - Acquisition order depends on source timestamp accuracy.
  - Adding registration inputs re-sorts the selection; **Up / Down** adjusts it manually.

### Convert and view

- **Convert:**
  - Add BLK scans with **Select files** or **Select folder**.
  - **Output folder:** defaults to `output` beside the scans; change if needed.
  - Click **Convert scans**; each scan produces one full-resolution PLY.
  - Example: `Setup26.blk → Setup26.ply`.
- **View:**
  - Click **View 3D** on a completed scan.
  - For existing files: **View saved point clouds → Open PLY files / Open folder**.
- **Editions:** both built together on Windows and Ubuntu.
  - **Intensity:** uint16 intensity.
    - Windows: `install/blk_studio.exe`.
    - Ubuntu: `install/blk_studio`.
  - **RGB:** uint8 red, green, blue; no intensity field.
    - Windows: `install/blk_studio_rgb.exe`.
    - Ubuntu: `install/blk_studio_rgb`.
  - RGB launcher: `Start-BLK-Studio-RGB.bat` or `bash start.sh --rgb`.
  - Build only: `-NoLaunch` on Windows; `--no-launch` on Ubuntu.
  - CLI converters: `blk_convert` and `blk_convert_rgb`; `.exe` suffix on Windows.
  - Separate work records: `projects/default` and `projects/rgb`.
  - Use separate output folders; existing PLY files are never overwritten.
- **RGB support:**
  - BLK360 G1 LDR images with decoded pinhole calibration; HDR unsupported.
  - Missing LDR images or unsupported camera models cause conversion errors.
  - Projection uses embedded intrinsics/extrinsics, depth visibility, and closest optical axis.
  - Uncoloured points: gray; coverage counts saved in conversion metadata.
  - Colour seams can occur between cameras.
  - Existing native JPEG XR codec; no additional Python/image-codec package.
- **Shared features:**
  - Scan/chunk placement, Additional Register, quality reports, and offline share HTML.
  - Raw and 1 cm / 3 cm exports; RGB preserved in raw exports and averaged in voxel maps.
  - Registration and quality metrics use geometry, not colour.
  - Inputs must match the edition; RGB and intensity scans cannot be mixed.
  - Run setup or replace installed files only when registration workers are stopped.
- **Viewer controls:**
  - Colour: fixed to the edition's **Intensity** or **RGB**.
  - Density: **1/10**, **1/5**, **1/2**, or **Full** (default).
  - Navigation: temporarily draws at most 2,000,000 points.
  - Left-drag: orbit around the surface near screen centre; look around if no surface is found.
    - Pivot selected at drag start and held during the drag; camera position preserved.
  - Right-/middle-drag or Shift-drag: pan using visible-surface depth.
    - Empty views retain the last usable pan speed; orbit pivot does not affect pan speed.
  - Wheel: move forward/back; Shift+wheel: slower movement.
  - Rotation: reduced speed for finer control.
  - Display settings leave the PLY unchanged.
- **Conversion state:**
  - Completed rows: current app session only; no saved conversion history.
  - Deleted output: returns to **Ready** if the source BLK exists; convert again to recreate.
  - Pending/interrupted work: retained for recovery.

### Register scans

- **Inputs:** add overlapping, individual scans with **Add PLY files**, **Add folder**, or **Add to map**.
- **Order:** confirm with **Up / Down**; the first scan defines the map frame.
- **Output:** confirm the base name and folder; default is the first selected PLY folder.
  - Use a separate folder for each registration result.
- **Start:** check **Registration setup**, then click **Register scans**.
- **Review:** below **Latest worker log**, open **Review and manage**.
  - Quality metrics, scan positions, and inferred-level images.
- **Optional scan placement:**
  - Open **Arrange PLY scans** after selecting inputs.
  - Save beside the selected PLYs unless another output folder is chosen.
  - **Move view / Place scan / chunk:** choose the left-drag action; default is view movement.
  - Placement axes: XY, X, Y, Z, yaw; numeric entry available.
  - First item fixed; **Next** uses the previous item as reference.
  - Right-/middle-drag always pans.
  - **Height cut:** hide points above a shared Z height after placement transforms.
  - **Show all:** restore visibility without changing registration inputs.
  - Load only the moving item and reference.
  - Placement defaults: **1/10** density, **Axes on** for scans, chunks, and Additional Register.
  - Other viewers: **Full** density, grid/axes off.
- **Placement persistence:**
  - Autosave to `initial_placements.json`; no Apply button.
  - Wait for saved status before closing; keep the editor open if saving fails.
  - Adding, removing, or reordering scans preserves other poses.
  - Reopen by selecting the same PLYs and output folder.
  - Registration reads saved poses; scans without placements use automatic registration.
  - Placements belong to their output folder; changing folders does not mix arrangements.
  - Unreadable placements or selection changes during loading stop registration.
- **Disconnected chunks:**
  - **Arrange chunks** autosaves `chunk_initial_placements.json`; the editor never starts registration.
  - Return to **Register → Register chunks**.
  - Each chunk is one rigid body: merged-cloud Nano-GICP → robust 1 cm ICP → chunk-level PGO.
  - Saved chunk poses initialize matching; unplaced chunks use KISS-Matcher.
  - Internal scan transforms stay fixed; final poses combine chunk and scan-local transforms.
  - No consecutive-scan search or scan-level ICP/PGO in this mode.
  - Bridge validation: at most 12 original scan pairs at fixed poses.
    - Two distinct passing pairs required; one for two single-scan chunks.
  - Quality metrics: final fixed poses on 1 cm scans.
  - **Refine individual scans afterward:** off by default.
    - Enabling it allows slower scan-level 1 cm ICP/PGO to change internal chunk geometry.
  - Changed membership/geometry regenerates placement JSON.
    - Previous placements preserved under `html_source/placement_history/`.
    - Unchanged chunk sets retain their placements.

### Extend an existing map

- **Reference:** in **Additional Register**, open `relative_poses.json`.
  - Keep original PLYs at their recorded paths.
- **Inputs:** add new PLYs or a folder; choose a separate output folder.
- **Optional placement:** **Arrange reference and scans**.
  - Reference remains fixed.
  - New poses autosave to `additional_initial_placements.json`.
  - Keys: source path and reference hash; removing a scan preserves its saved pose.
- **Start:** click **Register additional scans**; watch the log and open the result below it.
- **Registration:**
  - Preserve the reference's original transforms and map frame exactly.
  - Saved guesses or KISS-Matcher → Nano-GICP → robust 1 cm ICP → chunk-level PGO.
  - Match smaller scans into the larger map.
  - Validate bridges with fixed original scan pairs: one for a single new scan, otherwise two.
  - Disconnected groups: **View chunks / Arrange chunks → Register chunks**.
  - Individual-scan refinement disabled for this workflow.
- **Result:** new JSON includes old and new scans without copying PLYs or changing original results.
  - Generate merged PLYs or share HTML when needed.

### Generate a dataset trajectory

- **Inputs:** follow the four numbered steps in **Generate trajectory**.
  - **Scanned GT map:** merged 1 cm PLY from Register, or another registered scanner map.
  - **Sequence scans:** the dataset's individual LiDAR scans.
  - **Estimated trajectory:** the sequence's frontend or odometry poses; additional estimates are optional comparisons.
  - **Save result:** result name and output folder.
  - Sequences: timestamped PLY/PCD/NPY/NPZ/BIN folder, scan manifest JSON, or TartanStar extracted data.
  - Trajectories: TUM, CSV with column mapping, or KITTI poses with a timestamp file.
  - Add frontend, backend, or other available estimates; the first supplies scan motion and deskew.
  - Choose timestamp units, offsets, pose convention, and sensor-to-body calibration as needed.
- **Run:** choose a separate output folder → **Check inputs** → **Generate trajectory**.
  - KISS initialization → 10 cm point-to-plane + Huber localization → bidirectional 1 cm Open3D point-to-plane + Tukey recovery.
  - Retain accepted baseline poses after temporal checks; apply 1 cm recovery to rejected intervals.
  - Recovery seeds: trusted poses on both sides, 2 s / 6 s submaps, regional and full-map KISS at 30 / 50 / 8 cm.
  - Validate recovered poses on the original 1 cm map; check existing and recovered poses against chronological relative motion.
  - Seed checks withhold the anchor scan and require agreement from at least two other scans.
  - **Stop / Resume:** retain completed fits; verify input provenance before reusing them.
  - Optional: supply initial alignment JSON or an existing baseline result to recover.
- **Viewer:**
  - **Scanned GT map:** scanner reference map, displayed at 10 cm only; the original 1 cm input remains available for registration.
  - Available estimated trajectories and their reconstructed 10 cm maps.
  - **Generated ground-truth trajectory** and **Generated ground-truth trajectory map** at 10 cm; missing poses remain disconnected.
  - Shared TLS coordinate frame; map selection, color palettes, transparency, point styles, and start/end markers.
  - Bounded GPU uploads for large maps; screenshot export retained.
- **Files:** `recovery/candidate_trajectory.tum`, `recovery/quality.jsonl`, `viewer.json`, and `*_10cm.ply`.
  - Original TLS map and scans stay in place.
  - **Open existing result:** select the trajectory result folder.
  - Each saved result shows its path, **Open output folder**, and **Open trajectory viewer**.
  - Repeated results for the same scans and reference map show the latest available result.
- **Formats and configuration:** [Trajectory inputs and recovery](docs/trajectory.md).

### Outputs and recovery

- **Typical output layout:**

  ```text
  output/
      Setup26.ply
      Setup27.ply
      ...
      relative_poses.json
      result_review_and_manage.html
      html_source/
  ```

- **Original PLYs:** remain where selected; files stored elsewhere are not copied.
- **`relative_poses.json`:**
  - Source paths, hashes, and rigid transforms into the first scan/map frame.
  - Convention: `p_map = T_map_scan @ [x, y, z, 1]`; units: metres.
  - When moving a dataset, retain originals and preserve their paths relative to the JSON.
- **Reopen results:**
  - Scan registration tabs start with empty selections/output paths; trajectory settings and jobs are restored.
  - Choose **Open existing result → relative_poses.json**.
  - For disconnected jobs, select `partial_chunks.json`.
- **Working caches:** app-local `projects/default/registration_records/`.
  - Completed result viewing and product generation do not require these caches.
- **Fine ICP recovery:**
  - Completed pairs saved for **Resume**; final correspondences reused for graph weights.
  - Changed clouds, initial poses, or ICP settings invalidate the corresponding checkpoint.
  - Point density, robust loss, and convergence criteria preserved.
- **CPU usage:**
  - Automatic thread detection for native engines, Open3D, and quality queries.
  - Generate trajectory: **CPU threads → Auto** uses available logical CPUs; an explicit count sets a limit.
    - Parallel queries, normals, and map layers; ordered trajectory propagation and serial Open3D solver reductions.
    - Reuse exact distance bounds to skip points still outside the correspondence gate; unchanged fitting points and thresholds.
  - CPU affinity/container limits and available RAM detected per machine.
    - RAM controls concurrent maps and tile/scan cache sizes; point density remains unchanged.
  - Fine ICP: one pair at a time using available threads by default.
  - `BLK_CPU_THREADS`: limit the CPU budget.
  - `BLK_REGISTRATION_WORKERS`: enable memory-bounded concurrent pairs.
    - May be slower when one pair already saturates CPU or memory bandwidth.

### Generate maps and share results

- **Registration output:** poses and review materials; merged products generated on request.
- **Merged PLY:** choose **Generate 1 cm / 3 cm PLY**.
  - Files: `reference_map_1cm.ply`, `reference_map_3cm.ply`.
  - Voxel-averaged map at the selected resolution.
- **Raw PLY:** choose **Generate raw PLY**.
  - File: `reference_map_raw.ply`.
  - Every original point transformed; intensity or RGB preserved.
- **Standalone HTML:** choose **Generate 1 cm / 3 cm share HTML**.
  - Files: `reference_map_1cm_share.html`, `reference_map_3cm_share.html`.
  - Complete selected map embedded; recipient needs only the HTML and a browser.
- **Management:**
  - Open **Review and manage** in BLK Studio to generate products.
  - Open the HTML directly for offline review and saved-file information.
  - **Check output folder:** verify saved filenames and sizes after moving/deleting files.
  - Keep `html_source/` beside the management HTML.
- **Resolution and reuse:**
  - Select 1 cm or 3 cm independently for PLY and share HTML; both can coexist.
  - Registration refinement stays fixed at 1 cm.
  - Generation uses saved poses without re-registering.
  - Share HTML reuses a verified map at the chosen resolution; otherwise uses a temporary map.
  - Retain original PLYs and `relative_poses.json` for reproducibility.
- **Parallel voxel export:**
  - Concurrent scans bounded by available CPU and memory; small exports run serially.
  - `BLK_CPU_THREADS`: CPU limit; `BLK_MERGE_WORKERS`: concurrent-scan limit (`1` for serial).
  - Temporary float64 scan voxels stored beside the output and removed after merging.
  - Per-scan and final voxel averaging unchanged.
  - Progress: completed scans, final reduction, file writing.
  - Product generation also refreshes the review page.

<br>

## Registration and quality

- **Scan registration pipeline:**
  - KISS-Matcher: 30 cm clouds.
  - Nano-GICP: 30 cm, then 8 cm clouds.
  - Pose graph optimization.
  - Shared 1 cm engine in every registration tab: Open3D point-to-plane ICP + Tukey loss.
    - Scan/chunk refinement: 3 cm correspondence gate, 1 cm Tukey scale.
    - Trajectory recovery: 80 / 30 / 10 cm gates; Tukey scales 10 / 10 / 3.33 cm for its larger initial errors.
      - Up to 60 iterations per gate; final-stage convergence and held-out geometry required.
    - Cached target queries; same Open3D estimator and convergence criteria.
  - Final pose graph optimization.
- **Automatic disconnected-group recovery:**
  - Search other overlapping pairs, then run KISS-Matcher / Nano-GICP on merged chunks.
  - Require two consistent original scan-pair checks within 10 cm / 1 degree for a chunk bridge.
  - Continue through 1 cm refinement and pose graph optimization.
  - Keep ambiguous or unverified connections unresolved.
- **Evaluation:**
  - Fixed final poses on 1 cm per-scan clouds.
  - Distance thresholds: 1 / 2 / 3 / 5 cm; default: 3 cm.
  - Exclude distances at or above the threshold.
  - Metrics: bidirectional inlier Chamfer, directional mean/RMSE/p95,
    correspondence ratios/counts, and point-to-plane residuals.
  - Interpret errors with coverage; evaluation does not change registration.

<br>

## Troubleshooting

- **Missing file:** reselect the moved PLY; generation requires originals at recorded paths.
- **Disconnected groups:**
  - Open **View chunks**, or **Build chunk review** for older jobs.
    - Select one chunk at a time; previous/next controls and a full-width 3D view.
    - Height-level filter, scan table, and per-level top views with scan positions and filenames.
  - Files: `chunks_review.html`, `partial_chunks.json`, `html_source/chunks/`.
  - Sampled previews, scan positions, and original row numbers in separate chunk-local frames.
  - Diagnostic poses before final 1 cm refinement; no common final map yet.
  - **Arrange chunks:** provide initial placements.
  - **Resume:** reuse caches and search for connections automatically.
  - Final map/poses published only after all groups connect.
- **Slow viewer or large output:**
  - Choose a 3 cm export or lower display density for navigation.
  - Complete cloud uploaded in 16 MiB graphics buffers; total memory depends on point count.
- **Existing output:** move/remove the generated file before regenerating,
  or choose a different folder for a new registration.

<br>

## License

- Original project code uses the [MIT License](https://github.com/engcang/leica_blk360_gt_map_generator/blob/main/LICENSE).
- [KISS-Matcher](https://github.com/mit-spark/kiss-matcher), [Nano-GICP](https://github.com/engcang/nano_gicp), and [Open3D](https://github.com/isl-org/Open3D) use the MIT License.
