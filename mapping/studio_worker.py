"""Local registration worker launched and monitored by BLK Studio."""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import traceback


def saveJson(_path, _value):
    pending = _path.with_suffix(_path.suffix + ".pending")
    pending.write_text(json.dumps(_value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pending.replace(_path)


def parsePairs(_text, _count):
    pairs = set()
    for item in _text.split(","):
        if not item.strip():
            continue
        try:
            i, j = [int(value.strip()) - 1 for value in item.split(":")]
        except ValueError as error:
            raise ValueError("Use row numbers such as 1:4,2:5 for additional pairs") from error
        if not 0 <= i < j < _count:
            raise ValueError(f"Invalid pair {item}: use two different row numbers in ascending order")
        pairs.add((i, j))
    return sorted(pairs)


def connectedGroups(_edges, _count):
    groups = [{i} for i in range(_count)]
    for edge in _edges:
        if not edge["accepted"]:
            continue
        first = next(group for group in groups if edge["source"] in group)
        second = next(group for group in groups if edge["target"] in group)
        if first is not second:
            first.update(second)
            groups.remove(second)
    return [sorted(index + 1 for index in group) for group in groups]


def runRegistration(_config, _report):
    import register_scans as registration

    paths = [pathlib.Path(value).resolve(strict=True) for value in _config["paths"]]
    if len(paths) < 2 or len(set(paths)) != len(paths):
        raise ValueError("Select at least two different PLY scans")
    voxel = float(_config["voxel_m"])
    if voxel != 0.01:
        raise ValueError("This registration pipeline refines and evaluates on 1 cm clouds")
    pairs = parsePairs(_config.get("additional_pairs", ""), len(paths))
    target = pathlib.Path(_config["output_path"])
    from result_layout import reviewForRecords
    poses_target = pathlib.Path(_config["poses_path"]) if _config.get("poses_path") else target.parent / "relative_poses.json"
    review = pathlib.Path(_config["review_directory"]) if _config.get("review_directory") else reviewForRecords(_report)
    if poses_target.exists():
        raise FileExistsError(f"Transform JSON already exists: {poses_target}")

    def progress(_stage, **_extra):
        saveJson(_report / "progress.json", {"status": "running", "stage": _stage, **_extra})
        print(_stage, flush=True)

    progress("Preparing KISS-Matcher clouds (30 cm) and Nano-GICP clouds (8 cm)")
    records = registration.prepare([{"output_path": str(path)} for path in paths], _report)
    clouds = registration.loadClouds(_report, len(records))
    progress("Registering consecutive scans")
    edges = registration.adjacent(clouds, records, _report)
    for i, j in pairs:
        progress(f"Checking additional connection {i + 1}:{j + 1}")
        path = _report / "pairs" / f"{i:02d}_{j:02d}.json"
        if path.exists():
            edge = json.loads(path.read_text(encoding="utf-8"))
        else:
            edge = registration.registerPair(clouds[i], clouds[j], i, j)
            registration.saveJson(path, edge)
        edges.append(edge)
        print(f"Additional pair {i + 1}:{j + 1}: accepted {edge['accepted']}, overlap {edge['harmonic_overlap_12cm']:.3f}", flush=True)
    from automatic_connections import discoverConnections
    edges = discoverConnections(clouds, edges, _report, progress)
    groups = connectedGroups(edges, len(records))
    if len(groups) > 1:
        saveJson(_report / "progress.json", {"status": "needs_connections",
                                             "stage": "Automatic overlap search could not verify a complete connection. Review the scan coverage and connection report before resuming.",
                                             "groups": groups,
                                             "rejected_pairs": [[edge["source"] + 1, edge["target"] + 1] for edge in edges if not edge["accepted"]]})
        return
    progress("Finding overlap constraints and optimizing scan poses")
    result = registration.optimize(clouds, records, _report)
    del clouds
    progress("Refining point-to-plane registration on 1 cm clouds")
    result = registration.refineGraph(result, _report, progress)
    from result_products import verifySources
    verifySources(result, progress)
    progress("Creating scan position and inferred-level review images")
    from review_registration import createReview
    createReview(result, _report, review)
    from export_dataset import saveRegistrationPoses
    poses_path = saveRegistrationPoses(result, poses_target, _config.get("name", "reference_map"))
    saveJson(_report / "progress.json", {"status": "completed", "stage": "Poses and review ready; generate merged products when needed",
                                         "output_path": str(target),
                                         "poses_path": str(poses_path), "groups": groups,
                                         "retained_constraints": sum(edge["retained"] for edge in result["constraints"]),
                                         "review_available": True, "review_path": str(review / "result_review_and_manage.html"),
                                         "independently_validated": False})


def checkRuntime(_report):
    import numpy
    import open3d
    import matplotlib
    import scipy
    from native_backend import backendInfo

    if open3d.__version__ != "0.20.0":
        raise ValueError("BLK Studio registration requires Open3D 0.20.0")
    saveJson(_report / "progress.json", {"status": "completed", "stage": "Registration runtime ready",
                                         "python": sys.executable, "open3d": open3d.__version__,
                                         "numpy": numpy.__version__, "matplotlib": matplotlib.__version__,
                                         "scipy": scipy.__version__, "native_backend": backendInfo()})


def setupRuntime(_destination, _report):
    from native_backend import backendInfo

    backendInfo()
    if not (3, 10) <= sys.version_info[:2] <= (3, 13):
        raise ValueError("Install 64-bit Python 3.10-3.13, then select its executable")
    saveJson(_report / "progress.json", {"status": "running", "stage": "Creating a private registration environment"})
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    python = _destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(_destination)],
                       check=True, creationflags=flags, stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)
    subprocess.run([str(python), "-m", "ensurepip", "--upgrade"],
                   check=True, creationflags=flags, stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)
    saveJson(_report / "progress.json", {"status": "running", "stage": "Installing registration packages from PyPI (internet required)"})
    subprocess.run([str(python), "-u", "-m", "pip", "install", "--disable-pip-version-check", "-r",
                    str(pathlib.Path(__file__).with_name("requirements.txt"))],
                   check=True, creationflags=flags, stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)
    saveJson(_report / "progress.json", {"status": "running", "stage": "Checking the installed registration runtime"})
    result = subprocess.run([str(python), "-u", str(pathlib.Path(__file__).resolve()), "--check", "--report", str(_report)],
                            creationflags=flags, stdout=sys.stdout, stderr=sys.stderr, stdin=subprocess.DEVNULL)
    if result.returncode:
        progress = json.loads((_report / "progress.json").read_text(encoding="utf-8"))
        detail = progress.get("stage") if progress.get("status") == "failed" else "Registration runtime check failed; see the worker log"
        raise RuntimeError(detail)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", type=pathlib.Path)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--setup", type=pathlib.Path)
    mode.add_argument("--export", type=pathlib.Path, help="Export the registered map and one transform JSON")
    mode.add_argument("--generate", type=pathlib.Path, help="Generate an optional product from relative_poses.json")
    parser.add_argument("--product", choices=["raw", "voxel", "share"])
    parser.add_argument("--destination", type=pathlib.Path)
    parser.add_argument("--report", required=True, type=pathlib.Path)
    args = parser.parse_args()
    args.report.mkdir(parents=True, exist_ok=True)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        if args.generate:
            from result_products import generateProduct
            result = generateProduct(args.generate, args.product, args.report,
                                     lambda _stage: saveJson(args.report / "progress.json", {"status": "running", "stage": _stage}))
            saveJson(args.report / "progress.json", result)
        elif args.export:
            from export_dataset import exportDataset
            if not args.destination:
                raise ValueError("Choose an export destination")
            result = exportDataset(args.export, args.destination,
                                   lambda _stage: saveJson(args.report / "progress.json", {"status": "running", "stage": _stage}))
            saveJson(args.report / "progress.json", result)
        elif args.check:
            checkRuntime(args.report)
        elif args.setup:
            setupRuntime(args.setup, args.report)
        else:
            runRegistration(json.loads(args.run.read_text(encoding="utf-8")), args.report)
    except Exception as error:
        traceback.print_exc()
        saveJson(args.report / "progress.json", {"status": "failed", "stage": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
