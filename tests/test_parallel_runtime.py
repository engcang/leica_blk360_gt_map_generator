"""Verify bounded concurrency, deterministic result order, and CPU/memory budgets."""

import pathlib
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mapping"))
from parallel_runtime import orderedParallel, pairWorkers


class ParallelRuntimeTests(unittest.TestCase):
    def testWorkOverlapsButResultsKeepInputOrder(self):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = 0
        maximum = 0

        def work(_index):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            if _index < 3:
                barrier.wait(timeout=5)
            time.sleep((5 - _index) * 0.005)
            with lock:
                active -= 1
            return _index

        self.assertEqual(list(orderedParallel(work, range(6), 3)), list(range(6)))
        self.assertEqual(maximum, 3)

    def testFailedTaskDoesNotScheduleEntireJob(self):
        started = []

        def work(_index):
            started.append(_index)
            if _index == 0:
                raise ValueError("failed pair")
            return _index

        with self.assertRaisesRegex(ValueError, "failed pair"):
            list(orderedParallel(work, range(100), 2))
        self.assertLessEqual(len(started), 2)

    def testCpuAndMemoryBoundConcurrentPairs(self):
        clouds = [SimpleNamespace(points=range(4000000))]
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("parallel_runtime.cpuCount", return_value=20):
                with mock.patch("parallel_runtime.availableMemory", return_value=16 * 1024**3):
                    self.assertEqual(pairWorkers(clouds, 105), 1)
                    with mock.patch.dict("os.environ", {"BLK_REGISTRATION_WORKERS": "5"}):
                        self.assertEqual(pairWorkers(clouds, 105), 5)
                        self.assertEqual(pairWorkers(clouds, 2), 2)
                with mock.patch("parallel_runtime.availableMemory", return_value=1024**3):
                    with mock.patch.dict("os.environ", {"BLK_REGISTRATION_WORKERS": "5"}):
                        self.assertEqual(pairWorkers(clouds, 105), 1)
                with mock.patch("parallel_runtime.availableMemory", return_value=0):
                    self.assertEqual(pairWorkers(clouds, 105), 1)


if __name__ == "__main__":
    unittest.main()
