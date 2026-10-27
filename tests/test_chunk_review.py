"""Chunk images preserve saved poses, original station rows, and manual placement."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mapping'))
from chunk_review import writeChunkReview
from register_scans import writePly


def fixture(root):
    scans = []
    points = np.array([[x, y, z] for x in (-2, 2) for y in np.linspace(-3, 3, 20)
                       for z in np.linspace(-2, 2, 20)], dtype=np.float32)
    for row, height in ((3, 0.), (8, .1), (12, 3.2)):
        path = root / f'Scan{row}.ply'
        writePly(path, points, np.full(len(points), 100))
        pose = np.eye(4); pose[:3, 3] = [row*.5, 0, height]
        scans.append(dict(row=row, name=path.name, source_path=path.name, transform_chunk_from_scan=pose.tolist()))
    chunks = [dict(number=1, key='test-frame', rows=[3, 8, 12], scans=scans, points=4800,
                   viewer='preview.html', anchor_row=3),
              dict(number=2, key='single-frame', rows=[12], scans=scans[-1:], points=1600,
                   viewer='preview.html', anchor_row=12)]
    (root/'partial_chunks.json').write_text(json.dumps(dict(chunks=chunks)))
    (root/'chunk_initial_placements.json').write_text(json.dumps(dict(chunks=chunks, poses={'test-frame':np.eye(4).tolist()})))
    (root/'preview.html').write_text('<!doctype html><title>Chunk fixture</title><canvas></canvas>')


class ChunkReviewTests(unittest.TestCase):
    def test_saved_frames_and_levels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture(root)
            protected = {path:path.read_bytes() for path in root.iterdir()}
            writeChunkReview(root, root, lambda _: None)
            for path, before in protected.items():
                self.assertEqual(path.read_bytes(), before)
            assets = root/'html_source/chunks/test-frame/html_source'
            summary = json.loads((assets/'review_summary.json').read_text())
            self.assertEqual([s['station'] for s in summary['stations']], [3, 8, 12])
            self.assertEqual([s['inferred_level'] for s in summary['stations']], [1, 1, 2])
            np.testing.assert_allclose([s['map_xyz_m'][2] for s in summary['stations']], [0, .1, 3.2])
            for name in summary['images']:
                self.assertTrue((assets/name).read_bytes().startswith(b'\x89PNG'))
            self.assertFalse((assets.parent/'result_review_and_manage.html').exists())
            document = (root/'chunks_review.html').read_text()
            self.assertEqual(document.count('<iframe'), 1)
            self.assertNotIn('Download', document)
            self.assertIn('level_02.png', document)
            with patch('chunk_review.createReview', side_effect=AssertionError('Recomputed existing images')):
                writeChunkReview(root, root, lambda _: None)


if __name__ == '__main__':
    unittest.main()
