"""Build and launch BLK Studio using a project-local Python environment."""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys


def runCommand(_arguments, _root, _log):
    _log.write("\n$ " + subprocess.list2cmdline([str(value) for value in _arguments]) + "\n")
    _log.flush()
    result = subprocess.run([str(value) for value in _arguments], cwd=_root, stdout=_log, stderr=subprocess.STDOUT)
    _log.flush()
    if result.returncode:
        detail = pathlib.Path(_log.name).read_text(encoding="utf-8", errors="replace").splitlines()[-35:]
        raise RuntimeError("\n".join(detail) + f"\nCommand failed ({result.returncode}). Full log: {_log.name}")


def sourceFingerprint(_root):
    patterns = ["CMakeLists.txt", "cmake/*", "blk_core/**/*.hpp", "blk_core/**/*.cpp",
                "mapping/*.py", "mapping/requirements.txt", "mapping/native/**/*.hpp", "mapping/native/**/*.cpp",
                "mapping/native/*.cmake", "mapping/native/*.json", "mapping/native/CMakeLists.txt",
                "web/*", "tests/core_tests.cpp", "scripts/setup*", "scripts/windows_tools.json",
                "LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt"]
    result = hashlib.sha256()
    result.update(str(_root).encode())
    for path in sorted({path for pattern in patterns for path in _root.glob(pattern) if path.is_file()}):
        result.update(path.relative_to(_root).as_posix().encode())
        result.update(path.read_bytes())
    return result.hexdigest()


def synchronizeBuildRuntime(_install, _build_app):
    source = _install / "mapping" / "bin"
    if not source.is_dir():
        raise FileNotFoundError("The installed registration engine is missing; rerun setup")
    destination = _build_app / "mapping" / "bin"
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        target = destination / path.relative_to(source)
        if target.is_file() and hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def initializeProject(_install, _runtime):
    if _runtime.get("status") != "completed":
        raise RuntimeError("Registration runtime validation did not complete")
    project = _install / "projects" / "default"
    project.mkdir(parents=True, exist_ok=True)
    try:
        with (project / "registration_jobs.json").open("x", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "python": _runtime["python"], "runtime": _runtime, "jobs": []}, stream, indent=2)
            stream.write("\n")
    except FileExistsError:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-launch", action="store_true", help="Build and validate without opening the app")
    parser.add_argument("--jobs", type=int, default=2, help="Parallel compiler jobs (default: 2)")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if sys.prefix == sys.base_prefix or not (3, 10) <= sys.version_info[:2] < (3, 14):
        parser.error("Use the platform launcher to prepare Python 3.10-3.13 in .venv")
    root = pathlib.Path(__file__).resolve().parents[1]
    build = root / "build" / "local"
    build.mkdir(parents=True, exist_ok=True)
    install = root / "install"
    python_bin = pathlib.Path(sys.executable).parent
    os.environ["PATH"] = str(python_bin) + os.pathsep + os.environ.get("PATH", "")
    os.environ["PIP_CACHE_DIR"] = str(root / ".build-tools" / "pip-cache")
    os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    suffix = ".exe" if os.name == "nt" else ""
    cmake = python_bin / ("cmake" + suffix)
    ctest = python_bin / ("ctest" + suffix)
    app = install / ("blk_studio" + suffix)
    helper = install / "mapping" / "bin" / ("blk_registration" + suffix)
    stamp = build / "ready.sha256"
    fingerprint = sourceFingerprint(root)
    current = stamp.is_file() and stamp.read_text() == fingerprint and app.is_file() and helper.is_file()
    log_path = root / "build" / "setup.log"
    with log_path.open("a", encoding="utf-8") as log:
        if not current:
            stamp.unlink(missing_ok=True)
            print("Installing local build tools and Python packages...", flush=True)
            runCommand([sys.executable, "-m", "pip", "install", "--no-compile", "cmake==3.31.10", "ninja==1.13.0",
                        "-r", root / "mapping" / "requirements.txt"], root, log)
            compiler_options = []
            if os.name == "nt":
                for variable, setting in (("CC", "CMAKE_C_COMPILER"), ("CXX", "CMAKE_CXX_COMPILER"), ("RC", "CMAKE_RC_COMPILER")):
                    if not os.environ.get(variable):
                        raise RuntimeError("Use Start-BLK-Studio.bat to prepare the Windows compiler")
                    compiler_options.append(f"-D{setting}={pathlib.Path(os.environ[variable]).as_posix()}")
            for source, output, label in ((root, build / "app", "converter and app"),
                                          (root / "mapping" / "native", build / "native", "registration engine")):
                print(f"Building {label}. First setup may take several minutes. Log: {log_path}", flush=True)
                fresh_options = [] if (output / "build.ninja").is_file() else ["--fresh"]
                runCommand([cmake, "-S", source, "-B", output, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
                            *compiler_options, *fresh_options], root, log)
                runCommand([cmake, "--build", output, "--parallel", args.jobs], root, log)
                if source == root:
                    runCommand([ctest, "--test-dir", output, "--output-on-failure"], root, log)
                runCommand([cmake, "--install", output, "--prefix", install], root, log)
        stamp.unlink(missing_ok=True)
        synchronizeBuildRuntime(install, build / "app")
        print("Checking registration runtime...", flush=True)
        runCommand([helper, "--version"], root, log)
        runCommand([sys.executable, install / "mapping" / "studio_worker.py", "--check", "--report", build / "runtime-check"], root, log)
        runtime = json.loads((build / "runtime-check" / "progress.json").read_text(encoding="utf-8"))
        initializeProject(install, runtime)
        initializeProject(build / "app", runtime)
        stamp.write_text(fingerprint, encoding="ascii")
    print("BLK Studio is ready.", flush=True)
    if not args.no_launch:
        subprocess.Popen([str(app)], cwd=install, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        print("The app opens in your browser. Use Quit app to stop it.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
