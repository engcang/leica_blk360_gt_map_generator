"""Test placement persistence and launch guards against an isolated Studio process.

Uses 61 tiny synthetic PLY files and a stub worker, never real scan data or ICP.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='blk-placement-test-') as directory:
        root = Path(directory)
        app = root / 'app'
        app.mkdir()
        exe = app / Path(args.executable).name
        shutil.copy2(args.executable, exe)
        shutil.copytree(Path(args.executable).resolve().parent / "web", app / "web")
        (app / 'mapping').mkdir()
        (app / 'mapping/studio_worker.py').write_text(
            'import json,sys,pathlib\np=pathlib.Path(sys.argv[sys.argv.index("--report")+1])\n'
            '(p/"progress.json").write_text(json.dumps({"status":"needs_connections","stage":"Test worker captured config"}))\n', encoding='utf-8')
        project = root / 'project'
        project.mkdir()
        (project/'registration_jobs.json').write_text(json.dumps({'schema_version':1,'python':sys.executable,
            'runtime':{'status':'completed','native_backend':True},'jobs':[]}),encoding='utf-8')
        sources = root/'scans'
        sources.mkdir()
        paths=[]
        for i in range(61):
            p=sources/f'Setup{i}.ply'
            p.write_bytes(b'ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n'+struct.pack('<fffH',i,0,1,100))
            os.utime(p, (1700000000+i, 1700000000+i))
            paths.append(str(p))
        process=None
        def launch():
            nonlocal process
            session_file=project/'session.json'
            session_file.unlink(missing_ok=True)
            process=subprocess.Popen([str(exe),'--no-browser','--project',str(project)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for _ in range(100):
                if session_file.exists():break
                if process.poll() is not None:raise AssertionError('Test app exited early')
                time.sleep(.1)
            session=json.loads(session_file.read_text(encoding='utf-8'))
            def api(route,body=None):
                req=urllib.request.Request(f'http://127.0.0.1:{session["port"]}/api/'+route,
                    data=None if body is None else json.dumps(body).encode(),headers={'X-Session-Token':session['token']})
                with urllib.request.urlopen(req,timeout=20) as response:return json.load(response)
            return api
        def rejected(api,route,body):
            try:api(route,body)
            except urllib.error.HTTPError as error:
                assert error.code==400
                return
            raise AssertionError('Invalid request was accepted')
        try:
            api=launch()
            clouds=api('add-clouds',{'paths':paths})['clouds']
            ids=[c['id'] for c in clouds]
            poses=[[i,2*i,3*i,i*.01] for i in range(61)]
            output=root/'output'
            request={'cloud_ids':ids,'initial_guesses':poses,'output_directory':str(output),'name':'reference_map','voxel_m':.01}
            saved=api('registration/placements/save',request)
            assert saved['initial_guesses']==poses
            saved_file=Path(saved['path'])
            assert len(json.loads(saved_file.read_text(encoding='utf-8'))['poses'])==61
            # Restart the app; IDs are re-imported explicitly. No remembered selection is required.
            api('shutdown',{});process.wait(timeout=20)
            api=launch()
            clouds=api('add-clouds',{'paths':paths})['clouds']
            ids=[c['id'] for c in clouds]
            request['cloud_ids']=ids
            assert api('registration/placements/load',request)['initial_guesses']==poses
            kept=[i for i in reversed(range(61)) if i!=20]
            subset={**request,'cloud_ids':[ids[i] for i in kept],'initial_guesses':[poses[i] for i in kept]}
            assert api('registration/placements/load',subset)['initial_guesses']==subset['initial_guesses']
            api('registration/placements/save',subset)
            assert len(json.loads(saved_file.read_text(encoding='utf-8'))['poses'])==61
            # Silent loss must fail before a job/output review is created.
            rejected(api,'registration/start',{**subset,'initial_guesses':[None]*60})
            rejected(api,'registration/start',{**subset,'initial_guesses':[None]*60,'ignore_saved_placements':True})
            assert not (output/'html_source').exists()
            assert not api('registration')['jobs']
            rejected(api,'registration/placements/save',{**subset,'initial_guesses':[[1,2,3]]*60})
            assert api('registration/placements/load',subset)['initial_guesses']==subset['initial_guesses']
            api('registration/start',subset)
            for _ in range(100):
                state=api('registration')
                if not state['busy']:break
                time.sleep(.1)
            job=state['jobs'][-1]
            config=json.loads((Path(job['report_directory'])/'config.json').read_text(encoding='utf-8'))
            assert config['initial_guesses']==subset['initial_guesses']
            assert all(os.path.samefile(actual, paths[i]) for actual,i in zip(config['paths'],kept))
            assert job['initial_placement_count']==60
            assert job['status']=='needs_connections',job
            chunks=[{'key':'chunk-1'}, {'key':'chunk-2'}]
            (output/'partial_chunks.json').write_text(json.dumps({'chunks':chunks}),encoding='utf-8')
            chunk_request={'resume_id':job['id'],'chunk_keys':['chunk-1','chunk-2'],'initial_guesses':[[0,0,0,0],[2,3,4,.5]]}
            api('registration/placements/save',chunk_request)
            assert api('registration/placements/load',{'resume_id':job['id']})['initial_guesses']==chunk_request['initial_guesses']
            rejected(api,'registration/placements/save',{**chunk_request,'chunk_keys':['old-1','old-2']})
            rejected(api,'registration/placements/load',{'resume_id':job['id'],'chunk_keys':['old-1','old-2']})
            assert api('registration/placements/load',{'resume_id':job['id']})['initial_guesses']==chunk_request['initial_guesses']
            # A stale start cannot silently replace a newer on-disk arrangement.
            rejected(api,'registration/start',{'resume_id':job['id'],
                'chunk_initial_guesses':{'chunk-1':[0,0,0,0],'chunk-2':[999,0,0,0]}})
            assert api('registration/import',{'path':str(output/'partial_chunks.json')})['id']==job['id']
            assert api('registration/import',{'path':str(output)})['id']==job['id']
            api('registration/start',{'resume_id':job['id'],'chunk_initial_guesses':{
                'chunk-1':[0,0,0,0],'chunk-2':[2,3,4,.5]},'refine_individual_scans':True})
            for _ in range(100):
                if not api('registration')['busy']:break
                time.sleep(.1)
            updated=json.loads((Path(job['report_directory'])/'config.json').read_text(encoding='utf-8'))
            assert updated['refine_individual_scans'] is True
            (output/'partial_chunks.json').write_text(json.dumps({'chunks':[{'key':'different'}]}),encoding='utf-8')
            rejected(api,'registration/placements/load',{'resume_id':job['id']})

            print('PASS: disk save/readback, app restart, 61 -> 60 reordered scans, lost-placement launch rejected, real config contains all 60 poses.')
            api('shutdown',{});process.wait(timeout=20)
        finally:
            if process is not None and process.poll() is None:
                process.terminate();process.wait(timeout=20)


if __name__=='__main__':
    main()
