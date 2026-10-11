"""Checks for local setup failure reporting and rebuild decisions."""

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from setup import initializeProject, runCommand, sourceFingerprint


class SetupTests(unittest.TestCase):
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

    def testCommandArgumentsPreserveSpaces(self):
        with tempfile.TemporaryDirectory(prefix="setup with spaces ") as directory:
            root = pathlib.Path(directory)
            target = root / "result with spaces.txt"
            with (root / "setup.log").open("w", encoding="utf-8") as log:
                runCommand([sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ok')", target], root, log)
            self.assertEqual(target.read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
