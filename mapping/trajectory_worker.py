"""Generate a dataset trajectory against an existing, fixed TLS map."""
import argparse
from contextlib import ExitStack, contextmanager
from pathlib import Path
import json
import os
import shutil
import sys
import time

from parallel_runtime import threadBudget, availableMemory

for _variable in ('OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_variable] = '1'

import numpy as np
from scipy.spatial.transform import Rotation

from trajectory_io import Trajectory, ScanSource, PoseGap, digest, rigid, save_json, transform, urdf_transform
from trajectory_geometry import Matcher, voxel, ply_vertices
from trajectory_map import prepare_map, reconstruct_maps
from trajectory_native import NativeSession
from point_to_plane import FINE_ENGINE
from trajectory_validation import (confirm_partial_views, jointly_observable, VALIDATION_PROTOCOL)
from trajectory_recovery import Recovery, difference, pose, fit_scan
from trajectory_fine import FineMatcher
from trajectory_temporal import screen_temporal


def record_execution(output, workers):
    from datetime import datetime, timezone
    from importlib.metadata import version
    from native_backend import backendInfo, executablePath
    from native_correspondence import backendInfo as correspondenceInfo
    sources = sorted([*Path(__file__).parent.glob('trajectory*.py'), Path(__file__).with_name('parallel_runtime.py'),
                      Path(__file__).with_name('point_to_plane.py'), Path(__file__).with_name('native_correspondence.py')])
    record = dict(started_utc=datetime.now(timezone.utc).isoformat(), python=sys.version,
                  packages={name:version(name) for name in ('numpy', 'scipy', 'open3d', 'PyYAML')},
                  source_sha256={path.name:digest(path) for path in sources},
                  native=backendInfo(), native_sha256=digest(executablePath()), cpu_threads=workers,
                  correspondence=correspondenceInfo(),
                  fine_engine=FINE_ENGINE, solver_threads=1, blas_threads=1, available_memory_bytes=availableMemory())
    with (output/'execution.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, allow_nan=False)+'\n')


@contextmanager
def exclusive(path):
    """OS-owned lock, automatically released after a stop or crash."""
    with path.open('a+b') as f:
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0)
                if not f.read(1):
                    f.write(b'0'); f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('This trajectory output or map cache is already in use') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def inputs(config):
    estimates = config.get('trajectories', [])
    if not estimates:
        raise ValueError('Select at least one estimated trajectory')
    max_gap = float(config.get('max_pose_gap_s', .5))
    trajectories = [Trajectory(t, max_gap) for t in estimates]
    scan_config = dict(config['scans'])
    if scan_config.pop('calibration_preset', None) == 'tartanstar':
        scan_config['T_body_from_sensor'] = [
            [1.8365947616616474e-6, -4.921116731060324e-7, .9999999999981923, -.00979600000008645],
            [-.4999978792691724, -.8660266281853509, 4.921116729672548e-7, -.000016454465251352013],
            [.8660266281835433, -.49999787926917244, -1.8365947616616474e-6, -.04241308783330788],
            [0, 0, 0, 1]]
    calibration = scan_config.pop('calibration', None)
    if calibration:
        path = Path(calibration['path']).expanduser().resolve(strict=True)
        if path.suffix.lower() == '.urdf':
            scan_config['T_body_from_sensor'] = urdf_transform(path, calibration.get('sensor_frame', 'os_dome_lidar'), calibration.get('body_frame', 'base_link')).tolist()
        else:
            value = json.loads(path.read_text())
            scan_config['T_body_from_sensor'] = rigid(value.get('T_body_from_sensor', value) if isinstance(value, dict) else value).tolist()
    source = ScanSource(scan_config, trajectories[0])
    if calibration:
        source.files.append(path)
        source.initial_stats = source.stats()
        source.provenance['calibration_sha256'] = digest(path)
        source.provenance['file_stats'] = source.initial_stats
    for t in trajectories[1:]:
        source.files.extend(t.files)
    if isinstance(config.get('initial_alignment'), str):
        initial_path = Path(config['initial_alignment']).expanduser().resolve(strict=True)
        source.files.append(initial_path)
        source.provenance['initial_alignment_sha256'] = digest(initial_path)
    source.initial_stats = source.stats()
    source.provenance['file_stats'] = source.initial_stats
    tls = Path(config['tls_map']).expanduser().resolve(strict=True)
    output = Path(config['output_directory']).expanduser().resolve()
    cache = Path(config.get('cache_directory') or output/'map_cache').expanduser().resolve()
    roots = [source.path if source.path.is_dir() else source.path.parent]
    sequence = Path(config['sequence_directory']).expanduser().resolve(strict=True) if config.get('sequence_directory') else None
    public_trajectory = (sequence if sequence is not None else output)/'ground_truth_trajectory.tum'
    if public_trajectory.is_symlink() or public_trajectory in source.files+[tls]:
        raise ValueError('Ground-truth output must not replace an input file or symbolic link')
    sequence_output = sequence is not None and output == sequence/'.ground_truth' and cache == output/'map_cache'
    if sequence_output and (sequence == source.path or sequence in source.path.parents):
        roots = []  # Only the dedicated result subfolder is writable; inputs stay read-only.
    for target in (output, cache):
        if any(target == root or target.is_relative_to(root) for root in roots):
            raise ValueError('Choose an output and cache folder outside the scan input folder')
        if any(p == target or p.is_relative_to(target) for p in source.files+[tls]):
            raise ValueError('Output/cache folder must not contain any input files')
    if output == cache or output.is_relative_to(cache):
        raise ValueError('Map cache must not contain the result folder')
    period = float(config.get('period_s', 0))
    indices = source.selection(period)
    if len(indices) < 3:
        raise ValueError('Fewer than three scans overlap the estimated trajectory')
    return source, trajectories, tls, output, cache, indices


def checkpoint_baseline(output, rows):
    quality = output/'quality.jsonl.tmp'
    quality.write_text(''.join(json.dumps(q, allow_nan=False)+'\n' for q in rows))
    quality.replace(output/'quality.jsonl')
    temporary = output/'candidate_trajectory.tum.tmp'
    with temporary.open('w') as f:
        for q in rows:
            if q['accepted']:
                T = np.asarray(q['T_map_from_base'])
                values = np.r_[q['timestamp'], T[:3, 3], Rotation.from_matrix(T[:3, :3]).as_quat()]
                f.write(' '.join(f'{v:.9f}' for v in values)+'\n')
    temporary.replace(output/'candidate_trajectory.tum')


def initialize(config, source, matcher, cache, output, indices, workers, progress):
    path = output/'initial_alignment.json'
    if path.exists():
        return rigid(json.loads(path.read_text())['T_map_from_frontend'])
    if config.get('initial_alignment'):
        value = config['initial_alignment']
        if isinstance(value, str):
            value = json.loads(Path(value).read_text())
        A = rigid(value.get('T_map_from_frontend', value) if isinstance(value, dict) else value)
        save_json(path, dict(T_map_from_frontend=A.tolist(), method='supplied'))
        return A
    sample_ids = indices[np.unique(np.linspace(0, len(indices)-1, min(24, len(indices))).astype(int))]
    scans = list(source.iter_scans(scan_indices=sample_ids))
    if len(scans) < 5:
        raise ValueError('Automatic initialization needs at least five supported scans')
    # Whole scans are withheld from global matching, then checked independently.
    train, held = scans[::2], scans[1::2]
    cloud = voxel(np.concatenate([transform(voxel(s.points, .2), s.frontend_T) for s in train]), .2)
    hypotheses = []
    with NativeSession(cache) as native:
        for resolution in (.3, .5):
            progress(f'Initializing trajectory with KISS-Matcher at {resolution:g} m')
            try:
                A = native.kiss(voxel(cloud, resolution), voxel(matcher.p, resolution), resolution, workers)
                A = matcher.icp(cloud, A)
                quality = [matcher.quality(voxel(s.points, .1), A@s.frontend_T) for s in held]
                supported = [(q, A@s.frontend_T) for q, s in zip(quality, held) if q.get('geometry_supported')]
                joint = jointly_observable([q for q, _ in supported], [T for _, T in supported], A,
                                           shared_transform_fitted=True, minimum_scans=2)
                count = len(supported) if joint['accepted'] else 0
                hypotheses.append(dict(passes=count, T_map_from_frontend=A.tolist(), quality=quality,
                                       initialization_validation=joint))
            except (ValueError, RuntimeError) as exc:
                hypotheses.append(dict(passes=0, error=str(exc)))
    save_json(output/'alignment_diagnostics.json', {'hypotheses': hypotheses})
    best = max(hypotheses, key=lambda h: h['passes'])
    if best['passes'] < 2:
        raise ValueError('Automatic initialization failed; supply an initial TLS-from-estimate transform and resume')
    save_json(path, best)
    return rigid(best['T_map_from_frontend'])


def baseline(config, source, cache, output, indices, workers, progress):
    output.mkdir(exist_ok=True)
    matcher = Matcher(cache, workers)
    A = initialize(config, source, matcher, cache, output, indices, workers, progress)
    del matcher
    rows, path = [], output/'forward_attempts.jsonl'
    snapshot = output/'quality.jsonl'
    if not path.exists() and snapshot.exists():
        path.write_bytes(snapshot.read_bytes())
    if path.exists():
        valid = []
        for line in path.read_text().splitlines(keepends=True):
            try:
                q = json.loads(line)
            except ValueError:
                break
            if not line.endswith('\n'):
                break
            rows.append(q); valid.append(line)
        path.write_text(''.join(valid))
    confirm_partial_views(rows, source)
    done = {q['index'] for q in rows}
    accepted = [q for q in rows if q['accepted']]
    accepted_count = len(accepted)
    last_T = np.asarray(accepted[-1]['T_map_from_base']) if accepted else None
    last_F = source.pose([accepted[-1]['timestamp']])[0] if accepted else None
    with ExitStack() as resources:
        stream = resources.enter_context(path.open('a', buffering=1))
        fine = FineMatcher(cache, workers, result_cache=output/'fine_requests')
        resources.callback(fine.close)
        for scan in source.iter_scans(scan_indices=[i for i in indices if int(i) not in done]):
            initial = A@scan.frontend_T if last_T is None else last_T@np.linalg.inv(last_F)@scan.frontend_T
            T, q = fit_scan(fine, scan, initial)
            correction = difference(T, initial)
            q['stage'] = 'forward_fine'
            if correction[0] > .5 or correction[1] > 10:
                q.update(accepted=False, reason='unverified_large_correction')
            accepted_count += int(q['accepted'])
            rows.append(q)
            # Existing independent fits are enough for partial-view context;
            # weak single-scan directions wait for a fitted submap in recovery.
            recent = []
            for candidate in reversed(rows):
                if scan.timestamp-candidate['timestamp'] > 2:
                    break
                recent.append(candidate)
            accepted_count += len(confirm_partial_views(recent, source))
            latest = next((r for r in recent if r['accepted']), None)
            if latest is not None:
                last_T = np.asarray(latest['T_map_from_base'])
                last_F = source.pose([latest['timestamp']])[0]
            stream.write(json.dumps(q, allow_nan=False)+'\n')
            if len(rows) % 50 == 0:
                checkpoint_baseline(output, rows)
                save_json(output/'input_quality.json', source.scan_quality)
                progress(f'Localizing scans at 1 cm: {len(rows)}/{len(indices)}, {accepted_count} accepted')
        fine.verify_unchanged()
    checkpoint_baseline(output, rows)
    save_json(output/'input_quality.json', source.scan_quality)
    return A


def import_baseline(folder, output, source, binding):
    folder = Path(folder).expanduser().resolve(strict=True)
    previous = json.loads((folder/'provenance.json').read_text())
    if previous['input']['frontend_sha256'] != source.provenance['frontend_sha256'] or previous['map']['source_sha256'] != binding['source_sha256']:
        raise ValueError('Baseline refers to a different TLS map or estimated trajectory')
    metadata = previous['input'].get('lidar_metadata_sha256')
    if metadata and (source.kind != 'tartanstar' or digest(source.lidar/'meta.npz') != metadata):
        raise ValueError('Baseline LiDAR metadata does not match the selected sequence')
    output.mkdir(exist_ok=True)
    for name in ('candidate_trajectory.tum', 'quality.jsonl', 'initial_alignment.json', 'input_quality.json', 'provenance.json'):
        destination, original = output/name, folder/name
        if name == 'input_quality.json' and not original.exists():
            if not destination.exists():
                save_json(destination, dict(legacy_baseline_details_unavailable=True, skipped_scan_intervals=[]))
            continue
        if destination.exists():
            if digest(destination) != digest(original):
                raise ValueError('Imported baseline changed since the first run')
        else:
            shutil.copyfile(original, destination)
    return rigid(json.loads((output/'initial_alignment.json').read_text())['T_map_from_frontend'])


def export_viewer(config, source, trajectories, output, cache, A, progress, recovery_name='recovery', preview=False):
    result = output/recovery_name
    quality = [json.loads(line) for line in (result/'quality.jsonl').read_text().splitlines()]
    rows = np.loadtxt(result/'candidate_trajectory.tum', ndmin=2) if (result/'candidate_trajectory.tum').stat().st_size else np.empty((0, 8))
    accepted = [q for q in quality if q['accepted']]
    if len(accepted) != len(rows):
        raise ValueError('Result trajectory and quality rows disagree')
    if any(abs(q['timestamp']-row[0]) > 1e-6 for q, row in zip(accepted, rows)):
        raise ValueError('Result scan/pose timestamps disagree')
    selections = {'tls_gt': {q['index']: pose(row) for q, row in zip(accepted, rows)}}
    _, temporal_errors = screen_temporal(selections['tls_gt'], {q['index']:q['timestamp'] for q in accepted}, source)
    if temporal_errors:
        raise ValueError('Result still contains inconsistent temporal motion; recover it before map export')
    layers = [dict(key='tls_gt', label='Generated ground-truth trajectory', color='#FF9B54', rows=rows.tolist(),
                   gap_s=max(float(config.get('max_pose_gap_s', .5)), 1.5*float(config.get('period_s', 0))))]
    indices = source.selection(float(config.get('period_s', 0)))
    colors = ['#5EC8DE', '#CB8CFF', '#FFB75E', '#8CDB9B']
    for n, trajectory in enumerate(trajectories):
        key = f'estimate_{n}'
        aligned = A@trajectory.matrices
        data = np.column_stack((trajectory.times, aligned[:, :3, 3], Rotation.from_matrix(aligned[:, :3, :3]).as_quat()))
        layers.append(dict(key=key, label=config['trajectories'][n].get('label') or ('Odometry' if n == 0 else f'Estimate {n+1}'), color=colors[n%len(colors)], rows=data.tolist()))
    products, input_gaps = {}, []
    if not preview:
        products, input_gaps = reconstruct_maps(source, selections, output, progress, estimates=trajectories, alignment=A, indices=indices,
                                              workers=threadBudget(config.get('workers')),
                                              pose_timestamps={'tls_gt':{q['index']:q['timestamp'] for q in accepted}})
    maps = [dict(key='tls_10cm', label='Scanned GT map · 10 cm', path=str(cache/'tls_10cm.ply'), color='#FFFFFF')]
    for layer in layers:
        if layer['key'] in products:
            maps.append(dict(key=layer['key'], label=layer['label']+' map · 10 cm', color=layer['color'], **products[layer['key']]))
    from trajectory_geometry import ply_vertices
    coarse = np.load(cache/'points.npy', mmap_mode='r')
    bounds = [coarse.min(0).tolist(), coarse.max(0).tolist()]
    for item in maps:
        item.setdefault('points', len(ply_vertices(Path(item['path']))))
        item.setdefault('bounds', bounds)
        item['path'] = os.path.relpath(item['path'], output)
    input_quality = json.loads((output/'baseline/input_quality.json' if preview else result/'input_quality.json').read_text())
    input_quality['skipped_scan_intervals'] = input_gaps
    input_quality['checked_selected_scans'] = len(indices)
    save_json(output/'input_quality.json', input_quality)
    save_json(output/'viewer.json', dict(schema_version=1, name=config.get('name', source.sequence), preview=preview, maps=maps,
        trajectories=layers, gap_s=float(config.get('max_pose_gap_s', .5)),
        quality=[{k:q[k] for k in ('index','timestamp','accepted','reason','plane_rms_m','coverage_20cm','observable_directions') if k in q} for q in quality],
        input_quality=input_quality,
        T_map_from_estimate=A.tolist(), trajectory_sha256=digest(result/'candidate_trajectory.tum')))
    if not preview:
        save_json(output/'products.json', products)
        publish_trajectory(config, result/'candidate_trajectory.tum', output)


def publish_trajectory(config, source, output):
    destination = (Path(config['sequence_directory']).expanduser().resolve()
                   if config.get('sequence_directory') else output)/'ground_truth_trajectory.tum'
    for estimate in config.get('trajectories', []):
        if Path(estimate['path']).expanduser().resolve() == destination:
            raise ValueError('The output ground-truth trajectory must not replace an input trajectory')
    if destination.is_symlink():
        raise ValueError('Ground-truth output must not be a symbolic link')
    temporary = destination.with_suffix('.tum.pending')
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def pending_job_status(path):
    """Recognize the external scheduler's heartbeat before any result exists."""
    if path.name != 'job_status.json' or not path.is_file() or path.is_symlink():
        return False
    try:
        status = json.loads(path.read_text(encoding='utf-8'))
        updated = status.get('updated_unix')
        return (status.get('status') in ('queued', 'running')
                and isinstance(status.get('stage'), str)
                and type(updated) in (int, float) and np.isfinite(updated) and updated > 0)
    except (OSError, ValueError, TypeError, OverflowError, AttributeError):
        return False


def run(config, report, inspect_only=False, baseline_only=False, recovery_only=False, recovery_name='recovery'):
    if not recovery_name.startswith('recovery') or Path(recovery_name).name != recovery_name or '\\' in recovery_name:
        raise ValueError('Recovery name must be a single directory name starting with recovery')
    def progress(stage, status='running', **values):
        save_json(report, dict(status=status, stage=stage, **values))
        print(stage, flush=True)
    workers = threadBudget(config.get('workers'))
    source, trajectories, tls, output, cache, indices = inputs(config)
    probe = next(source.iter_scans(scan_indices=indices[:min(30, len(indices))]), None)
    if probe is None:
        raise ValueError('No usable scans in the first 30 selected scans')
    if inspect_only:
        tls_points = len(ply_vertices(tls))
        progress(f'Inputs ready: {len(indices):,} scans · {tls_points:,} TLS points · {workers} CPU threads', 'ready', scans=len(indices), tls_points=tls_points, cpu_threads=workers, available_memory_bytes=availableMemory(), first_timestamp=probe.timestamp,
                 first_scan_points=len(probe.points), trajectories=[dict(poses=len(t.times), start=float(t.times[0]), end=float(t.times[-1])) for t in trajectories])
        return
    if output.exists() and not (output/'provenance.json').exists():
        if any(p.name not in ('.trajectory.lock', 'map_cache') and not pending_job_status(p)
               for p in output.iterdir()):
            raise ValueError('The selected result folder is not empty; choose a new folder or resume an existing result')
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        locks.enter_context(exclusive(output/'.trajectory.lock'))
        locks.enter_context(exclusive(cache/'.map.lock'))
        for temporary in cache.glob('trajectory-native-*'):
            if temporary.is_dir() and (temporary/'.blk-native-session').is_file():
                shutil.rmtree(temporary)
        progress(f'Using up to {workers} CPU threads')
        binding = prepare_map(tls, cache, workers, progress)
        provenance = dict(protocol='studio-trajectory-v3', validation_protocol=VALIDATION_PROTOCOL, fine_engine=FINE_ENGINE, config=config, input=source.provenance, map=binding,
                          estimate_sha256=[{str(p): digest(p) for p in t.files} for t in trajectories])
        receipt = output/'provenance.json'
        if receipt.exists():
            previous = json.loads(receipt.read_text())
            if previous != provenance:
                old_config, new_config = dict(previous['config']), dict(config)
                old_config.pop('initial_alignment', None)
                new_config.pop('initial_alignment', None)
                # Before the first checkpoint, completed fits already live in
                # the append-only journal. A new initial transform must not
                # silently reuse those poses under a different provenance.
                logs = [output/'baseline'/name for name in ('quality.jsonl', 'forward_attempts.jsonl')]
                can_initialize = (all(not log.exists() or log.stat().st_size == 0 for log in logs)
                                  and bool(config.get('initial_alignment')))
                previous_without_config = {k:v for k,v in previous.items() if k != 'config'}
                current_without_config = {k:v for k,v in provenance.items() if k != 'config'}
                if can_initialize:
                    for document, settings in ((previous_without_config, previous['config']), (current_without_config, config)):
                        document['input'] = dict(document['input'])
                        document['input'].pop('initial_alignment_sha256', None)
                        initial = settings.get('initial_alignment')
                        if isinstance(initial, str):
                            initial = str(Path(initial).expanduser().resolve())
                            document['input']['file_stats'] = [row for row in document['input']['file_stats'] if row[0] != initial]
                if not (can_initialize and old_config == new_config and previous_without_config == current_without_config):
                    raise ValueError('Resume inputs or settings changed; choose a new output folder')
                (output/'baseline/initial_alignment.json').unlink(missing_ok=True)
        save_json(receipt, provenance)
        record_execution(output, workers)
        start = time.monotonic()
        from trajectory_stages import ready_baseline, mark_baseline_ready
        ready = ready_baseline(output, adopt_legacy=True, selected=len(indices))
        if ready:
            A = rigid(json.loads((output/'baseline/initial_alignment.json').read_text())['T_map_from_frontend'])
        elif recovery_only:
            raise ValueError('Finish the initial 1 cm fitting before running recovery')
        elif config.get('baseline_directory'):
            A = import_baseline(config['baseline_directory'], output/'baseline', source, binding)
        else:
            A = baseline(config, source, cache, output/'baseline', indices, workers, progress)
        source.verify_unchanged()
        if ready is None:
            ready = mark_baseline_ready(output, len(indices))
        if baseline_only:
            progress(f"Initial 1 cm fitting ready: {ready['accepted']}/{ready['processed']} poses; recovery pending",
                     'baseline_ready', phase='baseline_ready', accepted=ready['accepted'], processed=ready['processed'],
                     selected=len(indices), elapsed_s=time.monotonic()-start, output_directory=str(output))
            return
        progress('Preparing 1 cm point-to-plane / Tukey recovery')
        last_preview = [0.]
        def preview():
            if time.monotonic()-last_preview[0] >= 60:
                export_viewer(config, source, trajectories, output, cache, A, progress, recovery_name, preview=True)
                last_preview[0] = time.monotonic()
        recovery = Recovery(output/'baseline', output/recovery_name, cache, source, workers, progress,
                            baseline_is_fine=not bool(config.get('baseline_directory')), tracking_directory=output/'tracking', on_checkpoint=preview)
        try:
            recovery.run()
        finally:
            recovery.close()
        del recovery  # Release fine tiles and the normal index before map export.
        progress('Building trajectory and map viewer')
        export_viewer(config, source, trajectories, output, cache, A, progress, recovery_name)
        source.verify_unchanged()
        summary = json.loads((output/recovery_name/'status.json').read_text())
        progress(f"Completed: {summary['accepted']}/{summary['processed']} poses", 'completed',
                 accepted=summary['accepted'], rejected=summary['rejected'], recovered=summary['recovered'],
                 pending_global_search=False,
                 elapsed_s=time.monotonic()-start, output_directory=str(output))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--inspect', action='store_true')
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument('--baseline-only', action='store_true', help='Finish initial 1 cm fitting and preserve it for later recovery')
    stage.add_argument('--recovery-only', action='store_true', help='Resume recovery from a verified completed 1 cm baseline')
    parser.add_argument('--recovery-name', default='recovery', help='Separate recovery directory; preserves earlier recovery attempts')
    args = parser.parse_args()
    try:
        run(json.loads(args.config.read_text(encoding='utf-8')), args.report, args.inspect, args.baseline_only, args.recovery_only, args.recovery_name)
    except Exception as error:
        save_json(args.report, dict(status='failed', stage=str(error)))
        raise


if __name__ == '__main__':
    main()
