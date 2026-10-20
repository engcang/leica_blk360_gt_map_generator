"""Isolated native-server/browser test of additional inputs, autosave and launch."""
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
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',required=True)
    parser.add_argument('--chrome',required=True)
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='blk-additional-ui-') as temporary:
        root=Path(temporary).resolve(); app=root/'app'; app.mkdir()
        exe=app/Path(args.executable).name; shutil.copy2(args.executable,exe)
        shutil.copytree(repo/'web',app/'web'); (app/'mapping').mkdir()
        (app/'mapping/studio_worker.py').write_text(
            'import json,pathlib,sys,time\np=pathlib.Path(sys.argv[sys.argv.index("--report")+1])\n'
            'for i in range(120): print("Worker line",i,flush=True)\n'
            'time.sleep(1)\n(p/"progress.json").write_text(json.dumps({"status":"needs_connections","stage":"Captured config"}))\n',encoding='utf-8')
        project=root/'project'; project.mkdir()
        (project/'registration_jobs.json').write_text(json.dumps({'schema_version':1,'python':sys.executable,
            'runtime':{'status':'completed','native_backend':True},'jobs':[]}),encoding='utf-8')
        old=root/'old'; old.mkdir(); new=root/'new'; new.mkdir(); output=root/'extended'
        paths=[]
        for i in range(4):
            path=(old if i<2 else new)/f'Setup{100-i}.ply'
            header=('ply\nformat binary_little_endian 1.0\nelement vertex 1000\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n').encode()
            path.write_bytes(header+b''.join(struct.pack('<fffH',q%10*.1,(q//10)%10*.1,q//100*.1,q*50) for q in range(1000)))
            os.utime(path,(1700000000+i,1700000000+i)); paths.append(path)
        identity=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        manifest={'schema_version':3,'units':'metres','result_name':'reference','coordinate_frame':'reference_frame',
            'registration_voxel_m':.01,'scans':[{'index':i,'file':p.name,'name':p.name,'points':1000,
                'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'transform_map_from_scan':identity} for i,p in enumerate(paths[:2])]}
        source=old/'relative_poses.json'; source.write_text(json.dumps(manifest),encoding='utf-8')
        before=source.read_bytes()
        process=subprocess.Popen([str(exe),'--no-browser','--project',str(project)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            for _ in range(100):
                if (project/'session.json').exists(): break
                if process.poll() is not None: raise AssertionError('Isolated app exited')
                time.sleep(.1)
            session=json.loads((project/'session.json').read_text(encoding='utf-8')); base=f'http://127.0.0.1:{session["port"]}'
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(executable_path=args.chrome,headless=True,args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                page=browser.new_page(viewport={'width':1400,'height':1000}); errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/#'+session['token']); page.wait_for_function('registration_loaded')
                assert page.locator('#panel-convert').is_visible()
                page.locator('#tab-additional').click()
                assert page.locator('#additional-start').is_disabled()
                page.locator('#additional-reference').click()
                page.locator('#browser-path').fill(str(old)); page.locator('#browser-go').click()
                page.wait_for_function('!document.querySelector("#browser-go").disabled')
                page.get_by_text('relative_poses.json',exact=True).click()
                page.locator('#use-files').click()
                page.wait_for_function('additional_reference?.scans.length === 2')
                assert page.evaluate('registration_ids.length')==0
                assert page.evaluate('viewed_cloud_ids.size')==0
                page.locator('#additional-folder').click()
                page.locator('#browser-path').fill(str(new)); page.locator('#browser-go').click()
                page.wait_for_function('!document.querySelector("#browser-go").disabled')
                page.locator('#use-folder').click(); page.wait_for_function('additional_ids.length === 2')
                assert page.locator('#additional-scans').inner_text().index('Setup98') < page.locator('#additional-scans').inner_text().index('Setup97')
                page.locator('#additional-output').fill(str(output)); page.locator('#additional-name').click()
                page.wait_for_function('additional_poses.length === 3 && !additional_restoring')
                assert page.locator('#additional-start').is_enabled()
                with page.expect_popup() as popup: page.locator('#additional-arrange').click()
                editor=popup.value; editor.on('pageerror',lambda error:errors.append(str(error)))
                editor.wait_for_function('window.blkPlacement?.items().length === 3 && document.body.dataset.loaded === "true"')
                assert editor.evaluate('blkPlacement.items()[0].scans.length')==2
                assert editor.locator('#density').input_value()=='1'
                assert not editor.locator('#axes-visible').is_checked()
                editor.locator('#x').fill('1.25'); editor.locator('#x').press('Tab')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                editor.locator('#next').click(); editor.locator('#x').fill('2.5'); editor.locator('#x').press('Tab')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                page.wait_for_function('additional_poses[2]?.[0] === 2.5')
                editor.close()
                page.locator('#additional-scans').get_by_role('button',name='Remove').first.click()
                page.wait_for_function('additional_ids.length === 1 && additional_poses[1]?.[0] === 2.5')
                saved=json.loads((output/'additional_initial_placements.json').read_text())
                assert len(next(iter(saved['references'].values()))['poses'])==2
                # Reload starts empty; explicit selection restores disk poses, not cached selections.
                page.reload(); page.wait_for_function('registration_loaded')
                assert page.evaluate('additional_ids.length')==0
                assert page.evaluate('additional_reference') is None
                page.locator('#tab-additional').click()
                page.evaluate('path=>loadAdditionalReference(path)',str(source))
                page.evaluate('path=>addAdditionalPaths([path])',str(paths[3]))
                page.locator('#additional-output').fill(str(output)); page.locator('#additional-name').click()
                page.wait_for_function('additional_poses[1]?.[0] === 2.5')
                with page.expect_popup() as popup: page.locator('#additional-arrange').click()
                editor=popup.value
                editor.wait_for_function('window.blkPlacement?.items().length === 2 && document.body.dataset.loaded === "true"')
                editor.locator('#x').fill('3.75'); editor.locator('#x').press('Tab')
                # Direct function call also tests flushing the in-flight editor save.
                page.evaluate('startAdditional()')
                page.wait_for_function('registration_state.jobs.length === 1 && !registration_state.busy')
                config=json.loads(next((project/'registration_records').glob('*/config.json')).read_text())
                assert config['additional_initial_guesses']==[[0,0,0,0],[3.75,0,0,0]]
                assert [Path(p).resolve() for p in config['paths']]==[paths[0],paths[1],paths[3]]
                assert config['reference_sha256']==hashlib.sha256(before).hexdigest()
                assert 'initial_guesses' not in config
                assert page.locator('#additional-jobs article').count()==1
                assert page.locator('#registration-jobs article').count()==0
                assert editor.locator('#x').is_disabled()
                assert page.locator('#additional-log').evaluate('(e)=>e.closest("details")===null')
                assert page.locator('#additional-log').evaluate('(e)=>e.scrollHeight>e.clientHeight && Math.abs(e.scrollHeight-e.clientHeight-e.scrollTop)<3')
                screenshot=repo/'build/placement-fix/additional-tab.png'
                page.screenshot(path=str(screenshot),full_page=True)
                assert source.read_bytes()==before
                # Attempt to use the old output is rejected, as is a modified reference digest.
                failure=page.evaluate("""async directory => {
                    try { await api('registration/placements/load',{...additionalDraft(),output_directory:directory}); return ''; }
                    catch(e) { return e.message; }
                }""",str(old))
                assert 'separate' in failure
                failure=page.evaluate("""async () => {
                    try { await api('registration/reference',{reference_poses:additional_reference.path,reference_sha256:'0'.repeat(64)}); return ''; }
                    catch(e) { return e.message; }
                }""")
                assert 'changed' in failure
                assert not errors,errors
                browser.close()
            print('PASS: Additional tab, JSON/folder pickers, fixed-map editor, modification order, autosave/remove/reload, final save handshake, native job config and isolated results.')
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=20)


if __name__=='__main__': main()
