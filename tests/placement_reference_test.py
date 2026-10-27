"""Regression: comparison selection is persistent context, never a pose parent."""
import argparse
import functools
import http.server
from pathlib import Path
import struct
import threading

import numpy as np
from playwright.sync_api import sync_playwright
from scipy.spatial.transform import Rotation


def run(chrome):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(Path(__file__).resolve().parents[1] / 'web'))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    poses = [[12, -3, 2, .4], [7, 8, 1, -.2], [20, 4, 3, 1.2], [-2, 6, -1, .8]]
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    items = [dict(id=str(i), name=f'Scan {i}', pose=pose,
                  scans=[dict(name=f'Scan {i}', points=10, base=identity, url=f'/api/sample/{i}')])
             for i, pose in enumerate(poses)]
    saved = dict(path='/test/initial_placements.json', initial_guesses=poses,
                 comparison_references=[None] * len(items))
    errors = []
    saves = []

    def save(route):
        data = route.request.post_data_json
        saves.append(data)
        saved.update(initial_guesses=data['initial_guesses'], comparison_references=data['comparison_references'])
        route.fulfill(json=saved)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True,
                args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport=dict(width=1500, height=1000))
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/api/registration/placement/test', lambda route: route.fulfill(json=dict(items=items, busy=False)))
            page.route('**/api/registration/placements/load', lambda route: route.fulfill(json=saved))
            page.route('**/api/registration/placements/save', save)
            page.route('**/api/sample/**', lambda route: route.fulfill(body=struct.pack('<ffff', 1, 2, 3, 1000),
                content_type='application/octet-stream'))
            page.goto(f'http://127.0.0.1:{server.server_port}/placement.html?job=test')
            page.wait_for_function("document.body.dataset.loaded === 'true'")
            assert 'No saved comparison' in page.locator('#reference-note').inner_text()
            camera_before = page.evaluate('JSON.stringify(blkPlacement.camera)')

            def unchanged():
                assert page.evaluate('blkPlacement.items().map(item=>item.pose)') == poses

            def wait_saved():
                page.wait_for_function("document.getElementById('save-status').textContent.startsWith('Saved on disk:')")

            unchanged()  # Even a nonzero first pose must retain its coordinate frame.
            page.locator('#reference').select_option('3')
            wait_saved()
            page.wait_for_function("document.body.dataset.loaded === 'true'")
            assert page.evaluate('JSON.stringify(blkPlacement.camera)') == camera_before
            unchanged()
            assert saved['comparison_references'][1] == 3
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '1'  # No saved choice: the scan just left.
            unchanged()
            page.locator('#reference').select_option('0')
            wait_saved()
            page.locator('#previous').click()
            assert page.locator('#reference').input_value() == '3'
            page.locator('#moving').select_option('2')
            assert page.locator('#reference').input_value() == '0'
            unchanged()
            # A nonconsecutive saved choice also wins over Next's default.
            page.locator('#reference').select_option('3');wait_saved()
            page.locator('#previous').click()
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '3'
            unchanged()
            page.reload()
            page.wait_for_function("document.body.dataset.loaded === 'true'")
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '3'
            unchanged()
            assert all(value['initial_guesses'] == poses for value in saves)

            page.locator('#reference').select_option('0');wait_saved()
            page.reload()
            page.wait_for_function("document.body.dataset.loaded === 'true'")
            assert page.locator('#reference').input_value() == '3'
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '0'
            unchanged()
            # Loading an old file must not invent historical comparison choices.
            saved['comparison_references'] = [None] * len(items)
            page.reload()
            page.wait_for_function("document.body.dataset.loaded === 'true'")
            assert page.locator('#reference').input_value() == '0'
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '1'
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '2'
            page.locator('#moving').select_option('1')
            page.locator('#next').click()
            assert page.locator('#reference').input_value() == '1'
            assert 'No saved comparison' in page.locator('#reference-note').inner_text()
            unchanged()
            # Browser and worker use the same full rigid transform convention;
            # independent expected rotations catch axis/order/sign mistakes.
            for yaw, roll, pitch in ((0, np.pi/2, 0), (0, 0, np.pi/2), (.4, -.3, .2)):
                values = [2, 3, 4, yaw, roll, pitch]
                actual = np.array(page.evaluate('pose=>blkPlacement.poseMatrix(pose)', values)).reshape(4,4).T
                expected = np.eye(4)
                expected[:3,:3] = Rotation.from_euler('xyz', [roll, pitch, yaw]).as_matrix()
                expected[:3,3] = [2, 3, 4]
                np.testing.assert_allclose(actual, expected, atol=1e-12)
            # Roll drag, undo and numeric pitch share the same autosave path.
            page.locator('#mode-place').click()
            page.locator('#tool').select_option('roll')
            before = page.evaluate('blkPlacement.items().map(i=>i.pose)')
            box = page.locator('#scene').bounding_box()
            x,y = box['x']+box['width']/2, box['y']+box['height']/2
            page.mouse.move(x,y);page.mouse.down()
            page.mouse.move(x+40,y,steps=4);page.mouse.up()
            wait_saved()
            after = page.evaluate('blkPlacement.items().map(i=>i.pose)')
            assert after[2][:4] == before[2]
            assert abs(after[2][4]-.2)<1e-12 and after[2][5]==0
            assert all(after[i]==before[i] for i in (0,1,3))
            page.locator('#undo').click();wait_saved();unchanged()
            page.locator('#pitch').fill('-12.5');page.locator('#pitch').press('Tab');wait_saved()
            assert abs(saved['initial_guesses'][2][5]-np.deg2rad(-12.5))<1e-12
            page.locator('#undo').click();wait_saved();unchanged()
            assert not errors, errors
            browser.close()
            print('PASS: Next preserves saved zero/nonconsecutive references; missing references use the scan just left; reload, legacy navigation, rotation/undo and unchanged saved poses.')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome', default='/usr/bin/google-chrome')
    run(parser.parse_args().chrome)
