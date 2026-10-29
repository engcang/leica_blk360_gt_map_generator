#!/bin/bash
# Manual PLY placement on macOS; no Python or registration runtime required.
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

if [[ "$(uname -s)" != Darwin ]]; then
    echo 'This launcher requires macOS. Use start.sh on Linux.' >&2
    exit 1
fi

launch=1
rgb=0
jobs=2
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-launch) launch=0; shift ;;
        --rgb) rgb=1; shift ;;
        --jobs)
            [[ $# -ge 2 && "$2" =~ ^[1-9][0-9]*$ ]] || { echo '--jobs needs a positive integer.' >&2; exit 2; }
            jobs="$2"; shift 2 ;;
        --help|-h)
            echo 'Usage: ./Start-BLK-Studio-Mac.command [--rgb] [--no-launch] [--jobs N]'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if ! /usr/bin/xcrun --find clang++ >/dev/null 2>&1; then
    echo 'Install Apple Command Line Tools in the dialog, then run this launcher again.' >&2
    /usr/bin/xcode-select --install || true
    exit 1
fi
case "$(uname -m)" in arm64|x86_64) ;; *) echo 'Requires Apple silicon or an Intel Mac.' >&2; exit 1 ;; esac

cmake_version=3.31.10
cmake_archive="cmake-${cmake_version}-macos-universal.tar.gz"
tool_dir="$PWD/.build-tools"
cmake="$tool_dir/cmake-${cmake_version}-macos-universal/CMake.app/Contents/bin/cmake"
ctest="$(dirname "$cmake")/ctest"
mkdir -p "$tool_dir" build
if [[ ! -x "$cmake" ]]; then
    echo 'Downloading CMake (first launch only)…'
    /usr/bin/curl --fail --location --retry 3 --connect-timeout 30 \
        "https://github.com/Kitware/CMake/releases/download/v${cmake_version}/${cmake_archive}" \
        --output "$tool_dir/${cmake_archive}.pending"
    expected=be9f3faeeaf7921cc2d77cea711dd5e6f72c63af2810cacd9205b3ce8d1593c9
    actual=$(/usr/bin/shasum -a 256 "$tool_dir/${cmake_archive}.pending")
    if [[ "${actual%% *}" != "$expected" ]]; then
        rm -f "$tool_dir/${cmake_archive}.pending"
        echo 'CMake download checksum mismatch. Please run the launcher again.' >&2
        exit 1
    fi
    /usr/bin/tar -xzf "$tool_dir/${cmake_archive}.pending" -C "$tool_dir"
    rm -f "$tool_dir/${cmake_archive}.pending"
fi

log="$PWD/build/mac-setup.log"
trap 'code=$?; if [[ $code -ne 0 ]]; then echo "Setup failed. Log: $log" >&2; tail -n 50 "$log" >&2; fi' EXIT
: > "$log"
echo 'Building the manual placement app…'
"$cmake" -S . -B build/mac -G 'Unix Makefiles' \
    -DBLK_PLACEMENT_ONLY=ON -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=12.0 -DCMAKE_INSTALL_PREFIX="$PWD/install-mac" \
    -DBUILD_TESTING=ON >> "$log" 2>&1
"$cmake" --build build/mac --parallel "$jobs" >> "$log" 2>&1
"$ctest" --test-dir build/mac --output-on-failure >> "$log" 2>&1
"$cmake" --install build/mac >> "$log" 2>&1
trap - EXIT
echo 'Ready: install-mac/'
if [[ "$launch" == 1 ]]; then
    executable=blk_studio
    [[ "$rgb" == 0 ]] || executable=blk_studio_rgb
    exec "$PWD/install-mac/$executable"
fi
