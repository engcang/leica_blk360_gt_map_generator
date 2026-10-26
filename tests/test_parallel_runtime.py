"""Verify bounded concurrency, deterministic result order, and CPU/memory budgets."""

import pathlib
import os
import sys
import threading
import time
import unittest
import tempfile
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "mapping"))
from parallel_runtime import cpuCount, cpuQuota, availableMemory, cacheBudget, memoryWorkers, orderedParallel, pairWorkers, threadBudget


class ParallelRuntimeTests(unittest.TestCase):
    def testCgroupQuotaIncludesParentAndSupportsV1(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=pathlib.Path(folder);child=parent/'child';child.mkdir()
            (parent/'cpu.max').write_text('250000 100000')
            (child/'cpu.max').write_text('max 100000')
            with mock.patch('parallel_runtime.cgroupFolders',return_value=[child,parent]):
                self.assertEqual(cpuQuota(),3)
                (child/'cpu.max').write_text('150000 100000')
                self.assertEqual(cpuQuota(),2)
                (child/'cpu.max').unlink()
                (child/'cpu.cfs_quota_us').write_text('100000')
                (child/'cpu.cfs_period_us').write_text('100000')
                self.assertEqual(cpuQuota(),1)

    @unittest.skipUnless(os.name=='posix','Linux memory accounting')
    def testMemoryBudgetUsesCgroupRemainingMemory(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=pathlib.Path(folder);child=parent/'child';child.mkdir()
            (parent/'memory.max').write_text(str(4*1024**3));(parent/'memory.current').write_text(str(3*1024**3))
            (child/'memory.max').write_text('max');(child/'memory.current').write_text('0')
            original=pathlib.Path.read_text
            def read(path,*args,**kwargs):
                return 'MemAvailable: 16777216 kB\n' if str(path)=='/proc/meminfo' else original(path,*args,**kwargs)
            with mock.patch('parallel_runtime.cgroupFolders',return_value=[child,parent]),mock.patch.object(pathlib.Path,'read_text',read):
                self.assertEqual(availableMemory(),1024**3)
                self.assertEqual(cacheBudget(.1),int(.1*1024**3))
                self.assertEqual(memoryWorkers(28,3,512*1024**2),1)
                (parent/'memory.stat').write_text(f'inactive_file {512*1024**2}\n')
                self.assertEqual(availableMemory(),1536*1024**2)

    def testRamChangesCacheAndConcurrencyWithoutChangingCpuBudget(self):
        with mock.patch('parallel_runtime.availableMemory',return_value=8*1024**3):
            small=cacheBudget(.1);self.assertEqual(memoryWorkers(28,8,2*1024**3),2)
        with mock.patch('parallel_runtime.availableMemory',return_value=64*1024**3):
            self.assertGreater(cacheBudget(.1),7.99*small)
            self.assertEqual(memoryWorkers(28,8,2*1024**3),8)
        with mock.patch('parallel_runtime.availableMemory',return_value=0):
            self.assertEqual(memoryWorkers(28,8,1),1)

    def testAutoBudgetUsesAllAvailableCpusAndHonorsExplicitLimits(self):
        for count in (2, 12, 28, 64):
            with mock.patch('parallel_runtime.cpuCount', return_value=count):
                self.assertEqual(threadBudget(), count)
                self.assertEqual(threadBudget(0), count)
                self.assertEqual(threadBudget(4), min(4, count))
                self.assertEqual(threadBudget(128), count)
        for invalid in (-1, 257, 2.5, '8', True):
            with self.assertRaises(ValueError):
                threadBudget(invalid)

    @unittest.skipUnless(hasattr(os, 'sched_getaffinity'), 'Linux affinity')
    def testBudgetRespectsAffinityAndUserCpuLimit(self):
        with mock.patch('os.sched_getaffinity', return_value={0, 1, 2, 3}):
            with mock.patch.dict('os.environ', {'BLK_CPU_THREADS': '28'}):
                self.assertLessEqual(cpuCount(), 4)
                self.assertLessEqual(threadBudget(), 4)
            with mock.patch.dict('os.environ', {'BLK_CPU_THREADS': '2'}):
                self.assertLessEqual(threadBudget(), 2)

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
