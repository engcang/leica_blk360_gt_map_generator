"""Exercise build-time submodule initialization using isolated local Git repositories."""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CMAKE = shutil.which("cmake") or str(ROOT / ".venv" / ("Scripts/cmake.exe" if os.name == "nt" else "bin/cmake"))


class SubmoduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="submodules with spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.env = dict(os.environ, GIT_ALLOW_PROTOCOL="file", GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull)

    def git(self, directory, *arguments):
        return subprocess.check_output(["git", "-C", str(directory), *arguments],
                                       env=self.env, text=True, stderr=subprocess.PIPE).strip()

    def repository(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-b", "main")
        self.git(path, "config", "user.name", "Submodule Test")
        self.git(path, "config", "user.email", "submodule@example.invalid")
        return path

    def checkout(self):
        upstream = self.repository("upstream")
        (upstream / "source.cpp").write_text("original\n")
        self.git(upstream, "add", ".")
        self.git(upstream, "commit", "-m", "original")
        pinned = self.git(upstream, "rev-parse", "HEAD")
        (upstream / "source.cpp").write_text("new version\n")
        self.git(upstream, "commit", "-am", "new version")
        newer = self.git(upstream, "rev-parse", "HEAD")
        parent = self.repository("parent")
        (parent / "cmake").mkdir()
        shutil.copy2(ROOT / "cmake/registration_submodules.cmake", parent / "cmake")
        manifest = {}
        for name, path in (("kiss", "third_party/kiss-matcher"), ("nano", "third_party/nano_gicp"), ("nanoflann", "third_party/nanoflann")):
            self.git(parent, "submodule", "add", str(upstream), path)
            self.git(parent / path, "checkout", "--detach", pinned)
            manifest[name] = {"submodule": path, "commit": pinned}
        (parent / "mapping/native").mkdir(parents=True)
        (parent / "mapping/native/dependencies.json").write_text(json.dumps(manifest))
        self.git(parent, "add", ".")
        self.git(parent, "commit", "-m", "pin submodules")
        checkout = self.root / "checkout"
        self.git(self.root, "clone", "--no-local", str(parent), str(checkout))
        return checkout, upstream, pinned, newer

    def initialize(self, checkout, succeeds=True):
        result = subprocess.run([CMAKE, "-P", str(checkout / "cmake/registration_submodules.cmake")],
                                env=self.env, capture_output=True, text=True, timeout=30)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def testOrdinaryCloneInitializesMissingSubmodulesAndReusesThemOffline(self):
        checkout, upstream, pinned, _ = self.checkout()
        self.assertFalse((checkout / "third_party/kiss-matcher/source.cpp").exists())
        self.initialize(checkout)
        for path in ("third_party/kiss-matcher", "third_party/nano_gicp", "third_party/nanoflann"):
            self.assertEqual(self.git(checkout / path, "rev-parse", "HEAD"), pinned)
        upstream.rename(upstream.with_name("offline-upstream"))
        result = self.initialize(checkout)
        self.assertNotIn("Initializing pinned", result.stdout)

    def testWrongCheckoutReturnsToPinnedRevisionWithoutResettingLocalEdits(self):
        checkout, _, pinned, newer = self.checkout()
        self.initialize(checkout)
        source = checkout / "third_party/nanoflann"
        self.git(source, "checkout", "--detach", newer)
        self.initialize(checkout)
        self.assertEqual(self.git(source, "rev-parse", "HEAD"), pinned)
        (source / "source.cpp").write_text("local edit\n")
        self.initialize(checkout)
        self.assertEqual((source / "source.cpp").read_text(), "local edit\n")

    def testManifestMismatchStopsBeforeChangingSources(self):
        checkout, _, _, newer = self.checkout()
        manifest_path = checkout / "mapping/native/dependencies.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["nanoflann"]["commit"] = newer
        manifest_path.write_text(json.dumps(manifest))
        result = self.initialize(checkout, succeeds=False)
        self.assertIn("gitlink and dependencies.json", " ".join(result.stderr.split()))
        self.assertFalse((checkout / "third_party/kiss-matcher/source.cpp").exists())

    def testSourceZipFetchesPinnedArchivesIntoThirdParty(self):
        source = self.root / "source zip"
        native = source / "mapping/native"
        native.mkdir(parents=True)
        (source / "cmake").mkdir()
        shutil.copy2(ROOT / "cmake/registration_submodules.cmake", source / "cmake")
        shutil.copy2(ROOT / "mapping/native/dependencies.cmake", native)
        manifest = {}
        for name, path in (("kiss", "third_party/kiss-matcher"), ("nano", "third_party/nano_gicp"),
                           ("nanoflann", "third_party/nanoflann"), ("other", None)):
            payload = self.root / (name + "-source")
            payload.mkdir()
            (payload / "source.cpp").write_text(name)
            archive = self.root / (name + ".tar.gz")
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(payload, arcname=name)
            manifest[name] = {"url": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
            if path:
                manifest[name].update(submodule=path, commit="unused-without-git")
        (native / "dependencies.json").write_text(json.dumps(manifest))
        (native / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.24)
project(archive_fallback NONE)
include("${CMAKE_CURRENT_SOURCE_DIR}/../../cmake/registration_submodules.cmake")
set(BLK_REGISTRATION_DEPS "${BLK_PROJECT_ROOT}/third_party/registration")
set(BLK_DOWNLOAD_REGISTRATION_DEPS TRUE)
include(dependencies.cmake)
''')
        result = subprocess.run([CMAKE, "-S", str(native), "-B", str(self.root / "build")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, path in (("kiss", "kiss-matcher"), ("nano", "nano_gicp"), ("nanoflann", "nanoflann"), ("other", "registration/other")):
            self.assertEqual((source / "third_party" / path / "source.cpp").read_text(), name)


if __name__ == "__main__":
    unittest.main()
