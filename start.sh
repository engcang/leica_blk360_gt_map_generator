#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -f /etc/os-release ]]; then
    echo 'This launcher supports Ubuntu 22.04/24.04 on x86_64.' >&2
    exit 1
fi
source /etc/os-release
if [[ "$ID" != ubuntu || ! "$VERSION_ID" =~ ^(22\.04|24\.04)$ || "$(uname -m)" != x86_64 ]]; then
    echo 'This launcher supports Ubuntu 22.04/24.04 on x86_64.' >&2
    exit 1
fi

packages=(build-essential git python3 python3-venv python3-dev libgl1 libgomp1 xdg-utils ca-certificates)
missing=()
for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
        missing+=("$package")
    fi
done
if (( ${#missing[@]} )); then
    echo 'Installing required Ubuntu packages. Ubuntu may ask for your sudo password.'
    sudo apt-get update
    sudo apt-get install -y "${missing[@]}"
fi
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
exec .venv/bin/python scripts/setup.py "$@"
