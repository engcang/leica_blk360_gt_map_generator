"""CPU-aware, memory-bounded parallel work for local registration."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import ctypes
import os
import pathlib
import math
import re


def cgroupFolders(controller):
    """Mounted Linux cgroup hierarchy, including limits inherited from parents."""
    if os.name != 'posix':
        return []
    try:
        groups = [line.split(':', 2) for line in pathlib.Path('/proc/self/cgroup').read_text().splitlines()]
        mounts = pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        return []
    folders = []
    unescape = lambda s: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), s)
    for line in mounts:
        fields, separator, tail = line.partition(' - ')
        left, right = fields.split(), tail.split()
        if not separator or len(left) < 5 or len(right) < 3 or right[0] not in ('cgroup', 'cgroup2'):
            continue
        unified = right[0] == 'cgroup2'
        if not unified and controller not in right[2].split(','):
            continue
        for _, controllers, group in groups:
            if not ((unified and not controllers) or (not unified and controller in controllers.split(','))):
                continue
            mount = pathlib.Path(unescape(left[4]))
            try:
                relative = pathlib.PurePosixPath(group).relative_to(unescape(left[3]))
            except ValueError:
                continue
            if '..' in relative.parts:
                continue
            path = mount / relative
            while path == mount or mount in path.parents:
                folders.append(path)
                if path == mount:
                    break
                path = path.parent
    return folders


def cpuQuota():
    limits = []
    for path in cgroupFolders('cpu'):
        try:
            if (path / 'cpu.max').is_file():
                quota, period = (path / 'cpu.max').read_text().split()
                if quota == 'max':
                    continue
            else:
                quota = (path / 'cpu.cfs_quota_us').read_text()
                period = (path / 'cpu.cfs_period_us').read_text()
            if int(quota) > 0 and int(period) > 0:
                limits.append(max(1, math.ceil(int(quota) / int(period))))
        except (OSError, ValueError):
            pass
    return min(limits) if limits else None


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
    quota = cpuQuota()
    if quota is not None:
        available = min(available, quota)
    requested = int(os.environ.get("BLK_CPU_THREADS", available))
    return max(1, min(available, requested))


def configureThreads():
    os.environ.setdefault("OMP_NUM_THREADS", str(cpuCount()))
    # Small matrix products should not each create another full CPU thread pool.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")


def threadBudget(requested=None):
    """Zero/omitted means Auto; explicit budgets still respect CPU affinity."""
    if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int)
                                  or not 0 <= requested <= 256):
        raise ValueError('CPU threads must be 0 (Auto) or an integer from 1 to 256')
    available = min(cpuCount(), 256)
    return available if not requested else min(requested, available)


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
                    available = int(line.split()[1]) * 1024
                    for folder in cgroupFolders('memory'):
                        try:
                            unified = (folder / 'memory.max').is_file()
                            limit = (folder / ('memory.max' if unified else 'memory.limit_in_bytes')).read_text().strip()
                            if limit == 'max':
                                continue
                            used = int((folder / ('memory.current' if unified else 'memory.usage_in_bytes')).read_text())
                            reclaimable = 0
                            try:
                                stats = dict(line.split() for line in (folder / 'memory.stat').read_text().splitlines())
                                reclaimable = int(stats.get('inactive_file' if unified else 'total_inactive_file', 0))
                            except (OSError, ValueError):
                                pass
                            available = min(available, max(0, int(limit) - max(0, used - reclaimable)))
                        except (OSError, ValueError):
                            pass
                    return available
        except OSError:
            pass
    return 0


def cacheBudget(fraction=.1):
    """Caches may be evicted without changing computation or point density."""
    available = availableMemory()
    return int(available * fraction) if available else 32 * 1024**2


def memoryWorkers(requested, count, bytes_per_worker):
    available = availableMemory()
    memory_limit = max(1, int(available * .6) // max(1, int(bytes_per_worker))) if available else 1
    return max(1, min(requested, count, memory_limit))


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
