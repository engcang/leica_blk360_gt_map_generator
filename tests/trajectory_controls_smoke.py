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
let requests=[],state={draft:{},jobs:[{id:'hidden',name:'Development result',status:'completed',output_directory:'/debug',viewer_available:true}],running:false};
const seq=path=>({name:path.split('/').pop(),sequence_directory:path,scans:{path:path+'/scans',format:'directory'},trajectories:[{path:path+'/odometry.tum',format:'tum'}]});
window.fetch=async(url,options={})=>{
    const route=String(url).replace('/api/',''),body=options.body?JSON.parse(options.body):null;
    requests.push({route,body});let value;
    if(route==='project')value={jobs:[],point_clouds:[],running:false};
    else if(route==='trajectory')value=structuredClone(state);
    else if(route==='trajectory/draft'){state.draft=body;value={ok:true};}
    else if(route==='trajectory/discover')value={sequences:body.paths.map(seq)};
    else if(route==='trajectory/start')value={ok:true,ids:['job1','job2'],concurrency:2};
    else if(route==='trajectory/open')value={ok:true,ids:['opened']};
    else if(route==='trajectory/opened/folder')value={ok:true};
    else if(route.startsWith('browse?'))value={path:'/data',parent:'/',roots:[],entries:[
        {name:'one',path:'/data/one',directory:true},{name:'two',path:'/data/two',directory:true},
        {name:'TLS.ply',path:'/data/TLS.ply',directory:false},{name:'odometry.tum',path:'/data/odometry.tum',directory:false}]};
    else throw Error('Unexpected API request '+route);
    return {ok:true,status:200,json:async()=>value};
};
</script>'''
    checks=r'''<script>
addEventListener('load',async()=>{
    const result=document.createElement('p');result.id='trajectory-controls-test';document.body.append(result);
    const check=(value,message)=>{if(!value)throw Error(message);};
    const settle=async()=>{for(let i=0;i<100;i++)await Promise.resolve();};
    try{
        await refresh();check(trajectory_initialized,'Form not initialized');
        check(element('trajectory-jobs').children.length===0,'Cached project results were displayed');
        check(element('trajectory-message').textContent==='','Development jobs leaked into status');
        check(!element('trajectory-period')&&!element('trajectory-options')&&!element('trajectory-initial'),'Retired controls remain');
        check([...document.querySelectorAll('[role=tab]')].at(-1).textContent==='Generate trajectory','Tab is not last');
        element('tab-trajectory').click();
        element('trajectory-map-browse').click();await settle();
        element('browser-list').querySelector('input[type=checkbox]').click();element('use-files').click();await settle();
        check(element('trajectory-tls').value==='/data/TLS.ply','Map selection failed');
        element('trajectory-add-sequences').click();await settle();
        const checks=element('browser-list').querySelectorAll('input[type=checkbox]');
        check(checks.length===2,'Folder picker does not support multiple folders');checks.forEach(c=>c.click());
        check(!element('use-files').disabled,'Multiple folder selection is disabled');element('use-files').click();await settle();
        check(element('trajectory-sequences').children.length===2,'Sequences were not added');
        let configs=trajectorySequences();check(configs.every(c=>c.period_s===0),'New jobs do not use every scan');
        check(configs[0].output_directory==='/data/one/.ground_truth','Wrong result folder');
        check(element('trajectory-output-paths').children[1].textContent==='/data/two/ground_truth_trajectory.tum','Wrong output filename');
        const second=element('trajectory-sequences').children[1];
        second.querySelector('.sequence-odometry').open=true;second.querySelector('.sequence-add-estimate').click();const comparison=second.querySelector('.sequence-estimates').lastElementChild;
        comparison.querySelector('.estimate-path').value='/data/two/backend.csv';
        comparison.querySelector('.estimate-format').value='csv';comparison.querySelector('.estimate-format').dispatchEvent(new Event('change'));
        comparison.querySelector('.estimate-columns').value='ts,px,py,pz,q1,q2,q3,q0';
        comparison.querySelector('.estimate-time').value='0.000000001';
        configs=trajectorySequences();check(configs[1].trajectories[1].columns.qw==='q0'&&configs[1].trajectories[1].time_scale===1e-9,'Input format settings lost');
        element('trajectory-workers').value='8';element('trajectory-check').click();await settle();
        let request=requests.find(x=>x.route==='trajectory/start');check(request.body.configs.length===2&&request.body.workers===8&&request.body.inspect,'Batch check did not submit all sequences');
        requests=[];element('trajectory-start').click();await settle();check(requests.some(x=>x.route==='trajectory/start'&&!x.body.inspect),'Generation did not submit');
        state.jobs.push({id:'opened',name:'Opened result',status:'saved',output_directory:'/selected',viewer_available:true});
        await refreshTrajectory();check(element('trajectory-jobs').children.length===0,'Unselected results appeared');
        await openTrajectoryResults('/selected');
        check(element('trajectory-jobs').children.length===1,'Selected result missing');
        const viewer=element('trajectory-jobs').querySelector('a'),style=getComputedStyle(viewer);
        check(viewer.href.includes('job=opened')&&parseFloat(style.paddingLeft)>=17,'Viewer button style or link wrong');
        element('trajectory-clear-results').click();await settle();check(element('trajectory-jobs').children.length===0,'Clear results failed');
        check(element('trajectory-workers').getBoundingClientRect().top<element('trajectory-start').getBoundingClientRect().top,'CPU setting not above generation');
        state.running=true;state.jobs.push({id:'live',name:'Live batch',status:'running',output_directory:'/live',viewer_available:false});trajectory_batch_ids.clear();await refreshTrajectory();check(!element('trajectory-stop').disabled&&trajectory_batch_ids.has('live'),'Reload lost the active app batch');state.running=false;
        const goodFetch=window.fetch;
        window.fetch=async()=>({ok:false,status:403,json:async()=>({error:'Invalid session'})});
        await refresh();check(element('connection').textContent.startsWith('Session key missing or expired'),'Authentication failure mislabeled');
        window.fetch=goodFetch;result.textContent='PASS';
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
    print('Trajectory controls passed: multiple sequences, all scans, per-sequence formats, batch submission, explicit results, CPU placement.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--chrome',required=True)
    run(parser.parse_args().chrome)
