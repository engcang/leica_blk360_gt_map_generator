"""Trajectory frame, time, recovery, and read-only input regression checks."""
from pathlib import Path
import json
import lzma
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'mapping'))
from trajectory_io import Trajectory, ScanSource, PoseGap, rigid, save_json, transform
from trajectory_geometry import Matcher, voxel
from trajectory_map import write_ply, reconstruct_maps
from trajectory_native import NativeSession
from trajectory_worker import run, inputs
from trajectory_recovery import Recovery, choose_seed, save_recovery_provenance
from trajectory_temporal import screen_temporal


def fixture(root, count=8):
    """An asymmetric three-wall room, observed by a moving sensor."""
    rng = np.random.default_rng(12)
    uv = rng.uniform(-3, 3, (3500, 2))
    points = np.concatenate([np.column_stack((uv, np.full(len(uv), -1.5))),
                             np.column_stack((np.full(len(uv), 3), uv)),
                             np.column_stack((uv[:, 0], np.full(len(uv), 2.5), uv[:, 1]))]).astype(np.float32)
    scans = root/'scans'; scans.mkdir()
    stamps = 100+np.arange(count)*.1
    rows = []
    for i, stamp in enumerate(stamps):
        translation = np.array([i*.03, i*.01, 0])
        np.save(scans/f'{stamp:.6f}.npy', points-translation)
        rows.append([stamp, *translation, 0, 0, 0, 1])
    np.savetxt(root/'estimate.tum', rows)
    write_ply(root/'tls.ply', points)
    return dict(name='Synthetic room', tls_map=str(root/'tls.ply'), output_directory=str(root/'output'),
                scans=dict(format='directory', path=str(scans), min_range_m=0),
                trajectories=[dict(path=str(root/'estimate.tum'), format='tum', label='Odometry')],
                initial_alignment=np.eye(4).tolist(), period_s=.1, workers=2)


class InputTests(unittest.TestCase):
    def test_backend_switch_preserves_all_other_resume_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            old = dict(protocol='studio-recovery-v11', fine_engine='open3d-point-to-plane-tukey-v4',
                       map={'source_sha256':'a'*64}, thresholds={'plane_rms_m':.08})
            new = dict(old, correspondence=dict(backend='native-tiled-map-v1', sha256='b'*64))
            save_recovery_provenance(output, old)
            for changed in (dict(new, fine_engine='different'), dict(new, map={'source_sha256':'c'*64}),
                            dict(new, thresholds={'plane_rms_m':.2})):
                with self.assertRaisesRegex(ValueError, 'Recovery resume engine'):
                    save_recovery_provenance(output, changed)
            save_recovery_provenance(output, new)
            record=json.loads((output/'backend_transitions.jsonl').read_text())
            self.assertEqual(record['previous'], old)
            save_recovery_provenance(output, new)
            self.assertEqual(len((output/'backend_transitions.jsonl').read_text().splitlines()),1)
            with self.assertRaisesRegex(ValueError, 'Recovery resume engine'):
                save_recovery_provenance(output, dict(new, correspondence=dict(backend='native-tiled-map-v1', sha256='c'*64)))

    def test_fresh_output_accepts_external_scheduler_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            output = root/'output'; output.mkdir()
            heartbeat = dict(status='queued', stage='Waiting for a worker', updated_unix=1700000000.)
            save_json(output/'job_status.json', heartbeat)
            with patch('trajectory_worker.prepare_map', side_effect=RuntimeError('map preparation reached')):
                with self.assertRaisesRegex(RuntimeError, 'map preparation reached'):
                    run(config, root/'progress.json')
            self.assertEqual(json.loads((output/'job_status.json').read_text()), heartbeat)

    def test_fresh_output_rejects_unrelated_or_invalid_scheduler_files(self):
        for filename, contents in [('notes.txt', 'existing work'), ('job_status.json', 'not JSON'),
                                   ('job_status.json', '{"status":"queued"}'),
                                   ('job_status.json', '[]')]:
            with self.subTest(filename=filename, contents=contents), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); config = fixture(root)
                output = root/'output'; output.mkdir()
                (output/filename).write_text(contents)
                with patch('trajectory_worker.prepare_map') as prepare:
                    with self.assertRaisesRegex(ValueError, 'result folder is not empty'):
                        run(config, root/'progress.json')
                    prepare.assert_not_called()
                self.assertEqual((output/filename).read_text(), contents)

    def test_seed_window_skips_unavailable_anchor(self):
        recovery = Recovery.__new__(Recovery)
        recovery.scan = lambda index: None
        events = []
        recovery.log = events.append
        self.assertIsNone(recovery.seed_window(42, 1.))
        self.assertEqual(events, [dict(stage='submap_skipped', index=42, radius_s=1.,
                                      reason='anchor_scan_unavailable')])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_packed_voxels_preserve_order_and_representatives(self):
        rng = np.random.default_rng(17)
        points = rng.uniform(-20, 20, (20000, 3))
        points = np.concatenate((points, points[:500]+1e-6, [[np.nan, 0, 0]]))
        for dtype in (np.float32, np.float64):
            for offset in (0, 1e6):
                data = (points+offset).astype(dtype)
                finite = data[np.isfinite(data).all(axis=1)]
                _, ids = np.unique(np.floor(finite/.1).astype(np.int64), axis=0, return_index=True)
                np.testing.assert_array_equal(voxel(data, .1), finite[ids].astype(np.float32))
        self.assertEqual(voxel(np.empty((0, 3)), .1).shape, (0, 3))

    def test_parallel_map_layers_preserve_complete_ply_bytes(self):
        config = fixture(self.root, count=35)
        source, estimates, _, _, _, indices = inputs(config)
        shift = np.eye(4); shift[1, 3] = .4
        estimates.append(Trajectory(dict(config['trajectories'][0], T_pose_from_body=shift.tolist())))
        poses = {'tls_gt': {int(i): source.pose([source.timestamps[i]])[0] for i in indices}}
        outputs = []
        for name, workers, memory in [('serial', 1, 64*1024**3), ('parallel', 3, 64*1024**3), ('low-memory', 3, 1024)]:
            output = self.root/f'maps-{name}'; output.mkdir()
            with patch('parallel_runtime.availableMemory', return_value=memory):
                products, gaps = reconstruct_maps(source, poses, output, lambda text: None,
                    estimates=estimates, alignment=np.eye(4), indices=indices, workers=workers)
            self.assertEqual(gaps, [])
            outputs.append(products)
        self.assertEqual(set(outputs[0]), {'tls_gt', 'estimate_0', 'estimate_1'})
        for key in outputs[0]:
            self.assertEqual(outputs[0][key]['sha256'], outputs[1][key]['sha256'])
            self.assertEqual(outputs[0][key]['sha256'], outputs[2][key]['sha256'])

    def test_formats_frames_units_and_gaps(self):
        times = np.array([1, 1.1, 1.2, 2, 2.1])
        rotations = Rotation.from_euler('xyz', np.arange(15).reshape(5, 3)*.01)
        rows = np.column_stack((times, np.arange(15).reshape(5, 3)*.1, rotations.as_quat()))
        tum = self.root/'path.tum'; np.savetxt(tum, rows)
        reference = Trajectory(dict(path=str(tum)))
        csv = self.root/'custom.csv'
        custom = rows.copy(); custom[:, 0] *= 1e9; custom[:, 1:4] *= 1000
        np.savetxt(csv, custom, delimiter=',', header='stamp,px,py,pz,a,b,c,d', comments='')
        mapped = Trajectory(dict(path=str(csv), format='csv', time_scale=1e-9, position_scale=.001,
            columns=dict(zip(('t','x','y','z','qx','qy','qz','qw'), ('stamp','px','py','pz','a','b','c','d')))))
        np.testing.assert_allclose(mapped.matrices, reference.matrices, atol=1e-14)
        kitti, stamps = self.root/'poses.txt', self.root/'times.txt'
        np.savetxt(kitti, reference.matrices[:, :3].reshape(-1,12)); np.savetxt(stamps, times)
        loaded = Trajectory(dict(path=str(kitti), format='kitti', timestamps=str(stamps)))
        np.testing.assert_allclose(loaded.matrices, reference.matrices, atol=1e-14)
        with self.assertRaises(PoseGap): reference.pose([1.5])
        np.testing.assert_allclose(reference.pose([1.2,2]), reference.matrices[[2,3]])
        with self.assertRaises(PoseGap): reference.pose([.9])
        inverted = rows.copy(); matrices = np.linalg.inv(reference.matrices)
        inverted[:, 1:4], inverted[:, 4:8] = matrices[:, :3, 3], Rotation.from_matrix(matrices[:, :3, :3]).as_quat()
        np.savetxt(self.root/'inverse.tum', inverted)
        ext = np.eye(4); ext[0, 3] = .2
        loaded = Trajectory(dict(path=str(self.root/'inverse.tum'), inverse=True, T_pose_from_body=ext.tolist()))
        np.testing.assert_allclose(loaded.matrices, reference.matrices@ext, atol=1e-14)

    def test_point_deskew_and_input_changes(self):
        root = self.root
        rows = [[10,0,0,0,0,0,0,1],[10.1,1,0,0,0,0,0,1],[10.2,2,0,0,0,0,0,1]]
        np.savetxt(root/'t.tum', rows)
        times = np.linspace(10,10.1,100)
        fixed = np.column_stack((np.full(100,4.), np.linspace(0,2,100), np.ones(100)))
        raw = fixed-np.column_stack(((times-10)*10,np.zeros((100,2))))
        np.savez(root/'s.npz', points=raw, timestamps=times)
        save_json(root/'scans.json', {'scans':[{'timestamp':10.1,'path':'s.npz'}]})
        source = ScanSource(dict(path=str(root/'scans.json'),format='manifest',min_range_m=0), Trajectory(dict(path=str(root/'t.tum'))))
        result = source.scan(0)
        np.testing.assert_allclose(result.points, fixed-[1,0,0], atol=1e-12)
        source.verify_unchanged()
        with (root/'t.tum').open('a') as f: f.write('# modified\n')
        with self.assertRaisesRegex(ValueError, 'changed'): source.verify_unchanged()

    def test_output_must_not_overlap_inputs(self):
        config = fixture(self.root)
        config['output_directory'] = config['scans']['path']+'/output'
        with self.assertRaisesRegex(ValueError, 'outside'): inputs(config)

    def test_rank_deficiency_is_not_six_dof(self):
        cache = self.root
        x,y = np.meshgrid(np.linspace(-3,3,80), np.linspace(-3,3,80))
        points = np.column_stack((x.ravel(), y.ravel(), np.zeros(x.size)))
        np.save(cache/'points.npy', points)
        np.save(cache/'normals.npy', np.tile([0.,0.,1.], (len(points),1)))
        np.save(cache/'normal_valid.npy', np.ones(len(points),bool))
        q = Matcher(cache,2).quality(points,np.eye(4))
        self.assertFalse(q['accepted']); self.assertEqual(q['reason'],'weak_directions')
        self.assertEqual(q['observable_directions'],3); self.assertEqual(len(q['weak_directions']),3)
        self.assertEqual(q['coverage_20cm'],1)

    def test_lossless_extracted_adapter_and_legacy_rejection(self):
        import yaml
        lidar = self.root/'lidar'; lidar.mkdir()
        height, width = 16, 32
        config = {'range_dtype':'<u4', 'sensor_info':{'lidar_data_format':{'pixels_per_column':height,'columns_per_frame':width},
            'beam_intrinsics':{'beam_azimuth_angles':[0]*height,'beam_altitude_angles':np.linspace(-30,30,height).tolist(),
                               'beam_to_lidar_transform':np.eye(4).ravel().tolist()}}}
        (lidar/'meta.yaml').write_text(yaml.safe_dump(config))
        ranges = np.full((height,width),5000,dtype='<u4')
        ranges[0,0]=70000  # No uint16 wrap into a false near return.
        payload = lzma.compress(ranges.tobytes())
        times = lzma.compress(np.linspace(10,10.1,width).astype('<f8').tobytes())
        (lidar/'rng').write_bytes(payload); (lidar/'tcol').write_bytes(times)
        np.savez(lidar/'meta.npz', ts=[10.], rng_offset=[0,len(payload)], ts_offset=[0,len(times)])
        np.savetxt(self.root/'poses.tum',[[10,0,0,0,0,0,0,1],[10.1,0,0,0,0,0,0,1]])
        trajectory=Trajectory({'path':str(self.root/'poses.tum')})
        source=ScanSource({'path':str(self.root),'format':'tartanstar'},trajectory)
        scan=source.scan(0)
        self.assertEqual(len(scan.points),height*width-1)
        np.testing.assert_allclose(np.linalg.norm(scan.points,axis=1),5.,atol=1e-6)
        legacy=lzma.compress(ranges.astype('<u2').tobytes());(lidar/'rng').write_bytes(legacy)
        np.savez(lidar/'meta.npz',ts=[10.],rng_offset=[0,len(legacy)],ts_offset=[0,len(times)])
        with self.assertRaisesRegex(ValueError,'uint32'):ScanSource({'path':str(self.root),'format':'tartanstar'},trajectory)

    def test_large_corrections_and_conflicting_seeds_stay_rejected(self):
        recovery=Recovery.__new__(Recovery)
        recovery.rows=[{'index':i,'timestamp':t,'accepted':True} for i,t in enumerate([0,.1,20,40])]
        recovery.original_poses=np.array([[0,0,0,0,0,0,0,1],[.1,0,0,0,0,0,0,1],[20,4.94,0,0,0,0,.707106781,.707106781],[40,0,0,0,0,0,0,1]])
        class Source:
            def pose(self, stamps):return np.tile(np.eye(4),(len(stamps),1,1))
        recovery.source=Source();recovery.poses={};recovery.quality={};recovery.quarantined=[]
        from point_to_plane import FINE_ENGINE
        recovery.baseline_is_fine=True
        for q in recovery.rows:
            q.update(validation_map_voxel_m=.01, validation_protocol='independent-map-context-v2',
                     fine=dict(engine=FINE_ENGINE, converged=True))
        recovery.keep_baseline()
        self.assertIn(2,recovery.quarantined);self.assertNotIn(2,recovery.poses)
        recovery.conflicted=set();logs=[];recovery.log=logs.append
        translated=np.eye(4);translated[0,3]=1
        candidates={7:(np.eye(4),{})}
        recovery.merge_direction(candidates,{7:(translated,{})})
        self.assertIn(7,recovery.conflicted);self.assertNotIn(7,candidates)

    def test_temporal_screen_rejects_a_wrong_corridor_run_not_only_its_boundaries(self):
        n=100;times=10+np.arange(n)*.1
        estimates=np.tile(np.eye(4),(n,1,1));estimates[:,0,3]=np.arange(n)*.08
        class Source:
            def pose(self, stamps):return estimates[np.rint((np.asarray(stamps)-10)*10).astype(int)]
        poses={i:T.copy() for i,T in enumerate(estimates)}
        # Every pose could fit a repeated corridor independently. A locally
        # consistent wrong run still contradicts both chronological ends.
        for i in range(30,70):poses[i][0,3]+=6
        kept,edges=screen_temporal(poses,dict(enumerate(times)),Source())
        self.assertEqual(set(poses)-set(kept),set(range(30,70)))
        self.assertTrue(edges)
        self.assertFalse(screen_temporal(kept,dict(enumerate(times)),Source())[1])
        # A genuine return in the measured motion is never forbidden by a
        # position/time ordering rule or by a preset number of loops.
        estimates[50:,0,3]=estimates[49,0,3]-np.arange(1,51)*.08
        kept,edges=screen_temporal(dict(enumerate(estimates)),dict(enumerate(times)),Source())
        self.assertEqual(len(kept),n);self.assertFalse(edges)

    def test_temporal_screen_preserves_separate_time_components(self):
        times={0:1.,1:1.1,2:10.,3:10.1}
        poses={i:np.eye(4) for i in times}
        poses[2][1,3]=poses[3][1,3]=5
        class Source:
            def pose(self, stamps):return np.tile(np.eye(4),(len(stamps),1,1))
        kept,edges=screen_temporal(poses,times,Source())
        self.assertEqual(len(kept),4);self.assertFalse(edges)

    def test_map_export_rejects_shifted_scan_pose_pairing(self):
        config=fixture(self.root)
        source,*_=inputs(config)
        with self.assertRaisesRegex(ValueError,'timestamp mismatch'):
            reconstruct_maps(source,{'tls_gt':{0:np.eye(4)}},self.root,lambda _:None,
                             pose_timestamps={'tls_gt':{0:source.timestamps[0]+.1}})

    def test_bad_endpoints_do_not_discard_the_supported_interior(self):
        times={i:1+i*.1 for i in range(60)}
        poses={i:np.eye(4) for i in times}
        for i in list(range(3))+list(range(57,60)):poses[i][0,3]=6
        class Source:
            def pose(self, stamps):return np.tile(np.eye(4),(len(stamps),1,1))
        kept,edges=screen_temporal(poses,times,Source())
        self.assertEqual(set(kept),set(range(3,57)));self.assertTrue(edges)

    def test_seed_selection_compares_support_instead_of_first_convergence(self):
        shifted=np.eye(4);shifted[0,3]=.35
        weaker=dict(T=shifted,costs=[.014,.013,.014,.014],accepted=True)
        better=dict(T=np.eye(4),costs=[.004,.004,.003,.004],accepted=True)
        np.testing.assert_array_equal(choose_seed([weaker,better]),np.eye(4))
        self.assertIsNone(choose_seed([weaker,dict(better,accepted=False)]))
        self.assertIsNone(choose_seed([dict(weaker,costs=[.0042]*4),better]))

    def test_fine_validation_does_not_invent_plane_observability(self):
        from trajectory_fine import FineMatcher
        from point_to_plane import PointToPlaneTarget
        x,y=np.meshgrid(np.arange(-1,1,.01),np.arange(-1,1,.01))
        points=np.column_stack((x.ravel(),y.ravel(),np.zeros(x.size)))
        fine=FineMatcher.__new__(FineMatcher);fine.workers=2
        fine.target=PointToPlaneTarget(points,2)
        quality=fine.quality(points[::41],np.eye(4))
        self.assertEqual(quality['observable_directions'],3)
        self.assertFalse(quality['accepted'])

    def test_cached_fine_normals_include_neighbors_across_tile_edges(self):
        import open3d as o3d
        from trajectory_fine import FineMatcher
        x,y=np.meshgrid(np.arange(7.85,8.15,.009),np.arange(.1,.4,.009))
        dx=x.ravel()-8
        points=np.column_stack((x.ravel(),y.ravel(),.2+.4*dx+5*dx*dx)).astype('<f4')
        # Avoid exactly tied 40th neighbors on a synthetic regular grid.
        points += np.random.default_rng(23).normal(0,1e-4,points.shape).astype('<f4')
        fine=FineMatcher.__new__(FineMatcher);fine.folder=self.root/'tiles';fine.folder.mkdir()
        from collections import OrderedDict
        fine.index={'tiles':{}};fine.workers=2;fine.tile_cache=OrderedDict();fine.cache_bytes=0;fine.cache_limit=1024**3
        keys=np.floor(points/8).astype(int)
        reference=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points.astype(float)))
        reference.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=.04,max_nn=40))
        expected=np.asarray(reference.normals)
        for tile in np.unique(keys,axis=0):
            key='_'.join(str(v) for v in tile);use=np.all(keys==tile,axis=1)
            points[use].tofile(fine.folder/(key+'.bin'));fine.index['tiles'][key]=int(use.sum())
        for tile in np.unique(keys,axis=0):
            key='_'.join(str(v) for v in tile);use=np.all(keys==tile,axis=1)
            actual=fine.tile_normals(key).copy()
            np.testing.assert_allclose(abs(np.einsum('ij,ij->i',actual,expected[use])),1,atol=1e-8)
            np.testing.assert_array_equal(fine.tile_normals(key),actual)


@unittest.skipUnless(os.environ.get('BLK_REGISTRATION_EXECUTABLE'), 'native engine path required')
class NativeTests(unittest.TestCase):
    def test_reference_to_native_resume_retains_completed_fits(self):
        from native_correspondence import libraryPath
        if not libraryPath().is_file():
            self.skipTest('Build blk_correspondence for backend-switch integration')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=fixture(root);report=root/'progress.json'
            with patch.dict(os.environ, BLK_CORRESPONDENCE_BACKEND='python'):
                run(config,report)
            output=root/'output'
            trajectory=(output/'recovery/candidate_trajectory.tum').read_bytes()
            journal=(output/'baseline/forward_attempts.jsonl').read_bytes()
            with patch.dict(os.environ, BLK_CORRESPONDENCE_BACKEND='cpp'), \
                    patch('trajectory_fine.FineMatcher.align', side_effect=AssertionError('Completed fit repeated')):
                run(config,report)
            self.assertEqual(trajectory,(output/'recovery/candidate_trajectory.tum').read_bytes())
            self.assertEqual(journal,(output/'baseline/forward_attempts.jsonl').read_bytes())
            self.assertEqual(json.loads(report.read_text())['status'],'completed')
            self.assertTrue((output/'recovery/backend_transitions.jsonl').is_file())

    def test_partial_field_of_view_uses_neighbor_geometry_without_extra_fits(self):
        from trajectory_fine import FineMatcher
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            # Three mapped walls cover only about one third of each sensor scan.
            # Unmapped distant surfaces cannot be ICP inliers or validation support.
            rng = np.random.default_rng(701)
            outside = rng.uniform([20, 20, 20], [30, 30, 30], (22000, 3)).astype(np.float32)
            for path in (root/'scans').glob('*.npy'):
                np.save(path, np.r_[np.load(path), outside])
            original = FineMatcher.align; calls = []
            def recorded(matcher, points, initial, **kwargs):
                calls.append(1)
                return original(matcher, points, initial, **kwargs)
            with patch.object(FineMatcher, 'align', new=recorded):
                run(config, root/'progress.json', local_only=True)
            quality = [json.loads(line) for line in (root/'output/recovery/quality.jsonl').read_text().splitlines()]
            self.assertEqual(len(calls), 8)
            for q in quality:
                self.assertTrue(q['accepted'], q)
                self.assertLess(q['coverage_20cm'], .65)
                self.assertEqual(q['acceptance_scope'], 'independent-full-rank-scans')
                self.assertEqual(len(q['context_validation']['witness_indices']), 2)
            np.testing.assert_allclose(np.loadtxt(root/'output/recovery/candidate_trajectory.tum'),
                                       np.loadtxt(root/'estimate.tum'), atol=1e-4)
            before = (root/'output/recovery/candidate_trajectory.tum').read_bytes()
            run(config, root/'progress.json', local_only=True)
            self.assertEqual((root/'output/recovery/candidate_trajectory.tum').read_bytes(), before)

    def test_weak_single_planes_require_jointly_fitted_independent_submap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root, count=9)
            for i, path in enumerate(sorted((root/'scans').glob('*.npy'))):
                points = np.load(path)
                np.save(path, points[(i % 3)*3500:((i % 3)+1)*3500])
            A = np.eye(4); A[:3, 3] = [.08, -.06, .04]
            config['initial_alignment'] = A.tolist()
            run(config, root/'progress.json', local_only=True)
            baseline = [json.loads(line) for line in (root/'output/baseline/quality.jsonl').read_text().splitlines()]
            self.assertTrue(all(not q['accepted'] for q in baseline))
            quality = [json.loads(line) for line in (root/'output/recovery/quality.jsonl').read_text().splitlines()]
            accepted = [q for q in quality if q['accepted']]
            self.assertGreaterEqual(len(accepted), 3)
            for q in accepted:
                self.assertLess(q['observable_directions'], 6)
                self.assertEqual(q['acceptance_scope'], 'jointly-fitted-submap')
                self.assertTrue(q['context_validation']['shared_transform_fitted'])
                self.assertEqual(q['context_validation']['observable_directions'], 6)
                self.assertEqual(q['context_validation']['training_observable_directions'], 6)
                self.assertEqual(q['context_validation']['optimizer_observable_directions'], 6)
                self.assertTrue(q['fine']['converged'])
            expected = np.loadtxt(root/'estimate.tum')[[q['index'] for q in accepted]]
            np.testing.assert_allclose(np.loadtxt(root/'output/recovery/candidate_trajectory.tum'), expected, atol=1e-4)

    def test_a_shared_submap_of_only_one_plane_still_has_no_six_dof_gt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root, count=8)
            for path in (root/'scans').glob('*.npy'):
                np.save(path, np.load(path)[:3500])
            run(config, root/'progress.json', local_only=True)
            quality = [json.loads(line) for line in (root/'output/recovery/quality.jsonl').read_text().splitlines()]
            self.assertTrue(all(not q['accepted'] for q in quality))
            self.assertEqual((root/'output/recovery/candidate_trajectory.tum').stat().st_size, 0)
            attempts = [json.loads(line) for line in (root/'output/recovery/attempts.jsonl').read_text().splitlines()]
            seeds = [q for q in attempts if q['stage'] == 'submap_seed']
            self.assertTrue(seeds)
            self.assertTrue(all(not q['accepted'] for q in seeds))
            self.assertTrue(all(q['training_quality']['observable_directions'] == 3 for q in seeds))

    def test_weak_shared_training_fit_cannot_borrow_rank_from_heldout_walls(self):
        from trajectory_worker import baseline
        from trajectory_map import prepare_map
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root, count=6)
            for i, path in enumerate(sorted((root/'scans').glob('*.npy'))):
                points = np.load(path)
                np.save(path, points[(i % 3)*3500:((i % 3)+1)*3500])
            source, _, tls, output, cache, indices = inputs(config)
            output.mkdir()
            prepare_map(tls, cache, 2, lambda _:None)
            baseline(config, source, cache, output/'baseline', indices, 2, lambda _:None)
            recovery = Recovery(output/'baseline', root/'training-rank-check', cache, source, 2, lambda _:None,
                                baseline_is_fine=True)
            try:
                anchor = source.scan(0)
                floor_scan = source.scan(3)
                train = transform(floor_scan.points, np.linalg.inv(anchor.frontend_T)@floor_scan.frontend_T)
                train = train[np.all(abs(train[:, :2]) < 1., axis=1)]
                validators = [source.scan(i) for i in (1, 2, 4)]
                initial = np.eye(4); initial[0, 3] = .07
                # Training floor has no lateral constraint. Held-out walls can
                # refine by 7cm (below the agreement limit), but must not make
                # an unfitted shared lateral coordinate into GT.
                result = recovery.validate_seed(anchor, train, validators, initial, 'training-rank-test', 1.)
                self.assertIsNone(result)
                self.assertEqual(recovery.seed_hypotheses, [])
            finally:
                recovery.close()
            attempts = [json.loads(line) for line in (root/'training-rank-check/attempts.jsonl').read_text().splitlines()]
            result = next(q for q in attempts if q['stage'] == 'submap_seed')
            self.assertTrue(result['fine']['converged'])
            self.assertEqual(result['training_quality']['observable_directions'], 3)
            self.assertEqual(result['fine']['optimizer_information']['observable_directions'], 3)
            self.assertEqual(result['reason'], 'shared_fit_weak_geometry')
            self.assertEqual(result['checks'], [])
            self.assertAlmostEqual(result['T_map_from_base'][0][3], .07, places=5)

    def test_global_fallback_does_not_repeat_reverse_tracking(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            run(config, root/'progress.json')
            baseline = root/'output/baseline'
            rows = [json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
            rows[3]['accepted'] = False
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in rows))
            np.savetxt(baseline/'candidate_trajectory.tum', np.delete(np.loadtxt(root/'estimate.tum'), 3, axis=0))
            source, _, _, _, cache, _ = inputs(config)
            recovery = Recovery(baseline, root/'no-repeat', cache, source, 2, lambda _:None, baseline_is_fine=True)
            try:
                with patch.object(recovery, 'propagate', return_value={}) as propagation, \
                     patch.object(recovery, 'submap_seed', return_value=None) as seeds:
                    recovery.run()
                self.assertEqual([call.args[-1] for call in propagation.call_args_list], ['reverse_fine'])
                self.assertEqual([call.kwargs['allow_global'] for call in seeds.call_args_list], [False, True])
            finally:
                recovery.close()

    def test_fine_forward_failure_tries_reverse_before_any_submap(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=fixture(root)
            run(config,root/'progress.json')
            baseline=root/'output/baseline'
            rows=[json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
            expected=np.loadtxt(baseline/'candidate_trajectory.tum')
            rows[3]['accepted']=False
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in rows))
            np.savetxt(baseline/'candidate_trajectory.tum',np.delete(expected,3,axis=0))
            source,_,_,_,cache,_=inputs(config)
            recovery=Recovery(baseline,root/'reverse-only',cache,source,2,lambda _:None,baseline_is_fine=True)
            try:
                with patch.object(recovery,'propagate',wraps=recovery.propagate) as propagate, \
                     patch.object(recovery,'submap_seed',side_effect=AssertionError('Reverse already recovered the gap')):
                    recovery.run(local_only=True)
                    self.assertEqual([call.args[-1] for call in propagate.call_args_list],['reverse_fine'])
                    self.assertEqual(propagate.call_args.args[0],[3])
            finally:recovery.close()
            np.testing.assert_allclose(np.loadtxt(root/'reverse-only/candidate_trajectory.tum'),expected,atol=1e-4)

    def test_forward_pass_refines_every_pose_despite_large_global_slam_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=fixture(root,count=20)
            expected=np.loadtxt(root/'estimate.tum')
            drifted=expected.copy();drifted[:,1]+=10+np.arange(len(drifted))*.04
            np.savetxt(root/'estimate.tum',drifted)
            A=np.eye(4);A[0,3]=-10;config['initial_alignment']=A.tolist()
            run(config,root/'progress.json',local_only=True)
            rows=[json.loads(line) for line in (root/'output/baseline/quality.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows),len(expected))
            for q in rows:
                self.assertTrue(q['accepted'],q)
                self.assertEqual(q['stage'],'forward_fine')
                self.assertTrue(q['fine']['converged'])
                self.assertEqual(q['fine']['source_voxel_m'],.01)
                self.assertEqual(q['validation_map_voxel_m'],.01)
                self.assertGreaterEqual(q['validation_s'],0)
            np.testing.assert_allclose(np.loadtxt(root/'output/recovery/candidate_trajectory.tum'),expected,atol=1e-4)

    def test_corrupt_accepted_timestamp_is_removed_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            run(config, root/'progress.json')
            baseline = root/'output/baseline'
            rows = [json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
            poses = np.loadtxt(baseline/'candidate_trajectory.tum')
            # Both checkpoint files agree with each other, and the fine-engine
            # metadata is current, but this is not index 3's real scan anchor.
            rows[3]['timestamp'] += .03; poses[3, 0] += .03
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in rows))
            np.savetxt(baseline/'candidate_trajectory.tum', poses)
            source, _, _, _, cache, _ = inputs(config)
            output = root/'timestamp-audit'
            recovery = Recovery(baseline, output, cache, source, 2, lambda _:None, baseline_is_fine=True)
            try:
                with patch.object(recovery.fine, 'align', side_effect=AssertionError('Trusted metadata path must audit timestamps')):
                    recovery.keep_baseline()
                self.assertNotIn(3, recovery.poses)
                self.assertFalse(recovery.quality[3]['accepted'])
                self.assertEqual(recovery.quality[3]['reason'], 'scan_timestamp_mismatch')
                recovery.checkpoint()
            finally:
                recovery.close()
            saved = [json.loads(line) for line in (output/'quality.jsonl').read_text().splitlines()]
            self.assertFalse(saved[3]['accepted'])
            self.assertEqual(saved[3]['reason'], 'scan_timestamp_mismatch')
            np.testing.assert_allclose(np.loadtxt(output/'candidate_trajectory.tum'), np.delete(poses, 3, axis=0), atol=1e-9)

    def test_imported_accepted_poses_are_initial_guesses_for_fine_refitting(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=fixture(root)
            run(config,root/'progress.json')
            baseline=root/'output/baseline'
            rows=[json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
            expected=np.loadtxt(baseline/'candidate_trajectory.tum')
            shifted=expected.copy();shifted[:,1]+=.03
            np.savetxt(baseline/'candidate_trajectory.tum',shifted)
            for q in rows:
                q.pop('fine');q.pop('validation_map_voxel_m')
                q['T_map_from_base'][0][3]+=.03
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in rows))
            source,_,_,_,cache,_=inputs(config)
            recovery=Recovery(baseline,root/'refitted',cache,source,2,lambda _:None)
            try:
                recovery.keep_baseline();recovery.checkpoint()
            finally:recovery.close()
            np.testing.assert_allclose(np.loadtxt(root/'refitted/candidate_trajectory.tum'),expected,atol=1e-4)
            refitted=[json.loads(line) for line in (root/'refitted/quality.jsonl').read_text().splitlines()]
            self.assertTrue(all(q['accepted'] and q['stage']=='baseline_fine' and not q['baseline_reused'] for q in refitted))

    def test_conflicting_tracks_retry_an_independently_verified_window(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=fixture(root,count=10)
            run(config,root/'progress.json')
            baseline=root/'output/baseline'
            rows=[json.loads(s) for s in (baseline/'quality.jsonl').read_text().splitlines()]
            for row in rows:
                if 2<=row['index']<=7:row['accepted']=False
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            all_poses=np.loadtxt(root/'estimate.tum')
            np.savetxt(baseline/'candidate_trajectory.tum',all_poses[[0,1,8,9]])
            source,_,_,_,cache,_=inputs(config)
            recovery=Recovery(baseline,root/'conflict-recovery',cache,source,2,lambda _:None)
            original=recovery.propagate
            def conflicting(indices,seed_index,seed_T,direction):
                if direction in ('forward_fine','reverse_fine'):
                    result={}
                    for i in indices:
                        T=source.scan(i).frontend_T.copy();T[0,3]+=.4 if direction=='forward_fine' else -.4
                        result[i]=(T,dict(accepted=True,plane_rms_m=.02))
                    return result
                return original(indices,seed_index,seed_T,direction)
            try:
                with patch.object(recovery,'propagate',side_effect=conflicting):recovery.run(local_only=True)
            finally:recovery.close()
            actual=np.loadtxt(root/'conflict-recovery/candidate_trajectory.tum')
            np.testing.assert_allclose(actual,all_poses,atol=1e-4)
            attempts=[json.loads(s) for s in (root/'conflict-recovery/attempts.jsonl').read_text().splitlines()]
            self.assertTrue(any(r['stage']=='conflict_relocalized' for r in attempts))

    def test_kiss_releases_its_heap_and_the_session_remains_usable(self):
        from trajectory_geometry import ply_vertices
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture(root)
            vertices=ply_vertices(root/'tls.ply')
            points=np.column_stack([vertices[k] for k in 'xyz'])
            with NativeSession(root) as native:
                previous=native.process
                coarse=voxel(points,.3)
                T=native.kiss(coarse,coarse,.3,2)
                np.testing.assert_allclose(T,np.eye(4),atol=1e-4)
                self.assertEqual(previous.poll(),0)
                self.assertIsNone(native.process)
                repeated=native.kiss(coarse,coarse,.3,2)
                np.testing.assert_allclose(repeated,T,atol=1e-4)
                self.assertIsNone(native.process)

    def test_submap_end_seed_recovers_without_a_baseline_anchor(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            run(config, root/'progress.json')
            baseline = root/'output/baseline'
            quality = [json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
            for q in quality:
                q.update(accepted=False, reason='test_lost_tracking')
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in quality))
            (baseline/'candidate_trajectory.tum').write_text('')
            source, _, _, _, cache, _ = inputs(config)
            recovered = root/'new-seed'
            recovery = Recovery(baseline, recovered, cache, source, 2, lambda text: None)
            try:
                recovery.run(local_only=True)
            finally:
                recovery.close()
            actual = np.loadtxt(recovered/'candidate_trajectory.tum')
            self.assertTrue(json.loads((recovered/'status.json').read_text())['pending_global_search'])
            recovery = Recovery(baseline, recovered, cache, source, 2, lambda text: None)
            try:
                recovery.run()
            finally:
                recovery.close()
            np.testing.assert_array_equal(np.loadtxt(recovered/'candidate_trajectory.tum'),actual)
            self.assertFalse(json.loads((recovered/'status.json').read_text())['pending_global_search'])
            expected = np.loadtxt(root/'estimate.tum')
            np.testing.assert_allclose(actual, expected, atol=1e-4)
            attempts = [json.loads(line) for line in (recovered/'attempts.jsonl').read_text().splitlines()]
            self.assertTrue(any(q['stage'] == 'submap_reverse' for q in attempts))
            verified = [q for q in attempts if q['stage'] == 'submap_seed' and q['accepted']]
            self.assertTrue(verified)
            for seed in verified:
                self.assertGreaterEqual(sum(q['accepted'] for q in seed['checks']), 2)
                self.assertTrue(all(q['index'] != seed['index'] for q in seed['checks']))

    def test_failed_initialization_accepts_supplied_alignment_on_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            config.pop('initial_alignment')
            with patch('trajectory_worker.initialize', side_effect=ValueError('initialization failed')):
                with self.assertRaisesRegex(ValueError, 'initialization failed'):
                    run(config, root/'progress.json')
            alignment = root/'initial.json'
            save_json(alignment, {'T_map_from_frontend': np.eye(4).tolist()})
            config['initial_alignment'] = str(alignment)
            run(config, root/'progress.json')
            self.assertEqual(json.loads((root/'progress.json').read_text())['accepted'], 8)
            changed = dict(config, workers=3)
            with self.assertRaisesRegex(ValueError, 'Resume inputs or settings changed'):
                run(changed, root/'progress.json')

    def test_changed_initial_alignment_cannot_reuse_uncheckpointed_forward_fits(self):
        from trajectory_recovery import fit_scan
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            calls = []
            def interrupted(fine, scan, initial):
                calls.append(scan.index)
                if len(calls) > 1:
                    raise OSError('simulated stop before checkpoint')
                return fit_scan(fine, scan, initial)
            with patch('trajectory_worker.fit_scan', side_effect=interrupted):
                with self.assertRaisesRegex(OSError, 'simulated stop'):
                    run(config, root/'progress.json')
            baseline = root/'output/baseline'
            self.assertFalse((baseline/'quality.jsonl').exists())
            journal = baseline/'forward_attempts.jsonl'
            before = journal.read_bytes()
            self.assertEqual(len(before.splitlines()), 1)
            shifted = np.eye(4); shifted[0, 3] = .05
            with patch('trajectory_worker.fit_scan') as fitting:
                with self.assertRaisesRegex(ValueError, 'Resume inputs or settings changed'):
                    run(dict(config, initial_alignment=shifted.tolist()), root/'progress.json')
                fitting.assert_not_called()
            self.assertEqual(journal.read_bytes(), before)
            with patch('trajectory_worker.fit_scan', wraps=fit_scan) as fitting:
                run(config, root/'progress.json')
                self.assertEqual(fitting.call_count, 7)
            np.testing.assert_allclose(np.loadtxt(root/'output/recovery/candidate_trajectory.tum'),
                                       np.loadtxt(root/'estimate.tum'), atol=1e-4)

    def test_interrupted_recovery_reuses_complete_fit_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);config=fixture(root)
            run(config,root/'progress.json')
            output=root/'output';baseline=output/'baseline'
            quality=[json.loads(line) for line in (baseline/'quality.jsonl').read_text().splitlines()]
            quality[3].update(accepted=False,reason='test_missing_pose')
            (baseline/'quality.jsonl').write_text(''.join(json.dumps(q)+'\n' for q in quality))
            trajectory=np.loadtxt(baseline/'candidate_trajectory.tum')
            np.savetxt(baseline/'candidate_trajectory.tum',np.delete(trajectory,3,axis=0))
            source,_,_,_,cache,_=inputs(config)
            recovered=output/'resume-test'
            r=Recovery(baseline,recovered,cache,source,2,lambda text:None)
            native_align=r.fine.align;calls=[]
            def interrupted(points,initial):
                calls.append(1)
                if len(calls)>1:raise OSError('simulated interruption')
                return native_align(points,initial)
            r.fine.align=interrupted
            try:
                with self.assertRaisesRegex(OSError,'simulated'):r.run()
            finally:r.close()
            journal=recovered/'attempts.jsonl'
            with journal.open('a') as f:
                f.write(json.dumps({'stage':'submap_seed_failed', 'index':3, 'scope':'global', 'reason':'prior failed seed'})+'\n')
            prefix=journal.read_bytes()
            self.assertTrue(prefix.endswith(b'\n'))
            with journal.open('ab') as f:f.write(b'{"incomplete":')
            resumed=Recovery(baseline,recovered,cache,source,4,lambda text:None)
            self.assertEqual(len(resumed.fit_cache),1)
            self.assertEqual(resumed.global_attempts,1)
            self.assertEqual(journal.read_bytes(),prefix)
            try:resumed.run()
            finally:resumed.close()
            result=np.loadtxt(recovered/'candidate_trajectory.tum')
            self.assertEqual(len(result),8)
            self.assertEqual(json.loads((recovered/'status.json').read_text())['global_attempts'],1)
            np.testing.assert_allclose(result[:,1:4],trajectory[:,1:4],atol=1e-4)
            receipt=json.loads((recovered/'provenance.json').read_text())
            from point_to_plane import FINE_ENGINE
            self.assertEqual(receipt['fine_engine'], FINE_ENGINE)
            self.assertEqual(receipt['iterations_per_stage'],60)
            receipt['fine_engine']='previous-nano-engine'
            save_json(recovered/'provenance.json',receipt)
            with patch('trajectory_recovery.FineMatcher') as matcher:
                with self.assertRaisesRegex(ValueError,'Recovery resume engine'):
                    Recovery(baseline,recovered,cache,source,4,lambda text:None)
                matcher.assert_not_called()

    def test_complete_pipeline_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); config = fixture(root)
            config['workers'] = 0
            backend = np.loadtxt(root/'estimate.tum'); backend[:,1] += .2
            np.savetxt(root/'backend.tum', backend)
            config['trajectories'].append(dict(path=str(root/'backend.tum'),format='tum',label='Backend'))
            report = root/'progress.json'
            run(dict(config, workers=0),report,inspect_only=True)
            self.assertEqual(json.loads(report.read_text())['status'],'ready')
            from parallel_runtime import threadBudget
            self.assertEqual(json.loads(report.read_text())['cpu_threads'],threadBudget())
            with self.assertRaisesRegex(ValueError, 'CPU threads'):
                run(dict(config, workers=-1),report,inspect_only=True)
            run(config,report)
            result = json.loads(report.read_text())
            self.assertEqual(result['status'],'completed'); self.assertGreaterEqual(result['accepted'],6)
            output = root/'output'
            for name in ('baseline','recovery'):
                quality=[json.loads(line) for line in (output/name/'quality.jsonl').read_text().splitlines()]
                for q in quality:
                    if q['accepted']:
                        self.assertTrue(q['fine']['converged'])
                        self.assertEqual(q['validation_map_voxel_m'],.01)
            before = (output/'recovery/candidate_trajectory.tum').read_bytes()
            scene = json.loads((output/'viewer.json').read_text())
            self.assertEqual({x['key'] for x in scene['maps']},{'tls_10cm','tls_gt','estimate_0','estimate_1'})
            labels = {x['key']:x['label'] for x in scene['maps']}
            self.assertEqual(labels['tls_10cm'], 'Scanned GT map · 10 cm')
            self.assertEqual(labels['tls_gt'], 'Generated ground-truth trajectory map · 10 cm')
            paths={layer['key']:np.asarray(layer['rows']) for layer in scene['trajectories']}
            np.testing.assert_allclose(paths['estimate_1'][:,1]-paths['estimate_0'][:,1],.2)
            self.assertTrue(all(x['voxel_m']==.1 for x in scene['maps'] if x['key'] in ('tls_gt','estimate_0')))
            run(config,report)
            self.assertEqual(before,(output/'recovery/candidate_trajectory.tum').read_bytes())
            changed = dict(config, period_s=.2)
            with self.assertRaisesRegex(ValueError,'Resume inputs'): run(changed,report)


if __name__ == '__main__': unittest.main()
