// Regression tests for user-authored placements and explicit workspace selections.
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const path = require('path');
assert.equal(fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8').includes('registration-converted'),false);
assert.equal(fs.readFileSync(path.join(__dirname,'../web/registration.js'),'utf8').includes('registration-converted'),false);
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
    assert.match(element('registration-jobs').children[0].textContent,/current result/);
    run("registration_ids=project.point_clouds.map(c=>c.id); for(let i=0;i<61;i++)initial_placements.set('id'+i,[i,2*i,3*i,.01*i]); renderRegistrationScans();");
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
    // Apply must not acknowledge or discard memory if the disk save fails.
    const popup={postMessage:value=>messages.push(value)};
    context.popup=popup;
    run("placement_window=popup; placement_context=registrationDraft();");
    const values=JSON.parse(run('JSON.stringify(registrationDraft())'));
    const event={origin:'http://test',source:popup,data:{type:'placement-save',items:values.cloud_ids.map((id,i)=>({id,pose:values.initial_guesses[i]}))}};
    context.failSave=true;
    await callbacks.message(event);
    assert.equal(messages.at(-1).type,'placement-error');
    assert.equal(messages.some(m=>m.type==='placement-saved'),false);
    context.failSave=false;
    await callbacks.message(event);
    assert.equal(messages.at(-1).type,'placement-saved');
    assert.equal(messages.at(-1).path,'/output/initial_placements.json');
    run('clearTimeout(draft_timer); clearTimeout(notice_timer);');
    console.log('PASS: 61 -> 60 exact poses; anchor removal/reorder/add; disk failure prevents acknowledgement; empty initial tabs.');
})().catch(error=>{console.error(error);process.exitCode=1;run('clearTimeout(draft_timer); clearTimeout(notice_timer);');});
