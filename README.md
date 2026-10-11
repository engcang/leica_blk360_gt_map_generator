<h1 align="center">BLK Studio</h1>

+ Convert supported BLK360 G1 scans to full-resolution PLY through a local web app.
+ Register overlapping scans, review alignment, and generate maps or shareable 3D HTML when needed.
+ Original files stay in place. C++17 core, no ROS, MIT license.

<br>

## Dependencies

- **Platforms:** Windows and Ubuntu.
- **Build:** CMake 3.24+, Ninja, and a C/C++17 compiler with OpenMP: GCC on Ubuntu or LLVM-MinGW on Windows.
- **Viewing:** a WebGL-capable browser.
- **Registration and map generation:** 64-bit Python 3.10–3.13, the locally built KISS-Matcher / Nano-GICP engine, and [Python packages](mapping/requirements.txt). Use **Registration setup → Check runtime** or **Install packages** after installing Python. Conversion/viewing do not require Python.
- Initial dependency downloads need internet access; processing runs locally at `127.0.0.1`.

Windows has been tested locally. Ubuntu has a CI build configuration; local runtime validation is still pending.

<br>

## Build and run from source

Download the source and open a terminal in the `blk2ply` project root.
Users build the application locally. Executables, DLLs, downloaded dependencies,
scan data, and build/install folders are excluded from the source repository by
[.gitignore](.gitignore). CMake downloads checksum-verified dependencies as needed.

**Windows:** install LLVM-MinGW, CMake, and Ninja; add their `bin` directories to
`PATH`. In PowerShell, select the compiler before running the build commands:

```powershell
$env:CC = "clang"
$env:CXX = "clang++"
$env:RC = "windres"
```

**Ubuntu:** install CMake, Ninja, and GCC with OpenMP support.
MSVC is not supported for the registration engine.

**Both platforms:** build the registration engine first, then the app.
Skip the first three commands for conversion/viewing only:

```sh
cmake -S mapping/native -B build/native -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/native --config Release --parallel 4
cmake --install build/native --config Release --prefix .
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --parallel
ctest --test-dir build -C Release --output-on-failure
cmake --install build --config Release --prefix install
```

Run `install/blk_studio` on Ubuntu or the locally built `install/blk_studio.exe`
on Windows. Keep the generated `install/` contents together.
Ubuntu desktop opening requires `xdg-open`.
Use **Quit app** to stop the process; closing the browser tab leaves it running.

<br>

## Usage

### Convert and view

1. Click **Select files** or **Select folder** to add BLK scans.
2. Choose an **Output folder**, then click **Convert scans**.
3. Each scan becomes one full-resolution file: `Setup26.blk → Setup26.ply`.
4. Click **View 3D**, or use **Open PLY files** to view existing results without reconverting.

Color is always **Intensity**. Display density is **1/10**, **1/5**, **1/2**, or **Full**;
navigation temporarily draws at most 2,000,000 points. Left-drag orbits,
right-drag/Shift-drag pans, the wheel zooms, and double-click sets the orbit target.
Display settings do not change the PLY. Existing files are never overwritten.

### Register scans

1. Add overlapping, individual PLY scans with **Use converted scans**, **Add PLY files**, or **Add to map**.
2. Confirm acquisition order with **Up / Down**. The first scan defines the map frame.
3. Choose a product base name and output folder. Use a separate folder for each registration result.
4. Check the Python environment under **Registration setup**, then click **Register scans**.
5. Open **Review and manage** to inspect quality metrics, scan positions, and inferred-level images.

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

To reopen a result, choose **Open registration result → relative_poses.json**.
Working caches live under the app's `projects/default/registration_records/`;
completed result viewing and generation do not require those caches.

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
for other overlapping pairs; ambiguous or unverified connections are not forced.

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
- **Disconnected groups:** inspect overlap and acquisition order. Optional pair hints use row numbers such as `1:4,2:5`; **Resume** reuses completed caches.
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
