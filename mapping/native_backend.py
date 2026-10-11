"""Versioned, ROS-independent KISS-Matcher and Nano-GICP process adapter."""

import functools
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile

PIPELINE_ID = "kiss-1.0.2_nano-b21e79e_refine-1cm_v1"


def executablePath():
    default = pathlib.Path(__file__).with_name("bin") / ("blk_registration.exe" if os.name == "nt" else "blk_registration")
    path = pathlib.Path(os.environ.get("BLK_REGISTRATION_EXECUTABLE", default)).resolve()
    if not path.is_file():
        raise FileNotFoundError("KISS-Matcher/Nano-GICP helper is missing. Run Start-BLK-Studio.bat on Windows or bash start.sh on Ubuntu to build it.")
    return path


@functools.lru_cache(maxsize=1)
def backendInfo():
    path = executablePath()
    try:
        result = subprocess.run([str(path), "--version"], check=True, capture_output=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Cannot start the registration engine. Keep its runtime libraries beside the helper in mapping/bin and rerun the platform launcher.") from error
    info = json.loads(result.stdout)
    if info.get("protocol") != 1 or info.get("kiss_commit") != "e3440b63340af7414ebd0fdde04e40f02ff4e7d2" or info.get("nano_gicp_commit") != "b21e79edcceb7c6e86ec0dfec90f451b796b683a":
        raise ValueError("Registration helper version does not match this pipeline")
    info["executable_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return info


def align(_method, _source, _target, **_options):
    backendInfo()
    with tempfile.TemporaryDirectory(prefix="native-", dir=_source.parent) as directory:
        root = pathlib.Path(directory)
        request = root / "request.json"
        result = root / "result.json"
        request.write_text(json.dumps({"method": _method, "source": str(_source), "target": str(_target),
                                       "threads": 8, **_options}, allow_nan=False), encoding="utf-8")
        process = subprocess.run([str(executablePath()), str(request), str(result)],
                                 capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            detail = (process.stderr or process.stdout).decode("utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"Native {_method} failed ({process.returncode}): {detail}")
        return json.loads(result.read_text(encoding="utf-8"))
