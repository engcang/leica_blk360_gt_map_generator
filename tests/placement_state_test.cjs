// Regression tests for user-authored placements and explicit workspace selections.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const path = require('path');
assert.equal(fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').includes('placement-load'),false);
assert.equal(fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').includes('registration-converted'),false);
assert.equal(fs.readFileSync(path.join(__dirname,'../web/registration.js'),'utf8').includes('registration-converted'),false);
for (const id of ['project-name', 'save-name', 'export', 'directory']) {
    assert.equal(fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').includes(`id="${id}"`),false);
    assert.equal(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8').includes(`element('${id}')`),false);
}
class Element {
    constructor() { this.children = []; this.value = ''; this.textContent = ''; this.disabled = false; this.handlers = {}; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute() {}
    addEventListener(type, callback) { this.handlers[type] = callback; }
}
const nodes = new Map(), element = id => { if (!nodes.has(id)) nodes.set(id,new Element()); return nodes.get(id); };
const callbacks = {}, messages = [], calls = [];
const context = vm.createContext({console, setTimeout, clearTimeout, setInterval:()=>{}, URLSearchParams,
    document:{getElementById:element,createElement:()=>new Element(),querySelectorAll:()=>[],addEventListener:()=>{}},
    location:{origin:'http://test',hash:'',pathname:'/'},history:{replaceState:()=>{}},
    sessionStorage:{getItem:()=>'',setItem:()=>{}},window:{addEventListener:(type,handler)=>callbacks[type]=handler},
    fetch:()=>{throw Error('Unexpected network access');}, confirm:()=>true});
const run = code => vm.runInContext(code,context);
run(fs.readFileSync(path.join(__dirname,'../web/app.js'),'utf8'));
run(fs.readFileSync(path.join(__dirname,'../web/registration.js'),'utf8'));
const clouds = Array.from({length:61},(_,i)=>({id:`id${i}`,name:`Setup${i}.ply`,path:`/scans/${i}.ply`,available:true,modified_time:i}));
context.fixture={point_clouds:clouds,jobs:[{id:'old',status:'completed'},{id:'old-ready',status:'ready'}],output_directory:'/old/folder',name:'Test',project_directory:'/record'};
context.state={jobs:[{id:'old-reg',name:'Old',status:'needs_connections',scans:[],voxel_m:.01}],busy:false,runtime:{status:'completed'},draft:{output_directory:'/old/map'},python:'python'};
context.mockApi=async(route,body)=>{
    calls.push({route,body:body===undefined?null:JSON.parse(JSON.stringify(body))});
    if(route==='project')return context.fixture;
    if(route==='registration')return context.state;
    if(route==='registration/placements/load') { if(context.failLoad)throw Error('Disk read failed'); const records=context.savedByOutput?.[body.output_directory] ?? context.savedById; const result={path:body.output_directory+'/initial_placements.json',initial_guesses:body.cloud_ids.map(id=>records?.[id]||null)}; if(context.deferLoad)return new Promise(resolve=>context.finishLoad=()=>resolve(result)); return result; }
    if(route==='registration/placements/save') {if(context.failSave)throw Error('Disk write failed');return {path:'/output/initial_placements.json'};}
    return {ok:true};
};
run('api = mockApi;');
(async()=>{
    await run('refresh()');
    assert.equal(element('job-count').textContent,0);
    assert.equal(element('cloud-count').textContent,0);
    assert.equal(element('output-path').value,'');
    assert.equal(element('map-output').value,'');
    assert.equal(element('registration-jobs').children.length,1);
    assert.match(element('registration-jobs').children[0].textContent,/No result selected/);
    assert.equal(element('registration-scans').children.length,1);
    assert.match(element('registration-scans').children[0].textContent,/Select PLY/);
    assert.equal(element('map-name').value,'reference_map');
    assert.equal(element('map-pairs').value,'');
    assert.equal(element('open-folder').disabled,true);
    assert.match(element('registration-log').textContent,/No active registration/);
    await run('refresh()');
    assert.equal(element('job-count').textContent,0);
    assert.equal(element('cloud-count').textContent,0);
    assert.equal(run('registration_ids.length'),0);
    context.savedById=Object.fromEntries(clouds.map((cloud,i)=>[cloud.id,[i,2*i,3*i,i*.01]]));
    run("element('map-output').value='/output'; selectPlacementOutput(); registration_ids=project.point_clouds.map(c=>c.id); for(let i=0;i<61;i++)initial_placements.set('id'+i,[i,2*i,3*i,.01*i]); renderRegistrationScans();");
    // The actual Remove button must remove exactly one input, never the other poses.
    await element('registration-scans').children[20].children[1].children[2].onclick();
    let draft=JSON.parse(run('JSON.stringify(registrationDraft())'));
    assert.equal(draft.cloud_ids.length,60);
    assert.equal(draft.initial_guesses.filter(Boolean).length,60);
    assert.deepEqual(draft.initial_guesses[20],[21,42,63,.21]);
    // Remove the anchor, reorder and add another scan: all retained scan poses survive.
    await element('registration-scans').children[0].children[1].children[2].onclick();
    await element('registration-scans').children[1].children[1].children[0].onclick();
    run("addRegistrationScans([{id:'extra',available:true}]);");
    draft=JSON.parse(run('JSON.stringify(registrationDraft())'));
    assert.equal(draft.initial_guesses.filter(Boolean).length,59);
    assert.deepEqual(draft.initial_guesses[draft.cloud_ids.indexOf('id21')],[21,42,63,.21]);
    assert.equal(draft.initial_guesses[draft.cloud_ids.indexOf('extra')],null);
    // Restore exactly 60 and verify the actual start handler sends all 60 placements.
    run("registration_ids=project.point_clouds.filter(c=>c.id!=='id20').map(c=>c.id); element('map-output').value='/output'; renderRegistrationScans();");
    await element('registration-start').handlers.click();
    const start=calls.find(call=>call.route==='registration/start').body;
    assert.equal(start.initial_guesses.filter(Boolean).length,60);
    assert.equal(start.cloud_ids.includes('id20'),false);
    // Apply must verify disk content and never overwrite it.
    const popup={postMessage:value=>messages.push(value)};
    context.popup=popup;
    run("placement_window=popup; placement_context=registrationDraft();");
    const values=JSON.parse(run('JSON.stringify(registrationDraft())'));
    const event={origin:'http://test',source:popup,data:{type:'placement-save',items:values.cloud_ids.map((id,i)=>({id,pose:values.initial_guesses[i]}))}};
    context.failLoad=true;
    await callbacks.message(event);
    assert.equal(messages.at(-1).type,'placement-error');
    assert.equal(messages.some(m=>m.type==='placement-saved'),false);
    context.failLoad=false;
    await callbacks.message(event);
    assert.equal(messages.at(-1).type,'placement-saved');
    assert.equal(messages.at(-1).path,'/output/initial_placements.json');
    assert.equal(calls.some(call=>call.route==='registration/placements/save'),false);
    context.savedById.id21=[200,200,200,0];
    await callbacks.message(event);
    assert.equal(messages.at(-1).type,'placement-error');
    assert.deepEqual(context.savedById.id21,[200,200,200,0]);
    // Selecting PLYs with an output folder restores poses without a separate button.
    context.savedById=Object.fromEntries(clouds.map((cloud,i)=>[cloud.id,[i,2*i,3*i,i*.01]]));
    run("initial_placements.clear(); registration_ids=[]; element('map-output').value='/output';");
    await run('addRegistrationScans(project.point_clouds)');
    assert.equal(run('initial_placements.size'),61);
    // Selecting the output after the scans is handled by the draft/selection update too.
    run("initial_placements.clear(); element('map-output').value='';");
    await run('loadSavedPlacements()');
    assert.equal(run('initial_placements.size'),0);
    run("element('map-output').value='/output'; saveRegistrationDraft();");
    await new Promise(resolve=>setTimeout(resolve,350));
    assert.equal(run('initial_placements.size'),61);
    // A stale response must not overwrite poses after the selection changes.
    context.deferLoad=true;
    const pending=run('loadSavedPlacements()');
    run("registration_ids=registration_ids.filter(id=>id!=='id20'); initial_placements.set('id21',[99,98,97,.5]);");
    context.finishLoad();
    await pending;
    assert.deepEqual(JSON.parse(run("JSON.stringify(initial_placements.get('id21'))")),[99,98,97,.5]);
    assert.equal(run('registrationDraft().initial_guesses.filter(Boolean).length'),60);
    context.deferLoad=false;
    assert.equal(Object.hasOwn(JSON.parse(run('JSON.stringify(registrationDraft())')), 'ignore_saved_placements'),false);
    assert.equal(element('placement-clear').handlers.click,undefined);
    assert.equal(fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').includes('id="placement-clear"'),false);
    // Switching output folders cannot mix two independent arrangements.
    context.savedByOutput={'/empty':{},'/output':context.savedById};
    run("element('map-output').value='/empty';");
    await run('loadSavedPlacements()');
    assert.equal(run('registrationDraft().initial_guesses.filter(Boolean).length'),0);
    run("element('map-output').value='/output';");
    await run('loadSavedPlacements()');
    assert.equal(run('registrationDraft().initial_guesses.filter(Boolean).length'),60);
    // Null in the saved file is authoritative, never an old in-memory pose.
    context.savedById.id21=null;
    await run('loadSavedPlacements()');
    assert.equal(run("initial_placements.has('id21')"),false);
    // A failed read cannot launch registration or present an automatic fallback.
    const starts=()=>calls.filter(call=>call.route==='registration/start').length;
    let previousStarts=starts();
    context.failLoad=true;
    await element('registration-start').handlers.click();
    assert.equal(starts(),previousStarts);
    assert.equal(element('registration-start').disabled,true);
    assert.match(element('placement-status').textContent,/could not be verified/);
    context.failLoad=false;
    await run('loadSavedPlacements()');
    assert.equal(element('registration-start').disabled,false);
    // Start rejects a selection changed while disk verification was in flight.
    context.deferLoad=true;
    const starting=element('registration-start').handlers.click();
    await element('registration-start').handlers.click();
    run('registration_ids.pop();');
    context.finishLoad(); await starting;
    assert.equal(starts(),previousStarts);
    context.deferLoad=false;
    // Reopening Arrange focuses the existing editor without replacing its context.
    let focused=0; popup.focus=()=>focused++; popup.closed=false;
    await element('placement-open').handlers.click();
    assert.equal(focused,1);
    // Open output must apply the current field, even if an older server path exists.
    run("element('output-path').value='/selected/output';");
    await element('open-folder').handlers.click();
    assert.equal(calls.filter(call=>call.route==='output').at(-1).body.path,'/selected/output');
    assert.equal(calls.filter(call=>call.route==='open-folder').at(-1).body.path,'/selected/output');
    assert.equal(calls.some(call=>call.route==='registration/draft'),false);
    run('clearTimeout(draft_timer); clearTimeout(notice_timer);');
    console.log('PASS: 61 -> 60 exact poses; anchor removal/reorder/add; disk read failure prevents acknowledgement; output isolation; stale Apply never writes; guarded start; empty initial tabs; automatic restoration; stale responses cannot overwrite edited selections; no clear/ignore control.');
})().catch(error=>{console.error(error);process.exitCode=1;run('clearTimeout(draft_timer); clearTimeout(notice_timer);');});
