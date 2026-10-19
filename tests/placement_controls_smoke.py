"""Check placement mouse modes and transformed-height clipping in an actual WebGL browser."""
import argparse
import functools
import http.server
import json
import pathlib
import struct
import threading
from playwright.sync_api import sync_playwright


def run(chrome, screenshot=None):
    repo = pathlib.Path(__file__).resolve().parents[1]
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(repo / "web"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    identity = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]]
    shifted = [[1,0,0,5],[0,1,0,0],[0,0,1,1],[0,0,0,1]]
    points = [(x*.055 + 2.5*roof, y*.055, 2.*roof, 40000) for roof in (0,1) for x in range(40) for y in range(40)]
    payload = b''.join(struct.pack('<ffff',*point) for point in points)
    items = [{'id':str(i),'name':f'Chunk {i+1}','pose':None,
              'scans':[{'name':f'Scan {i+1}','points':len(points)*10,'base':matrix,'url':f'/api/sample/{i}'}]}
             for i,matrix in enumerate((identity,shifted))]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome,headless=True,
                        args=['--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width':1500,'height':1000})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/api/registration/placement/test',lambda route:route.fulfill(json={'items':items,'busy':False}))
            page.route('**/api/sample/**',lambda route:route.fulfill(body=payload,content_type='application/octet-stream'))
            page.goto(f'http://127.0.0.1:{server.server_port}/placement.html?job=test')
            page.wait_for_function("document.body.dataset.loaded === 'true'")
            assert page.locator('#mode-view').get_attribute('aria-pressed')=='true'
            assert page.locator('#tool').is_disabled()
            initial_pose=page.evaluate('JSON.stringify(blkPlacement.items())')
            initial_camera=page.evaluate('JSON.stringify(blkPlacement.camera)')
            box=page.locator('#scene').bounding_box()
            x,y=box['x']+box['width']*.5,box['y']+box['height']*.5
            def drag(button='left'):
                page.mouse.move(x,y);page.mouse.down(button=button)
                page.mouse.move(x+45,y+25,steps=3);page.mouse.up(button=button)
            drag()
            assert page.evaluate('JSON.stringify(blkPlacement.camera)')!=initial_camera
            assert page.evaluate('JSON.stringify(blkPlacement.items())')==initial_pose
            page.locator('#top').click()
            page.locator('#mode-place').click()
            initial_camera=page.evaluate('JSON.stringify(blkPlacement.camera)')
            drag()
            assert page.evaluate('JSON.stringify(blkPlacement.camera)')==initial_camera
            assert page.evaluate('JSON.stringify(blkPlacement.items())')!=initial_pose
            page.locator('#undo').click()
            assert page.evaluate('JSON.stringify(blkPlacement.items())')==initial_pose
            drag('right')
            assert page.evaluate('JSON.stringify(blkPlacement.items())')==initial_pose
            # Changing modes during a drag cancels the gesture rather than continuing to move a cloud.
            page.mouse.move(x,y);page.mouse.down()
            page.evaluate("document.getElementById('mode-view').click()")
            page.mouse.move(x+30,y+30);page.mouse.up()
            assert page.evaluate('JSON.stringify(blkPlacement.items())')==initial_pose
            page.locator('#fit').click()
            # Read the actual WebGL framebuffer immediately after the requested draw, before compositing.
            def pixels(value):
                return page.evaluate("""value => new Promise(resolve => {
                    if(value===null)document.getElementById('height-all').click();
                    else { const input=document.getElementById('height-value');input.value=value;input.dispatchEvent(new Event('change')); }
                    requestAnimationFrame(()=>{
                        const c=document.getElementById('scene'),gl=c.getContext('webgl');
                        const rgba=new Uint8Array(c.width*c.height*4);gl.readPixels(0,0,c.width,c.height,gl.RGBA,gl.UNSIGNED_BYTE,rgba);
                        let n=0;for(let i=0;i<rgba.length;i+=4)if(rgba[i]>35||rgba[i+1]>50||rgba[i+2]>70)n++;
                        resolve(n);
                    });
                })""",value)
            all_pixels=pixels(None)
            clipped=pixels(.5)
            assert all_pixels>1000 and .20 < clipped/all_pixels < .30,(all_pixels,clipped)
            assert page.locator('#height-enabled').is_checked()
            assert page.evaluate('JSON.stringify(blkPlacement.items())')==initial_pose
            # The selected chunk's base translates Z by +1. Editing Z by -1 should expose its floor too.
            page.locator('#z').fill('-1');page.locator('#z').press('Tab')
            two_floors=pixels(.5)
            assert .45 < two_floors/all_pixels < .55,(all_pixels,two_floors)
            assert page.locator('#height-value').input_value()=='0.50'
            page.locator('#height-enabled').uncheck()
            restored=pixels(None)
            assert abs(restored-all_pixels)/all_pixels<.01
            page.locator('#height-cut').evaluate("node=>{node.value='0.5';node.dispatchEvent(new Event('input'));}")
            assert page.locator('#height-enabled').is_checked()
            if screenshot:page.screenshot(path=screenshot)
            assert not errors,errors
            browser.close()
            print('Placement controls passed: camera/placement isolation, drag cancellation, XYZ-transformed height clipping, slider and show-all restoration.')
    finally:
        server.shutdown();server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--chrome',default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    parser.add_argument('--screenshot')
    args=parser.parse_args();run(args.chrome,args.screenshot)
