"""Exercise trajectory layers and bounded PLY uploads in Chrome."""
import argparse
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright


def run(chrome, standalone=False):
    web = Path(__file__).resolve().parents[1]/'web'
    prelude = r'''<script>
const uploads=[];let failMapRead=false;
localStorage.setItem('blk-trajectory-viewer-v1',JSON.stringify({common:{'point-style':'round-points',size:1},views:{}}));
const original=WebGL2RenderingContext.prototype.bufferData;
WebGL2RenderingContext.prototype.bufferData=function(target,data,usage){
    const size=typeof data==='number'?data:data.byteLength;
    if(size>16*1024*1024)throw Error('Oversized GPU allocation');
    uploads.push(size);return original.call(this,target,data,usage);
};
const count=1048576+17;
const header=new TextEncoder().encode(`ply\nformat binary_little_endian 1.0\nelement vertex ${count}\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n`);
const bytes=new Uint8Array(header.length+count*14);bytes.set(header);
const view=new DataView(bytes.buffer);
for(let i=0;i<count;i++){view.setFloat32(header.length+i*14,(i%100)*.01,true);view.setFloat32(header.length+i*14+4,(Math.floor(i/100)%100)*.01,true);view.setFloat32(header.length+i*14+8,Math.floor(i/10000)*.01,true);}
const scene={name:'Viewer test',gap_s:.5,maps:[{key:'tls_10cm',label:'TLS map · 10 cm',url:'/map.ply',points:count,bounds:[[0,0,0],[1,1,1]],color:'#ffffff'},{key:'tls_1cm',label:'TLS map · 1 cm',url:'/map.ply',points:count,bounds:[[0,0,0],[1,1,1]],color:'#ffffff'},{key:'tls_gt',label:'TLS GT map · 10 cm',url:'/map.ply',points:count,bounds:[[0,0,0],[1,1,1]],color:'#ff9b54'}],trajectories:[{key:'tls_gt',label:'TLS GT trajectory',color:'#ffffff',rows:[[0,0,0,0],[.1,1,0,0],[.3,2,0,0],[.4,3,0,0]]},{key:'estimate_0',label:'Odometry',color:'#5ec8de',rows:[[0,0,1,0],[.1,1,1,0]]}],quality:[{index:0,accepted:true},{index:1,accepted:true},{index:2,accepted:false},{index:3,accepted:true},{index:4,accepted:true}]};
window.fetch=async(url,options={})=>{
    if(String(url).includes('/api/trajectory/'))return new Response(JSON.stringify(scene),{status:200});
    if(options.method==='HEAD')return new Response(null,{status:200,headers:{'Content-Length':bytes.length}});
    if(failMapRead)return new Response(null,{status:500});
    const match=options.headers.Range.match(/bytes=(\d+)-(\d+)/);
    return new Response(bytes.slice(Number(match[1]),Number(match[2])+1),{status:206});
};
</script>'''
    if standalone: prelude = prelude.replace('</script>', 'scene.maps=[];</script>')
    checks = r'''<script>
addEventListener('load',async()=>{
    const result=document.createElement('p');result.id='trajectory-test';document.body.append(result);
    const check=(value,message)=>{if(!value)throw Error(message);};
    const wait=async(fn)=>{for(let i=0;i<200;i++){if(fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('Viewer timeout: '+document.getElementById('error').textContent);};
    try{
        await wait(()=>window.blkTrajectoryViewer&&!blkTrajectoryViewer.stats().loading&&blkTrajectoryViewer.stats().loaded===count);
        const v=blkTrajectoryViewer;
        const options=[...document.getElementById('map').options];
        check(!options.some(o=>o.value==='tls_1cm'),'Legacy 1 cm scanned map is still offered');
        check(options.find(o=>o.value==='tls_10cm').textContent==='Scanned GT map · 10 cm','Scanned map label did not migrate');
        check(options.find(o=>o.value==='tls_gt').textContent==='Generated ground-truth trajectory map · 10 cm','Generated map label did not migrate');
        v.render();check(v.stats().displayed===count,'Cloud lost points');
        check(uploads.includes(1048576*12)&&uploads.includes(17*12),'Map was not uploaded in bounded chunks');
        check(v.stats().visibleTrajectories.length===2,'Missing trajectories');
        check(v.stats().segments.tls_gt===2,'Rejected GT scan was bridged by a line');
        document.getElementById('path-tls_gt').checked=false;v.render();
        check(JSON.stringify(v.stats().visibleTrajectories)===JSON.stringify(['estimate_0']),'Trajectory toggle failed');
        const camera=JSON.stringify(v.camera()), mapUploads=uploads.filter(n=>n===1048576*12).length;
        scene.preview=true;
        scene.trajectories[0].rows.push([.5,4,0,0]);
        scene.quality.push({index:5,accepted:true});
        await v.refresh();v.render();
        check(v.stats().segments.tls_gt===3,'Live trajectory did not grow');
        check(JSON.stringify(v.camera())===camera,'Live update moved the camera');
        check(!document.getElementById('path-tls_gt').checked,'Live update reset a trajectory toggle');
        check(uploads.filter(n=>n===1048576*12).length===mapUploads,'Live update reuploaded an unchanged map');
        check(document.getElementById('trajectory-summary').textContent.includes('Auto-updating'),'Missing live status');
        check(await v.refresh()===false,'Unchanged scene was rebuilt');
        document.getElementById('map').value='none';document.getElementById('map').dispatchEvent(new Event('change'));v.render();
        check(v.stats().map===''&&v.stats().displayed===0,'Hide map failed');
        document.getElementById('map').value='tls_gt';document.getElementById('map').dispatchEvent(new Event('change'));
        await wait(()=>!v.stats().loading&&v.stats().loaded===count);
        document.getElementById('point-style').value='round-points';document.getElementById('point-style').dispatchEvent(new Event('change'));v.render();
        check(v.stats().pointStyle==='round-points'&&v.stats().displayed===count,'Point style failed');
        check(v.stats().error==='','Unexpected renderer error: '+v.stats().error);
        check(!document.querySelector('a[download]'),'Redundant map download link');
        failMapRead=true;document.getElementById('map').dispatchEvent(new Event('change'));
        await wait(()=>!v.stats().loading);v.render();
        check(v.stats().loaded===0&&v.stats().displayed===0&&v.stats().error.includes('500'),'Failed load left partial cloud or hid the error');
        result.textContent='PASS';
    }catch(error){result.textContent='FAIL: '+error.message;}
});
</script>'''
    if standalone:
        checks = r'''<script>
addEventListener('load',async()=>{
 const result=document.createElement('p');result.id='trajectory-test';document.body.append(result);
 try {
  for(let n=0;n<200&&!window.blkTrajectoryViewer;n++)await new Promise(r=>setTimeout(r,25));
  const viewer=window.blkTrajectoryViewer;if(!viewer)throw Error('Viewer did not initialize');
  viewer.render();const state=viewer.stats();
  if(state.loading||state.error||state.displayed||state.visibleTrajectories.length!==2)throw Error(JSON.stringify(state));
  if(document.getElementById('map').value!=='none')throw Error('Missing map should default to trajectories only');
  document.getElementById('fit').click();viewer.render();
  if(!viewer.camera().center.every(Number.isFinite))throw Error('Invalid camera for TUM-only inputs');
  result.textContent='PASS';
 }catch(error){result.textContent='FAIL: '+error.message;}
});
</script>'''
    html = (web/'trajectory-viewer.html').read_text()
    html = html.replace('<link rel="stylesheet" href="trajectory-viewer.css">', '<style>'+(web/'trajectory-viewer.css').read_text()+'</style>'+prelude)
    for name in ('trajectory-viewer.js','trajectory-renderer.js'):
        html = html.replace(f'<script src="{name}"></script>', '<script>'+(web/name).read_text()+'</script>')
    html = html.replace('</body>', checks+'</body>')
    with tempfile.TemporaryDirectory(prefix='blk-trajectory-viewer-') as directory:
        root = Path(directory); page = root/'test.html'; page.write_text(html)
        # Real browser time avoids virtual-time starvation around recurring fetches.
        with sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=chrome, headless=True,
                args=['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            try:
                tab = browser.new_page(viewport={'width':1000,'height':700})
                errors = []
                tab.on('pageerror', lambda error: errors.append(str(error)))
                tab.goto(page.as_uri()+'?job=test', wait_until='domcontentloaded')
                tab.wait_for_function('document.getElementById("trajectory-test")?.textContent', timeout=30000)
                assert tab.locator('#trajectory-test').inner_text() == 'PASS', tab.locator('#trajectory-test').inner_text()
                assert not errors, errors
            finally:
                browser.close()
    print('Trajectory viewer passed: complete uploads, live updates, camera, paths and map switching.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--chrome',required=True)
    chrome=parser.parse_args().chrome
    run(chrome)
    run(chrome, standalone=True)
