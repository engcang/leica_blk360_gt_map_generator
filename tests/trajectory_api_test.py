"""Trajectory tab API, worker lifecycle, saved result, and authenticated map reads."""
import argparse
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

from test_trajectory import fixture


def wait(fn, seconds=45):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        value=fn()
        if value:return value
        time.sleep(.1)
    raise AssertionError('Timed out waiting for trajectory worker')


def run(executable):
    with tempfile.TemporaryDirectory(prefix='blk-trajectory-api-') as folder:
        root=Path(folder);data=root/'Scan data 한글';data.mkdir();config=fixture(data)
        environment=os.environ.copy()
        if sys.platform.startswith('linux'):
            launchers=root/'launchers';launchers.mkdir()
            opener=launchers/'xdg-open'
            opener.write_text('#!/bin/sh\nprintf \'%s\\n\' "$1" > "$BLK_TEST_OPEN_LOG"\n')
            opener.chmod(0o755)
            environment['PATH']=str(launchers)+os.pathsep+environment.get('PATH','')
            environment['BLK_TEST_OPEN_LOG']=str(root/'opened-folder.txt')
        project=root/'project';project.mkdir()
        (project/'registration_jobs.json').write_text(json.dumps({'schema_version':1,'python':sys.executable,
            'runtime':{'status':'completed','native_backend':{'test':True}},'jobs':[]}))
        process=None
        def launch():
            nonlocal process,base,headers
            process=subprocess.Popen([str(Path(executable).resolve()),'--no-browser','--project',str(project)],stdout=subprocess.DEVNULL,env=environment)
            wait(lambda:(project/'session.json').exists())
            session=json.loads((project/'session.json').read_text())
            base=f"http://127.0.0.1:{session['port']}";headers={'X-Session-Token':session['token']}
        base='';headers={}
        def api(route,body=None):
            request=urllib.request.Request(base+'/api/'+route,data=None if body is None else json.dumps(body).encode(),headers=headers)
            with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)
        def idle():
            state=api('trajectory');return state if not state['running'] else None
        try:
            launch()
            with urllib.request.urlopen(base+'/') as response:
                html=response.read().decode();assert html.index('tab-trajectory')>html.index('tab-additional')
            api('trajectory/draft',config);assert api('trajectory')['draft']==config
            api('trajectory/start',{'config':config,'inspect':True})
            state=wait(idle);assert state['jobs'][-1]['status']=='ready',state
            api('trajectory/start',{'config':config})
            state=wait(idle);job=state['jobs'][-1]
            assert job['status']=='completed',job
            assert job['viewer_available']
            if sys.platform.startswith('linux'):
                assert api('trajectory/'+job['id']+'/folder',{})['ok']
                wait(lambda:(root/'opened-folder.txt').exists())
                assert (root/'opened-folder.txt').read_text().strip()==config['output_directory']
            scene=api('trajectory/'+job['id']+'/viewer')
            assert {item['key'] for item in scene['maps']}=={'tls_10cm','tls_gt','estimate_0'}
            assert all('path' not in item for item in scene['maps'])
            request=urllib.request.Request(base+scene['maps'][0]['url'],headers={**headers,'Range':'bytes=0-99'})
            with urllib.request.urlopen(request) as response:
                assert response.status==206 and response.read().startswith(b'ply\n')
            try:urllib.request.urlopen(base+scene['maps'][0]['url'])
            except urllib.error.HTTPError as error:assert error.code==403
            else:raise AssertionError('Unauthenticated map read succeeded')
            api('shutdown',{});assert process.wait(timeout=15)==0
            launch();assert api('trajectory')['draft']==config
            assert api('trajectory')['jobs'][-1]['viewer_available']
            api('trajectory/open',{'path':config['output_directory']})
            assert len([j for j in api('trajectory')['jobs'] if not j.get('inspect')])==1
            saved=root/'saved-result';shutil.copytree(config['output_directory'],saved)
            api('trajectory/open',{'path':str(saved)})
            assert api('trajectory')['jobs'][-1]['status']=='saved'
            pending=dict(config,output_directory=str(root/'pending-result'))
            Path(pending['output_directory']).mkdir()
            progress=Path(pending['output_directory'])/'job_status.json'
            def update(status,age=0):
                progress.write_text(json.dumps(dict(status=status,stage='Live '+status,updated_unix=time.time()-age)))
            update('queued');api('trajectory/monitor',{'config':pending})
            external=api('trajectory')['jobs'][-1]
            assert external['status']=='queued' and not external['viewer_available']
            update('running');assert api('trajectory')['jobs'][-1]['status']=='running'
            api('trajectory/monitor',{'config':pending})
            assert sum(j['output_directory']==pending['output_directory'] for j in api('trajectory')['jobs'])==1
            update('running',1000);assert api('trajectory')['jobs'][-1]['status']=='interrupted'
            try:api('trajectory/start',{'resume_id':external['id']})
            except urllib.error.HTTPError as error:assert error.code==400
            else:raise AssertionError('External worker was launched a second time')
            update('completed',1000);assert api('trajectory')['jobs'][-1]['status']=='completed'
            status=Path(config['output_directory'])/'job_status.json'
            status.write_text(json.dumps(dict(status='running',stage='Global recovery',updated_unix=time.time())))
            api('trajectory/monitor',{'config':config})
            current=next(j for j in api('trajectory')['jobs'] if j['id']==job['id'])
            assert current['status']=='running' and current['viewer_available'] and current['stage']=='Global recovery'
            api('shutdown',{});assert process.wait(timeout=15)==0
            launch()
            current=next(j for j in api('trajectory')['jobs'] if j['id']==job['id'])
            assert current['status']=='running' and current['viewer_available']
        finally:
            if process and process.poll() is None:
                api('shutdown',{});process.wait(timeout=15)
    print('Trajectory API passed: generation, authenticated PLY reads, saved results, live background status, stale updates, duplicate-worker prevention, restart/reopen.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--executable',required=True)
    run(parser.parse_args().executable)
