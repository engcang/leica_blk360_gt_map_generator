"""Test Arrange entry, folder selection and saved poses in an isolated headless browser."""
import argparse
import json
from pathlib import Path
import shutil
import struct
import subprocess
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
                assert editor.locator('#density').input_value() == '10'
                editor.locator('#x').fill('1.25')
                editor.locator('#x').press('Tab')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                saved = json.loads((output / 'initial_placements.json').read_text(encoding='utf-8'))
                assert any(pose and pose[0] == 1.25 for pose in saved['poses'].values())
                editor.locator('#apply').click()
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk and verified:")')
                assert page.evaluate('registrationDraft().initial_guesses[1][0]') == 1.25
                editor.close()
                with page.expect_popup() as popup:
                    page.locator('#placement-open').click()
                reopened = popup.value
                reopened.on('pageerror', lambda error: errors.append(str(error)))
                reopened.wait_for_function('window.blkPlacement?.items().length === 3 && document.body.dataset.loaded === "true"')
                assert reopened.evaluate('blkPlacement.items()[1].pose[0]') == 1.25
                assert not errors, errors
                browser.close()
            request = urllib.request.Request(base + '/api/shutdown', data=b'{}',
                headers={'X-Session-Token': session['token']})
            with urllib.request.urlopen(request, timeout=5):
                pass
            process.wait(timeout=15)
            print('PASS: Add folder imports PLYs once; Arrange opens directly without another picker; render; autosave beside PLYs; Apply; reopen preserves poses; no browser errors.')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == '__main__':
    main()
