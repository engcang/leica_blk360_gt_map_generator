"""Test placement persistence and launch guards against an isolated Studio process.

Uses 61 tiny synthetic PLY files and a stub worker, never real scan data or ICP.
"""
import argparse
import hashlib
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
            poses=[[i,2*i,3*i,i*.01]+([.02,-.03] if i%5==2 else []) for i in range(61)]
            output=root/'output'
            request={'cloud_ids':ids,'initial_guesses':poses,'output_directory':str(output),'name':'reference_map','voxel_m':.01}
            saved=api('registration/placements/save',request)
            assert saved['initial_guesses']==poses
            saved_file=Path(saved['path'])
            saved_poses=json.loads(saved_file.read_text(encoding='utf-8'))['poses']
            assert len(saved_poses)==61
            assert set(saved_poses)=={f'../scans/Setup{i}.ply' for i in range(61)}
            # Legacy documents have no recorded comparison choice. References are
            # context only: changing them must preserve every numerical pose.
            assert saved['comparison_references']==[None]*61
            refs=[None]*61;refs[1]=40;refs[40]=5
            reference_request={**request,'comparison_references':refs}
            assert api('registration/placements/save',reference_request)['comparison_references']==refs
            doc=json.loads(saved_file.read_text())
            assert doc['poses']==saved_poses
            assert doc['comparison_references']=={'../scans/Setup1.ply':'../scans/Setup40.ply',
                                                  '../scans/Setup40.ply':'../scans/Setup5.ply'}
            before=saved_file.read_bytes()
            for invalid in ([None], [1]*61, [-1]*61, [61]*61, ['0']*61, [0.5]*61):
                rejected(api,'registration/placements/save',{**request,'comparison_references':invalid})
                assert saved_file.read_bytes()==before
            # Restart the app; IDs are re-imported explicitly. No remembered selection is required.
            api('shutdown',{});process.wait(timeout=20)
            api=launch()
            clouds=api('add-clouds',{'paths':paths})['clouds']
            ids=[c['id'] for c in clouds]
            request['cloud_ids']=ids
            assert api('registration/placements/load',request)['initial_guesses']==poses
            assert api('registration/placements/load',request)['comparison_references']==refs
            kept=[i for i in reversed(range(61)) if i!=20]
            subset={**request,'cloud_ids':[ids[i] for i in kept],'initial_guesses':[poses[i] for i in kept]}
            assert api('registration/placements/load',subset)['initial_guesses']==subset['initial_guesses']
            api('registration/placements/save',subset)
            assert len(json.loads(saved_file.read_text(encoding='utf-8'))['poses'])==61
            reloaded=api('registration/placements/load',subset)
            assert reloaded['comparison_references'][kept.index(1)]==kept.index(40)
            assert reloaded['comparison_references'][kept.index(40)]==kept.index(5)
            # Omitted reference scans and old clients must not erase saved choices.
            narrow={**request,'cloud_ids':ids[:2],'initial_guesses':poses[:2],'comparison_references':[None,None]}
            api('registration/placements/save',narrow)
            assert api('registration/placements/load',request)['comparison_references']==refs
            # Only the selected output directory supplies placements.
            portable = sources/'initial_placements.json'
            portable_doc = dict(schema_version=1, kind='scan_initial_placements', units='metres_radians',
                poses={r'C:\\Users\\Colleague\\Scans\\'+Path(path).name:pose for path,pose in zip(paths,poses)})
            portable.write_text(json.dumps(portable_doc))
            portable_bytes = portable.read_bytes()
            moved = {**subset, 'output_directory':str(root/'new-output')}
            assert api('registration/placements/load',moved)['initial_guesses']==[None]*60
            assert portable.read_bytes()==portable_bytes
            # Explicit resets in the output document override imported poses.
            reset={**moved,'initial_guesses':[None]*60}
            api('registration/placements/save',reset)
            assert api('registration/placements/load',reset)['initial_guesses']==[None]*60
            # Portable documents also work in the output directory; saving
            # replaces old path aliases so another move remains unambiguous.
            copied=root/'copied-output';copied.mkdir()
            (copied/'initial_placements.json').write_bytes(portable_bytes)
            copied_request={**subset,'output_directory':str(copied)}
            assert api('registration/placements/load',copied_request)['initial_guesses']==subset['initial_guesses']
            api('registration/placements/save',copied_request)
            converted=json.loads((copied/'initial_placements.json').read_text())['poses']
            assert set(converted)=={f'../scans/Setup{i}.ply' for i in range(61)}
            # Ambiguous basenames must never assign one pose to another scan.
            portable_doc['poses']['/different/Setup0.ply']=[999,0,0,0]
            (copied/'initial_placements.json').write_text(json.dumps(portable_doc))
            rejected(api,'registration/placements/load',copied_request)
            portable.unlink()
            # Relocate both scans and output; no old absolute path may be needed.
            relocated=root/'relocated';relocated.mkdir()
            shutil.copytree(sources,relocated/'scans')
            shutil.copytree(output,relocated/'output')
            moved_ids=[c['id'] for c in api('add-clouds',{'paths':[str(relocated/'scans'/Path(path).name) for path in paths]})['clouds']]
            assert api('registration/placements/load',{**request,'cloud_ids':moved_ids,
                'output_directory':str(relocated/'output')})['initial_guesses']==poses
            assert api('registration/placements/load',{**request,'cloud_ids':moved_ids,
                'output_directory':str(relocated/'output')})['comparison_references']==refs
            # Identical basenames in different relative directories stay distinct.
            other=root/'other';other.mkdir();shutil.copy2(sources/'Setup0.ply',other/'Setup0.ply')
            other_id=api('add-clouds',{'paths':[str(other/'Setup0.ply')]})['clouds'][0]['id']
            pair={**request,'cloud_ids':[other_id,ids[1]],'initial_guesses':[[99,0,0,0],poses[1]]}
            api('registration/placements/save',pair)
            assert api('registration/placements/load',request)['initial_guesses']==poses
            # Additional Register persists both its reference and new scans
            # relative to its own output, then survives moving all three folders.
            reference=root/'reference';reference.mkdir()
            identity=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
            manifest=dict(schema_version=3,units='metres',result_name='reference',scans=[
                dict(file='../scans/'+Path(path).name,points=1,sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                     transform_map_from_scan=identity) for path in paths[:2]])
            (reference/'relative_poses.json').write_text(json.dumps(manifest))
            extra=dict(reference_poses=str(reference/'relative_poses.json'),output_directory=str(root/'extended'),
                       cloud_ids=ids[2:4],initial_guesses=[[0,0,0,0],poses[2],poses[3]],comparison_references=[None,0,1])
            api('registration/placements/save',extra)
            entry=next(iter(json.loads((root/'extended/additional_initial_placements.json').read_text())['references'].values()))
            assert entry['reference_poses']=='../reference/relative_poses.json'
            assert set(entry['poses'])=={'../scans/Setup2.ply','../scans/Setup3.ply'}
            assert entry['comparison_references']=={'../scans/Setup2.ply':'@reference','../scans/Setup3.ply':'../scans/Setup2.ply'}
            shutil.copytree(reference,relocated/'reference');shutil.copytree(root/'extended',relocated/'extended')
            assert api('registration/placements/load',{**extra,'reference_poses':str(relocated/'reference/relative_poses.json'),
                'cloud_ids':moved_ids[2:4],'output_directory':str(relocated/'extended')})['initial_guesses']==extra['initial_guesses']
            assert api('registration/placements/load',{**extra,'reference_poses':str(relocated/'reference/relative_poses.json'),
                'cloud_ids':list(reversed(moved_ids[2:4])),'output_directory':str(relocated/'extended')})['comparison_references']==[None,2,0]
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
            chunk_request={'resume_id':job['id'],'chunk_keys':['chunk-1','chunk-2'],'initial_guesses':[[0,0,0,0],[2,3,4,.5,.02,-.03]],'comparison_references':[None,0]}
            api('registration/placements/save',chunk_request)
            assert api('registration/placements/load',{'resume_id':job['id']})['initial_guesses']==chunk_request['initial_guesses']
            assert api('registration/placements/load',{'resume_id':job['id']})['comparison_references']==[None,0]
            rejected(api,'registration/placements/save',{**chunk_request,'chunk_keys':['old-1','old-2']})
            rejected(api,'registration/placements/load',{'resume_id':job['id'],'chunk_keys':['old-1','old-2']})
            assert api('registration/placements/load',{'resume_id':job['id']})['initial_guesses']==chunk_request['initial_guesses']
            # A stale start cannot silently replace a newer on-disk arrangement.
            rejected(api,'registration/start',{'resume_id':job['id'],
                'chunk_initial_guesses':{'chunk-1':[0,0,0,0],'chunk-2':[999,0,0,0]}})
            assert api('registration/import',{'path':str(output/'partial_chunks.json')})['id']==job['id']
            assert api('registration/import',{'path':str(output)})['id']==job['id']
            api('registration/start',{'resume_id':job['id'],'chunk_initial_guesses':{
                'chunk-1':[0,0,0,0],'chunk-2':[2,3,4,.5,.02,-.03]},'refine_individual_scans':True})
            for _ in range(100):
                if not api('registration')['busy']:break
                time.sleep(.1)
            updated=json.loads((Path(job['report_directory'])/'config.json').read_text(encoding='utf-8'))
            assert updated['refine_individual_scans'] is True
            assert updated['chunk_initial_guesses']['chunk-2']==[2,3,4,.5,.02,-.03]
            (output/'partial_chunks.json').write_text(json.dumps({'chunks':[{'key':'different'}]}),encoding='utf-8')
            rejected(api,'registration/placements/load',{'resume_id':job['id']})

            print('PASS: poses and comparison references survive restart, reorder, subsets and relocation; scan/additional/chunk persistence; invalid saves and stale launches rejected.')
            api('shutdown',{});process.wait(timeout=20)
        finally:
            if process is not None and process.poll() is None:
                process.terminate();process.wait(timeout=20)


if __name__=='__main__':
    main()
