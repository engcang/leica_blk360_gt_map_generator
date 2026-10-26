'use strict';
(async () => {
const $=id=>document.getElementById(id), canvas=$('canvas');
const cfg=await window.blkTrajectoryScene();
const preferences=await window.ViewerPreferences.open(cfg.settings_scope);
const metricsPanel=$('metrics-panel');
if(metricsPanel){metricsPanel.open=preferences.common().metricsOpen!==false;metricsPanel.ontoggle=()=>preferences.set({metricsOpen:metricsPanel.open});}
let colorRangeAuto=preferences.common().colorRangeAuto!==false;
const savedCamera=preferences.common().camera;
const navigationVersion='surface-orbit-v1',verticalFov=50*Math.PI/180,tanHalfFov=Math.tan(verticalFov/2);
let savedOrthoScale=null;
function saveCamera(){preferences.set({camera:{navigation:navigationVersion,yaw,pitch,orthographic,orthoScaleRatio:orthographicScale/radius}});}
const identity=new Float32Array([1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]);
const modelFor=key=>new Float32Array(cfg.transforms[key]||identity);
const style=key=>cfg.styles[key];
const error=e=>{console.error(e);$('error').hidden=false;$('error').textContent=String(e.message||e);};
const gl=canvas.getContext('webgl2',{antialias:true,alpha:false,preserveDrawingBuffer:true});
if(!gl){error('WebGL 2 is required. Open this file in a browser with hardware acceleration enabled.');return;}
let lost=false;canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();lost=true;error('Graphics context was lost. Close other graphics tabs and reload.');});
function program(vs,fs){const p=gl.createProgram();for(const [kind,src] of [[gl.VERTEX_SHADER,vs],[gl.FRAGMENT_SHADER,fs]]){const s=gl.createShader(kind);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));gl.attachShader(p,s);gl.deleteShader(s);}gl.linkProgram(p);if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(p));return p;}
const pointProgram=program(`#version 300 es
precision highp float;
layout(location=0) in vec3 p;
uniform mat4 mvp;uniform mat4 model;uniform float pointSize;uniform int axis;
out float value;
void main(){vec3 w=(model*vec4(p,1.0)).xyz;gl_Position=mvp*vec4(w,1.0);gl_PointSize=pointSize;value=axis==0?w.x:(axis==1?w.y:w.z);}`,`#version 300 es
precision highp float;
in float value;uniform sampler2D lut;uniform vec2 range;uniform vec3 solid;uniform float alpha;uniform bool single;uniform bool circles;out vec4 frag;
void main(){if(circles && length(gl_PointCoord-0.5)>0.5)discard;float t=clamp((value-range.x)/max(range.y-range.x,0.000001),0.0,1.0);frag=vec4(single?solid:texture(lut,vec2(t,0.5)).rgb,alpha);}`);
// Flat Squares: one shared quad per point, facing the camera, with a physical side length.
// Instancing keeps the original XYZ buffer; there are no per-point CPU geometry copies.
const squareProgram=program(`#version 300 es
precision highp float;
layout(location=0) in vec3 p;layout(location=1) in vec2 corner;
uniform mat4 mvp;uniform mat4 model;uniform vec3 cameraRight;uniform vec3 cameraUp;uniform vec3 cameraEye;uniform bool isOrthographic;
uniform float sizeMeters;uniform int axis;
out float value;
void main(){
    vec3 w=(model*vec4(p,1.0)).xyz;
    vec3 right=cameraRight,up=cameraUp;
    if(!isOrthographic){
        vec3 towardEye=cameraEye-w;
        vec3 normal=towardEye/max(length(towardEye),0.000001);
        vec3 tangent=cross(cameraUp,normal);
        if(length(tangent)>0.000001){right=normalize(tangent);up=cross(normal,right);}
    }
    vec3 billboard=w+sizeMeters*(corner.x*right+corner.y*up);
    gl_Position=mvp*vec4(billboard,1.0);
    value=axis==0?w.x:(axis==1?w.y:w.z);
}`,`#version 300 es
precision highp float;
in float value;uniform sampler2D lut;uniform vec2 range;uniform vec3 solid;uniform float alpha;uniform bool single;out vec4 frag;
void main(){float t=clamp((value-range.x)/max(range.y-range.x,0.000001),0.0,1.0);frag=vec4(single?solid:texture(lut,vec2(t,0.5)).rgb,alpha);}`);
const squareVAO=gl.createVertexArray(),squareCorners=gl.createBuffer();
gl.bindVertexArray(squareVAO);gl.bindBuffer(gl.ARRAY_BUFFER,squareCorners);
gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-.5,-.5,.5,-.5,.5,.5,-.5,-.5,.5,.5,-.5,.5]),gl.STATIC_DRAW);
gl.enableVertexAttribArray(1);gl.vertexAttribPointer(1,2,gl.FLOAT,false,8,0);gl.vertexAttribDivisor(1,0);
gl.bindVertexArray(null);
const lineProgram=program(`#version 300 es
precision highp float;
layout(location=0) in vec3 a;layout(location=1) in vec3 b;layout(location=2) in vec2 corner;
uniform mat4 mvp;uniform vec2 viewport;uniform float width;
void main(){vec4 p=mvp*vec4(a,1),q=mvp*vec4(b,1);vec2 d=q.xy/max(q.w,0.00001)-p.xy/max(p.w,0.00001);float len=length(d);vec2 normal=len>0.0000001?vec2(-d.y,d.x)/len:vec2(0,1);vec4 c=mix(p,q,corner.x);c.xy+=normal*corner.y*width/viewport*c.w;gl_Position=c;}`,`#version 300 es
precision highp float;uniform vec3 color;out vec4 frag;void main(){frag=vec4(color,1);}`);
const loc=(p,n)=>gl.getUniformLocation(p,n),pu={},lu={},su={};
for(const n of ['mvp','model','pointSize','axis','lut','range','solid','alpha','single','circles'])pu[n]=loc(pointProgram,n);
for(const n of ['mvp','viewport','width','color'])lu[n]=loc(lineProgram,n);
for(const n of ['mvp','model','cameraRight','cameraUp','cameraEye','isOrthographic','sizeMeters','axis','lut','range','solid','alpha','single'])su[n]=loc(squareProgram,n);
const rgb=h=>h.match(/[a-f\d]{2}/gi).map(x=>parseInt(x,16)/255);
const palettes={turbo:['#30123b','#4145ab','#4675ed','#39a2fc','#1bcfd4','#24eca6','#61fc6c','#a4fc3c','#d1e834','#f3c63a','#fe9b2d','#f36315','#d93806','#ae1801','#7a0403'],viridis:['#440154','#482878','#3e4989','#31688e','#26828e','#1f9e89','#35b779','#6ece58','#b5de2b','#fde725'],plasma:['#0d0887','#46039f','#7201a8','#9c179e','#bd3786','#d8576b','#ed7953','#fb9f3a','#fdca26','#f0f921'],rainbow:['#6500ff','#006dff','#00ddff','#00e75b','#a5ef00','#ffe600','#ff7300','#ee0000'],gray:['#20252b','#ffffff']};
const texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
function palette(){const colors=palettes[$('palette').value],c=colors.map(rgb),a=new Uint8Array(256*4);for(let i=0;i<256;i++){const t=i/255*(c.length-1),j=Math.min(c.length-2,Math.floor(t)),f=t-j;for(let k=0;k<3;k++)a[i*4+k]=Math.round(255*(c[j][k]*(1-f)+c[j+1][k]*f));a[i*4+3]=255;}gl.bindTexture(gl.TEXTURE_2D,texture);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,256,1,0,gl.RGBA,gl.UNSIGNED_BYTE,a);$('colorbar').style.background='linear-gradient(to right,'+colors.join(',')+')';}
const add=(a,b)=>a.map((v,i)=>v+b[i]),sub=(a,b)=>a.map((v,i)=>v-b[i]),scale=(a,t)=>a.map(v=>v*t),dot=(a,b)=>a.reduce((s,v,i)=>s+v*b[i],0),cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]],norm=a=>Math.hypot(...a),unit=a=>scale(a,1/Math.max(norm(a),1e-12));
function multiply(a,b){const o=new Float32Array(16);for(let c=0;c<4;c++)for(let r=0;r<4;r++)for(let k=0;k<4;k++)o[c*4+r]+=a[k*4+r]*b[c*4+k];return o;}
let center=[0,0,0],sceneCenter=[0,0,0],distance=100,orthographicScale=100,radius=50;
let yaw=-Math.PI/4,pitch=Math.PI/5,orthographic=false,orbitAtEye=false;
// This small CPU sample is only for choosing the pivot. Rendering retains ALL points.
const maxFocusPoints=200000;
let focusPoints=new Float32Array(),focusCount=0,focusStride=1;
const mapBuffers=[];
let mapKey='',loaded=0,mapCount=0,token=0,loading=false,queued=false,displayed=0;
const dpr=()=>Math.min(window.devicePixelRatio||1,2), selected=()=>cfg.maps[mapKey]||cfg.maps[cfg.default_map];
function basis(){const offset=[Math.cos(pitch)*Math.cos(yaw),Math.cos(pitch)*Math.sin(yaw),Math.sin(pitch)],eye=add(center,scale(offset,distance)),f=unit(sub(center,eye)),right=unit(cross(f,[0,0,1])),up=cross(right,f);return {eye,f,right,up};}
// Use the selected saved cloud's longest bounding-box side in meters, not the
// trajectory radius or camera distance. It is identical in both viewer modes.
function cloudExtent(){const b=selected().bounds;return Math.max(.001,...sub(b[1],b[0]));}
function nearClip(){return Math.max(.0001,Math.min(cloudExtent()*.0001,distance*.01));}
function matrix(){const {eye,f,right:r,up:u}=basis(),v=new Float32Array([r[0],u[0],-f[0],0,r[1],u[1],-f[1],0,r[2],u[2],-f[2],0,-dot(r,eye),-dot(u,eye),dot(f,eye),1]);const aspect=Math.max(1,canvas.clientWidth)/Math.max(1,canvas.clientHeight),near=nearClip(),far=Math.max(distance,norm(sub(eye,sceneCenter)))+cloudExtent()*4+10;let p;if(orthographic){const h=orthographicScale*tanHalfFov,w=h*aspect;p=new Float32Array([1/w,0,0,0,0,1/h,0,0,0,0,-2/(far-near),0,0,0,-(far+near)/(far-near),1]);}else{const fov=1/tanHalfFov;p=new Float32Array([fov/aspect,0,0,0,0,fov,0,0,0,0,(far+near)/(near-far),-1,0,0,2*far*near/(near-far),0]);}return multiply(p,v);}
function fittedDistance(){const aspect=Math.max(1,canvas.clientWidth)/Math.max(1,canvas.clientHeight);return radius/Math.sin(Math.atan(tanHalfFov*Math.min(1,aspect)))*1.08;}
function fit(bounds,resetScale=false){if(resetScale)savedOrthoScale=null;center=scale(add(bounds[0],bounds[1]),.5);sceneCenter=center.slice();radius=Math.max(.001,norm(sub(bounds[1],bounds[0]))*.5);distance=fittedDistance();orthographicScale=orthographic&&savedOrthoScale?radius*savedOrthoScale:distance;orbitAtEye=false;buildGrid();drawSoon();}
function sampleFocusPoints(xyz,start){
    for(let point=Math.ceil(start/focusStride)*focusStride;point<start+xyz.length/3;point+=focusStride){
        const src=(point-start)*3,dst=focusCount*3;
        focusPoints[dst]=xyz[src];focusPoints[dst+1]=xyz[src+1];focusPoints[dst+2]=xyz[src+2];
        focusCount++;
    }
}
function focusView(){
    const b=basis(),height=Math.max(1,canvas.clientHeight);
    const searchRadius=Math.max(16,Math.min(canvas.clientWidth,height)*.04),near=nearClip();
    const m=modelFor(mapKey),limit=searchRadius*searchRadius;
    let best=limit,depth=null;
    // Hidden/transparent maps cannot supply a surface. Ignore points behind the eye.
    if(mapKey&&Number($('alpha').value)>0){
        for(let i=0;i<focusCount*3;i+=3){
            const px=focusPoints[i],py=focusPoints[i+1],pz=focusPoints[i+2];
            const x=m[0]*px+m[4]*py+m[8]*pz+m[12]-b.eye[0];
            const y=m[1]*px+m[5]*py+m[9]*pz+m[13]-b.eye[1];
            const z=m[2]*px+m[6]*py+m[10]*pz+m[14]-b.eye[2];
            const forward=x*b.f[0]+y*b.f[1]+z*b.f[2];
            if(forward<=near)continue;
            const pixels=height/(2*(orthographic?orthographicScale:forward)*tanHalfFov);
            const sx=(x*b.right[0]+y*b.right[1]+z*b.right[2])*pixels;
            const sy=(x*b.up[0]+y*b.up[1]+z*b.up[2])*pixels;
            const screenDistance=sx*sx+sy*sy;
            if(screenDistance>limit)continue;
            if(depth===null||screenDistance<best-1||(Math.abs(screenDistance-best)<=1&&forward<depth)){
                best=screenDistance;depth=forward;
            }
        }
    }
    orbitAtEye=depth===null;
    // Empty space changes orbit behavior, not the scale of a pan. Resetting
    // this to <=1m made right/middle drags nearly stop until a surface was hit.
    if(depth!==null)distance=depth;
    // Use only the depth on the center ray; choosing the point itself shifts the image.
    center=add(b.eye,scale(b.f,distance));
}
function orbit(dx,dy){
    const eye=orbitAtEye?basis().eye:null,height=Math.max(1,canvas.clientHeight);
    yaw-=dx*Math.PI/height;
    pitch=Math.max(-Math.PI/2+.001,Math.min(Math.PI/2-.001,pitch+dy*Math.PI/height));
    if(eye)center=add(eye,scale(basis().f,distance));
}
function panBy(dx,dy){
    const b=basis(),units=2*(orthographic?orthographicScale:distance)*tanHalfFov/Math.max(1,canvas.clientHeight);
    center=add(center,add(scale(b.right,-dx*units),scale(b.up,dy*units)));
}
const paths={};
for(const [key,rows] of Object.entries(cfg.trajectories)){const a=[],breaks=new Set(cfg.trajectory_break_before?.[key]||[]);for(let i=1;i<rows.length;i++){if(breaks.has(i)||rows[i][0]-rows[i-1][0]>(cfg.trajectory_gap_s?.[key]??.25))continue;for(const [t,side] of [[0,-1],[1,-1],[1,1],[0,-1],[1,1],[0,1]])a.push(...rows[i-1].slice(1),...rows[i].slice(1),t,side);}const b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(a),gl.STATIC_DRAW);paths[key]={buffer:b,count:a.length/8};}
// Endpoints use the same saved poses and alignment transform as their trajectory.
const endpoints={};
for(const [key,rows] of Object.entries(cfg.trajectories)){
    if(!rows.length)continue;
    const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
    gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([...rows[0].slice(1),...rows[rows.length-1].slice(1)]),gl.STATIC_DRAW);
    endpoints[key]=buffer;
}
const endpointLegend=document.createElement('div');
endpointLegend.textContent='○ Start · □ End (trajectory color)';
$('legend').appendChild(endpointLegend);
function drawEndpoints(m,ratio){
    for(const [key,buffer] of Object.entries(endpoints)){
        if(!$(style(key).control).checked)continue;
        pointsUniforms(m,modelFor(key));
        gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.vertexAttribPointer(0,3,gl.FLOAT,false,12,0);
        gl.uniform1i(pu.single,1);gl.uniform1f(pu.alpha,1);
        // Draw the larger start circle around the end square when poses coincide.
        for(const [index,size] of [[0,19],[1,11]]){
            gl.uniform1i(pu.circles,index===0?1:0);
            for(const [diameter,color] of [[size+4,[.05,.07,.1]],[size+2,[1,1,1]],[size,rgb(style(key).color)]]){
                gl.uniform1f(pu.pointSize,diameter*ratio);gl.uniform3fv(pu.solid,color);
                gl.drawArrays(gl.POINTS,index,1);
            }
        }
    }
}
let gridBuffer=gl.createBuffer(),gridCount=0,gridSpacing=10;
function buildGrid(){const extent=Math.max(radius*1.2,2),rough=extent/10,power=10**Math.floor(Math.log10(rough));gridSpacing=[1,2,5,10].map(x=>x*power).find(x=>x>=rough);const a=[],x0=Math.floor((center[0]-extent)/gridSpacing)*gridSpacing,x1=Math.ceil((center[0]+extent)/gridSpacing)*gridSpacing,y0=Math.floor((center[1]-extent)/gridSpacing)*gridSpacing,y1=Math.ceil((center[1]+extent)/gridSpacing)*gridSpacing;for(let x=x0;x<=x1+gridSpacing*.01;x+=gridSpacing)a.push(x,y0,0,x,y1,0);for(let y=y0;y<=y1+gridSpacing*.01;y+=gridSpacing)a.push(x0,y,0,x1,y,0);gl.bindBuffer(gl.ARRAY_BUFFER,gridBuffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(a),gl.STATIC_DRAW);gridCount=a.length/3;$('grid-scale').textContent=`Grid ${gridSpacing.toLocaleString()} m · Z up`;}
const axes=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,axes);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([0,0,0,5,0,0,0,0,0,0,5,0,0,0,0,0,0,5]),gl.STATIC_DRAW);
function pointsUniforms(m,model=identity){gl.useProgram(pointProgram);gl.uniformMatrix4fv(pu.model,false,model);gl.uniformMatrix4fv(pu.mvp,false,m);gl.uniform1i(pu.lut,0);gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,texture);gl.enableVertexAttribArray(0);gl.disableVertexAttribArray(1);gl.disableVertexAttribArray(2);}
function lines(buffer,count,color,m,first=0){pointsUniforms(m);gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.vertexAttribPointer(0,3,gl.FLOAT,false,12,0);gl.uniform1i(pu.single,1);gl.uniform1i(pu.circles,0);gl.uniform3fv(pu.solid,color);gl.uniform1f(pu.alpha,1);gl.drawArrays(gl.LINES,first,count);}
function pointStyle(){return $('point-style')?.value||'round-points';}
function worldPointSize(){const v=Number($('size-m')?.value);return Number.isFinite(v)&&v>0?Math.min(5,v):.1;}
function updatePointStyle(){
    const flat=pointStyle()==='flat-squares';
    if($('size-m-control'))$('size-m-control').hidden=!flat;
    if($('size-px-control'))$('size-px-control').hidden=flat;
    if($('point-style-help'))$('point-style-help').textContent=flat?
        'Camera-facing flat squares. Size is the side length in meters; zooming changes their screen size. Alpha 1 shows opaque surfaces.':
        'Round points with a fixed screen size in pixels.';
    drawSoon();
}
function updateStatus(){let msg=mapKey?`${loaded.toLocaleString()} points · ${style(mapKey).mapLabel || style(mapKey).label}`:'Map hidden';if(loading)msg=`Loading ${loaded.toLocaleString()} / ${mapCount.toLocaleString()} points…`;$('status').textContent=msg;}
function render(){queued=false;if(lost)return;const ratio=dpr(),w=Math.max(1,Math.round(canvas.clientWidth*ratio)),h=Math.max(1,Math.round(canvas.clientHeight*ratio));if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}gl.viewport(0,0,w,h);const bg=rgb($('background').value);gl.clearColor(...bg,1);gl.depthMask(true);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.enable(gl.DEPTH_TEST);gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);const m=matrix();
if($('grid').checked){lines(gridBuffer,gridCount,[.19,.26,.33],m);lines(axes,2,[1,.25,.25],m,0);lines(axes,2,[.35,1,.35],m,2);lines(axes,2,[.3,.6,1],m,4);}
displayed=0;if(mapKey&&mapBuffers.length&&loaded){
    const alpha=Number($('alpha').value),flat=pointStyle()==='flat-squares',u=flat?su:pu;
    displayed=loaded;
    if(flat){
        gl.useProgram(squareProgram);gl.bindVertexArray(squareVAO);
        gl.uniformMatrix4fv(su.mvp,false,m);gl.uniformMatrix4fv(su.model,false,modelFor(mapKey));
        const camera=basis();gl.uniform3fv(su.cameraRight,camera.right);gl.uniform3fv(su.cameraUp,camera.up);
        gl.uniform3fv(su.cameraEye,camera.eye);gl.uniform1i(su.isOrthographic,orthographic);
        gl.uniform1f(su.sizeMeters,worldPointSize());gl.uniform1i(su.lut,0);
        gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,texture);
        gl.enableVertexAttribArray(0);gl.vertexAttribDivisor(0,1);
    }else{
        pointsUniforms(m,modelFor(mapKey));gl.uniform1f(pu.pointSize,Number($('size').value)*ratio);
        gl.uniform1i(pu.circles,1);
    }
    gl.uniform1f(u.alpha,alpha);gl.uniform1i(u.axis,{x:0,y:1,z:2}[$('color-by').value]??2);
    gl.uniform1i(u.single,$('color-by').value==='solid');gl.uniform3fv(u.solid,rgb($('solid').value));
    gl.uniform2f(u.range,Number($('color-min').value),Number($('color-max').value));
    gl.depthMask(alpha===1);
    for(const chunk of mapBuffers){
        gl.bindBuffer(gl.ARRAY_BUFFER,chunk.buffer);gl.vertexAttribPointer(0,3,gl.FLOAT,false,12,0);
        if(flat)gl.drawArraysInstanced(gl.TRIANGLES,0,6,chunk.count);
        else gl.drawArrays(gl.POINTS,0,chunk.count);
    }
    if(flat)gl.bindVertexArray(null);
    gl.depthMask(true);
}
gl.disable(gl.DEPTH_TEST);gl.disable(gl.BLEND);gl.useProgram(lineProgram);gl.uniformMatrix4fv(lu.mvp,false,m);gl.uniform2f(lu.viewport,w,h);gl.uniform1f(lu.width,2.3*ratio);for(const [key,p] of Object.entries(paths)){if(!$(style(key).control).checked)continue;gl.uniformMatrix4fv(lu.mvp,false,multiply(m,modelFor(key)));gl.uniform1f(lu.width,style(key).width*dpr());gl.bindBuffer(gl.ARRAY_BUFFER,p.buffer);for(let k=0;k<3;k++){gl.enableVertexAttribArray(k);gl.vertexAttribPointer(k,k===2?2:3,gl.FLOAT,false,32,k*12);}gl.uniform3fv(lu.color,rgb(style(key).color));gl.drawArrays(gl.TRIANGLES,0,p.count);}drawEndpoints(m,ratio);updateStatus();}
function drawSoon(){if(!queued){queued=true;requestAnimationFrame(render);}}
function autoRange(){if(!colorRangeAuto){drawSoon();return;}const axis={x:0,y:1,z:2}[$('color-by').value]??2,b=worldBounds(selected().color_bounds,mapKey||cfg.default_map);$('color-min').value=b[0][axis].toFixed(2);$('color-max').value=Math.max(b[1][axis],b[0][axis]+.01).toFixed(2);drawSoon();}
async function readRange(url,start,end,signal){
    const response=await fetch(url,{headers:{'X-Session-Token':window.blkTrajectoryToken,Range:`bytes=${start}-${end-1}`},signal});
    if(response.status!==206)throw Error(`Map read failed (HTTP ${response.status}); reopen from BLK Studio.`);
    const data=await response.arrayBuffer();
    if(data.byteLength!==end-start)throw Error('Incomplete map read');
    return data;
}
let mapAbort=null;
async function loadMap(key){
    const id=++token;
    mapAbort?.abort();mapAbort=new AbortController();
    const signal=mapAbort.signal;
    mapKey=key;loaded=0;mapCount=0;loading=!!key;
    focusCount=0;focusPoints=new Float32Array();orbitAtEye=false;
    $('error').hidden=true;
    for(const chunk of mapBuffers)gl.deleteBuffer(chunk.buffer);
    mapBuffers.length=0;
    drawSoon();if(!key)return;
    const info=cfg.maps[key];mapCount=info.count;
    focusStride=Math.max(1,Math.ceil(mapCount/maxFocusPoints));
    focusPoints=new Float32Array(Math.ceil(mapCount/focusStride)*3);
    autoRange();
    try{
        const head=await fetch(info.url,{method:'HEAD',headers:{'X-Session-Token':window.blkTrajectoryToken},signal});
        if(!head.ok)throw Error('Cannot open map: '+head.status);
        const fileSize=Number(head.headers.get('Content-Length'));
        const headerBytes=await readRange(info.url,0,Math.min(65536,fileSize),signal);
        const text=new TextDecoder('ascii').decode(headerBytes),marker=text.indexOf('end_header\n');
        if(marker<0||!text.startsWith('ply\nformat binary_little_endian 1.0\n'))throw Error('Expected binary little-endian PLY');
        const header=text.slice(0,marker+11),match=header.match(/^element vertex (\d+)$/m);
        if(!match||Number(match[1])!==mapCount)throw Error('Map count changed since trajectory generation');
        const sizes={char:1,uchar:1,int8:1,uint8:1,short:2,ushort:2,int16:2,uint16:2,int:4,uint:4,int32:4,uint32:4,float:4,float32:4,double:8,float64:8};
        let stride=0,inVertex=false;const fields={};
        for(const line of header.split('\n')){
            const p=line.trim().split(/\s+/);
            if(p[0]==='element')inVertex=p[1]==='vertex';
            if(inVertex&&p[0]==='property'){
                if(!sizes[p[1]])throw Error('Unsupported PLY vertex property');
                fields[p[2]]={offset:stride,type:p[1]};stride+=sizes[p[1]];
            }
        }
        if(!['x','y','z'].every(k=>fields[k]&&['float','float32','double','float64'].includes(fields[k].type)))throw Error('PLY requires floating-point XYZ');
        const begin=marker+11;
        if(fileSize<begin+mapCount*stride)throw Error('Truncated PLY');
        const chunkSize=1048576;
        for(let start=0;start<mapCount;start+=chunkSize){
            const count=Math.min(chunkSize,mapCount-start);
            const raw=await readRange(info.url,begin+start*stride,begin+(start+count)*stride,signal);
            if(id!==token)return;
            const view=new DataView(raw),xyz=new Float32Array(count*3);
            for(let i=0;i<count;i++)for(let j=0;j<3;j++){
                const f=fields[['x','y','z'][j]],offset=i*stride+f.offset;
                const value=sizes[f.type]===8?view.getFloat64(offset,true):view.getFloat32(offset,true);
                if(!Number.isFinite(value))throw Error('Non-finite map coordinate');
                xyz[i*3+j]=value;
            }
            const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,xyz,gl.STATIC_DRAW);
            if(gl.getError()!==gl.NO_ERROR){gl.deleteBuffer(buffer);throw Error('GPU allocation failed while loading the map.');}
            mapBuffers.push({buffer,count});sampleFocusPoints(xyz,loaded);
            // Upload each chunk once; drawing every accumulated prefix makes
            // large cloud loading quadratic in the number of chunks.
            loaded+=count;updateStatus();
        }
        loading=false;drawSoon();
    }catch(e){if(id===token&&e.name!=='AbortError'){
        loading=false;loaded=0;focusCount=0;focusPoints=new Float32Array();
        for(const chunk of mapBuffers)gl.deleteBuffer(chunk.buffer);
        mapBuffers.length=0;error(e);drawSoon();
    }}
}
$('map').onchange=()=>loadMap($('map').value==='none'?'':$('map').value);
$('point-style')?.addEventListener('change',updatePointStyle);
$('size-m')?.addEventListener('input',()=>{if($('size-m').checkValidity())drawSoon();});
for(const id of ['size','alpha','solid','grid','background','color-min','color-max',...Object.values(cfg.styles).map(s=>s.control)])$(id).addEventListener('input',()=>{$('size-value').value=$('size').value+' px';$('alpha-value').value=Number($('alpha').value).toFixed(2);drawSoon();});
$('palette').onchange=()=>{palette();drawSoon();};$('color-by').onchange=()=>{const solid=$('color-by').value==='solid';$('solid-label').hidden=!solid;$('palette-label').hidden=solid;$('range-controls').hidden=solid;autoRange();};$('auto-color').onclick=()=>{colorRangeAuto=true;preferences.set({colorRangeAuto:true});autoRange();};
$('top').onclick=()=>{if(!orthographic)orthographicScale=distance;orthographic=true;pitch=Math.PI/2-.001;yaw=-Math.PI/2;saveCamera();drawSoon();};
$('perspective').onclick=()=>{orthographic=false;pitch=Math.PI/5;yaw=-Math.PI/4;saveCamera();drawSoon();};
$('fit').onclick=()=>{fit(worldBounds(selected().bounds,mapKey||cfg.default_map),true);saveCamera();};
$('fit-path').onclick=()=>{fit(pathBounds(),true);saveCamera();};
$('capture').onclick=()=>{render();canvas.toBlob(b=>{const a=document.createElement('a'),u=URL.createObjectURL(b);a.href=u;a.download=cfg.sequence+'-'+(mapKey||'trajectories')+'.png';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);});};
let drag=null;
canvas.oncontextmenu=e=>e.preventDefault();
canvas.onpointerdown=e=>{
    if(drag||e.button>2)return;
    e.preventDefault();focusView();
    drag={id:e.pointerId,x:e.clientX,y:e.clientY,pan:e.button===1||e.button===2||e.shiftKey};
    canvas.setPointerCapture(e.pointerId);canvas.focus({preventScroll:true});drawSoon();
};
canvas.onpointermove=e=>{
    if(!drag||e.pointerId!==drag.id)return;
    const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
    if(drag.pan)panBy(dx,dy);else orbit(dx,dy);
    drag.x=e.clientX;drag.y=e.clientY;drawSoon();
};
function releasePointer(e){if(drag&&e.pointerId===drag.id){drag=null;saveCamera();drawSoon();}}
canvas.onpointerup=canvas.onpointercancel=canvas.onlostpointercapture=releasePointer;
canvas.addEventListener('wheel',e=>{
    e.preventDefault();
    const units=e.deltaMode===1?16:e.deltaMode===2?canvas.clientHeight:1;
    const delta=Math.max(-1000,Math.min(1000,e.deltaY*units*(e.shiftKey ? .1 : 1)));
    if(orthographic){
        // Translating along the view axis cannot zoom an orthographic view.
        orthographicScale=Math.max(.0001,Math.min(1e9,orthographicScale*Math.exp(delta*.001)));
    }else{
        const step=.5*Math.max(.0001,cloudExtent()*.001)*delta;
        center=add(center,scale(basis().f,-step));
    }
    saveCamera();drawSoon();
},{passive:false});
new ResizeObserver(drawSoon).observe(canvas);
window.blkTrajectoryViewer={stats:()=>({sequence:cfg.sequence,segments:Object.fromEntries(Object.entries(paths).map(([k,p])=>[k,p.count/6])),alignment:'fixed TLS frame',visibleTrajectories:Object.keys(paths).filter(k=>$(style(k).control).checked),map:mapKey,loaded,total:mapCount,displayed,loading,pointStyle:pointStyle(),sizeMeters:worldPointSize(),size:Number($('size').value),alpha:Number($('alpha').value),colorBy:$('color-by').value,palette:$('palette').value,error:$('error').hidden?'':$('error').textContent}),render,gl,canvas,modelFor};
function worldBounds(bounds,key){const m=modelFor(key),lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];for(let mask=0;mask<8;mask++){const p=[0,1,2].map(k=>bounds[(mask>>k)&1][k]);for(let r=0;r<3;r++){const v=m[r]*p[0]+m[4+r]*p[1]+m[8+r]*p[2]+m[12+r];lo[r]=Math.min(lo[r],v);hi[r]=Math.max(hi[r],v);}}return [lo,hi];}
function pathBounds(){const b=Object.entries(cfg.path_bounds).filter(([key])=>$(style(key).control).checked).map(([key,b])=>worldBounds(b,key));if(!b.length)return worldBounds(selected().bounds,mapKey||cfg.default_map);return [0,1].map(i=>[0,1,2].map(k=>Math[i?'max':'min'](...b.map(x=>x[i][k]))));}
$('map').value=cfg.default_map;
preferences.bind(['point-style','size-m','size','alpha','solid','grid','background','color-by','palette','color-min','color-max'],['map',...Object.values(cfg.styles).map(s=>s.control)]);
if(savedCamera?.navigation===navigationVersion&&Number.isFinite(savedCamera.yaw)&&Number.isFinite(savedCamera.pitch)){
    yaw=savedCamera.yaw;pitch=Math.max(-Math.PI/2+.001,Math.min(Math.PI/2-.001,savedCamera.pitch));orthographic=!!savedCamera.orthographic;
    if(Number.isFinite(savedCamera.orthoScaleRatio)&&savedCamera.orthoScaleRatio>0)savedOrthoScale=savedCamera.orthoScaleRatio;
}
for(const id of ['color-min','color-max'])$(id).addEventListener('input',()=>{
    if($(id).value&&$(id).checkValidity()){colorRangeAuto=false;preferences.set({colorRangeAuto:false});}
});
$('size-value').value=$('size').value+' px';$('alpha-value').value=Number($('alpha').value).toFixed(2);
const solid=$('color-by').value==='solid';$('solid-label').hidden=!solid;$('palette-label').hidden=solid;$('range-controls').hidden=solid;
window.blkTrajectoryViewer.preferences=preferences;
window.blkTrajectoryViewer.camera=()=>({yaw,pitch,orthographic,orthographicScale,fovDegrees:50,eye:basis().eye,center:center.slice(),distance,extent:cloudExtent(),orbitAtEye,focusSamples:focusCount});
palette();updatePointStyle();fit(pathBounds());
loadMap($('map').value==='none'?'':$('map').value);

})().catch(error=>{document.getElementById('error').hidden=false;document.getElementById('error').textContent=error.message;});
