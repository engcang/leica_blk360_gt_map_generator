"""Native KISS sessions with bounded descriptor memory lifetime."""
from pathlib import Path
from contextlib import contextmanager, nullcontext
import json
import os
import subprocess
import tempfile
import time

import numpy as np

from native_backend import backendInfo, executablePath


@contextmanager
def dense_kiss_slot():
    """Avoid overlapping multi-gigabyte dense descriptor allocations."""
    path = Path(tempfile.gettempdir())/f'blk-dense-kiss-{getattr(os, "getuid", lambda: 0)()}.lock'
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if stream.tell() == 0:
                stream.write(b'0');stream.flush()
            while True:
                try:
                    stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);break
                except OSError:
                    time.sleep(.25)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream,fcntl.LOCK_UN)


class NativeSession:
    def __init__(self, directory):
        info = backendInfo()
        if info.get('trajectory_session') != 1:
            raise ValueError('Rebuild BLK Studio to enable the trajectory registration engine')
        self.temporary = tempfile.TemporaryDirectory(prefix='trajectory-native-', dir=directory)
        self.root = Path(self.temporary.name)
        (self.root/'.blk-native-session').write_text('1\n', encoding='ascii')
        self.log = (self.root/'native.log').open('w')
        self.process = None
        self.start_process()

    def start_process(self):
        self.process = subprocess.Popen([str(executablePath()), '--serve'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.log, text=True,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def request(self, request):
        if self.process is None:
            self.start_process()
        self.process.stdin.write(json.dumps(request, allow_nan=False)+'\n')
        self.process.stdin.flush()
        for line in self.process.stdout:
            if line.startswith('BLK_RESULT '):
                result = json.loads(line[len('BLK_RESULT '):])
                if 'error' in result:
                    raise RuntimeError(result['error'])
                return result
        raise RuntimeError('Native trajectory worker exited unexpectedly')

    def kiss(self, source, target, resolution, workers):
        a, b = self.root/'kiss-source.bin', self.root/'kiss-target.bin'
        np.asarray(source, dtype='<f4').tofile(a)
        np.asarray(target, dtype='<f4').tofile(b)
        slot = dense_kiss_slot() if resolution <= .1 and len(target) > 500_000 else nullcontext()
        with slot:
            try:
                result = self.request(dict(method='kiss', source=str(a), target=str(b), resolution=resolution, threads=workers))
            finally:
                # KISS builds a fresh matcher per request. Its freed descriptor
                # heap otherwise remains resident in the persistent process.
                self.stop_process()
        if not result['valid']:
            raise ValueError('KISS-Matcher found no valid alignment')
        return np.asarray(result['transform_target_from_source'])

    def stop_process(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.process = None

    def close(self):
        self.stop_process()
        self.log.close()
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
