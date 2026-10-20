"""Shared, strict binary XYZ/intensity and XYZ/RGB PLY support."""
import re
import numpy as np

XYZ = "property float x\nproperty float y\nproperty float z\n"
INTENSITY = "property ushort intensity\n"
RGB = "property uchar red\nproperty uchar green\nproperty uchar blue\n"

def layout(path):
    with path.open("rb") as stream:
        data = stream.read(65536)
    end = data.find(b"end_header\n")
    if end < 0:
        raise ValueError("PLY header not found")
    header = data[:end+11].decode("ascii")
    match = re.search(r"^element vertex (\d+)$", header, re.MULTILINE)
    rgb = header.endswith(XYZ + RGB + "end_header\n")
    if not header.startswith("ply\nformat binary_little_endian 1.0\n") or not match or not (rgb or header.endswith(XYZ+INTENSITY+"end_header\n")):
        raise ValueError("Expected binary XYZ/intensity or XYZ/RGB PLY")
    dtype = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4")] +
                     ([(c, "u1") for c in ("red", "green", "blue")] if rgb else [("intensity", "<u2")]))
    count = int(match[1])
    if count <= 0 or path.stat().st_size != end+11+count*dtype.itemsize:
        raise ValueError("PLY count/size mismatch")
    return count, end+11, dtype, rgb

def readPly(path):
    count, offset, dtype, _ = layout(path)
    return np.memmap(path, dtype=dtype, mode="r", offset=offset, shape=(count,))

def colourValues(raw):
    if "red" in raw.dtype.names:
        return np.column_stack([raw[c] for c in ("red", "green", "blue")])
    return np.asarray(raw["intensity"])

def plyHeader(count, rgb):
    return "ply\nformat binary_little_endian 1.0\nelement vertex " + str(count) + "\n" + XYZ + (RGB if rgb else INTENSITY) + "end_header\n"
