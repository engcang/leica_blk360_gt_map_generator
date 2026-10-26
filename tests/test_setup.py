"""Checks for local setup failure reporting and rebuild decisions."""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from setup import initializeProject, runCommand, sourceFingerprint, synchronizeBuildRuntime


class SetupTests(unittest.TestCase):
    def testBuildAppReceivesHelperAndLibrariesWithoutRewritingUnchangedFiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            install = root / "install"
            app = root / "build" / "local" / "app"
            source = install / "mapping" / "bin"
            source.mkdir(parents=True)
            for name in ("blk_registration.exe", "libomp.dll", "libtbb12.dll"):
                (source / name).write_bytes(name.encode())
            synchronizeBuildRuntime(install, app)
            for path in source.iterdir():
                self.assertEqual((app / "mapping" / "bin" / path.name).read_bytes(), path.read_bytes())
            with mock.patch("setup.shutil.copy2", side_effect=AssertionError("Unchanged runtime must not be overwritten")):
                synchronizeBuildRuntime(install, app)
            (source / "blk_registration.exe").write_bytes(b"updated engine")
            synchronizeBuildRuntime(install, app)
            self.assertEqual((app / "mapping" / "bin" / "blk_registration.exe").read_bytes(), b"updated engine")

    def testValidatedRuntimeIsReadyWithoutReplacingExistingJobs(self):
        with tempfile.TemporaryDirectory() as directory:
            install = pathlib.Path(directory)
            runtime = {"status": "completed", "python": "local/python", "native_backend": {"protocol": 1}}
            initializeProject(install, runtime)
            path = install / "projects" / "default" / "registration_jobs.json"
            state = json.loads(path.read_text())
            self.assertEqual(state["runtime"], runtime)
            state["jobs"] = [{"id": "existing-result"}]
            path.write_text(json.dumps(state))
            initializeProject(install, runtime)
            self.assertEqual(json.loads(path.read_text())["jobs"], state["jobs"])

    def testRuntimeArtifactsDoNotInvalidateBuild(self):
        with tempfile.TemporaryDirectory(prefix="setup with spaces ") as directory:
            root = pathlib.Path(directory)
            (root / "web").mkdir()
            source = root / "web" / "app.js"
            source.write_text("initial source\n")
            initial = sourceFingerprint(root)
            (root / "build").mkdir()
            (root / "build" / "setup.log").write_text("generated log\n")
            (root / "third_party").mkdir()
            (root / "third_party" / "download.hpp").write_text("downloaded dependency\n")
            self.assertEqual(initial, sourceFingerprint(root))
            source.write_text("updated source\n")
            self.assertNotEqual(initial, sourceFingerprint(root))

    def testFailureKeepsDiagnosticAndStops(self):
        with tempfile.TemporaryDirectory(prefix="setup with spaces ") as directory:
            root = pathlib.Path(directory)
            with (root / "setup.log").open("w", encoding="utf-8") as log:
                with self.assertRaisesRegex(RuntimeError, "missing test dependency"):
                    runCommand([sys.executable, "-c", "import sys; print('missing test dependency'); sys.exit(7)"], root, log)
            self.assertIn("missing test dependency", (root / "setup.log").read_text())

    def testSubmoduleSourcesAndConfigurationInvalidateBuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for relative in (".gitmodules", "third_party/kiss-matcher/cpp/kiss_matcher/core/kiss_matcher/KISSMatcher.cpp",
                             "third_party/nano_gicp/src/nano_gicp.cc", "third_party/nano_gicp/include/nano_gicp/nano_gicp.hpp"):
                before = sourceFingerprint(root)
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("initial source\n")
                initial = sourceFingerprint(root)
                self.assertNotEqual(before, initial)
                source.write_text("updated source\n")
                self.assertNotEqual(initial, sourceFingerprint(root))

    def testCommandArgumentsPreserveSpaces(self):
        with tempfile.TemporaryDirectory(prefix="setup with spaces ") as directory:
            root = pathlib.Path(directory)
            target = root / "result with spaces.txt"
            with (root / "setup.log").open("w", encoding="utf-8") as log:
                runCommand([sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ok')", target], root, log)
            self.assertEqual(target.read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
