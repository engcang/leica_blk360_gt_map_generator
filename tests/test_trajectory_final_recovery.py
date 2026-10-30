"""Tracking checkpoint reuse, reverse initialization, and local-only recovery."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'mapping'))
from test_trajectory import fixture
from trajectory_worker import run, inputs
from trajectory_recovery import Recovery
from trajectory_io import save_json


class ReverseRestartTests(unittest.TestCase):
    def recovery(self, count):
        r = Recovery.__new__(Recovery)
        r.rows_by_index = {i:dict(timestamp=i*.1) for i in range(count+1)}
        r.poses = {count:np.eye(4)}
        r.seed_fits, r.conflicted = {}, set()
        r.certify_submap_pose = lambda *args: None
        r.log = lambda record: None
        r.seed_pose = lambda index, T: (T, dict(accepted=True))
        return r

    def test_later_anchor_does_not_prevent_restart_inside_long_gap(self):
        r = self.recovery(1201)
        def propagate(ids, seed, T, direction):
            if direction == 'reverse_fine' and seed in (900,600):
                return {i:(T,dict(accepted=True)) for i in ids if i >= seed-100}
            return {}
        def initialize(index, allow_global=True):
            return np.eye(4) if index in (900,600) else None
        with patch.object(r,'propagate',side_effect=propagate), \
             patch.object(r,'initialize_tail',side_effect=initialize) as locate:
            forward, backward = r.reverse_block(list(range(1201)),1201)
        attempted = [call.args[0] for call in locate.call_args_list]
        self.assertEqual(attempted[0],900)
        self.assertIn(600,attempted)
        self.assertIn(800,backward)
        self.assertIn(500,backward)
        self.assertFalse(forward)

    def test_short_gap_reuses_end_without_new_initialization(self):
        r = self.recovery(10)
        with patch.object(r,'propagate',return_value={}), \
             patch.object(r,'initialize_tail') as locate:
            r.reverse_block([7,8,9],10)
        locate.assert_not_called()

    def test_successful_reverse_does_not_repeat_localization(self):
        r = self.recovery(1201)
        poses = {i:(np.eye(4),dict(accepted=True)) for i in range(1201)}
        with patch.object(r,'propagate',return_value=poses), \
             patch.object(r,'initialize_tail') as locate:
            _, backward = r.reverse_block(list(range(1201)),1201)
        locate.assert_not_called()
        self.assertEqual(len(backward),1201)


class AnchorRestartTests(unittest.TestCase):
    def recovery(self):
        r = Recovery.__new__(Recovery)
        r.tail_window = lambda i, radius: SimpleNamespace(anchor=SimpleNamespace(index=i))
        r._accept_tail_hypothesis = lambda T: T
        r.seed_guesses = lambda s: [('initial_alignment', np.eye(4)),
            ('before_anchor', np.eye(4)), ('after_anchor', np.eye(4))]
        return r

    def test_endpoint_probes_never_call_kiss_or_dense_shared_fitting(self):
        r = self.recovery()
        def validate(window, T, scope, resolution, *, allow_shared_support):
            self.assertFalse(allow_shared_support)
            self.assertEqual(scope, 'local_capture_before_anchor')
            r.seed_hypotheses.append(dict(T=T,costs=[.001]*3,accepted=True))
        with patch.object(r,'validate_tail_seed',side_effect=validate) as check, \
             patch.object(r,'kiss_request',side_effect=AssertionError('Feature search')), \
             patch.object(r,'_tail_support_fit',side_effect=AssertionError('Dense shared fitting')):
            self.assertIsNotNone(r.initialize_from_anchors(20))
        self.assertEqual(check.call_count,1)

    def test_conflicting_endpoint_windows_remain_rejected(self):
        r = self.recovery()
        other = np.eye(4); other[0,3] = 4
        r.seed_guesses = lambda s: [('before_anchor', np.eye(4)),('after_anchor', other)]
        def validate(window,T,*args,**kwargs):
            r.seed_hypotheses.append(dict(T=T,costs=[.001]*3,accepted=True))
        with patch.object(r,'validate_tail_seed',side_effect=validate) as check:
            self.assertIsNone(r.initialize_from_anchors(20))
        self.assertEqual(check.call_count,4)

    def test_probe_spacing_does_not_discard_interior_scan_islands(self):
        r = Recovery.__new__(Recovery)
        r.rows_by_index = {i:dict(timestamp=i*.1) for i in range(210)}
        seeds = r.seed_indices(list(range(210)),spacing_s=5.)
        self.assertTrue(any(45<=i<=55 for i in seeds))
        self.assertTrue(any(145<=i<=155 for i in seeds))
        self.assertIn(209,seeds)


@unittest.skipUnless(os.environ.get('BLK_REGISTRATION_EXECUTABLE'), 'Native registration executable required')
class FinalRecoveryTests(unittest.TestCase):
    def test_new_anchor_schedule_reuses_completed_reverse_search(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, _ = self.prepare(root, {3, 4})
            tracking = first.tracking_directory
            try:
                first.run(tracking_only=True)
            finally:
                first.close()
            receipt = json.loads((tracking/'tracking_complete.json').read_text())
            receipt['provenance']['seeds'].pop('anchor_recovery')
            save_json(tracking/'tracking_complete.json', receipt)
            source, _, _, _, cache, _ = inputs(json.loads((root/'output/provenance.json').read_text())['config'])
            resumed = Recovery(root/'output/baseline',root/'anchor-update',cache,source,2,lambda _:None,
                               baseline_is_fine=True,tracking_directory=tracking)
            try:
                self.assertTrue(resumed.restore_tracking())
                self.assertFalse(resumed.anchor_tracking_ready)
                with patch.object(resumed,'reverse_block',side_effect=AssertionError('Repeated reverse search')):
                    resumed.run(tracking_only=True)
                saved=json.loads((tracking/'tracking_complete.json').read_text())
                self.assertEqual(saved['provenance']['seeds']['anchor_recovery'],'endpoint-window-v1')
            finally:
                resumed.close()

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
            provenance_path = previous/'provenance.json'
            old_provenance = json.loads(provenance_path.read_text())
            old_provenance['validation']['context_protocol'] = 'old-lenient-validation'
            provenance_path.write_text(json.dumps(old_provenance))
            with self.assertRaisesRegex(ValueError, 'TLS validation changed'):
                run(config, root/'old-protocol-report.json', recovery_only=True,
                    recovery_name='recovery_incompatible', revalidate_from='recovery')
            provenance_path.write_bytes(saved[provenance_path])
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
