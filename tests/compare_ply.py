import argparse
import json
import math
import struct


def readHeader(_stream):
    header = bytearray()
    while not header.endswith(b"end_header\n"):
        line = _stream.readline()
        if not line or len(header) > 65536:
            raise ValueError("Invalid PLY header")
        header.extend(line)
    if b"format binary_little_endian 1.0\n" not in header:
        raise ValueError("Unexpected format")
    for line in header.splitlines():
        if line.startswith(b"element vertex "):
            return int(line.split()[-1])
    raise ValueError("Missing vertex count")


def compareFiles(_reference, _actual):
    max_error = 0.0
    differing_points = 0
    with open(_reference, "rb") as reference, open(_actual, "rb") as actual:
        count = readHeader(reference)
        if readHeader(actual) != count:
            raise AssertionError("Vertex count mismatch")
        seen = 0
        while seen < count:
            size = min(65536, count - seen)
            left = reference.read(size * 14)
            right = actual.read(size * 14)
            if len(left) != size * 14 or len(right) != size * 14:
                raise AssertionError("Truncated payload")
            if left != right:
                for a, b in zip(struct.iter_unpack("<fffH", left), struct.iter_unpack("<fffH", right)):
                    if a[3] != b[3]:
                        raise AssertionError("Intensity mismatch")
                    errors = [abs(a[i] - b[i]) for i in range(3)]
                    if not all(math.isfinite(value) for value in errors):
                        raise AssertionError("Non-finite coordinate")
                    max_error = max(max_error, *errors)
                    differing_points += a != b
            seen += size
        if reference.read(1) or actual.read(1):
            raise AssertionError("Unexpected trailing records")
    if max_error > 0.000001:
        raise AssertionError(f"Coordinate regression: {max_error} m")
    return {"compared_points": count, "differing_points": differing_points, "max_coordinate_error_m": max_error}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("reference")
    parser.add_argument("actual")
    arguments = parser.parse_args()
    print(json.dumps(compareFiles(arguments.reference, arguments.actual), indent=4))
