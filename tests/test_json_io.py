"""Exercise atomic progress updates, including a real Windows sharing conflict."""

import ctypes
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mapping"))
from json_io import saveJson


class JsonIoTests(unittest.TestCase):
    def testAtomicReplacementAndCleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "progress.json"
            saveJson(path, {"stage": "old"})
            saveJson(path, {"stage": "new"})
            self.assertEqual(json.loads(path.read_text()), {"stage": "new"})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def testInvalidPayloadPreservesOldFile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "progress.json"
            saveJson(path, {"stage": "old"})
            with self.assertRaises(ValueError):
                saveJson(path, {"value": float("nan")})
            self.assertEqual(json.loads(path.read_text()), {"stage": "old"})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def testTransientWindowsErrorsRetry(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "progress.json"
                error = PermissionError("temporarily locked")
                error.winerror = code
                replace = os.replace
                attempts = []

                def replaceAfterLock(_source, _target):
                    attempts.append(_source)
                    if len(attempts) == 1:
                        raise error
                    replace(_source, _target)

                with mock.patch("json_io.os.replace", side_effect=replaceAfterLock):
                    saveJson(path, {"stage": "new"})
                self.assertEqual(len(attempts), 2)
                self.assertEqual(json.loads(path.read_text()), {"stage": "new"})
                self.assertEqual(list(path.parent.iterdir()), [path])

    def testPermanentLockStopsAndPreservesOldFile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "progress.json"
            saveJson(path, {"stage": "old"})
            error = PermissionError("persistent lock")
            error.winerror = 5
            with mock.patch("json_io.os.replace", side_effect=error) as replace:
                with mock.patch("json_io.time.monotonic", side_effect=[0.0, 0.0, 5.1]):
                    with mock.patch("json_io.time.sleep"):
                        with self.assertRaises(PermissionError):
                            saveJson(path, {"stage": "new"})
                self.assertEqual(replace.call_count, 2)
            self.assertEqual(json.loads(path.read_text()), {"stage": "old"})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def testOtherErrorsAreNotRetried(self):
        for error in (PermissionError("permission denied"), OSError("disk full")):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "progress.json"
                with mock.patch("json_io.os.replace", side_effect=error) as replace:
                    with self.assertRaises(type(error)):
                        saveJson(path, {"stage": "new"})
                    self.assertEqual(replace.call_count, 1)
                self.assertEqual(list(path.parent.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing semantics")
    def testRealWindowsReaderLock(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "progress.json"
            saveJson(path, {"stage": "old"})
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            # The reader permits reads, but prevents deletion/replacement until it closes.
            timer = threading.Timer(0.2, kernel.CloseHandle, args=(handle,))
            timer.start()
            try:
                with mock.patch("json_io.os.replace", wraps=os.replace) as replace:
                    saveJson(path, {"stage": "new"})
                    self.assertGreater(replace.call_count, 1)
            finally:
                timer.join()
            self.assertEqual(json.loads(path.read_text()), {"stage": "new"})
            self.assertEqual(list(path.parent.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
