import base64
import gzip
import hashlib
import pathlib
import re
import tempfile
import unittest

from share_map import createShareViewer


class ShareViewerTests(unittest.TestCase):
    def testEmbeddedMapPreservesEveryByteAcrossBlocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "scan.ply"
            header = b"ply\nformat binary_little_endian 1.0\nelement vertex 300000\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n"
            original = header + bytes(300000 * 14)
            source.write_bytes(original)
            output = root / "shared.html"
            result = createShareViewer(source, output)
            document = output.read_text(encoding="utf-8")
            blocks = re.findall(r'<script id="ply-block-\d+" type="application/octet-stream">([^<]+)</script>', document)
            self.assertEqual(len(blocks), 2)
            recovered = b"".join(gzip.decompress(base64.b64decode(block)) for block in blocks)
            self.assertEqual(recovered, original)
            self.assertEqual(result["sha256"], hashlib.sha256(original).hexdigest())
            self.assertNotIn('<script src=', document)
            self.assertNotIn('<link rel="stylesheet"', document)
            self.assertIn("Copyright (c) 2026 blk2ply contributors", document)
            self.assertIn("Permission is hereby granted", document)
            with self.assertRaises(FileExistsError):
                createShareViewer(source, output)
            self.assertEqual(source.read_bytes(), original)

    def testInvalidMapDoesNotLeaveAnOutput(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "bad.ply"
            source.write_bytes(b"invalid")
            with self.assertRaises(ValueError):
                createShareViewer(source, root / "shared.html")
            self.assertFalse((root / "shared.html").exists())
            self.assertFalse((root / "shared.html.pending").exists())


if __name__ == "__main__":
    unittest.main()
