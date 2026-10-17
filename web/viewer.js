(() =>
{
    'use strict';
    const $ = id => document.getElementById(id),
        c = $('cloud'),
        gl = c.getContext('webgl',
        {
            antialias: true
        });
    if (!gl)
    {
        $('error').textContent = 'WebGL is unavailable. Open the full PLY in another point-cloud viewer.';
        return;
    }
    let data = new Float32Array(),
        count = 0,
        busy = true,
        min = [-1, -1, -1],
        max = [1, 1, 1],
        extent = 1,
        center = [0, 0, 0],
        ilow = 0,
        ihigh = 65535,
        drag = null,
        active = false,
        settle = 0,
        frame = 0,
        orbitAtEye = false,
        panDistance = 10;
    const camera = {
            target: [0, 0, 0],
            azimuth: -Math.PI / 4,
            elevation: Math.PI / 5,
            distance: 10,
            orthographicScale: 10,
            projection: 'perspective'
        },
        fov = 50 * Math.PI / 180,
        tanHalf = Math.tan(fov / 2),
        clip = $('clip');
    const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

    function basis()
    {
        const a = camera.azimuth,
            e = camera.elevation,
            ca = Math.cos(a),
            sa = Math.sin(a),
            ce = Math.cos(e),
            se = Math.sin(e);
        return {
            right: [-sa, ca, 0],
            up: [-se * ca, -se * sa, ce],
            back: [ce * ca, ce * sa, se]
        };
    }

    function multiply(a, b)
    {
        const o = new Float32Array(16);
        for (let col = 0; col < 4; col++)
            for (let row = 0; row < 4; row++)
                for (let k = 0; k < 4; k++) o[col * 4 + row] += a[k * 4 + row] * b[col * 4 + k];
        return o;
    }

    function matrices()
    {
        const
        {
            right: r,
            up: u,
            back: b
        } = basis(), eye = camera.target.map((v, i) => v + b[i] * camera.distance), near = Math.max(.0001, Math.min(extent * .0001, camera.distance * .01)), far = Math.max(camera.distance, Math.hypot(...eye.map((v, i) => v - center[i]))) + extent * 4 + 10, aspect = c.clientWidth / c.clientHeight;
        const view = new Float32Array([r[0], u[0], b[0], 0, r[1], u[1], b[1], 0, r[2], u[2], b[2], 0, -dot(r, eye), -dot(u, eye), -dot(b, eye), 1]);
        let p;
        if (camera.projection === 'perspective')
        {
            const f = 1 / tanHalf;
            p = new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, 2 * far * near / (near - far), 0]);
        }
        else
        {
            const h = camera.orthographicScale * tanHalf,
                w = h * aspect;
            p = new Float32Array([1 / w, 0, 0, 0, 0, 1 / h, 0, 0, 0, 0, -2 / (far - near), 0, 0, 0, -(far + near) / (far - near), 1]);
        }
        return {
            mvp: multiply(p, view),
            near,
            far,
            aspect,
            right: r,
            up: u,
            back: b
        };
    }

    function shader(type, source)
    {
        const s = gl.createShader(type);
        gl.shaderSource(s, source);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(s));
        return s;
    }

    function program(vs, fs)
    {
        const p = gl.createProgram();
        gl.attachShader(p, shader(gl.VERTEX_SHADER, vs));
        gl.attachShader(p, shader(gl.FRAGMENT_SHADER, fs));
        gl.linkProgram(p);
        if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(p));
        return p;
    }
    const points = program(`attribute vec4 point;uniform mat4 mvp;uniform float size,clipZ;uniform vec2 intensityRange;varying vec3 rgb;varying float visible;void main(){gl_Position=mvp*vec4(point.xyz,1.0);gl_PointSize=size;visible=point.z<=clipZ?1.0:0.0;float v=clamp((point.w-intensityRange.x)/max(1.0,intensityRange.y-intensityRange.x),0.0,1.0);rgb=mix(vec3(.18,.3,.42),vec3(.95,.97,1.0),v);}`, `precision mediump float;varying vec3 rgb;varying float visible;void main(){if(visible<.5)discard;vec2 p=gl_PointCoord-vec2(.5);if(dot(p,p)>.25)discard;gl_FragColor=vec4(rgb,1.0);}`);
    const lineProgram = program('attribute vec3 position;attribute vec3 colour;uniform mat4 mvp;varying vec3 rgb;void main(){gl_Position=mvp*vec4(position,1.0);rgb=colour;}', 'precision mediump float;varying vec3 rgb;void main(){gl_FragColor=vec4(rgb,1.0);}');
    const u = {};
    for (const n of ['mvp', 'size', 'clipZ', 'intensityRange']) u[n] = gl.getUniformLocation(points, n);
    const pointAttr = gl.getAttribLocation(points, 'point'),
        linePos = gl.getAttribLocation(lineProgram, 'position'),
        lineCol = gl.getAttribLocation(lineProgram, 'colour'),
        lineMvp = gl.getUniformLocation(lineProgram, 'mvp');
    const pointBuffer = gl.createBuffer(),
        lineBuffer = gl.createBuffer();
    let lineCount = 0,
        gridStep = 1;
    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(.055, .09, .13, 1);

    function makeGrid()
    {
        gridStep = Math.pow(10, Math.floor(Math.log10(Math.max(.01, extent / 20))));
        const span = Math.ceil(extent / gridStep) * gridStep,
            x0 = Math.floor(center[0] / gridStep) * gridStep,
            y0 = Math.floor(center[1] / gridStep) * gridStep,
            a = [];

        function line(p, q, color)
        {
            a.push(...p, ...color, ...q, ...color);
        }
        for (let d = -span; d <= span + gridStep * .1; d += gridStep)
        {
            line([x0 + d, y0 - span, 0], [x0 + d, y0 + span, 0], [.15, .21, .28]);
            line([x0 - span, y0 + d, 0], [x0 + span, y0 + d, 0], [.15, .21, .28]);
        }
        const len = Math.max(.1, extent * .2);
        line([0, 0, 0], [len, 0, 0], [1, .25, .25]);
        line([0, 0, 0], [0, len, 0], [.2, 1, .4]);
        line([0, 0, 0], [0, 0, len], [.3, .6, 1]);
        lineCount = a.length / 6;
        gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(a), gl.STATIC_DRAW);
    }

    function gizmo(b)
    {
        const ctx = $('axes').getContext('2d');
        ctx.clearRect(0, 0, 140, 140);
        ctx.fillStyle = 'rgba(10,17,26,.8)';
        ctx.beginPath();
        ctx.arc(70, 70, 64, 0, Math.PI * 2);
        ctx.fill();
        const axes = [
            [1, 0, 0, 'X', '#ff6666'],
            [0, 1, 0, 'Y', '#56e486'],
            [0, 0, 1, 'Z', '#66adff']
        ].sort((a, c) => dot(a, b.back) - dot(c, b.back));
        for (const a of axes)
        {
            const x = 70 + dot(a, b.right) * 42,
                y = 70 - dot(a, b.up) * 42;
            ctx.strokeStyle = a[4];
            ctx.lineWidth = 3;
            ctx.beginPath();
            ctx.moveTo(70, 70);
            ctx.lineTo(x, y);
            ctx.stroke();
            ctx.fillStyle = a[4];
            ctx.font = 'bold 14px system-ui';
            ctx.textAlign = 'center';
            ctx.fillText(a[3], x, y - 7);
        }
        ctx.fillStyle = '#d9e5f3';
        ctx.font = '11px system-ui';
        ctx.fillText('Z up', 70, 126);
    }

    function shown()
    {
        const selected = Math.ceil(count / Number($('density').value));
        return active ? Math.min(selected, 2000000) : selected;
    }

    function draw()
    {
        frame = 0;
        if (busy || !count) return;
        const dpr = Math.min(devicePixelRatio || 1, 2),
            w = Math.round(c.clientWidth * dpr),
            h = Math.round(c.clientHeight * dpr);
        if (c.width !== w || c.height !== h)
        {
            c.width = w;
            c.height = h;
        }
        gl.viewport(0, 0, w, h);
        gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        const b = matrices();
        if ($('grid').checked)
        {
            gl.useProgram(lineProgram);
            gl.bindBuffer(gl.ARRAY_BUFFER, lineBuffer);
            gl.enableVertexAttribArray(linePos);
            gl.enableVertexAttribArray(lineCol);
            gl.vertexAttribPointer(linePos, 3, gl.FLOAT, false, 24, 0);
            gl.vertexAttribPointer(lineCol, 3, gl.FLOAT, false, 24, 12);
            gl.uniformMatrix4fv(lineMvp, false, b.mvp);
            gl.drawArrays(gl.LINES, 0, lineCount);
            gl.disableVertexAttribArray(linePos);
            gl.disableVertexAttribArray(lineCol);
        }
        gl.useProgram(points);
        gl.bindBuffer(gl.ARRAY_BUFFER, pointBuffer);
        gl.enableVertexAttribArray(pointAttr);
        gl.vertexAttribPointer(pointAttr, 4, gl.FLOAT, false, 16, 0);
        gl.uniformMatrix4fv(u.mvp, false, b.mvp);
        gl.uniform1f(u.size, Number($('size').value) * dpr);
        gl.uniform1f(u.clipZ, Number(clip.value) + .00001);
        gl.uniform2f(u.intensityRange, ilow, ihigh);
        const displayed = shown();
        gl.drawArrays(gl.POINTS, 0, displayed);
        gizmo(b);
        document.body.dataset.loaded = String(count);
        document.body.dataset.shown = String(displayed);
        document.body.dataset.mode = displayed === count ? 'full' : 'sampled';
        document.body.dataset.interacting = String(active);
        document.body.dataset.projection = camera.projection;
        const density = $('density').selectedOptions[0].textContent;
        $('densityStatus').textContent = displayed.toLocaleString('en-US') + ' / ' + count.toLocaleString('en-US') + ' points displayed · selected: ' + density + (active && displayed < Math.ceil(count / Number($('density').value)) ? ' · temporarily reduced while navigating' : '') + (Number(clip.value) < max[2] - 1e-6 ? ' · Z clipping enabled' : '');
        $('cameraStatus').textContent = 'Target [' + camera.target.map(v => v.toFixed(2)).join(', ') + '] m · Distance ' + camera.distance.toFixed(2) + ' m · XY grid (Z=0): ' + gridStep.toPrecision(2) + ' m · scanner-local axes';
    }

    function requestDraw()
    {
        if (!frame) frame = requestAnimationFrame(draw);
    }

    function gesture()
    {
        active = true;
        clearTimeout(settle);
        settle = setTimeout(() =>
        {
            if (!drag)
            {
                active = false;
                requestDraw();
            }
        }, 180);
        requestDraw();
    }

    function fit()
    {
        camera.target = center.slice();
        const radius = Math.max(.001, Math.hypot(...max.map((v, i) => (v - min[i]) / 2))),
            aspect = c.clientWidth / c.clientHeight;
        camera.distance = radius / Math.sin(Math.atan(tanHalf * Math.min(1, aspect))) * 1.08;
        camera.orthographicScale = camera.distance;
        panDistance = camera.distance;
        orbitAtEye = false;
        requestDraw();
    }

    function reset()
    {
        camera.azimuth = -Math.PI / 4;
        camera.elevation = Math.PI / 5;
        camera.projection = 'perspective';
        $('projection').value = 'perspective';
        $('view').value = 'oblique';
        clip.value = clip.max;
        $('clipvalue').textContent = 'All';
        fit();
    }

    function setCloud(next)
    {
        data = next;
        count = data.length / 4;
        min = [Infinity, Infinity, Infinity];
        max = [-Infinity, -Infinity, -Infinity];
        const histogram = new Uint32Array(65536);
        for (let i = 0; i < data.length; i += 4)
        {
            for (let k = 0; k < 3; k++)
            {
                min[k] = Math.min(min[k], data[i + k]);
                max[k] = Math.max(max[k], data[i + k]);
            }
            histogram[Math.max(0, Math.min(65535, data[i + 3] | 0))]++;
        }
        let sum = 0;
        ilow = 0;
        ihigh = 65535;
        for (let i = 0; i < histogram.length; i++)
        {
            sum += histogram[i];
            if (sum < count * .01) ilow = i;
            if (sum >= count * .99)
            {
                ihigh = i;
                break;
            }
        }
        center = min.map((v, i) => (v + max[i]) / 2);
        extent = Math.max(.001, ...max.map((v, i) => v - min[i]));
        gl.bindBuffer(gl.ARRAY_BUFFER, pointBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
        if (gl.getError() !== gl.NO_ERROR) throw Error('The graphics device could not allocate the full cloud. Close other 3D tabs and try again.');
        makeGrid();
        clip.min = min[2];
        clip.max = max[2];
        clip.step = 'any';
        clip.value = max[2];
        $('clipvalue').textContent = 'All';
        fit();
        draw();
    }

    function preset(value)
    {
        if (value === 'top')
        {
            camera.azimuth = -Math.PI / 2;
            camera.elevation = Math.PI / 2;
        }
        else if (value === 'front')
        {
            camera.azimuth = -Math.PI / 2;
            camera.elevation = 0;
        }
        else if (value === 'right')
        {
            camera.azimuth = 0;
            camera.elevation = 0;
        }
        else
        {
            camera.azimuth = -Math.PI / 4;
            camera.elevation = Math.PI / 5;
        }
        requestDraw();
    }

    function cameraEye()
    {
        const back = basis().back;
        return camera.target.map((value, index) => value + back[index] * camera.distance);
    }

    function focusView()
    {
        // Change only the pivot depth along the center ray; never jump the eye or view direction.
        const b = basis(), eye = cameraEye(), height = c.clientHeight,
            radius = Math.max(16, Math.min(c.clientWidth, height) * .04),
            visible = shown(), stride = Math.max(1, Math.ceil(visible / 200000)),
            near = Math.max(.0001, Math.min(extent * .0001, camera.distance * .01)),
            zLimit = Number(clip.value) + .00001;
        let best = radius * radius, depth = null;
        for (let point = 0; point < visible; point += stride)
        {
            const index = point * 4;
            if (data[index + 2] > zLimit) continue;
            const x = data[index] - eye[0], y = data[index + 1] - eye[1], z = data[index + 2] - eye[2],
                forward = -(x * b.back[0] + y * b.back[1] + z * b.back[2]);
            if (forward <= near) continue;
            const halfHeight = (camera.projection === 'perspective' ? forward : camera.orthographicScale) * tanHalf,
                scale = height / (2 * halfHeight),
                px = (x * b.right[0] + y * b.right[1] + z * b.right[2]) * scale,
                py = (x * b.up[0] + y * b.up[1] + z * b.up[2]) * scale,
                screenDistance = px * px + py * py;
            if (screenDistance > radius * radius) continue;
            if (depth === null || screenDistance < best - 1 || (Math.abs(screenDistance - best) <= 1 && forward < depth))
            {
                best = screenDistance;
                depth = forward;
            }
        }
        orbitAtEye = depth === null;
        camera.distance = depth === null ? Math.max(.01, Math.min(camera.distance, 1)) : depth;
        camera.target = eye.map((value, index) => value - b.back[index] * camera.distance);
    }

    function orbit(dx, dy)
    {
        const eye = orbitAtEye ? cameraEye() : null;
        camera.azimuth -= dx * Math.PI / c.clientHeight;
        camera.elevation = Math.max(-Math.PI / 2 + .001, Math.min(Math.PI / 2 - .001, camera.elevation + dy * Math.PI / c.clientHeight));
        if (eye)
        {
            const back = basis().back;
            camera.target = eye.map((value, index) => value - back[index] * camera.distance);
        }
        $('view').selectedIndex = -1;
    }

    function preparePan()
    {
        if (camera.projection !== 'perspective') return;
        // Estimate the visible surface depth independently of the orbit pivot.
        // Frontmost samples per screen tile prevent dense, hidden backgrounds from dominating.
        const b = basis(), eye = cameraEye(), columns = 24, rows = 16,
            depths = new Float64Array(columns * rows).fill(Infinity),
            visible = shown(), stride = Math.max(1, Math.ceil(visible / 200000)),
            aspect = c.clientWidth / c.clientHeight,
            near = Math.max(.0001, Math.min(extent * .0001, camera.distance * .01)),
            zLimit = Number(clip.value) + .00001;
        for (let point = 0; point < visible; point += stride)
        {
            const index = point * 4;
            if (data[index + 2] > zLimit) continue;
            const offset = [data[index] - eye[0], data[index + 1] - eye[1], data[index + 2] - eye[2]],
                depth = -dot(offset, b.back);
            if (depth <= near) continue;
            const x = dot(offset, b.right) / (depth * tanHalf * aspect),
                y = dot(offset, b.up) / (depth * tanHalf);
            if (Math.abs(x) >= 1 || Math.abs(y) >= 1) continue;
            const tile = Math.floor((y + 1) * .5 * rows) * columns + Math.floor((x + 1) * .5 * columns);
            depths[tile] = Math.min(depths[tile], depth);
        }
        const valid = Array.from(depths).filter(Number.isFinite).sort((a, b) => a - b);
        // A median rejects isolated foreground points. Empty views retain the last usable speed.
        // This value remains fixed throughout the drag, even as the visible geometry changes.
        if (valid.length)
        {
            const middle = Math.floor(valid.length / 2);
            panDistance = (valid[middle] + valid[Math.floor((valid.length - 1) / 2)]) * .5;
        }
    }

    function panBy(dx, dy)
    {
        const b = basis(),
            units = 2 * (camera.projection === 'perspective' ? panDistance : camera.orthographicScale) * tanHalf / c.clientHeight;
        camera.target = camera.target.map((v, i) => v + (-dx * b.right[i] + dy * b.up[i]) * units);
    }

    function dolly(delta)
    {
        const back = basis().back,
            step = .5 * Math.max(.0001, extent * .001) * Math.max(-1000, Math.min(1000, delta));
        // Move eye and orbit center together; preserve orbit radius and pan sensitivity.
        camera.target = camera.target.map((value, index) => value + back[index] * step);
    }
    $('reset').onclick = reset;
    $('fit').onclick = fit;
    $('view').onchange = e => preset(e.target.value);
    $('projection').onchange = e =>
    {
        if (e.target.value === 'orthographic') camera.orthographicScale = camera.distance;
        camera.projection = e.target.value;
        requestDraw();
    };
    for (const id of ['size', 'grid', 'density']) $(id).oninput = requestDraw;
    clip.oninput = () =>
    {
        $('clipvalue').textContent = Number(clip.value).toFixed(2) + ' m';
        requestDraw();
    };
    c.oncontextmenu = e => e.preventDefault();
    c.onpointerdown = e =>
    {
        if (e.button > 2) return;
        e.preventDefault();
        const pan = e.shiftKey || e.button === 1 || e.button === 2;
        if (pan) preparePan();
        else focusView();
        drag = {
            id: e.pointerId,
            x: e.clientX,
            y: e.clientY,
            pan
        };
        c.setPointerCapture(e.pointerId);
        c.focus();
        gesture();
    };
    c.onpointermove = e =>
    {
        if (!drag || e.pointerId !== drag.id) return;
        const dx = e.clientX - drag.x,
            dy = e.clientY - drag.y;
        drag.x = e.clientX;
        drag.y = e.clientY;
        if (drag.pan) panBy(dx, dy);
        else orbit(dx, dy);
        gesture();
    };

    function release(e)
    {
        if (drag && e.pointerId === drag.id)
        {
            drag = null;
            active = false;
            clearTimeout(settle);
            requestDraw();
        }
    }
    c.onpointerup = c.onpointercancel = c.onlostpointercapture = release;
    c.addEventListener('wheel', e =>
    {
        e.preventDefault();
        const delta = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? c.clientHeight : 1);
        dolly(delta * (e.shiftKey ? .1 : 1));
        gesture();
    },
    {
        passive: false
    });
    c.onkeydown = e =>
    {
        if (e.key.startsWith('Arrow')) focusView();
        if (e.key === 'ArrowLeft') orbit(-15, 0);
        else if (e.key === 'ArrowRight') orbit(15, 0);
        else if (e.key === 'ArrowUp') orbit(0, -15);
        else if (e.key === 'ArrowDown') orbit(0, 15);
        else if (e.key === '+' || e.key === '=') dolly(-100);
        else if (e.key === '-') dolly(100);
        else return;
        e.preventDefault();
        gesture();
    };

    async function prepareDisplay(vertices)
    {
        //Codex: Shuffle complete XYZ/intensity records once so every draw prefix covers the entire scan.
        //Codex: The deterministic order keeps samples stable and nested without per-frame sampling or extra GPU buffers.
        const total = vertices.length / 4;
        let state = 0x9e3779b9;
        for (let i = total - 1; i > 0; --i)
        {
            state ^= state << 13;
            state ^= state >>> 17;
            state ^= state << 5;
            const other = Math.floor((state >>> 0) / 4294967296 * (i + 1)) * 4;
            for (let k = 0; k < 4; ++k)
            {
                const value = vertices[i * 4 + k];
                vertices[i * 4 + k] = vertices[other + k];
                vertices[other + k] = value;
            }
            if (i % 262144 === 0)
            {
                $('load-progress').value = (total - i) / total * 100;
                await new Promise(resolve => setTimeout(resolve, 0));
            }
        }
    }

    async function loadPly(source, name, expectedCount)
    {
        busy = true;
        count = 0;
        $('file').disabled = true;
        $('error').textContent = '';
        $('load-progress').hidden = false;
        $('load-progress').value = 0;
        $('load-status').textContent = 'Reading PLY header…';
        try
        {
            const headerBytes = await source.slice(0, Math.min(source.size, 65536)).arrayBuffer();
            const text = new TextDecoder('ascii').decode(headerBytes);
            const marker = text.indexOf('end_header\n');
            if (marker < 0) throw Error('PLY header not found.');
            const header = text.slice(0, marker + 11),
                match = header.match(/^element vertex (\d+)$/m);
            if (!header.startsWith('ply\nformat binary_little_endian 1.0\n') || !header.endsWith('property float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n') || !match) throw Error('Expected a binary little-endian XYZ + uint16 intensity PLY.');
            const total = Number(match[1]),
                start = marker + 11;
            if (!Number.isSafeInteger(total) || total <= 0 || source.size !== start + total * 14) throw Error('PLY size/count mismatch.');
            if (expectedCount !== undefined && total !== expectedCount) throw Error('PLY count differs from the project record.');
            data = new Float32Array();
            gl.bindBuffer(gl.ARRAY_BUFFER, pointBuffer);
            gl.bufferData(gl.ARRAY_BUFFER, 0, gl.STATIC_DRAW);
            const vertices = new Float32Array(total * 4),
                chunk = 262144;
            for (let base = 0; base < total; base += chunk)
            {
                const take = Math.min(chunk, total - base),
                    raw = await source.slice(start + base * 14, start + (base + take) * 14).arrayBuffer();
                if (raw.byteLength !== take * 14) throw Error('Incomplete PLY read.');
                const view = new DataView(raw);
                for (let i = 0; i < take; i++)
                {
                    const q = i * 14,
                        p = (base + i) * 4;
                    for (let k = 0; k < 3; k++)
                    {
                        const v = view.getFloat32(q + k * 4, true);
                        if (!Number.isFinite(v)) throw Error('Non-finite PLY coordinate.');
                        vertices[p + k] = v;
                    }
                    vertices[p + 3] = view.getUint16(q + 12, true);
                }
                $('load-progress').value = (base + take) / total * 100;
                $('load-status').textContent = 'Loading full PLY: ' + (base + take).toLocaleString('en-US') + ' / ' + total.toLocaleString('en-US') + ' points';
                await new Promise(resolve => setTimeout(resolve, 0));
            }
            $('load-status').textContent = 'Preparing display densities…';
            $('load-progress').value = 0;
            await new Promise(resolve => setTimeout(resolve, 0));
            await prepareDisplay(vertices);
            setCloud(vertices);
            busy = false;
            draw();
            $('cloud-name').textContent = name;
            document.title = name + ' · BLK Studio';
            $('load-status').textContent = 'Loaded the full PLY: ' + name + '. Display density does not change the file.';
        }
        catch (error)
        {
            count = 0;
            data = new Float32Array();
            $('error').textContent = error.message;
            $('load-status').textContent = 'Unable to load the complete cloud. No sampled substitute was displayed.';
            document.body.dataset.loaded = '0';
            document.body.dataset.shown = '0';
        }
        finally
        {
            busy = false;
            $('file').disabled = false;
            $('load-progress').hidden = true;
        }
    }
    async function loadScan()
    {
        const params = new URLSearchParams(location.search),
            cloud = params.get('cloud'),
            scan = cloud || params.get('scan');
        if (!scan)
        {
            busy = false;
            $('load-status').textContent = 'Select a full-resolution PLY file to begin.';
            return;
        }
        const token = location.hash.slice(1) || sessionStorage.getItem('blk-session') || '';
        if (token) sessionStorage.setItem('blk-session', token);
        history.replaceState(null, '', location.pathname + location.search);
        const headers = {
            'X-Session-Token': token
        };
        try
        {
            const response = await fetch((cloud ? '/api/cloud/' : '/api/scan/') + encodeURIComponent(scan),
            {
                headers
            });
            const job = await response.json();
            if (!response.ok) throw Error(job.error || 'Cannot read scan record.');
            if (!cloud && job.status !== 'completed') throw Error('This scan has not completed conversion. Open an existing PLY from View saved point clouds.');
            const url = (cloud ? '/files/cloud/' : '/files/') + encodeURIComponent(scan) + '.ply';
            const head = await fetch(url,
            {
                method: 'HEAD',
                headers
            });
            if (!head.ok) throw Error('PLY file is missing or the app has closed.');
            const size = Number(head.headers.get('Content-Length'));
            if (!Number.isSafeInteger(size) || size <= 0) throw Error('Invalid PLY size.');
            const source = {
                size,
                slice: (start, end) => (
                {
                    arrayBuffer: async () =>
                    {
                        const block = await fetch(url,
                        {
                            headers:
                            {
                                ...headers,
                                Range: `bytes=${start}-${Math.min(end,size)-1}`
                            }
                        });
                        if (block.status !== 206) throw Error('PLY range read failed.');
                        return block.arrayBuffer();
                    }
                })
            };
            await loadPly(source, job.name.replace(/\.blk$/i, '.ply'), cloud ? job.valid_points : job.result.valid_points);
        }
        catch (error)
        {
            busy = false;
            $('error').textContent = error.message;
            $('load-status').textContent = 'Reopen this view from BLK Studio or select a PLY file.';
        }
    }
    $('file').onchange = () =>
    {
        if ($('file').files[0]) loadPly($('file').files[0], $('file').files[0].name);
    };
    c.addEventListener('webglcontextlost', event =>
    {
        event.preventDefault();
        count = 0;
        document.body.dataset.loaded = '0';
        document.body.dataset.shown = '0';
        $('error').textContent = 'Graphics context lost. Reload the view to load the full cloud again.';
    });
    window.blkViewer = {
        camera,
        basis,
        matrices,
        cameraEye,
        focusView,
        orbit,
        panBy,
        dolly,
        draw,
        preset,
        loadPly,
        get count()
        {
            return count;
        }
    };
    new ResizeObserver(requestDraw).observe(c);
    loadScan();
})();
