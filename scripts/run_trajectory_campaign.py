"""Run the authorized eight fresh trajectories with bounded concurrency."""
from pathlib import Path
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--batch", type=Path, required=True)
parser.add_argument("--plan", default='plan.json')
parser.add_argument("--release", default='recovery_ready.json')
args = parser.parse_args()
BATCH = args.batch.resolve()
PLAN = json.loads((BATCH/args.plan).read_text())
ROOT = Path(PLAN['root'])
sys.path.insert(0, str(ROOT/'mapping'))
from parallel_runtime import availableMemory
from trajectory_io import save_json, digest
from trajectory_stages import ready_baseline

STOP = False
def stop(_signal, _frame):
    global STOP
    STOP = True

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
lock = (BATCH/'controller.lock').open('a+')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
active, states, registered = {}, {}, set()
started = time.time()

def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default

def heartbeat(job, **changes):
    state = states[job['sequence']]
    state.update(changes, updated_unix=time.time())
    save_json(Path(job['output'])/'job_status.json', state)

def seed_cache(job):
    cache, shared = Path(job['cache']), Path(job['shared_map_cache'])
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache/'map.json').exists():
        shutil.copy2(shared/'map.json', cache/'map.json')
    fine = cache/'fine_1cm'
    if not fine.exists():
        # Original tiles/index are immutable; normal files use PID-specific
        # temporary files plus atomic replace. Native sessions/locks stay private.
        fine.symlink_to(shared/'fine_1cm', target_is_directory=True)
    names = ('points.npy', 'normals.npy', 'normal_valid.npy', 'tls_10cm.ply')
    for other in PLAN['jobs']:
        donor = Path(other['cache'])
        if other['map_sha256'] != job['map_sha256'] or donor == cache:
            continue
        if all((donor/name).is_file() for name in names):
            for name in names:
                if not (cache/name).exists():
                    os.link(donor/name, cache/name)
            break

def register_apps():
    for app in ('build/local/app', 'install'):
        session = read(ROOT/app/'projects/default/session.json')
        if not session:
            continue
        for job in PLAN['jobs']:
            key = (app, session.get('token'), job['sequence'])
            if key in registered:
                continue
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{session['port']}/api/trajectory/monitor",
                    data=json.dumps({'config':read(Path(job['config']))}).encode(),
                    headers={'Content-Type':'application/json', 'X-Session-Token':session['token']})
                with urllib.request.urlopen(request, timeout=2) as response:
                    response.read()
                registered.add(key)
            except (OSError, ValueError):
                break

def recovery_ready():
    release = read(BATCH/args.release)
    if not release.get('validated') or not release.get('source_sha256'):
        return False
    return all((ROOT/name).is_file() and digest(ROOT/name) == expected
               for name, expected in release['source_sha256'].items())

def launch(job, phase):
    seq = job['sequence']
    seed_cache(job)
    env = os.environ.copy()
    env.update(BLK_CPU_THREADS=str(PLAN['threads_per_job']),
               OMP_NUM_THREADS=str(PLAN['threads_per_job']),
               OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
               BLK_REGISTRATION_EXECUTABLE=str(ROOT/'build/local/native/blk_registration'))
    env.update(BLK_CORRESPONDENCE_BACKEND=PLAN['correspondence_backend'],
               BLK_CORRESPONDENCE_LIBRARY=PLAN['correspondence_library'])
    log = (BATCH/'logs'/f'{seq}.log').open('a', buffering=1)
    report = BATCH/'reports'/f'{seq}.json'
    save_json(report, dict(status='running', stage='Starting initial fitting' if phase == 'forward' else 'Starting recovery'))
    command = [str(ROOT/'.venv/bin/python'), '-u', str(ROOT/'mapping/trajectory_worker.py'),
               '--config', job['config'], '--report', str(report),
               '--baseline-only' if phase == 'forward' else '--recovery-only']
    if phase == 'recovery':
        command += ['--recovery-name', PLAN.get('recovery_name', 'recovery')]
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
        stderr=subprocess.STDOUT, start_new_session=True)
    active[seq] = dict(job=job, process=process, log=log, report=report, phase=phase)
    heartbeat(job, status='running', phase=phase,
              stage='Resuming initial 1 cm fitting' if phase == 'forward' else 'Recovering from completed 1 cm fitting',
              elapsed_before_s=states[seq].get('elapsed_s', 0),
              correspondence_backend=PLAN['correspondence_backend'],
              pid=process.pid, started_unix=time.time(), cpu_threads=PLAN['threads_per_job'])
    print(f'Started {seq}: {phase}, PID {process.pid}', flush=True)

def snapshot():
    for job in PLAN['jobs']:
        if states[job['sequence']]['status'] == 'queued':
            heartbeat(job)
    ordered = [dict(sequence=j['sequence'], output=j['output'], **states[j['sequence']])
               for j in sorted(PLAN['jobs'], key=lambda j:j['sequence'])]
    counts = {s:sum(j['status']==s for j in ordered)
              for s in ('queued','running','completed','failed','cancelled')}
    save_json(BATCH/'status.json', dict(updated_unix=time.time(), started_unix=started,
              controller_pid=os.getpid(), counts=counts, baseline_ready=sum(bool(j.get('baseline_ready')) for j in ordered), jobs=ordered))
    rows = ['# Trajectory generation', '',
            '- Preserve completed forward/reverse checkpoints; finish missing reverse work, then local submaps.',
            '- Local submap fitting → individual 1 cm fitting and validation; no later KISS fallback.',
            '- C++ correspondence search; completed checkpoint poses and attempts retained on resume.',
            f"- Concurrency: {PLAN['max_concurrent']} jobs, {PLAN['threads_per_job']} CPU threads each.", '',
            '| Sequence | State | Stage | Elapsed |', '|---|---|---|---:|']
    for row in ordered:
        elapsed = row.get('elapsed_s', 0)
        rows.append(f"| {row['sequence']} | {row['status']} | {row.get('stage','').replace('|','/')} | {elapsed/60:.1f} min |")
    temporary = BATCH/'results.md.tmp'
    temporary.write_text('\n'.join(rows)+'\n')
    temporary.replace(BATCH/'results.md')

for name in ('logs','reports','results','caches'):
    (BATCH/name).mkdir(exist_ok=True)
pending = []
recoveries = []
for job in PLAN['jobs']:
    output = Path(job['output']); output.mkdir(exist_ok=True)
    previous = read(output/'job_status.json')
    ready = ready_baseline(output, adopt_legacy=True, selected=previous.get('selected'))
    result = output/PLAN.get('recovery_name', 'recovery')
    viewer, summary = read(output/'viewer.json'), read(result/'status.json')
    if (ready and previous.get('status') == 'completed' and summary.get('status') == 'candidate_complete'
            and not summary.get('pending_global_search') and (result/'candidate_trajectory.tum').is_file()
            and viewer.get('trajectory_sha256') == digest(result/'candidate_trajectory.tum')):
        states[job['sequence']] = dict(previous, baseline_ready=True)
        heartbeat(job)
        continue
    states[job['sequence']] = dict(previous, status='queued', phase='recovery_pending' if ready else 'forward',
        stage='Initial 1 cm fitting ready; waiting for a recovery worker' if ready else 'Waiting for initial 1 cm fitting',
        baseline_ready=bool(ready), elapsed_before_s=previous.get('elapsed_s',0))
    states[job['sequence']].pop('pid', None)
    if ready:
        states[job['sequence']].update(processed=ready['processed'], accepted=ready['accepted'])
        if (result/'quality.jsonl').is_file():
            states[job['sequence']]['accepted'] = sum(bool(json.loads(line).get('accepted'))
                for line in (result/'quality.jsonl').read_text().splitlines())
        recoveries.append(job)
    else:
        pending.append(job)
    heartbeat(job)
snapshot()
last_register = 0
try:
    while (pending or active or recoveries) and not STOP:
        queue, phase = pending, 'forward'
        if (not pending and all(state.get('baseline_ready') for state in states.values())
                and not any(item['phase'] == 'forward' for item in active.values()) and recovery_ready()):
            queue, phase = recoveries, 'recovery'
        while queue and len(active) < PLAN['max_concurrent'] and not STOP:
            if active and availableMemory() < 8*1024**3:
                break
            job = queue.pop(0)
            try:
                launch(job, phase)
            except Exception as error:
                heartbeat(job, status='failed', stage=str(error))
                print(f"Launch failed {job['sequence']}: {error}", flush=True)
        for seq, item in list(active.items()):
            job, process = item['job'], item['process']
            progress = read(item['report'])
            values = {k:progress[k] for k in ('stage','accepted','processed','recovered') if k in progress}
            stage = progress.get('stage', '')
            match = re.search(r'Localizing scans at 1 cm: (\d+)/(\d+), (\d+) accepted', stage)
            if match:
                values.update(processed=int(match[1]), selected=int(match[2]), accepted=int(match[3]))
            if 'Localizing scans at 1 cm:' in stage:
                values['phase'] = 'forward'
            elif stage.startswith(('Preparing 1 cm point-to-plane', 'Validating scan', 'Recovering ', 'Recovered ', 'Refining imported')):
                values['phase'] = 'recovery'
            elif stage.startswith(('Building trajectory', 'Reconstructing ')):
                values['phase'] = 'export'
            elapsed = states[seq].get('elapsed_before_s',0)+time.time()-states[seq]['started_unix']
            rc = process.poll()
            if rc is None:
                heartbeat(job, status='running', elapsed_s=elapsed, **values)
            else:
                item['log'].close();del active[seq]
                if item['phase'] == 'forward' and rc == 0 and progress.get('status') == 'baseline_ready':
                    ready = ready_baseline(Path(job['output']))
                    if not ready:
                        raise ValueError('Worker did not publish its baseline completion receipt')
                    heartbeat(job, status='queued', phase='recovery_pending', baseline_ready=True,
                              stage='Initial 1 cm fitting ready; waiting for a recovery worker',
                              accepted=ready['accepted'], processed=ready['processed'], elapsed_s=elapsed)
                    recoveries.append(job)
                else:
                    success = rc == 0 and progress.get('status') == 'completed' and (Path(job['output'])/'viewer.json').is_file()
                    heartbeat(job, status='completed' if success else 'failed', exit_code=rc,
                              elapsed_s=elapsed, finished_unix=time.time(), **values)
                print(f"Finished {seq}: {states[seq]['status']} after {elapsed:.1f}s", flush=True)
        for job in pending:
            heartbeat(job)
        if not pending and not active and any(not state.get('baseline_ready') for state in states.values()):
            for job in recoveries:
                heartbeat(job, stage='Recovery held: an initial 1 cm fitting job failed; inspect its report')
            snapshot()
            break
        snapshot()
        if time.time()-last_register >= 30:
            register_apps();last_register=time.time()
        if pending or active or recoveries:
            time.sleep(5)
finally:
    for item in active.values():
        try:
            os.killpg(item['process'].pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for item in active.values():
        try:
            item['process'].wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(item['process'].pid, signal.SIGKILL)
            item['process'].wait()
        item['log'].close()
        heartbeat(item['job'], status='cancelled', stage='Batch stopped')
    for job in pending + recoveries:
        if STOP or not states[job['sequence']].get('stage', '').startswith('Recovery held:'):
            heartbeat(job, status='cancelled', stage='Batch stopped before launch')
    snapshot()
