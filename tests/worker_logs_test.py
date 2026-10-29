"""Real native-server/browser checks for paged logs and user-controlled scrolling."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True)
    parser.add_argument('--chrome', required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='blk-worker-logs-') as temporary:
        root = Path(temporary)
        app = root / 'app'
        app.mkdir()
        executable = app / Path(args.executable).name
        shutil.copy2(args.executable, executable)
        shutil.copytree(repo / 'web', app / 'web')
        project = root / 'project'
        project.mkdir()
        report = root / 'report'
        report.mkdir()
        log = report / 'worker.log'
        content = ''.join(f'{i:06d} 정합 progress — scan {i} 🌍\n' for i in range(12000))
        log.write_text(content, encoding='utf-8')
        job_id = 'a' * 32
        job = {'id': job_id, 'name': 'Test', 'status': 'completed',
               'report_directory': str(report), 'output_directory': str(root / 'output')}
        (project / 'registration_jobs.json').write_text(json.dumps({
            'schema_version': 1, 'python': '', 'runtime': {
                'status': 'completed', 'native_backend': True, 'report_directory': str(report)},
            'jobs': [{**job, 'export': {'report_directory': str(report)}}]}))
        (project / 'trajectory_jobs.json').write_text(json.dumps({'draft': {}, 'jobs': [job]}))
        process = subprocess.Popen([str(executable), '--no-browser', '--project', str(project)],
                                   stdout=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if (project / 'session.json').exists():
                    break
                assert process.poll() is None, 'Isolated app exited'
                time.sleep(.1)
            session = json.loads((project / 'session.json').read_text())
            base = f'http://127.0.0.1:{session["port"]}'

            def api(path, authenticated=True):
                headers = {'X-Session-Token': session['token']} if authenticated else {}
                with urllib.request.urlopen(urllib.request.Request(base + '/api/' + path, headers=headers)) as response:
                    return json.load(response)

            endpoints = ['registration/log/runtime', f'registration/log/{job_id}',
                         f'registration/log/{job_id}?export=1', f'trajectory/{job_id}/log']
            for endpoint in endpoints:
                page = api(endpoint)
                assert len(page['text'].encode()) <= 32768 and page['start'] > 12000
                restored = page['text']
                last_end = page['end']
                while page['start']:
                    previous = api(endpoint + ('&' if '?' in endpoint else '?') + f'before={page["start"]}')
                    assert previous['end'] == page['start']
                    assert previous['start'] < page['start']
                    restored = previous['text'] + restored
                    page = previous
                assert restored == content, 'Backward paging dropped or duplicated UTF-8 text'
                assert api(endpoint + ('&' if '?' in endpoint else '?') + f'after={last_end}')['text'] == ''
            endpoint = endpoints[0]
            for query, status in [('?before=-1', 400), ('?after=abc', 400), ('?before=1&after=2', 400)]:
                try:
                    api(endpoint + query)
                    raise AssertionError('Invalid cursor accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == status
            for path, auth, status in [(endpoint, False, 403), ('registration/log/' + 'b' * 32, True, 400)]:
                try:
                    api(path, auth)
                    raise AssertionError('Unauthorized/unknown log accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == status
            assert api('registration')['log_url'] == endpoint
            assert api('trajectory')['jobs'][0]['log_url'] == endpoints[-1]

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
                page = browser.new_page(viewport={'width': 1400, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/#' + session['token'])
                page.wait_for_function('registration_loaded && trajectory_initialized')
                page.evaluate('closed = true')  # Test panels without unrelated automatic job selection.
                for panel in ['registration', 'additional', 'trajectory']:
                    log_id = panel + '-log'
                    tab = 'register' if panel == 'registration' else panel
                    page.evaluate('tab => selectTab(tab)', tab)
                    if panel == 'trajectory':
                        page.evaluate('element("trajectory-batch").hidden = false; element("trajectory-log").closest("details").open = true')
                    source = {'key': panel, 'url': endpoints[-1] if panel == 'trajectory' else endpoint}

                    def update():
                        page.evaluate('([id, source]) => updateWorkerLog(id, source)', [log_id, source])
                        page.wait_for_function('id => worker_logs.get(id)?.end !== null && !worker_logs.get(id)?.busy', arg=log_id)

                    def near_bottom():
                        return page.locator('#' + log_id).evaluate('(e) => Math.abs(e.scrollHeight - e.clientHeight - e.scrollTop) < 3')

                    update()
                    page.wait_for_function('id => element(id).scrollTop > 100', arg=log_id)
                    assert near_bottom(), panel + ' did not initially follow output'
                    page.locator('#' + log_id).evaluate('(e) => e.scrollTop -= 200')
                    page.wait_for_function('id => !worker_logs.get(id).follow', arg=log_id)
                    top = page.locator('#' + log_id).evaluate('(e) => e.scrollTop')
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write('New output while reading earlier messages\n' * 25)
                    update()
                    assert page.locator('#' + log_id).evaluate('(e) => e.scrollTop') == top
                    update()  # An unchanged poll must also leave the position alone.
                    assert page.locator('#' + log_id).evaluate('(e) => e.scrollTop') == top
                    page.evaluate('selectTab("convert")')
                    update()
                    page.evaluate('tab => selectTab(tab)', tab)
                    page.wait_for_timeout(100)
                    assert page.locator('#' + log_id).evaluate('(e) => e.scrollTop') == top, 'Tab switch moved reading position'

                    # Scrolling to the top prepends older pages without moving the visible text.
                    page.locator('#' + log_id).evaluate('(e) => e.scrollTop = 0')
                    page.wait_for_function('id => !worker_logs.get(id).busy && element(id).childNodes.length >= 3 && element(id).scrollTop > 100', arg=log_id)
                    for _ in range(30):
                        if page.evaluate('id => worker_logs.get(id).start === 0', log_id):
                            break
                        previous = page.evaluate('id => worker_logs.get(id).start', log_id)
                        page.locator('#' + log_id).evaluate('(e) => e.scrollTop = 0')
                        page.wait_for_function('([id, start]) => !worker_logs.get(id).busy && worker_logs.get(id).start < start', arg=[log_id, previous])
                    assert page.locator('#' + log_id).text_content() == log.read_text(encoding='utf-8')
                    page.locator('#' + log_id).evaluate('(e) => e.scrollTop = e.scrollHeight')
                    page.wait_for_function('id => worker_logs.get(id).follow', arg=log_id)
                    with log.open('a', encoding='utf-8') as stream:
                        stream.write('Following live output again\n' * 10)
                    update()
                    assert near_bottom(), 'Scrolling to bottom did not resume following'

                # Older running servers have no history endpoint; retain all received tails.
                page.evaluate('selectTab("additional")')
                old = ''.join(f'legacy {i}\n' for i in range(500))
                page.evaluate('tail => updateWorkerLog("additional-log", {key:"legacy", tail})', old)
                page.locator('#additional-log').evaluate('(e) => e.scrollTop -= 200')
                page.wait_for_function('!worker_logs.get("additional-log").follow')
                top = page.locator('#additional-log').evaluate('(e) => e.scrollTop')
                page.evaluate('tail => updateWorkerLog("additional-log", {key:"legacy", tail})', old[-1000:] + 'appended\n')
                assert page.locator('#additional-log').text_content() == old + 'appended\n'
                assert page.locator('#additional-log').evaluate('(e) => e.scrollTop') == top
                assert not errors, errors
                browser.close()

            # Resume polling after truncation and after a partially written Unicode character.
            old_size = log.stat().st_size
            log.write_bytes(b'new run\n' + '한'.encode()[:2])
            reset = api(endpoint + f'?after={old_size}')
            assert reset['reset'] and reset['text'] == 'new run\n'
            with log.open('ab') as stream:
                stream.write('한'.encode()[2:] + b'\n')
            assert api(endpoint + f'?after={reset["end"]}')['text'] == '한\n'
            print('PASS: bounded UTF-8 history, auth/cursors, all three panels, scroll lock/resume, tab switching, backward paging, legacy server, truncation.')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)


if __name__ == '__main__':
    main()
