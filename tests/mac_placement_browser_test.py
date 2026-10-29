"""Exercise the manual-only workspace through its real browser controls."""
import argparse
import json
import math
from pathlib import Path
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
    parser.add_argument('--rgb', action='store_true')
    parser.add_argument('--screenshot')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='blk mac browser ') as directory:
        root = Path(directory)
        project = root / 'project'
        paths = []
        for scan in range(4):
            path = root / f'Setup {scan}.ply'
            channels = 'property uchar red\nproperty uchar green\nproperty uchar blue\n' if args.rgb else 'property ushort intensity\n'
            header = ('ply\nformat binary_little_endian 1.0\nelement vertex 1000\n'
                      'property float x\nproperty float y\nproperty float z\n' + channels + 'end_header\n').encode()
            path.write_bytes(header + b''.join(struct.pack('<fffBBB' if args.rgb else '<fffH',
                (i % 10) * .1, ((i // 10) % 10) * .1, (i // 100) * .1,
                *([255, 16, i % 256] if args.rgb else [i * 50])) for i in range(1000)))
            paths.append(path)
        process = subprocess.Popen([str(Path(args.executable).resolve()), '--no-browser', '--project', str(project)])
        try:
            for _ in range(200):
                if (project / 'session.json').exists():
                    break
                assert process.poll() is None
                time.sleep(.1)
            session = json.loads((project / 'session.json').read_text())
            base = f'http://127.0.0.1:{session["port"]}'
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chrome, headless=True,
                    args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                page = browser.new_page(viewport={'width': 1400, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/#' + session['token'])
                page.wait_for_function('registration_loaded')
                assert page.evaluate('Boolean(window.BLK_RGB)') == args.rgb
                assert page.locator('[role="tab"]:visible').all_text_contents() == ['Register']
                assert page.locator('#panel-register').is_visible()
                assert page.locator('#registration-start').is_hidden()
                assert page.locator('#registration-setup').is_hidden()
                assert page.locator('#open-registration-result').is_hidden()
                assert page.locator('#placement-open').is_disabled()
                page.locator('#registration-folder').click()
                page.locator('#browser-path').fill(str(root))
                page.locator('#browser-go').click()
                page.wait_for_function('!document.querySelector("#browser-go").disabled')
                page.locator('#use-folder').click()
                page.wait_for_function('registration_ids.length === 4')
                assert page.locator('#placement-open').is_enabled()
                assert page.locator('#placement-open').inner_text() == 'Arrange PLY scans'
                if args.screenshot:
                    page.screenshot(path=args.screenshot)
                with page.expect_popup() as popup:
                    page.locator('#placement-open').click()
                editor = popup.value
                editor.on('pageerror', lambda error: errors.append(str(error)))
                editor.wait_for_function('window.blkPlacement?.items().length === 4 && document.body.dataset.loaded === "true"')
                assert editor.locator('#trim-step').input_value() == '0.5'
                assert int(editor.locator('body').get_attribute('data-shown')) > 0
                editor.locator('#x').fill('1.25')
                editor.locator('#x').press('Tab')
                editor.locator('#roll-plus').click()
                editor.locator('#pitch-minus').click()
                editor.locator('#reference').select_option('3')
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                poses = editor.evaluate('blkPlacement.items().map(i=>i.pose)')
                assert poses[1][0] == 1.25
                assert math.isclose(poses[1][4], math.radians(.5))
                assert math.isclose(poses[1][5], math.radians(-.5))
                camera = editor.evaluate('JSON.stringify(blkPlacement.camera)')
                editor.locator('#visualize-0').check()
                editor.locator('#visualize-3').click(modifiers=['Shift'])
                editor.wait_for_function('blkPlacement.visible().length === 4 && document.body.dataset.loaded === "true"')
                assert editor.evaluate('JSON.stringify(blkPlacement.camera)') == camera
                editor.locator('#next').click()
                editor.wait_for_function('document.body.dataset.loaded === "true"')
                assert sorted(editor.evaluate('blkPlacement.visible()')) == [1, 2]
                assert editor.evaluate('JSON.stringify(blkPlacement.camera)') == camera
                editor.locator('#moving').select_option('1')
                assert editor.locator('#reference').input_value() == '3'
                editor.wait_for_function('document.querySelector("#save-status").textContent.startsWith("Saved on disk:")')
                saved = json.loads((root / 'initial_placements.json').read_text())
                assert saved['poses']['Setup 1.ply'] == poses[1]
                assert saved['comparison_references']['Setup 1.ply'] == 'Setup 3.ply'
                editor.close()
                with page.expect_popup() as popup:
                    page.locator('#placement-open').click()
                editor = popup.value
                editor.wait_for_function('window.blkPlacement?.items().length === 4 && document.body.dataset.loaded === "true"')
                assert editor.evaluate('blkPlacement.items()[1].pose') == poses[1]
                assert editor.locator('#reference').input_value() == '3'
                assert not errors, errors
                browser.close()
            request = urllib.request.Request(base + '/api/shutdown', data=b'{}', headers={'X-Session-Token': session['token']})
            urllib.request.urlopen(request).close()
            process.wait(timeout=20)
            print('PASS: manual workspace, PLY picker, WebGL, RPY, references, autosave, camera, reopening')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
