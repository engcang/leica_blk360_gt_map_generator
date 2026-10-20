(() =>
{
    'use strict';
    const $ = id => document.getElementById(id), canvas = $('scene'), overlay = $('overlay');
    const token = location.hash.slice(1) || sessionStorage.getItem('blk-session') || '';
    if (token) sessionStorage.setItem('blk-session', token);
    const job = new URLSearchParams(location.search).get('job');
    history.replaceState(null, '', location.pathname + location.search);
    const gl = canvas.getContext('webgl', {antialias: true});
    if (!gl) { $('error').textContent = 'WebGL is unavailable.'; return; }
    let items = [], selected = 1, reference = 0, loading = false, generation = 0, controller = null;
    let drag = null, frame = 0, extent = 10, locked = false;
    let interactionMode = 'view', heightCut = null;
    let saveContext = null, saveTimer = null, saveQueue = Promise.resolve(), dirty = false;
    const cache = new Map(), undo = [];
    const camera = {target: [0, 0, 0], distance: 20, azimuth: -Math.PI / 2, elevation: Math.PI / 2 - .001};
    const tanHalf = Math.tan(25 * Math.PI / 180);
    const identity = () => [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
    const dot = (a,b) => a.reduce((s,v,i) => s + v*b[i],0);
    const add = (a,b,k=1) => a.map((v,i) => v + k*b[i]);
    const multiply = (a,b) =>
    {
        const out = new Float32Array(16);
        for (let c=0;c<4;c++) for(let r=0;r<4;r++) for(let k=0;k<4;k++) out[c*4+r] += a[k*4+r]*b[c*4+k];
        return out;
    };
    const fromRows = rows => rows[0].map((_,c) => rows.map(row => row[c])).flat();
    function poseMatrix(pose)
    {
        const [x,y,z,yaw] = pose, c = Math.cos(yaw), s = Math.sin(yaw);
        return [c,s,0,0,-s,c,0,0,0,0,1,0,x,y,z,1];
    }
    const point = (m,p) => [0,1,2].map(i => m[i]*p[0]+m[4+i]*p[1]+m[8+i]*p[2]+m[12+i]);
    function basis()
    {
        const ca=Math.cos(camera.azimuth),sa=Math.sin(camera.azimuth),ce=Math.cos(camera.elevation),se=Math.sin(camera.elevation);
        return {right:[-sa,ca,0],up:[-se*ca,-se*sa,ce],back:[ce*ca,ce*sa,se]};
    }
    function view()
    {
        const b=basis(), eye=add(camera.target,b.back,camera.distance), aspect=canvas.clientWidth/canvas.clientHeight;
        const {right:r,up:u,back:k}=b, near=.001, far=Math.max(100, camera.distance + extent*6 + Math.hypot(...eye));
        const v=[r[0],u[0],k[0],0,r[1],u[1],k[1],0,r[2],u[2],k[2],0,-dot(r,eye),-dot(u,eye),-dot(k,eye),1];
        const h=camera.distance*tanHalf,w=h*aspect;
        const p=$('projection').value==='orthographic' ? [1/w,0,0,0,0,1/h,0,0,0,0,-2/(far-near),0,0,0,-(far+near)/(far-near),1]
            : [1/(tanHalf*aspect),0,0,0,0,1/tanHalf,0,0,0,0,(far+near)/(near-far),-1,0,0,2*far*near/(near-far),0];
        return {...b,eye,mvp:multiply(p,v)};
    }
    function shader(kind,source)
    {
        const result=gl.createShader(kind);gl.shaderSource(result,source);gl.compileShader(result);
        if(!gl.getShaderParameter(result,gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(result));return result;
    }
    const program=gl.createProgram();
    gl.attachShader(program,shader(gl.VERTEX_SHADER,'attribute vec4 point;uniform mat4 mvp;uniform float size,clipZ,clipEnabled;uniform vec4 heightRow;varying float intensity,visible;void main(){gl_Position=mvp*vec4(point.xyz,1.0);gl_PointSize=size;intensity=point.w;visible=clipEnabled<.5||dot(heightRow,vec4(point.xyz,1.0))<=clipZ?1.:0.;}'));
    gl.attachShader(program,shader(gl.FRAGMENT_SHADER,'precision mediump float;varying float intensity,visible;uniform vec2 range;void main(){if(visible<.5)discard;vec2 q=gl_PointCoord-.5;if(dot(q,q)>.25)discard;float t=clamp((intensity-range.x)/max(1.,range.y-range.x),0.,1.);gl_FragColor=vec4(mix(vec3(.18,.3,.42),vec3(.95,.97,1.),t),1.);}'));
    gl.linkProgram(program);
    if(!gl.getProgramParameter(program,gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(program));
    const uniforms={mvp:gl.getUniformLocation(program,'mvp'),size:gl.getUniformLocation(program,'size'),range:gl.getUniformLocation(program,'range')};
    for(const name of ['clipZ','clipEnabled','heightRow'])uniforms[name]=gl.getUniformLocation(program,name);
    const attr=gl.getAttribLocation(program,'point');
    gl.enable(gl.DEPTH_TEST);gl.clearColor(.055,.09,.13,1);
    function requestDraw(){if(!frame)frame=requestAnimationFrame(draw);}
    function project(p,mvp)
    {
        const q=[0,1,2,3].map(i=>mvp[i]*p[0]+mvp[4+i]*p[1]+mvp[8+i]*p[2]+mvp[12+i]);
        if(q[3]<=0)return null;
        return [(q[0]/q[3]+1)*canvas.clientWidth/2,(1-q[1]/q[3])*canvas.clientHeight/2];
    }
    function visible(){return [...new Set([reference,selected])];}
    function draw()
    {
        frame=0;
        const dpr=Math.min(devicePixelRatio||1,2),w=Math.round(canvas.clientWidth*dpr),h=Math.round(canvas.clientHeight*dpr);
        if(canvas.width!==w||canvas.height!==h){canvas.width=overlay.width=w;canvas.height=overlay.height=h;}
        gl.viewport(0,0,w,h);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(program);gl.enableVertexAttribArray(attr);
        const b=view();let shown=0;
        for(const index of visible())
        {
            const item=items[index], entry=item&&cache.get(item.id);
            if(!entry)continue;
            for(const scan of entry.scans)
            {
                gl.bindBuffer(gl.ARRAY_BUFFER,scan.buffer);gl.vertexAttribPointer(attr,4,gl.FLOAT,false,16,0);
                const model=multiply(poseMatrix(item.pose),scan.base);
                gl.uniformMatrix4fv(uniforms.mvp,false,multiply(b.mvp,model));
                // Clip both clouds in their shared, edited coordinate frame, including chunk-local poses.
                gl.uniform4f(uniforms.heightRow,model[2],model[6],model[10],model[14]);
                gl.uniform1f(uniforms.clipZ,heightCut===null?0:heightCut);
                gl.uniform1f(uniforms.clipEnabled,heightCut===null?0:1);
                gl.uniform1f(uniforms.size,1.5*dpr);gl.uniform2f(uniforms.range,entry.low,entry.high);
                // Use a strided GPU view while dragging so the subset still covers each complete scan.
                const stride=drag?Math.min(15,Math.max(1,Math.ceil(entry.points/2000000))):1;
                gl.vertexAttribPointer(attr,4,gl.FLOAT,false,16*stride,0);
                const count=Math.min(Math.ceil(scan.loaded/stride),drag?Math.ceil(2000000*scan.count/entry.points):Infinity);gl.drawArrays(gl.POINTS,0,count);shown+=count;
            }
        }
        const ctx=overlay.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,canvas.clientWidth,canvas.clientHeight);
        if(items[selected] && $('axes-visible').checked)
        {
            const origin=items[selected].pose.slice(0,3),o=project(origin,b.mvp);
            if(o)
            {
                const length=camera.distance*tanHalf*.18;
                for(const [axis,color] of [[0,'#ff7777'],[1,'#62dc92'],[2,'#6eb8ff']])
                {
                    const p=origin.slice();p[axis]+=length;const end=project(p,b.mvp);if(!end)continue;
                    ctx.strokeStyle=ctx.fillStyle=color;ctx.lineWidth=3;ctx.beginPath();ctx.moveTo(...o);ctx.lineTo(...end);ctx.stroke();ctx.font='bold 14px system-ui';ctx.fillText('XYZ'[axis],end[0]+5,end[1]-4);
                }
                ctx.strokeStyle='#f2ca6b';ctx.lineWidth=2;ctx.beginPath();ctx.arc(o[0],o[1],10,0,Math.PI*2);ctx.stroke();ctx.fillStyle='#f2ca6b';ctx.fillText(items[selected].name,o[0]+14,o[1]+20);
            }
        }
        document.body.dataset.shown=String(shown);document.body.dataset.loaded=String(items.length>0&&!loading&&!$('error').textContent);
    }
    function bounds()
    {
        const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
        for(const index of visible())
        {
            const item=items[index],entry=item&&cache.get(item.id);if(!entry)continue;
            for(const scan of entry.scans)
            {
                const transform=multiply(poseMatrix(item.pose),scan.base);
                for(let mask=0;mask<8;mask++)
                {
                    const p=point(transform,scan.min.map((v,k)=>mask&(1<<k)?scan.max[k]:v));
                    p.forEach((v,k)=>{min[k]=Math.min(min[k],v);max[k]=Math.max(max[k],v);});
                }
            }
        }
        return {min,max};
    }
    function updateHeightControls()
    {
        const {min,max}=bounds();
        const ready=Number.isFinite(min[2])&&Number.isFinite(max[2]);
        $('height-cut').disabled=$('height-value').disabled=$('height-enabled').disabled=!ready;
        if(!ready)return;
        const low=Math.floor(Math.min(min[2],heightCut??min[2])*100)/100;
        const high=Math.ceil(Math.max(max[2],heightCut??max[2])*100)/100;
        $('height-cut').min=low;$('height-cut').max=Math.max(low+.01,high);
        const value=heightCut===null?high:heightCut;
        $('height-cut').value=value;$('height-value').value=value.toFixed(2);
        $('height-enabled').checked=heightCut!==null;
        $('height-status').textContent=heightCut===null?'All heights':`Hide above Z = ${heightCut.toFixed(2)} m (placement frame)`;
        document.body.dataset.heightCut=heightCut===null?'all':String(heightCut);
    }
    function setHeight(value)
    {
        if(value!==null&&(!Number.isFinite(value)||Math.abs(value)>1e6))return;
        heightCut=value;updateHeightControls();requestDraw();
    }
    $('height-cut').oninput=e=>setHeight(Number(e.target.value));
    $('height-value').onchange=e=>{if(e.target.value!=='')setHeight(Number(e.target.value));};
    $('height-enabled').onchange=e=>setHeight(e.target.checked?Number($('height-value').value):null);
    $('height-all').onclick=()=>setHeight(null);
    function setInteractionMode(mode)
    {
        if(drag)
        {
            const id=drag.id;drag=null;
            if(canvas.hasPointerCapture(id))canvas.releasePointerCapture(id);
        }
        interactionMode=mode;
        $('mode-view').setAttribute('aria-pressed',String(mode==='view'));
        $('mode-place').setAttribute('aria-pressed',String(mode==='place'));
        $('tool').disabled=mode!=='place';
        canvas.style.cursor=mode==='view'?'grab':'move';
        document.body.dataset.interactionMode=mode;
        $('details').textContent=(mode==='view'?'Left drag: orbit view.':'Left drag: move/rotate selected scan or chunk.')+' Right/middle drag: pan view. Wheel: navigate. Shift: fine adjustment. Colour: intensity. Height cut affects display only.';
        requestDraw();
    }
    $('mode-view').onclick=()=>setInteractionMode('view');
    $('mode-place').onclick=()=>setInteractionMode('place');
    $('tool').onchange=()=>setInteractionMode('place');
    setInteractionMode('view');
    function fit()
    {
        const {min,max}=bounds();if(!Number.isFinite(min[0]))return;
        camera.target=min.map((v,i)=>(v+max[i])/2);extent=Math.max(1,...max.map((v,i)=>v-min[i]));
        camera.distance=Math.max(1,Math.hypot(...max.map((v,i)=>(v-min[i])/2))/(tanHalf*Math.min(1,canvas.clientWidth/canvas.clientHeight))*1.12);
        requestDraw();
    }
    async function api(path,body)
    {
        const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'X-Session-Token':token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
        const data=await response.json();if(!response.ok)throw Error(data.error||'Request failed');return data;
    }
    function dispose(entry){for(const scan of entry.scans)gl.deleteBuffer(scan.buffer);}
    async function loadVisible(refit=false)
    {
        const version=++generation;controller?.abort();controller=new AbortController();const signal=controller.signal;
        loading=true;document.body.dataset.loaded='false';$('error').textContent='';$('progress').hidden=false;
        const keep=new Set(visible().map(i=>items[i].id)),stride=Number($('density').value);
        for(const [id,entry] of cache)if(!keep.has(id)||entry.stride!==stride||!entry.complete){dispose(entry);cache.delete(id);}
        try
        {
            for(const index of visible())
            {
                const item=items[index];if(cache.has(item.id))continue;
                const entry={scans:[],stride,points:0,low:0,high:65535,complete:false};cache.set(item.id,entry);
                const histogram=new Uint32Array(65536);
                for(const info of item.scans)
                {
                    const count=Math.ceil(info.points/stride);if(!Number.isSafeInteger(count)||count<1)throw Error('Invalid source point count');
                    const scan={base:fromRows(info.base),buffer:gl.createBuffer(),count,loaded:0,min:[Infinity,Infinity,Infinity],max:[-Infinity,-Infinity,-Infinity]};entry.scans.push(scan);
                    gl.bindBuffer(gl.ARRAY_BUFFER,scan.buffer);gl.bufferData(gl.ARRAY_BUFFER,count*16,gl.STATIC_DRAW);
                    if(gl.getError()!==gl.NO_ERROR)throw Error('Not enough graphics memory. Choose a lower display density.');
                    for(let start=0;start<count;start+=262144)
                    {
                        const response=await fetch(`${info.url}?stride=${stride}&start=${start}`,{headers:{'X-Session-Token':token},signal});
                        if(!response.ok){const error=await response.json();throw Error(error.error||'Cannot read PLY sample');}
                        const bytes=await response.arrayBuffer();if(version!==generation)return;
                        const block=new Float32Array(bytes);if(block.length!==Math.min(262144,count-start)*4)throw Error('Incomplete sampled PLY response');
                        gl.bindBuffer(gl.ARRAY_BUFFER,scan.buffer);gl.bufferSubData(gl.ARRAY_BUFFER,start*16,block);
                        for(let i=0;i<block.length;i+=4)
                        {
                            for(let axis=0;axis<3;axis++){scan.min[axis]=Math.min(scan.min[axis],block[i+axis]);scan.max[axis]=Math.max(scan.max[axis],block[i+axis]);}
                            histogram[Math.max(0,Math.min(65535,block[i+3]|0))]++;
                        }
                        scan.loaded=start+block.length/4;entry.points+=block.length/4;
                        $('load').textContent=`Loading ${item.name} / ${info.name}: ${scan.loaded.toLocaleString()} / ${count.toLocaleString()} sampled points`;
                        $('progress').value=scan.loaded/count;requestDraw();
                    }
                }
                let total=0;for(let i=0;i<histogram.length;i++){total+=histogram[i];if(total<entry.points*.01)entry.low=i;if(total>=entry.points*.99){entry.high=i;break;}}
                entry.complete=true;
            }
            $('load').textContent=`${visible().reduce((n,i)=>n+(cache.get(items[i].id)?.points||0),0).toLocaleString()} points loaded at ${stride===1?'full density':'1/'+stride}. Display sampling does not change registration inputs.`;
            updateHeightControls();
            if(refit)fit();
        }
        catch(error){if(error.name!=='AbortError'&&version===generation)$('error').textContent=error.message;}
        finally{if(version===generation){loading=false;$('progress').hidden=true;requestDraw();}}
    }
    function sync()
    {
        if(!items.length)return;
        const item=items[selected];$('moving').value=selected;$('reference').value=reference;
        for(const option of $('reference').options)option.disabled=Number(option.value)===selected;
        ['x','y','z','yaw'].forEach((id,i)=>{$(id).value=(item.pose[i]*(i===3?180/Math.PI:1)).toFixed(i===3?2:3);$(id).disabled=selected===0||locked;});
        $('use').checked=item.enabled;$('use').disabled=selected===0||locked;
        $('previous').disabled=selected===0;$('next').disabled=selected===items.length-1;
        $('zero').disabled=selected===0||locked;$('undo').disabled=!undo.length||locked;
        $('pose-note').textContent=selected===0?'First item is the fixed coordinate anchor.':`${item.name}: drag to move, or enter numerical offsets. Yellow axes mark its scanner/chunk origin.`;
        updateHeightControls();
        [...$('items').children].forEach((button,i)=>{button.classList.toggle('selected',i===selected);button.textContent=(items[i].enabled?'[Placed] ':'')+items[i].name;});
        requestDraw();
    }
    function remember(){undo.push({index:selected,pose:items[selected].pose.slice(),enabled:items[selected].enabled});if(undo.length>100)undo.shift();}
    function changed(){items[selected].enabled=true;backupChanged();sync();}
    function choose(index,advance=false,inherit=true)
    {
        if(advance)reference=selected;
        selected=index;
        if(reference===selected)reference=selected?selected-1:Math.min(1,items.length-1);
        if(inherit&&!items[selected].enabled&&selected!==0)items[selected].pose=items[reference].pose.slice();
        sync();loadVisible(true);
    }
    function initialize(input)
    {
        if(items.length||!Array.isArray(input)||input.length<2)return;
        items=input.map(item=>({...item,enabled:item.pose!==null,pose:item.pose?item.pose.slice():[0,0,0,0]}));
        if (!items[0].enabled) { items[0].pose=[0,0,0,0];items[0].enabled=true; }
        for(const [i,item] of items.entries())
        {
            for(const id of ['moving','reference']){const option=document.createElement('option');option.value=i;option.textContent=item.name;$(id).append(option);}
            const button=document.createElement('button');button.textContent=item.name;button.onclick=()=>choose(i);$('items').append(button);
        }
        sync();loadVisible(true);
    }
    $('moving').onchange=e=>choose(Number(e.target.value));$('reference').onchange=e=>{reference=Number(e.target.value);sync();loadVisible(true);};
    $('previous').onclick=()=>choose(Math.max(0,selected-1));$('next').onclick=()=>choose(Math.min(items.length-1,selected+1),true);
    $('use').onchange=()=>{remember();items[selected].enabled=$('use').checked;backupChanged();sync();};
    for(const [i,id] of ['x','y','z','yaw'].entries())$(id).onchange=()=>
    {
        const value=Number($(id).value)*(i===3?Math.PI/180:1);
        if(!Number.isFinite(value)||Math.abs(value)>1e6){$('error').textContent='Enter a finite placement value within supported bounds.';sync();return;}
        remember();items[selected].pose[i]=value;changed();
    };
    $('undo').onclick=()=>{const step=undo.pop();if(!step)return;items[step.index].pose=step.pose;items[step.index].enabled=step.enabled;choose(step.index,false,false);backupChanged();};
    $('zero').onclick=()=>{remember();items[selected].pose=[0,0,0,0];items[selected].enabled=false;backupChanged();sync();};
    $('top').onclick=()=>{camera.azimuth=-Math.PI/2;camera.elevation=Math.PI/2-.001;requestDraw();};
    $('oblique').onclick=()=>{camera.azimuth=-Math.PI/4;camera.elevation=Math.PI/5;requestDraw();};
    $('axes-visible').onchange=requestDraw;
    $('fit').onclick=fit;$('projection').onchange=requestDraw;$('density').onchange=()=>loadVisible(false);
    const units=()=>2*camera.distance*tanHalf/canvas.clientHeight;
    function xyAt(x,y,z)
    {
        const b=view(),scale=units(),sx=(x-canvas.clientWidth/2)*scale,sy=(canvas.clientHeight/2-y)*scale;
        const offset=add(b.right.map(v=>v*sx),b.up,sy);
        const ortho=$('projection').value==='orthographic';
        const origin=ortho?add(b.eye,offset):b.eye, direction=ortho?b.back.map(v=>-v):add(b.back.map(v=>-v),offset,1/camera.distance);
        if(Math.abs(direction[2])<.03)return null;
        const t=(z-origin[2])/direction[2];return add(origin,direction,t);
    }
    canvas.oncontextmenu=e=>e.preventDefault();
    canvas.onpointerdown=e=>
    {
        if(e.button>2||!items.length)return;e.preventDefault();
        const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top;
        const mode=e.button===1||e.button===2?'pan':interactionMode==='view'?'orbit':$('tool').value;
        if(locked&&mode!=='pan'&&mode!=='orbit')return;
        if(mode!=='pan'&&mode!=='orbit'&&selected===0){$('save-status').textContent='Select another item; the first item is the fixed anchor.';return;}
        if(mode!=='pan'&&mode!=='orbit')remember();
        drag={id:e.pointerId,x,y,mode};canvas.setPointerCapture(e.pointerId);canvas.focus();requestDraw();
    };
    canvas.onpointermove=e=>
    {
        if(!drag||drag.id!==e.pointerId)return;
        const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,y=e.clientY-rect.top,dx=x-drag.x,dy=y-drag.y,k=e.shiftKey ? .1 : 1,b=basis();
        if(drag.mode==='pan')camera.target=camera.target.map((v,i)=>v+(-dx*b.right[i]+dy*b.up[i])*units());
        else if(drag.mode==='orbit'){camera.azimuth-=dx*Math.PI/canvas.clientHeight*.5;camera.elevation=Math.max(-Math.PI/2+.001,Math.min(Math.PI/2-.001,camera.elevation+dy*Math.PI/canvas.clientHeight*.5));}
        else
        {
            const pose=items[selected].pose;
            if(drag.mode==='yaw')pose[3]+=dx*.005*k;
            else if(drag.mode==='z')pose[2]-=dy*units()*k;
            else
            {
                const first=xyAt(drag.x,drag.y,pose[2]),second=xyAt(x,y,pose[2]);
                if(first&&second){if(drag.mode!=='y')pose[0]+=(second[0]-first[0])*k;if(drag.mode!=='x')pose[1]+=(second[1]-first[1])*k;}
                else $('save-status').textContent='Use Top or an oblique view to move along the XY plane.';
            }
            changed();
        }
        drag.x=x;drag.y=y;requestDraw();
    };
    const release=e=>{if(drag?.id===e.pointerId){drag=null;requestDraw();}};
    canvas.onpointerup=canvas.onpointercancel=canvas.onlostpointercapture=release;
    canvas.addEventListener('wheel',e=>
    {
        e.preventDefault();const delta=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?canvas.clientHeight:1)*(e.shiftKey ? .1 : 1);
        if($('projection').value==='orthographic')camera.distance=Math.max(.01,camera.distance*Math.exp(Math.max(-1000,Math.min(1000,delta))*.0005));
        else camera.target=add(camera.target,basis().back,.5*Math.max(.0001,extent*.001)*Math.max(-1000,Math.min(1000,delta)));
        requestDraw();
    },{passive:false});
    function backupChanged()
    {
        dirty = true;
        window.opener?.postMessage({type:'placement-saving', job}, location.origin);
        $('save-status').textContent = 'Saving placement changes...';
        clearTimeout(saveTimer);
        if (saveContext) saveTimer = setTimeout(() => saveBackup().catch(error => $('error').textContent = error.message), 300);
    }
    function saveBackup()
    {
        clearTimeout(saveTimer);
        const values = items.map(item => item.enabled ? item.pose.slice() : null);
        saveQueue = saveQueue.catch(() => {}).then(async () =>
        {
            if (!saveContext) throw Error('Placement save context is unavailable. Keep this editor open.');
            if (job)
            {
                const current = await api('registration/placement/' + encodeURIComponent(job));
                if (current.busy || JSON.stringify(current.items.map(item => item.id)) !== JSON.stringify(saveContext.chunk_keys))
                {
                    locked = true; sync();
                    throw Error('Registration is running or chunks changed. Keep this tab open to preserve unsaved edits; reopen Arrange chunks after registration.');
                }
            }
            const result = await api('registration/placements/save', {...saveContext, initial_guesses:values});
            if (JSON.stringify(values) === JSON.stringify(items.map(item => item.enabled ? item.pose : null)))
            {
                dirty = false;
                $('save-status').textContent = `Saved on disk: ${result.path}`;
                window.opener?.postMessage({type:'placement-autosaved', job}, location.origin);
            }
            return result;
        });
        return saveQueue;
    }
    window.addEventListener('beforeunload', event =>
    {
        if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('message',async e=>
    {
        if(e.origin!==location.origin||e.source!==window.opener)return;
        if (job && ['placement-lock','placement-started','placement-unlock'].includes(e.data?.type))
        {
            locked = e.data.type !== 'placement-unlock'; drag = null; sync();
            if (e.data.type === 'placement-lock')
            {
                try
                {
                    if (dirty) await saveBackup();
                    else await saveQueue;
                    window.opener?.postMessage({type:'placement-locked', job}, location.origin);
                }
                catch (error) { window.opener?.postMessage({type:'placement-locked', job, error:error.message}, location.origin); }
            }
            if (e.data.type === 'placement-started') $('save-status').textContent = 'Registration started. Follow the worker log in the Register tab. Reopen Arrange chunks for the new result.';
        }
        if(e.data?.type==='placement-input'){saveContext=e.data.context||null;initialize(e.data.items);}
        if(e.data?.type==='placement-error')$('error').textContent=e.data.error;
    });
    canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();controller?.abort();$('error').textContent='Graphics context lost. Reload the placement editor.';});
    new ResizeObserver(requestDraw).observe(canvas);
    window.blkPlacement={items:()=>items,poseMatrix,multiply,fromRows,choose,fit,camera};
    if(job)api('registration/placement/'+encodeURIComponent(job)).then(async result=>
    {
        if(result.busy)throw Error('Wait for the current registration to finish before arranging chunks.');
        saveContext={resume_id:job, chunk_keys:result.items.map(item=>item.id)};
        const saved=await api('registration/placements/load',saveContext);
        if (saved.initial_guesses.length !== result.items.length) throw Error('Chunks changed. Reopen Arrange chunks.');
        saved.initial_guesses.forEach((pose,i)=>{result.items[i].pose=pose || null;});
        initialize(result.items);
    }).catch(error=>$('error').textContent=error.message);
    else if(window.opener)window.opener.postMessage({type:'placement-ready'},location.origin);
    else $('error').textContent='Open this editor from the Register tab.';
})();
