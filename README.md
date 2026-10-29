# BLK Studio — Mac manual placement

- **`mac` branch:** load PLY scans, arrange them manually, and save `initial_placements.json`.
- **Platform:** macOS 12 or later; Apple silicon and Intel.
- **Full application:** [Windows / Ubuntu version](https://github.com/engcang/leica_blk360_gt_map_generator/tree/main).
  - Automatic registration, BLK conversion, and trajectory generation run in the full application.

## Start

- **Download:** clone this branch, or download its source ZIP and extract it.
  - Registration submodules are not needed for manual placement.

  ```sh
  git clone --branch mac https://github.com/engcang/leica_blk360_gt_map_generator.git
  cd leica_blk360_gt_map_generator
  ./Start-BLK-Studio-Mac.command
  ```

- **Finder:** double-click `Start-BLK-Studio-Mac.command`.
  - If the extracted file is not executable, run `bash Start-BLK-Studio-Mac.command` in Terminal.
- **First launch:** install Apple Command Line Tools if prompted, then launch again.
  - Downloads verified CMake and C++ dependencies; compiles the app locally.
  - Requires internet for the initial downloads; subsequent launches reuse the build.
  - No Homebrew, Python, Open3D, or registration runtime required.
- **RGB PLYs:** `./Start-BLK-Studio-Mac.command --rgb`.
  - Default edition uses intensity; RGB edition preserves color in the preview.
- **Build without launching:** `./Start-BLK-Studio-Mac.command --no-launch`.
- **Launch after building:** `./install-mac/blk_studio` or `./install-mac/blk_studio_rgb`.
  - Browser opens automatically; keep the app process running while editing.
  - Setup log: `build/mac-setup.log`.

## Arrange and save

- **1. Select scans:** in **Register**, choose **PLY files** or **PLY folder**.
  - Choose at least two scans; binary little-endian XYZ PLYs supported.
  - Original PLY files stay unchanged.
- **2. Choose output:** select the folder for `initial_placements.json`.
  - Existing placements in that folder load automatically.
- **3. Arrange:** click **Arrange PLY scans**.
  - **Moving item:** select the scan to position.
  - **Fixed reference:** select the comparison scan; each saved choice is retained.
  - **Visualizing scans:** choose surrounding scans; Shift-click selects a range.
  - Adjust XYZ and yaw, roll, pitch; trim step defaults to **0.5**.
  - Visibility changes and Next preserve your viewpoint.
  - **Save now:** save immediately; edits also autosave.
  - **Recovery copy:** export current edits if saving fails.
- **Reopen:** select the same scans and output folder, then open the editor.
- **Use on Windows / Ubuntu:** copy the PLYs and placement JSON together, retaining their relative folder layout.
  - Select those PLYs and the folder containing `initial_placements.json` in the full app's Register tab.
  - JSON stores relative paths, numerical poses, and fixed-reference choices.
  - Pose format: `[x, y, z, yaw]` or `[x, y, z, yaw, roll, pitch]`.
    - Metres and radians; rotation: `Rz(yaw) × Ry(pitch) × Rx(roll)`.

## License

- Original project code uses the [MIT License](LICENSE).
- Bundled dependency licenses are installed in `install-mac/licenses/`.
