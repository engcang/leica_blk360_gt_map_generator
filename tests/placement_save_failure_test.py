"""Browser regression for save errors, recovery, old servers and in-flight edits."""
import asyncio
import functools
import http.server
import json
from pathlib import Path
import struct
import tempfile
import threading

from playwright.async_api import async_playwright


async def run(base):
    identity = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    poses = [[0,0,0,0], [1,2,3,.4], [4,5,6,.7]]
    items = [dict(id=str(i), name=f'Scan {i}', pose=pose,
                  scans=[dict(name=f'Scan {i}', points=10, base=identity, url=f'/api/sample/{i}')])
             for i, pose in enumerate(poses)]
    saved = dict(path='/test/initial_placements.json', initial_guesses=poses,
                 comparison_references=[None,0,1])
    state = dict(mode='error', old=False, calls=[], held=None)
    release = asyncio.Event()
    errors = []

    async def load(route):
        result = dict(saved)
        if state['old']:
            result.pop('comparison_references')
        await route.fulfill(json=result)

    async def save(route):
        body = route.request.post_data_json
        state['calls'].append(body)
        mode = state['mode']
        if mode == 'error':
            await route.fulfill(status=400, json=dict(error='Synthetic disk write failure'))
            return
        if mode in ('hold', 'timeout'):
            state['held'] = body
            await release.wait()
        saved.update(initial_guesses=body['initial_guesses'], comparison_references=body['comparison_references'])
        await route.fulfill(json=saved)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path='/usr/bin/google-chrome', headless=True,
            args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page = await browser.new_page(accept_downloads=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.route('**/api/registration/placement/test', lambda route: route.fulfill(json=dict(items=items,busy=False)))
        await page.route('**/api/registration/placements/load', load)
        await page.route('**/api/registration/placements/save', save)
        await page.route('**/api/sample/**', lambda route: route.fulfill(body=struct.pack('<ffff',1,2,3,1000), content_type='application/octet-stream'))
        url=base+'/placement.html?job=test'
        await page.goto(url)
        await page.wait_for_function('document.body.dataset.loaded === "true"')
        await page.locator('#roll-plus').click()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "error"')
        assert 'Not saved to disk: Synthetic disk write failure' in await page.locator('#save-status').inner_text()
        current=await page.evaluate('blkPlacement.items().map(i=>i.pose)')
        assert len(current[1])==6
        assert await page.evaluate('Object.keys(localStorage).some(k=>k.startsWith("blk-placement-draft:"))')
        await page.locator('#visualize-0').check()
        await page.wait_for_function('document.body.dataset.loaded === "true"')
        assert await page.locator('#save-status').get_attribute('data-state')=='error'
        async with page.expect_download() as event:
            await page.locator('#recovery-copy').click()
        download=await event.value
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'recovery.json';await download.save_as(path)
            recovery=json.loads(path.read_text())
            assert [i['pose'] for i in recovery['items']]==current
            assert recovery['items'][1]['comparisonReference']==0
            assert recovery['context']['resume_id']=='test'
        # Reload cannot erase the browser copy. Restoring is explicit and then saved.
        page.on('dialog', lambda dialog: dialog.accept())
        await page.reload()
        await page.wait_for_function('document.body.dataset.loaded === "true"')
        assert await page.locator('#restore-draft').is_visible()
        state['mode']='success'
        await page.locator('#restore-draft').click()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "saved"')
        assert saved['initial_guesses']==current
        assert not await page.evaluate('Object.keys(localStorage).some(k=>k.startsWith("blk-placement-draft:"))')
        # A failed autosave can be retried without changing the pose again.
        state['mode']='error'
        await page.locator('#pitch-plus').click()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "error"')
        state['mode']='success';await page.locator('#save-now').click()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "saved"')
        assert saved['initial_guesses']==await page.evaluate('blkPlacement.items().map(i=>i.pose)')
        # Edits made during a request are coalesced into the next current snapshot.
        state['mode']='hold';release.clear();state['held']=None
        await page.locator('#roll-plus').click()
        while state['held'] is None:
            await asyncio.sleep(.02)
        await page.locator('#pitch-plus').click()
        expected=await page.evaluate('blkPlacement.items().map(i=>i.pose)')
        state['mode']='success';release.set()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "saved"')
        assert saved['initial_guesses']==expected
        # A hung request must leave Saving, retain its draft, and allow a retry.
        state['mode']='timeout';release.clear();state['held']=None
        await page.locator('#roll-plus').click()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "error"', timeout=20000)
        assert '15 seconds' in await page.locator('#save-status').inner_text()
        assert await page.evaluate('Object.keys(localStorage).some(k=>k.startsWith("blk-placement-draft:"))')
        release.set();state['mode']='success'
        await page.locator('#save-now').click()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "saved"')
        # An incompatible server must be detected before any edits or writes.
        state['old']=True;before=len(state['calls'])
        await page.reload()
        await page.wait_for_function('document.querySelector("#save-status").dataset.state === "error"')
        assert 'outdated' in await page.locator('#save-status').inner_text()
        assert await page.evaluate('blkPlacement.items().length')==0
        assert await page.locator('#save-now').is_disabled()
        assert len(state['calls'])==before
        assert not errors,errors
        await browser.close()
    print('PASS: persistent save errors, full recovery download, browser-draft restore, manual retry, in-flight edits, timeout, and old-server rejection before editing.')


if __name__=='__main__':
    handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=str(Path(__file__).resolve().parents[1]/'web'))
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        asyncio.run(run(f'http://127.0.0.1:{server.server_port}'))
    finally:
        server.shutdown();server.server_close()
