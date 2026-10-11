"""Atomic JSON updates that tolerate brief Windows reader locks."""

import json
import os
import pathlib
import tempfile
import time


def saveJson(_path, _value):
    path = pathlib.Path(_path)
    payload = json.dumps(_value, indent=2, allow_nan=False) + "\n"
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".pending", delete=False) as stream:
            pending = pathlib.Path(stream.name)
            stream.write(payload)
        deadline = time.monotonic() + 5.0
        delay = 0.01
        while True:
            try:
                os.replace(pending, path)
                break
            except PermissionError as error:
                # Windows readers can briefly prevent replacement without FILE_SHARE_DELETE.
                remaining = deadline - time.monotonic()
                if getattr(error, "winerror", None) not in (5, 32, 33) or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.1)
    finally:
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                # Preserve the original write error if cleanup is also denied.
                pass
