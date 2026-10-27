"""Run the real staged scheduler against cheap workers and inspect its barrier."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'mapping'))
from trajectory_io import save_json,digest

WORKER='''import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from trajectory_io import save_json,digest
from trajectory_stages import mark_baseline_ready
args=sys.argv[1:]
config=json.loads(Path(args[args.index('--config')+1]).read_text())
out=Path(config['output']);out.mkdir(exist_ok=True)
report=Path(args[args.index('--report')+1])
phase='baseline' if '--baseline-only' in args else 'recovery'
with Path(config['events']).open('a') as f:f.write(json.dumps(dict(name=config['name'],phase=phase))+'\\n')
if phase=='baseline':
 b=out/'baseline';b.mkdir(exist_ok=True)
 (b/'quality.jsonl').write_text(json.dumps(dict(index=0,timestamp=1.,accepted=True))+'\\n')
 (b/'candidate_trajectory.tum').write_text('1 0 0 0 0 0 0 1\\n')
 save_json(b/'initial_alignment.json',{})
 save_json(b/'input_quality.json',{})
 mark_baseline_ready(out,1)
 save_json(report,dict(status='baseline_ready',stage='Initial fitting ready',accepted=1,processed=1))
else:
 r=out/args[args.index('--recovery-name')+1];r.mkdir(exist_ok=True)
 (r/'candidate_trajectory.tum').write_bytes((out/'baseline/candidate_trajectory.tum').read_bytes())
 save_json(r/'status.json',dict(status='candidate_complete',pending_global_search=False))
 save_json(out/'viewer.json',dict(trajectory_sha256=digest(r/'candidate_trajectory.tum')))
 save_json(report,dict(status='completed',stage='Completed',accepted=1,processed=1))
'''


class CampaignTests(unittest.TestCase):
 def test_all_baselines_finish_before_validated_recovery_and_completed_restart_is_idle(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);batch=root/'batch';batch.mkdir();(root/'mapping').mkdir()
   for name in ('trajectory_stages.py','trajectory_io.py','parallel_runtime.py'):
    (root/'mapping'/name).symlink_to(ROOT/'mapping'/name)
   (root/'.venv/bin').mkdir(parents=True)
   (root/'.venv/bin/python').symlink_to(sys.executable)
   (root/'.venv/pyvenv.cfg').write_bytes((ROOT/'.venv/pyvenv.cfg').read_bytes())
   (root/'.venv/lib').symlink_to(ROOT/'.venv/lib',target_is_directory=True)
   worker=root/'mapping/trajectory_worker.py';worker.write_text(WORKER)
   shared=root/'shared';shared.mkdir();save_json(shared/'map.json',{});(shared/'fine_1cm').mkdir()
   jobs=[]
   for i in range(3):
    output=batch/f'job{i}';cache=batch/f'cache{i}';config=batch/f'config{i}.json'
    save_json(config,dict(output=str(output),name=str(i),events=str(batch/'events.jsonl')))
    jobs.append(dict(sequence=str(i),output=str(output),cache=str(cache),shared_map_cache=str(shared),config=str(config),map_sha256='a'))
   save_json(batch/'plan.json',dict(root=str(root),jobs=jobs,max_concurrent=2,threads_per_job=1,
            correspondence_backend='python',correspondence_library='unused'))
   command=[sys.executable,str(ROOT/'scripts/run_trajectory_campaign.py'),'--batch',str(batch)]
   with (batch/'log.txt').open('w') as log:
    process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
    try:
     deadline=time.monotonic()+60
     while time.monotonic()<deadline:
      status=json.loads((batch/'status.json').read_text()) if (batch/'status.json').exists() else {}
      if status.get('baseline_ready')==3:break
      if process.poll() is not None:self.fail((batch/'log.txt').read_text()+''.join(p.read_text() for p in (batch/'logs').glob('*.log')))
      time.sleep(.1)
     self.assertEqual(status.get('baseline_ready'),3)
     events=[json.loads(l) for l in (batch/'events.jsonl').read_text().splitlines()]
     self.assertEqual([e['phase'] for e in events],['baseline']*3)
     save_json(batch/'recovery_ready.json',dict(validated=True,source_sha256={'mapping/trajectory_worker.py':'invalid'}))
     time.sleep(5.5)
     self.assertEqual(len((batch/'events.jsonl').read_text().splitlines()),3)
     save_json(batch/'recovery_ready.json',dict(validated=True,source_sha256={'mapping/trajectory_worker.py':digest(worker)}))
     self.assertEqual(process.wait(timeout=40),0,(batch/'log.txt').read_text())
    finally:
     if process.poll() is None:process.terminate();process.wait(timeout=20)
   status=json.loads((batch/'status.json').read_text());self.assertEqual(status['counts']['completed'],3)
   before=(batch/'events.jsonl').read_bytes()
   subprocess.run(command,check=True,stdout=subprocess.DEVNULL,timeout=10)
   self.assertEqual((batch/'events.jsonl').read_bytes(),before)
   self.assertEqual([e['phase'] for e in map(json.loads,before.splitlines())],['baseline']*3+['recovery']*3)


if __name__=='__main__':unittest.main()
