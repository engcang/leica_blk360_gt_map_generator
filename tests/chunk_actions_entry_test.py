"""Native app: finalize one chunk and prepare extension of all disconnected chunks."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--executable',required=True)
    parser.add_argument('--chrome',default='/usr/bin/google-chrome'); args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='blk-chunk-actions-ui-') as temporary:
        root=Path(temporary); app=root/'app'; app.mkdir()
        exe=app/Path(args.executable).name; shutil.copy2(args.executable,exe)
        shutil.copytree(repo/'web',app/'web'); (app/'mapping').mkdir()
        # Finalize uses the real worker implementation; extension captures config.
        (app/'mapping/studio_worker.py').write_text(
            f'import sys;sys.path.insert(0,{str(repo/"mapping")!r})\n'
            'import json,pathlib\nfrom json_io import saveJson\n'
            'p=pathlib.Path(sys.argv[sys.argv.index("--report")+1]);c=json.loads((p/"config.json").read_text())\n'
            'if c.get("chunk_source"):\n from chunk_result import finalizeChunk\n r=finalizeChunk(c,p,print)\n'
            'else:\n r={"status":"needs_connections","stage":"Captured extension config"}\n'
            'saveJson(p/"progress.json",r)\n')
        output=root/'original'; output.mkdir(); paths=[]
        identity=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        for i in range(4):
            path=root/f'scan{i}.ply'
            path.write_bytes(b'ply\nformat binary_little_endian 1.0\nelement vertex 1000\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n'+b''.join(struct.pack('<fffH',q%10*.1,(q//10)%10*.1,q//100*.1,q*50) for q in range(1000)))
            paths.append(path)
        chunks=[{'key':f'key-{n}','number':n+1,'rows':[i+1 for i in indices], 'scans':[
            {'row':i+1,'name':paths[i].name,'source_path':'../'+paths[i].name,'source_sha256':hashlib.sha256(paths[i].read_bytes()).hexdigest(),
             'original_points':1000,'transform_chunk_from_scan':identity} for i in indices]} for n,indices in enumerate(([0,1],[2]))]
        manifest=output/'partial_chunks.json'; manifest.write_text(json.dumps({'status':'disconnected_partial_result','chunks':chunks}))
        before=manifest.read_bytes(); (output/'chunks_review.html').write_text('<html>Chunk preview</html>')
        project=root/'project'; project.mkdir(); job_id='a'*32
        report=project/'registration_records'/job_id; report.mkdir(parents=True)
        config={'paths':list(map(str,paths[:3])),'output_path':str(output/'reference_1cm.ply'),'poses_path':str(output/'relative_poses.json'),'voxel_m':.01,'name':'reference'}
        (report/'config.json').write_text(json.dumps(config))
        job={**config,'id':job_id,'report_directory':str(report),'scans':[{'name':p.name,'path':str(p)} for p in paths[:3]],
             'status':'needs_connections','stage':'Disconnected fixture','chunks_manifest_path':str(manifest),'groups':[[1,2],[3]]}
        (project/'registration_jobs.json').write_text(json.dumps({'schema_version':1,'python':sys.executable,
            'runtime':{'status':'completed','native_backend':True},'jobs':[job]}))
        process=subprocess.Popen([str(exe),'--no-browser','--project',str(project)])
        try:
            for _ in range(100):
                if (project/'session.json').exists(): break
                time.sleep(.1)
            session=json.loads((project/'session.json').read_text())
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(executable_path=args.chrome,headless=True,args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                page=browser.new_page(viewport={'width':1500,'height':1100}); errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(f'http://127.0.0.1:{session["port"]}/#{session["token"]}')
                page.wait_for_function('registration_loaded'); page.locator('#tab-register').click()
                page.evaluate('id=>{registration_visible_jobs.add(id);return refreshRegistration()}',job_id)
                row=page.locator(f'article[data-registration-id="{job_id}"]')
                assert row.get_by_label('Chunk to finalize').input_value()=='key-0'
                row.get_by_role('button',name='Finalize selected chunk').click()
                page.wait_for_function('registration_state.jobs.length===2 && !registration_state.busy',timeout=60000)
                assert page.evaluate('registration_state.jobs[1].status')=='completed',page.evaluate('registration_state.jobs[1]')
                final=output/'final_chunk_1'/'relative_poses.json'
                assert len(json.loads(final.read_text())['scans'])==2
                assert (final.parent/'result_review_and_manage.html').exists()
                assert page.get_by_role('button',name='Review and manage',exact=True).count()==1
                assert manifest.read_bytes()==before
                # The original disconnected job remains extendable after finalizing a subset.
                row.get_by_role('button',name='Add scans to chunks').click()
                page.wait_for_function('additional_reference?.items.length===2')
                page.locator('#panel-additional').wait_for(state='visible')
                page.evaluate('path=>addAdditionalPaths([path])',str(paths[3]))
                extension=root/'extension'; page.locator('#additional-output').fill(str(extension)); page.locator('#additional-name').click()
                page.wait_for_function('additional_poses.length===3 && !additional_restoring')
                with page.expect_popup() as popup: page.locator('#additional-arrange').click()
                editor=popup.value; editor.on('pageerror',lambda e:errors.append(str(e)))
                editor.wait_for_function('window.blkPlacement?.items().length===3 && document.body.dataset.loaded==="true"')
                assert editor.evaluate('blkPlacement.items().map(i=>i.scans.length)')==[2,1,1]
                editor.locator('#x').fill('2'); editor.locator('#x').press('Tab')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                editor.locator('#next').click(); editor.locator('#x').fill('3'); editor.locator('#x').press('Tab')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                editor.close()
                page.evaluate('loadAdditionalPlacements()')
                assert page.evaluate('additional_poses.map(p=>p?.[0])')==[0,2,3]
                page.locator('#additional-start').click()
                page.wait_for_function('registration_state.jobs.length===3 && !registration_state.busy')
                new_job=page.evaluate('registration_state.jobs[2]')
                saved=json.loads((Path(new_job['report_directory'])/'config.json').read_text())
                assert saved['reference_poses']==str(manifest)
                assert saved['reference_item_count']==2
                assert saved['additional_initial_guesses']==[[0,0,0,0],[2,0,0,0],[3,0,0,0]]
                assert saved['paths']==list(map(str,paths))
                assert new_job['initial_placement_count']==1
                assert manifest.read_bytes()==before
                assert not errors,errors
                browser.close()
            print('PASS: largest-chunk default, real finalize and review, original result retained, partial reference UI, chunk/new-scan autosave, extension config.')
        finally:
            if process.poll() is None: process.terminate(); process.wait(timeout=20)


if __name__=='__main__': main()
