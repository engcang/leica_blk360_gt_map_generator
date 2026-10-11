"""Preserve useful runtime errors and avoid pip installs for missing native engines."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mapping"))
from studio_worker import setupRuntime


class RuntimeTests(unittest.TestCase):
    def testMissingNativeEngineDoesNotInstallPythonPackages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            with mock.patch("native_backend.backendInfo", side_effect=FileNotFoundError("Native engine is missing")):
                with mock.patch("studio_worker.subprocess.run") as run:
                    with self.assertRaisesRegex(FileNotFoundError, "Native engine is missing"):
                        setupRuntime(root / "runtime", root)
                    run.assert_not_called()

    def testChildFailureKeepsOriginalDiagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            environment = root / "runtime"
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()

            def run(_arguments, **_options):
                if "--check" in _arguments:
                    (root / "progress.json").write_text(json.dumps({"status": "failed", "stage": "Native library could not be loaded"}))
                    return subprocess.CompletedProcess(_arguments, 1)
                return subprocess.CompletedProcess(_arguments, 0)

            with mock.patch("native_backend.backendInfo", return_value={"protocol": 1}):
                with mock.patch("studio_worker.subprocess.run", side_effect=run):
                    with self.assertRaisesRegex(RuntimeError, "Native library could not be loaded"):
                        setupRuntime(environment, root)


if __name__ == "__main__":
    unittest.main()
