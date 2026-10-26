"""Exercise the new form and native file picker using the real app JavaScript."""
import argparse
import http.server
from pathlib import Path
import re
import subprocess
import tempfile
import threading


def run(chrome):
    web=Path(__file__).resolve().parents[1]/'web'
    prelude=r'''<script>
async function refreshRegistration(){}
let requests=[],state={draft:{},jobs:[],running:false};
window.fetch=async(url,options={})=>{
    const route=String(url).replace('/api/',''),body=options.body?JSON.parse(options.body):null;
    requests.push({route,body});let value;
    if(route==='project')value={jobs:[],point_clouds:[],running:false};
    else if(route==='trajectory')value=structuredClone(state);
    else if(route==='trajectory/draft'){state.draft=body;value={ok:true};}
    else if(route==='trajectory/start'){value={ok:true};}
    else if(route==='trajectory/0123456789abcdef0123456789abcdef/folder'){value={ok:true};}
    else if(route.startsWith('browse?'))value={path:'/data',parent:'/',roots:[],entries:[{name:'TLS.ply',path:'/data/TLS.ply',directory:false,modified_time:0},{name:'poses.tum',path:'/data/poses.tum',directory:false,modified_time:0}]};
    else throw Error('Unexpected API request '+route);
    return {ok:true,status:200,json:async()=>value};
};
</script>'''
    checks=r'''<script>
addEventListener('load',async()=>{
    const result=document.createElement('p');result.id='trajectory-controls-test';document.body.append(result);
    const check=(value,message)=>{if(!value)throw Error(message);};
    const settle=async()=>{for(let i=0;i<80;i++)await Promise.resolve();};
    try{
        await refresh();check(trajectory_initialized,'Trajectory settings did not initialize');
        check(location.hash==='#session-test','Copied app URL lost its session key');
        check(element('trajectory-workers').value===''&&trajectoryConfig().workers===0,'CPU default is not Auto');
        restoreTrajectory({workers:4});check(trajectoryConfig().workers===4,'Explicit CPU setting lost');
        restoreTrajectory({workers:0});check(element('trajectory-workers').value===''&&trajectoryConfig().workers===0,'Auto CPU setting lost');
        check([...document.querySelectorAll('[role=tab]')].at(-1).textContent==='Generate trajectory','Tab is not last');
        element('tab-trajectory').click();check(!element('panel-trajectory').hidden&&element('panel-convert').hidden,'Tab switching failed');
        document.querySelector('[data-trajectory-browse="trajectory-tls"]').click();await settle();
        check(element('browser').open&&!element('use-files').hidden,'File browser not opened');
        const checkbox=element('browser-list').querySelector('input[type=checkbox]');checkbox.click();
        element('use-files').click();await settle();check(element('trajectory-tls').value==='/data/TLS.ply','Selected file was not applied');
        document.querySelector('[data-trajectory-folder="trajectory-output"]').click();await settle();
        element('use-folder').click();await settle();check(element('trajectory-output').value==='/data','Selected output folder was not applied');
        element('trajectory-scans').value='/scans';element('trajectory-scan-format').value='tartanstar';element('trajectory-scan-format').dispatchEvent(new Event('change'));
        check(element('trajectory-calibration-mode').value==='tartanstar','Dataset calibration not selected');
        const first=element('trajectory-estimates').children[0];first.querySelector('.estimate-path').value='/poses/frontend.tum';
        element('trajectory-add-estimate').click();const second=element('trajectory-estimates').children[1];
        second.querySelector('.estimate-path').value='/poses/backend.csv';second.querySelector('.estimate-format').value='csv';
        second.querySelector('.estimate-format').dispatchEvent(new Event('change'));
        check(!second.querySelector('.estimate-csv').hidden&&second.querySelector('.estimate-kitti').hidden,'Format fields do not match CSV');
        second.querySelector('.estimate-columns').value='ts,px,py,pz,q1,q2,q3,q0';second.querySelector('.estimate-delimiter').value=';';
        second.querySelector('.estimate-time').value='0.000000001';
        element('trajectory-scan-time').value='0.000000001';
        element('trajectory-check').click();await settle();
        const request=requests.find(x=>x.route==='trajectory/start');check(request?.body.inspect,'Check inputs did not submit');
        const cfg=request.body.config;check(cfg.scans.calibration_preset==='tartanstar','Calibration lost');
        check(cfg.workers===0,'Auto CPU setting was not submitted');
        check(cfg.trajectories[1].columns.qw==='q0'&&cfg.trajectories[1].delimiter===';'&&cfg.trajectories[1].time_scale===1e-9,'CSV settings lost');
        restoreTrajectory(cfg);const restored=trajectoryConfig();
        check(restored.trajectories[1].delimiter===';','Restored format settings changed');
        check(restored.trajectories[1].time_scale===1e-9&&restored.scans.time_scale===1e-9,'Restored nanosecond units changed');
        requests=[];element('trajectory-start').click();await settle();check(requests.some(x=>x.route==='trajectory/start'&&!x.body.inspect),'Generate did not submit');
        state.jobs=[{id:'0123456789abcdef0123456789abcdef',name:'Saved trajectory',status:'completed',output_directory:'/results/trajectory',viewer_available:true}];
        await refreshTrajectory();const folderButton=[...element('trajectory-jobs').querySelectorAll('button')].find(x=>x.textContent==='Open output folder');
        const viewer=element('trajectory-jobs').querySelector('a'),style=getComputedStyle(viewer);
        check(viewer.href.includes('job=0123456789abcdef0123456789abcdef')&&viewer.target==='_blank','Viewer action targets wrong result');
        check(parseFloat(style.paddingLeft)>=17&&style.backgroundColor==='rgb(23, 110, 91)','Viewer action lacks the app button style');
        check(folderButton,'Output folder action missing');folderButton.click();await settle();
        check(requests.some(x=>x.route==='trajectory/0123456789abcdef0123456789abcdef/folder'),'Output folder action targets wrong job');
        const saved={status:'completed',viewer_available:true,config:{scans:{path:'/scans/sequence'},tls_map:'/maps/site.ply'}};
        const history=[{...saved,id:'old'},{...saved,id:'new'},
            {...saved,id:'running',status:'running',viewer_available:false},
            {...saved,id:'failed',status:'failed',viewer_available:false},
            {...saved,id:'other-site',config:{scans:{path:'/different/sequence'},tls_map:'/maps/site.ply'}},
            {...saved,id:'other-map',config:{scans:{path:'/scans/sequence'},tls_map:'/maps/other.ply'}}];
        check(visibleTrajectoryJobs(history).map(j=>j.id).join(',')==='new,running,failed,other-site,other-map','Repeated result was not replaced, or an unrelated/pending result was hidden');
        check(visibleTrajectoryJobs([{...saved,id:'old',status:'saved'},{...saved,id:'new',status:'saved'}]).length===1,'Saved result duplicates remain');
        state.jobs=[
            {...saved,id:'active',name:'Active recovery',status:'running',managed_externally:true,output_directory:'/active'},
            {...saved,id:'waiting',name:'Queued recovery',status:'queued',managed_externally:true,viewer_available:false,output_directory:'/waiting'},
            {...saved,id:'finished',name:'Finished recovery',status:'completed',managed_externally:true,output_directory:'/finished'}];
        await refreshTrajectory();
        check(element('trajectory-message').textContent==='1 running · 1 queued · 1 completed','Live status summary is incorrect');
        check(element('trajectory-jobs').children.length===3,'Pending trajectory is missing');
        check(element('trajectory-jobs').children[0].querySelector('a'),'Running recovery lost its saved-result viewer');
        state.jobs[0].status='interrupted';await refreshTrajectory();
        check(![...element('trajectory-jobs').children[0].querySelectorAll('button')].some(b=>b.textContent==='Resume'),'External recovery offered a duplicate worker');
        const goodFetch=window.fetch;
        window.fetch=async()=>({ok:false,status:403,json:async()=>({error:'Invalid session'})});
        await refresh();check(element('connection').textContent.startsWith('Session key missing or expired'),'Authentication failure mislabeled as disconnected');
        window.fetch=async()=>{throw new TypeError('Failed to fetch');};
        await refresh();check(element('connection').textContent.startsWith('Disconnected'),'Network failure mislabeled');
        window.fetch=goodFetch;
        result.textContent='PASS';
    }catch(error){result.textContent='FAIL: '+error.message;}
});
</script>'''
    html=(web/'index.html').read_text().replace('<script src="/app.js" defer></script>',prelude+'<script src="/app.js" defer></script>')
    for name in ('registration','additional'):html=html.replace(f'<script src="/{name}.js" defer></script>','')
    html=html.replace('</body>',checks+'</body>')
    routes={'/':html.encode(),**{'/'+name:(web/name).read_bytes() for name in ('app.js','app.css','trajectory.js')}}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            value=routes.get(self.path);self.send_response(200 if value is not None else 404)
            self.send_header('Content-Type','application/javascript' if self.path.endswith('.js') else 'text/css' if self.path.endswith('.css') else 'text/html; charset=utf-8')
            self.end_headers();self.wfile.write(value or b'')
        def log_message(self,*args):pass
    with http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='blk-trajectory-controls-') as directory:
                result=subprocess.run([chrome,'--headless','--no-sandbox','--no-first-run','--disable-background-networking','--disable-gpu',
                    '--user-data-dir='+directory,'--virtual-time-budget=4000','--dump-dom',f'http://127.0.0.1:{server.server_port}/#session-test'],capture_output=True,text=True,timeout=45)
                if result.returncode or '<p id="trajectory-controls-test">PASS</p>' not in result.stdout:
                    detail=re.search(r'<p id="trajectory-controls-test">(.*?)</p>',result.stdout)
                    raise AssertionError(detail[1] if detail else result.stderr[-2000:])
        finally:server.shutdown();thread.join()
    print('Trajectory controls passed: last tab, file/folder selection, CSV mapping, calibration, input check, generation.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--chrome',required=True)
    run(parser.parse_args().chrome)
