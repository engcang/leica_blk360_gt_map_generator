"""Separate initial fitting from recovery without recomputing or corrupting it."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from test_trajectory import fixture
from trajectory_worker import run, inputs
from trajectory_recovery import Recovery
from trajectory_stages import ready_baseline
from trajectory_io import digest, save_json


class StagedTrajectoryTests(unittest.TestCase):
    def test_legacy_completion_requires_matching_recovery_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=fixture(root)
            run(config,root/'report.json',baseline_only=True)
            out=root/'output'; b=out/'baseline'; (b/'status.json').unlink()
            self.assertIsNone(ready_baseline(out,adopt_legacy=True))
            old=out/'recovery';old.mkdir()
            save_json(old/'provenance.json',dict(baseline_policy='verified-1cm',
                baseline_trajectory_sha256=digest(b/'candidate_trajectory.tum'),
                baseline_quality_sha256=digest(b/'quality.jsonl')))
            self.assertEqual(ready_baseline(out,adopt_legacy=True)['accepted'],8)
            (b/'status.json').unlink();(b/'quality.jsonl').write_text('changed')
            with self.assertRaisesRegex(ValueError,'baseline changed'):
                ready_baseline(out,adopt_legacy=True)

    def test_initial_only_then_new_recovery_preserves_baseline_and_old_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=fixture(root)
            with patch('trajectory_worker.Recovery', side_effect=AssertionError('Premature recovery')):
                run(config, root/'report.json', baseline_only=True)
            out=root/'output'
            receipt=ready_baseline(out)
            self.assertEqual(receipt['accepted'],8)
            self.assertEqual(json.loads((root/'report.json').read_text())['status'],'baseline_ready')
            self.assertFalse((out/'viewer.json').exists())
            saved={p:p.read_bytes() for p in (out/'baseline').glob('*') if p.is_file()}
            old=out/'recovery'; old.mkdir(); (old/'keep.txt').write_text('comparison result')
            with patch('trajectory_worker.baseline', side_effect=AssertionError('Baseline recomputed')):
                run(config,root/'report.json',recovery_only=True,recovery_name='recovery_v12')
            self.assertEqual(json.loads((root/'report.json').read_text())['status'],'completed')
            self.assertEqual(saved,{p:p.read_bytes() for p in saved})
            self.assertEqual((old/'keep.txt').read_text(),'comparison result')
            self.assertTrue((out/'recovery_v12/candidate_trajectory.tum').exists())
            (out/'baseline/quality.jsonl').write_text('changed')
            with self.assertRaisesRegex(ValueError,'baseline changed'):
                ready_baseline(out)

    def test_recovery_cannot_run_before_initial_fitting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=fixture(root)
            with patch('trajectory_worker.baseline', side_effect=AssertionError('Unexpected initial run')):
                with self.assertRaisesRegex(ValueError,'Finish the initial'):
                    run(config,root/'report.json',recovery_only=True)
            self.assertFalse((root/'output/baseline/status.json').exists())

    def test_iteration_continuation_preserves_acceptance_gates_and_cache_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=fixture(root)
            run(config,root/'report.json',baseline_only=True)
            source,_,_,out,cache,_=inputs(config)
            r=Recovery(out/'baseline',out/'probe',cache,source,2,lambda _:None,baseline_is_fine=True)
            try:
                scan=r.scan(3); expected=scan.frontend_T
                actual,q=r.fit(scan,expected,'first')
                q.update(accepted=False,reason='tracking_motion_inconsistent')
                with patch.object(r.fine,'align',side_effect=AssertionError('Same fit recomputed')):
                    _,again=r.fit(scan,expected,'different_stage')
                self.assertTrue(again['accepted'])
                self.assertEqual(again['stage'],'different_stage')
                supported=r.fine.quality(r.prepared_scan(scan)[1],expected)
                for converged, geometric in [(True,True),(False,True),(True,False)]:
                    quality=dict(supported,geometry_supported=geometric,accepted=geometric,
                                 reason='geometry_pass' if geometric else 'surface_residual')
                    with patch.object(r.fine,'align',side_effect=[
                        (expected,dict(converged=False,convergence_reason='iteration_limit')),
                        (expected,dict(converged=converged,convergence_reason='fitness_rmse' if converged else 'iteration_limit'))]) as align, \
                         patch.object(r.fine,'quality',return_value=quality):
                        _,candidate=r.fit_continued(scan,expected)
                    self.assertEqual(candidate['accepted'],converged and geometric)
                    self.assertEqual(align.call_count,2 if geometric else 1)
            finally:r.close()

    def test_every_reverse_gap_runs_before_any_submap_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=fixture(root)
            run(config,root/'report.json',baseline_only=True)
            source,_,_,out,cache,_=inputs(config)
            b=out/'baseline'; rows=[json.loads(s) for s in (b/'quality.jsonl').read_text().splitlines()]
            for i in (2,5):rows[i]['accepted']=False
            (b/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in rows))
            np.savetxt(b/'candidate_trajectory.tum',np.delete(np.loadtxt(b/'candidate_trajectory.tum'),[2,5],axis=0))
            r=Recovery(b,out/'probe',cache,source,2,lambda _:None,baseline_is_fine=True)
            events=[]
            try:
                with patch.object(r,'propagate',side_effect=lambda *a:events.append(('reverse',a[0])) or {}), \
                     patch.object(r,'submap_seed',side_effect=lambda *a,**kw:events.append(('submap',a[0]))):
                    r.run(local_only=True)
                self.assertEqual([e[0] for e in events[:2]],['reverse','reverse'])
                self.assertTrue(all(e[0]=='submap' for e in events[2:]))
                window=r.seed_window(3,1.)
                with patch.object(r,'scan',side_effect=AssertionError('Submap rebuilt')):
                    self.assertIs(r.seed_window(3,1.),window)
            finally:r.close()


if __name__=='__main__':unittest.main()
