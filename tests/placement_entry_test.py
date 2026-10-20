"""Test Arrange entry, folder selection and saved poses in an isolated headless browser."""
import argparse
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True)
    parser.add_argument('--chrome', required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='blk-arrange-entry-') as directory:
        root = Path(directory)
        app = root / 'app'
        app.mkdir()
        exe = app / Path(args.executable).name
        shutil.copy2(args.executable, exe)
        shutil.copytree(repo / 'web', app / 'web')
        (app / 'mapping').mkdir()
        project = root / 'project'
        output = root
        paths = []
        for scan in range(3):
            path = root / f'Setup{scan}.ply'
            header = ('ply\nformat binary_little_endian 1.0\nelement vertex 1000\n'
                      'property float x\nproperty float y\nproperty float z\n'
                      'property ushort intensity\nend_header\n').encode()
            path.write_bytes(header + b''.join(struct.pack('<fffH', (i % 10) * .1,
                ((i // 10) % 10) * .1, (i // 100) * .1, i * 50) for i in range(1000)))
            paths.append(str(path))
        # A stub worker tests launch/config plumbing without running real ICP.
        for name in ('chunk_placements.py', 'json_io.py'):
            shutil.copy2(repo / 'mapping' / name, app / 'mapping' / name)
        (app / 'mapping/studio_worker.py').write_text(
            'import json,sys,pathlib,time\nfrom chunk_placements import refreshChunkPlacements\n'
            'from json_io import saveJson\n'
            'p=pathlib.Path(sys.argv[sys.argv.index("--report")+1])\n'
            'config=json.loads((p/"config.json").read_text())\n'
            'for i in range(120): print("Worker line",i,flush=True)\n'
            'time.sleep(1)\n'
            'out=pathlib.Path(config["output_path"]).parent\n'
            'chunks=config["manual_chunks"]\n'
            'for c in chunks: c["key"]="updated-"+c["key"]\n'
            'refreshChunkPlacements(out,chunks)\n'
            'saveJson(out/"partial_chunks.json",{"chunks":chunks})\n'
            'saveJson(p/"progress.json",{"status":"needs_connections","stage":"Test worker finished"})\n',
            encoding='utf-8')
        chunk_output = root / 'chunks-output'
        chunk_output.mkdir()
        report = project / 'registration_records' / ('a' * 32)
        report.mkdir(parents=True)
        identity = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
        chunks = [{'key':f'chunk-{i}', 'number':i+1, 'rows':[i+1],
                   'scans':[{'row':i+1,'name':Path(paths[i]).name,'transform_chunk_from_scan':identity}]}
                  for i in range(2)]
        (chunk_output / 'partial_chunks.json').write_text(json.dumps({'chunks':chunks}),encoding='utf-8')
        (chunk_output / 'chunks_review.html').write_text('<html>Fixture</html>',encoding='utf-8')
        config = {'name':'test_chunks','paths':paths,'voxel_m':.01,'additional_pairs':'',
                  'output_path':str(chunk_output / 'test_chunks_1cm.ply'),
                  'poses_path':str(chunk_output / 'relative_poses.json'),'review_directory':str(chunk_output)}
        (report / 'config.json').write_text(json.dumps(config),encoding='utf-8')
        job = {'id':'a'*32,'name':'Test chunks','status':'needs_connections','stage':'Fixture',
               'report_directory':str(report),'output_path':config['output_path'],'poses_path':config['poses_path'],
               'voxel_m':.01,'scans':[{'name':Path(p).name,'path':p} for p in paths]}
        (project / 'registration_jobs.json').write_text(json.dumps({'schema_version':1,'python':sys.executable,
            'runtime':{'status':'completed','native_backend':True},'jobs':[job]}),encoding='utf-8')
        process = subprocess.Popen([str(exe), '--no-browser', '--project', str(project)],
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            for _ in range(100):
                if (project / 'session.json').exists():
                    break
                if process.poll() is not None:
                    raise AssertionError('Isolated app exited before startup')
                time.sleep(.1)
            session = json.loads((project / 'session.json').read_text(encoding='utf-8'))
            base = f'http://127.0.0.1:{session["port"]}'
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chrome, headless=True,
                    args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                page = browser.new_page(viewport={'width': 1400, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/#' + session['token'])
                page.locator('#tab-register').click()
                page.wait_for_function('registration_loaded')
                assert page.locator('#placement-open').is_disabled()
                # Use the same Add folder workflow as the user: pick PLYs once.
                page.locator('#registration-folder').click()
                page.locator('#browser-path').fill(str(root))
                page.locator('#browser-go').click()
                page.wait_for_function('!document.querySelector("#browser-go").disabled')
                assert Path(page.evaluate('browser_state.path')).samefile(root)
                page.locator('#use-folder').click()
                page.wait_for_function('registration_ids.length === 3')
                assert Path(page.locator('#map-output').input_value()).samefile(root)
                assert page.locator('#placement-open').is_enabled()
                # Emptying the optional output field must still not request another folder.
                page.locator('#map-output').fill('')
                with page.expect_popup() as popup:
                    page.locator('#placement-open').click()
                assert not page.locator('#browser').is_visible()
                editor = popup.value
                editor.on('pageerror', lambda error: errors.append(str(error)))
                editor.wait_for_function('window.blkPlacement?.items().length === 3 && document.body.dataset.loaded === "true"')
                assert editor.locator('#density').input_value() == '1'
                assert not editor.locator('#axes-visible').is_checked()
                editor.locator('#x').fill('1.25')
                editor.locator('#x').press('Tab')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                saved = json.loads((output / 'initial_placements.json').read_text(encoding='utf-8'))
                assert any(pose and pose[0] == 1.25 for pose in saved['poses'].values())
                assert editor.locator('#apply').count() == 0
                page.wait_for_function('registrationDraft().initial_guesses[1]?.[0] === 1.25')
                assert page.evaluate('registrationDraft().initial_guesses[1][0]') == 1.25
                editor.close()
                with page.expect_popup() as popup:
                    page.locator('#placement-open').click()
                reopened = popup.value
                reopened.on('pageerror', lambda error: errors.append(str(error)))
                reopened.wait_for_function('window.blkPlacement?.items().length === 3 && document.body.dataset.loaded === "true"')
                assert reopened.evaluate('blkPlacement.items()[1].pose[0]') == 1.25
                reopened.close()
                assert page.locator('.registration-results').count() == 0
                assert page.locator('#registration-log').evaluate('(el) => el.closest("details") === null')
                assert page.evaluate("""() => {
                    const ids=['registration-folder','registration-scans','placement-open','registration-start','registration-log','open-registration-result','registration-jobs'];
                    return ids.every((id,i)=>!i || (document.getElementById(ids[i-1]).compareDocumentPosition(document.getElementById(id)) & Node.DOCUMENT_POSITION_FOLLOWING));
                }""")
                await_import = """async path => {
                    const result=await api('registration/import',{path});
                    registration_visible_jobs.add(result.id); await refreshRegistration();
                }"""
                page.evaluate(await_import, str(chunk_output / 'partial_chunks.json'))
                with page.expect_popup() as popup:
                    page.get_by_role('button',name='Arrange chunks',exact=True).click()
                chunk_editor = popup.value
                chunk_editor.on('pageerror', lambda error: errors.append(str(error)))
                chunk_editor.wait_for_function('window.blkPlacement?.items().length === 2 && document.body.dataset.loaded === "true"')
                assert chunk_editor.locator('#apply').count() == 0
                chunk_editor.locator('#x').fill('3.5')
                chunk_editor.locator('#x').press('Tab')
                chunk_editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                page.wait_for_function('chunk_pending.size === 0')
                chunk_file = chunk_output / 'chunk_initial_placements.json'
                assert json.loads(chunk_file.read_text())['poses']['chunk-1'][0] == 3.5
                # The editor saves only; registration starts exclusively from the main tab.
                assert not (report / 'progress.json').exists()
                assert not page.locator('.chunk-refinement-option input').is_checked()
                page.get_by_role('button',name='Register chunks',exact=True).click()
                page.wait_for_function('!registration_starting && registration_state.jobs[0].stage === "Test worker finished"')
                launched = json.loads((report / 'config.json').read_text())
                assert launched['chunk_initial_guesses']['chunk-1'] == [3.5,0,0,0]
                assert launched['manual_chunks'] == chunks
                assert not launched.get('refine_individual_scans', False)
                assert chunk_editor.locator('#x').is_disabled()
                page.wait_for_function('document.querySelector("#registration-log").textContent.includes("Worker line 119")')
                assert page.locator('#registration-log').is_visible()
                assert page.locator('#registration-log').evaluate('(el)=>el.scrollHeight > el.clientHeight && Math.abs(el.scrollHeight-el.clientHeight-el.scrollTop)<2')
                page.locator('#tab-convert').click()
                page.locator('#tab-register').click()
                assert page.locator('#registration-log').evaluate('(el)=>Math.abs(el.scrollHeight-el.clientHeight-el.scrollTop)<2')
                updated = json.loads(chunk_file.read_text())
                assert updated['poses'] == {'updated-chunk-0':None,'updated-chunk-1':None}
                archived = list((chunk_output / 'html_source/placement_history').glob('*.json'))
                assert len(archived) == 1
                assert json.loads(archived[0].read_text())['poses']['chunk-1'][0] == 3.5
                with page.expect_popup() as popup:
                    page.get_by_role('button',name='Arrange chunks',exact=True).click()
                fresh = popup.value
                fresh.on('pageerror', lambda error: errors.append(str(error)))
                fresh.wait_for_function('window.blkPlacement?.items().length === 2 && document.body.dataset.loaded === "true"')
                assert fresh.evaluate('blkPlacement.items()[1].id') == 'updated-chunk-1'
                assert fresh.evaluate('blkPlacement.items()[1].enabled') is False
                assert not errors, errors
                browser.close()
            request = urllib.request.Request(base + '/api/shutdown', data=b'{}',
                headers={'X-Session-Token': session['token']})
            with urllib.request.urlopen(request, timeout=5):
                pass
            process.wait(timeout=15)
            print('PASS: folder/Arrange entry; scan and chunk autosave; no editor launch button; main-tab chunk launch/config; expanded ordered log with auto-scroll after completion/tab switch; changed chunk JSON and preserved history; fresh editor after worker completion; no browser errors. Stub worker only, no ICP accuracy test.')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    main()
