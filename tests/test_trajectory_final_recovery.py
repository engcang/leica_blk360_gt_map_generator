"""Tracking checkpoint reuse, reverse initialization, and local-only recovery."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'mapping'))
from test_trajectory import fixture
from trajectory_worker import run, inputs
from trajectory_recovery import Recovery
from trajectory_io import save_json


@unittest.skipUnless(os.environ.get('BLK_REGISTRATION_EXECUTABLE'), 'Native registration executable required')
class FinalRecoveryTests(unittest.TestCase):
    def test_completed_tracking_is_reused_without_any_refitting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, _ = self.prepare(root, {3, 4})
            tracking = first.tracking_directory
            try:
                first.run(tracking_only=True)
                expected = (tracking/'candidate_trajectory.tum').read_bytes()
            finally:
                first.close()
            source, _, _, _, cache, _ = inputs(json.loads((root/'output/provenance.json').read_text())['config'])
            resumed = Recovery(root/'output/baseline', root/'final', cache, source, 2, lambda _: None,
                               baseline_is_fine=True, tracking_directory=tracking)
            try:
                with patch.object(resumed.fine, 'align', side_effect=AssertionError('Tracking was refitted')), \
                     patch.object(resumed, 'propagate', side_effect=AssertionError('Tracking was repeated')):
                    resumed.run()
                self.assertEqual((root/'final/candidate_trajectory.tum').read_bytes(), expected)
                receipt = json.loads((tracking/'tracking_complete.json').read_text())
                receipt['quality_sha256'] = 'invalid'
                save_json(tracking/'tracking_complete.json', receipt)
                with self.assertRaisesRegex(ValueError, 'incomplete or changed'):
                    resumed.restore_tracking()
            finally:
                resumed.close()

    def test_local_submap_cannot_call_kiss_after_reverse(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery, _ = self.prepare(Path(folder), {3, 4})
            try:
                with patch.object(recovery, 'propagate', return_value={}), \
                     patch.object(recovery.fine.native, 'kiss', side_effect=AssertionError('Post-reverse KISS')):
                    recovery.run()
                records = [json.loads(line) for line in (recovery.output/'attempts.jsonl').read_text().splitlines()]
                self.assertTrue(any(r['stage']=='submap_seed' for r in records))
                self.assertFalse(any(r['stage']=='kiss_search' for r in records))
            finally:
                recovery.close()

    def test_revalidation_preserves_previous_result_and_reuses_fits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = fixture(root, count=8)
            run(config, root/'first-report.json')
            previous = root/'output/recovery'
            saved = {p:p.read_bytes() for p in previous.iterdir() if p.is_file()}
            with patch('trajectory_fine.FineMatcher.align', side_effect=AssertionError('Verified fits repeated')):
                run(config, root/'second-report.json', recovery_only=True,
                    recovery_name='recovery_connected_v2', revalidate_from='recovery')
            self.assertEqual(saved, {p:p.read_bytes() for p in saved})
            new = root/'output/recovery_connected_v2'
            self.assertEqual((new/'candidate_trajectory.tum').read_bytes(), saved[previous/'candidate_trajectory.tum'])
            self.assertIn('revalidated_source', json.loads((new/'provenance.json').read_text()))
            (previous/'candidate_trajectory.tum').write_text('modified')
            with self.assertRaisesRegex(ValueError,'changed'):
                run(config, root/'third-report.json', recovery_only=True,
                    recovery_name='recovery_connected_v2', revalidate_from='recovery')

    def prepare(self, root, missing):
        config = fixture(root, count=12)
        run(config, root/'report.json', baseline_only=True)
        baseline = root/'output/baseline'
        original = [json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
        rows = copy.deepcopy(original)
        for i in missing: rows[i]['accepted'] = False
        (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in rows))
        poses = np.loadtxt(root/'estimate.tum')
        np.savetxt(baseline/'candidate_trajectory.tum', poses[[i for i in range(len(rows)) if i not in missing]])
        source, _, _, _, cache, _ = inputs(config)
        return Recovery(baseline, root/'recovered', cache, source, 2, lambda _:None, baseline_is_fine=True), original

    def test_tail_localization_precedes_reverse_and_keeps_the_forward_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery, _ = self.prepare(Path(folder), set(range(3,12)))
            try:
                with patch.object(recovery, 'initialize_tail', wraps=recovery.initialize_tail) as seeds:
                    recovery.run()
                self.assertEqual(seeds.call_args_list[0].args[0], 11)
                poses = np.loadtxt(Path(folder)/'recovered/candidate_trajectory.tum')
                np.testing.assert_allclose(poses, np.loadtxt(Path(folder)/'estimate.tum'), atol=1e-4)
                events = [json.loads(line) for line in (Path(folder)/'recovered/attempts.jsonl').read_text().splitlines()]
                self.assertTrue(any(e['stage']=='tail_initialized' for e in events))
                self.assertTrue(any(e['stage']=='reverse_fine' and e['index']==3 for e in events))
            finally:
                recovery.close()

    def test_failed_last_window_tries_an_earlier_reverse_anchor(self):
        with tempfile.TemporaryDirectory() as folder:
            recovery, _ = self.prepare(Path(folder), set(range(3,12)))
            original = recovery.initialize_tail
            def seed(index, allow_global=True):
                return None if index == 11 else original(index, allow_global)
            try:
                with patch.object(recovery, 'initialize_tail', side_effect=seed) as seeds:
                    recovery.run()
                self.assertEqual(seeds.call_args_list[0].args[0], 11)
                self.assertTrue(any(call.args[0] < 11 for call in seeds.call_args_list[1:]))
                self.assertEqual(len(recovery.poses), 12)
            finally:
                recovery.close()
