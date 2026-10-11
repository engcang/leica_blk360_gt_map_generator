"""CPU-aware, memory-bounded parallel work for local registration."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import ctypes
import os
import pathlib


def cpuCount():
    available = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    if hasattr(os, "sched_getaffinity"):
        available = min(available, len(os.sched_getaffinity(0)))
    if os.name == "nt" and available <= 64:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
        process_mask, system_mask = ctypes.c_size_t(), ctypes.c_size_t()
        if kernel.GetProcessAffinityMask(kernel.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask)):
            available = min(available, process_mask.value.bit_count())
    requested = int(os.environ.get("BLK_CPU_THREADS", available))
    return max(1, min(available, requested))


def configureThreads():
    os.environ.setdefault("OMP_NUM_THREADS", str(cpuCount()))
    # Small matrix products should not each create another full CPU thread pool.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")


def availableMemory():
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                        ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
                        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
                        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
                        ("available_extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available_physical
    else:
        try:
            for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except OSError:
            pass
    return 0


def pairWorkers(_clouds, _count):
    cpus = cpuCount()
    # Open3D already parallelizes each ICP across the CPU budget. Extra pairs are opt-in.
    requested = int(os.environ.get("BLK_REGISTRATION_WORKERS", "1"))
    # Clouds are shared read-only; each ICP still allocates a source copy and a KD tree.
    per_pair = max(256 * 1024 ** 2, max((len(cloud.points) for cloud in _clouds), default=0) * 160)
    memory_limit = max(1, int(availableMemory() * 0.65) // per_pair)
    return max(1, min(_count, cpus, requested, memory_limit))


def orderedParallel(_function, _items, _workers):
    """Bound in-flight work and preserve graph edge order regardless of finish order."""
    items = iter(_items)
    if _workers <= 1:
        for item in items:
            yield _function(item)
        return
    executor = ThreadPoolExecutor(max_workers=_workers)
    pending = deque()
    try:
        for _ in range(_workers):
            item = next(items, None)
            if item is None:
                break
            pending.append(executor.submit(_function, item))
        while pending:
            result = pending.popleft().result()
            yield result
            item = next(items, None)
            if item is not None:
                pending.append(executor.submit(_function, item))
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
