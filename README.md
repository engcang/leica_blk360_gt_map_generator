<h1 align="center">BLK Studio</h1>

+ Convert supported BLK360 G1 scans to full-resolution PLY through a local web app.
+ Register overlapping scans, review alignment, and generate maps or shareable 3D HTML when needed.
+ Original files stay in place. C++17 core, no ROS, MIT license.

<br>

## Install and run

Download or clone this source repository into a writable folder.

- **Windows 10/11 x64:** double-click **`Start-BLK-Studio.bat`**.
- **Ubuntu 22.04/24.04 x86_64:** open a terminal in this folder and run **`bash start.sh`**.
  Ubuntu asks for your sudo password only if required system packages are missing.

The launcher downloads required tools, prepares a local Python environment, builds
both the app and registration engine, checks the runtime, and opens your browser.
The first run needs internet access, several GB of free disk space, and time for
compilation. Later runs reuse the installation; source updates trigger a rebuild.
No manual CMake commands or Python package installation are needed.

Tools, Python packages, dependencies, and compiled files stay in ignored local
folders (`.build-tools/`, `.venv/`, `third_party/`, `build/`, and `install/`).
Windows setup does not require administrator rights or modify the system PATH.
Keep these folders with the project; Python environments contain absolute paths.

Use the same launcher every time. Processing runs locally at `127.0.0.1`.
Use **Quit app** to stop the process; closing the browser tab leaves it running.
If setup fails, the launcher reports the error; build details are in `build/setup.log`.
To build without opening the app, use `Start-BLK-Studio.bat -NoLaunch` or
`bash start.sh --no-launch`. The default is two compiler jobs to limit memory use.

No GitHub Actions workflows are included. Cloning, pushing, and starting the app
do not trigger builds on GitHub.

The Windows launcher has been validated from a fresh source checkout.
Ubuntu runtime validation is still pending.

<br>

## Usage

The main screen has **Convert** (default), **View saved point clouds**, **Register**, and **Additional Register** tabs.
PLY viewing and registration input selections are not saved or restored; choose
files or a folder each time. Viewing reads files in place without creating a library cache.
All scan input tabs select files by modification time, oldest first (path breaks ties).
New conversions preserve the BLK modification time on the PLY. Existing PLY timestamps
are unchanged. Modification time reflects scan order only if the source timestamps do.
Adding registration inputs re-sorts the selection; **Up / Down** allows manual correction.

### Convert and view

1. Click **Select files** or **Select folder** to add BLK scans.
2. Choose an **Output folder**, then click **Convert scans**.
3. Each scan becomes one full-resolution file: `Setup26.blk → Setup26.ply`.
4. Click **View 3D**, or open **View saved point clouds** and choose **Open PLY files** / **Open folder**.

Color is always **Intensity**. Display density is **1/10**, **1/5**, **1/2**, or **Full** (default);
navigation temporarily draws at most 2,000,000 points. Left-drag orbits around
the surface near the screen center, or looks around in place when no surface is
found. The pivot updates at drag start without moving the camera, and stays fixed
during the drag. Right-/middle-drag or Shift-drag pans at a stable speed based on
the visible surfaces, independently of the orbit pivot; empty views retain the last
usable pan speed. The wheel moves forward/back;
Shift+wheel moves more slowly. Rotation is slower for finer control.
Display settings do not change the PLY. Existing files are never overwritten.
Completed conversions are shown only during the current app session, not saved as history.
If an output PLY is removed, its row returns to **Ready** when the source BLK is available;
click **Convert scans** to recreate it. Pending/interrupted work is retained for recovery.

### Register scans

1. Add overlapping, individual PLY scans with **Add PLY files**, **Add folder**, or **Add to map**.
2. Confirm acquisition order with **Up / Down**. The first scan defines the map frame.
3. Confirm the product base name and output folder (defaults to the first selected PLY folder). Use a separate folder for each registration result.
4. Check the Python environment under **Registration setup**, then click **Register scans**.
5. Below the always-open **Latest worker log**, open **Review and manage** to inspect quality metrics, scan positions, and inferred-level images.

**Optional initial placement:** after selecting PLYs, click **Arrange PLY scans** to open the editor directly. Placements save beside the selected PLYs unless you choose another output folder.
Switch **Move view / Place scan / chunk** to choose the left-drag action (view movement is the default).
In placement mode, select XY, X, Y, Z, or yaw, or enter numeric values.
The first item is fixed; **Next** uses the previous item as a reference. Right/middle
drag always pans the view. **Height cut** hides points above a shared Z height after
placement transforms; **Show all** restores them without changing registration inputs.
Only the moving item and reference are loaded, at **1/10** display density with **Axes on** by default in placement editors (scans, chunks, and Additional Register). Other 3D viewers default to Full density with grid and axes off.
Edits automatically save to `initial_placements.json` in the output folder. No Apply
button is needed: Register reads the saved poses before starting. Wait for the saved
status before closing the editor; keep it open if saving fails.
Removing, adding, or reordering scans preserves the other poses. To recover after
closing the app, select the PLYs and output folder; saved placements restore automatically.
Registration uses the saved poses for selected scans and refuses to silently ignore them.
Scans without saved poses use automatic registration. Placements are scoped to the
selected output folder; changing folders does not mix arrangements. Registration
checks the saved poses before starting and stops if they cannot be read or the
selection changes during that check.

For disconnected results, **Arrange chunks** autosaves `chunk_initial_placements.json`.
Return to **Register** and click **Register chunks**. Each chunk is one rigid body:
merged-cloud Nano-GICP, robust 1 cm ICP, then **chunk-level PGO**. The saved chunk
transform initializes matching; unplaced chunks use KISS-Matcher. Internal scan
transforms remain fixed, and final scan poses are composed from the chunk pose and
the saved scan-local pose. There is no consecutive-scan search or scan-level ICP/PGO
in this mode. Each bridge is checked on at most 12 original scan pairs at fixed
poses (no scan registration); two distinct passing pairs are required, or one for
two single-scan chunks. Quality metrics evaluate final fixed poses on 1 cm scans.
**Refine individual scans afterward** is unchecked by default; enabling it explicitly
allows the slower scan-level 1 cm ICP/PGO to change internal chunk geometry.
The editor only autosaves and never launches registration. Changed chunk membership
or geometry regenerates the placement JSON; previous user placements are preserved
under `html_source/placement_history/`. An unchanged chunk set keeps its placements.

### Extend an existing map

1. In **Additional Register**, open the existing `relative_poses.json`. Keep its original PLY files at the paths recorded in the JSON.
2. Add new PLY files or a folder, and choose a **separate output folder**.
3. Optionally **Arrange reference and scans**. The reference is one fixed item;
   new scan poses autosave to `additional_initial_placements.json`, keyed by source
   path and reference hash. Removing a scan does not erase its saved pose.
4. Click **Register additional scans**. Watch the log and open the result below it.

The reference stays rigid and its original transforms/map frame are preserved exactly.
New scans use saved guesses or KISS-Matcher, then Nano-GICP, robust 1 cm ICP and
chunk-level PGO. Small scans are matched into the larger map; fixed original scan
pairs validate each bridge (one passing pair for a single new scan, otherwise two).
Disconnected groups remain available through **View chunks / Arrange chunks** and
**Register chunks**. Individual-scan refinement is disabled for this workflow.
The new JSON includes all old and new scans without copying their PLYs. Original
results are unchanged. Generate merged PLYs or share HTML only when needed.

After conversion and registration, the output is:

```text
output/
    Setup26.ply
    Setup27.ply
    ...
    relative_poses.json
    result_review_and_manage.html
    html_source/
```

Original PLYs remain where selected; they are not copied if stored elsewhere.
`relative_poses.json` preserves source paths, hashes, and each scan's rigid transform
into the first scan/map frame: `p_map = T_map_scan @ [x, y, z, 1]`, in metres.
Keep the originals and preserve their paths relative to the JSON when moving a dataset.

Tabs start with empty selections and output paths. Only active jobs are shown automatically.
To reopen a result, choose **Open existing result → relative_poses.json**
(or **partial_chunks.json** for a previous disconnected job).
Working caches live under the app's `projects/default/registration_records/`;
completed result viewing and generation do not require those caches.
Fine ICP saves completed pairs for **Resume** and reuses their final correspondences
for graph weights. Changed clouds, initial poses, or ICP settings invalidate a pair
checkpoint. Point density, robust loss, and convergence criteria stay unchanged.
Available CPU threads are detected automatically for the native engines, Open3D,
and quality queries. Fine ICP processes one pair at a time using all available
threads by default. Optional `BLK_CPU_THREADS` limits the CPU budget;
`BLK_REGISTRATION_WORKERS` enables memory-bounded concurrent pairs, which can be
slower when one pair already saturates CPU or memory bandwidth.

### Generate maps and share results

Registration saves poses and review materials. Merged products are generated only when requested:

| Button | Output with the default base name |
| --- | --- |
| **Generate 1 cm PLY** | `reference_map_1cm.ply`: voxel-averaged map for matching and distribution |
| **Generate raw PLY** | `reference_map_raw.ply`: every original point transformed, with intensity preserved |
| **Generate share HTML** | `reference_map_1cm_share.html`: complete 1 cm map embedded in an offline 3D viewer |

Open the management page through **Review and manage** in BLK Studio to use its
generation buttons. Opening the HTML directly shows saved files and offline review;
generation buttons explain how to connect to the app. Use **Check output folder**
to verify saved filenames and sizes after moving or deleting files, and
keep `html_source/` beside it. Colleagues need only the generated **share HTML**
and a browser—no app, Python, or separate PLY.

Generation uses saved poses without re-registering. Share HTML reuses a verified
1 cm map when available; otherwise its intermediate map is temporary.
Retain original PLYs plus `relative_poses.json` for reproducibility.

<br>

## Registration and quality

**Pipeline:** KISS-Matcher (30 cm) → Nano-GICP (30 cm, then 8 cm) → pose-graph
optimization → robust point-to-plane ICP (1 cm clouds, 3 cm gate, 1 cm Tukey scale)
→ second pose-graph optimization. Disconnected groups trigger automatic searches
for other overlapping pairs, followed by KISS-Matcher / Nano-GICP on merged chunks.
A chunk bridge needs at least two consistent original scan-pair verifications
(within 10 cm / 1 degree), then follows the same 1 cm refinement and pose-graph pipeline.
Ambiguous or unverified connections are not forced.

**Evaluation:** fixed final poses on **1 cm per-scan clouds**, at **1/2/3/5 cm**
distance thresholds (3 cm default). Distances at or above the threshold are excluded.
The review reports bidirectional inlier Chamfer, directional mean/RMSE/p95,
correspondence ratios/counts, and point-to-plane residuals. Read errors together
with coverage; these diagnostics do not change registration.

The decoder and maps are experimental: official-export comparison and independent
GT accuracy validation remain outstanding. Smaller voxels do not imply higher
accuracy. Scan-to-map trajectory estimation is not implemented.

<br>

## Troubleshooting

- **Missing file:** select a moved PLY again. Generation requires originals at the paths recorded in the poses JSON.
- **Disconnected groups:** click **View chunks** (or **Build chunk review** for an older job). `chunks_review.html`, `partial_chunks.json`, and `html_source/chunks/` show sampled 3D previews, scan positions, and original row numbers in separate chunk-local frames. These are diagnostic poses before final 1 cm refinement, not a common GT map. Use **Arrange chunks** to provide initial placements; **Resume** reuses caches and searches for chunk connections automatically. No final map/poses are published until all groups connect.
- **Slow viewer or large output:** lower display density for navigation. The full cloud still occupies memory; raw maps and embedded HTML can be large.
- **Existing output:** move/remove the generated file before regenerating, or choose a different folder for a new registration.

<br>

## Acknowledgements

Microsoft JPEG XR / jxrlib, cpp-httplib, nlohmann/json, PicoSHA2, KISS-Matcher,
Nano-GICP, ROBIN, PCL, Eigen, oneTBB, FLANN, and Open3D.
See [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) for versions and licenses.
Leica and BLK360 are trademarks of their owners. This is an independent project.

<br>

## License

Original project code uses the [MIT License](LICENSE).
Third-party components retain their original licenses and attribution.
