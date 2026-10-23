(() =>
{
    'use strict';
    const $ = id => document.getElementById(id);
    const sphere = $('panorama'), flat = $('flat'), flatContext = flat.getContext('2d');
    const gl = sphere.getContext('webgl', {antialias: false});
    const token = location.hash.slice(1) || sessionStorage.getItem('blk-session') || '';
    if (token) sessionStorage.setItem('blk-session', token);
    history.replaceState(null, '', location.pathname + location.search);
    const scan = new URLSearchParams(location.search).get('scan') || '';
    const headers = {'X-Session-Token': token};
    const view = {azimuth: 0, elevation: 0, fov: 75};
    const flatView = {scale: 1, x: 0, y: 0, fit: 1, key: ''};
    const images = new Map();
    const sphereHelp = 'Left drag: look around · Wheel: zoom · Shift+wheel: fine zoom · Double-click: reset';
    const flatHelp = 'Left drag: move · Wheel: zoom at the cursor · Double-click: fit';
    const noExports = 'Convert with Export panoramas or Export camera images to view images.';
    let job = null, scanName = '', tabs = [], active = null, mode = gl ? 'sphere' : 'flat';
    let cameraBitmap = null, current = null, currentKey = '', cameras = null, selected = '', textureKey = '', textureNote = '', pending = false, drag = null, program = null, locations = null, texture = null;

    const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
    const basename = text => String(text || '').split(/[\\/]/).pop();
    const setError = text => $('error').textContent = text;
    const ratio = () => Math.min(devicePixelRatio || 1, 2);

    function fileLabel(key)
    {
        const exported = job.result.panorama;
        if (key === 'colour') return basename(exported.colour_path);
        if (key === 'intensity') return basename(exported.intensity_path);
        if (key === 'cameras.json') return key;
        return `${key}.png`;
    }

    function fileUrl(key)
    {
        if (key === 'colour' || key === 'intensity') return `/files/panorama/${scan}/${key}`;
        if (key === 'cameras.json') return `/files/images/${scan}/cameras.json`;
        return `/files/images/${scan}/${encodeURIComponent(key)}.png`;
    }

    async function fetchFile(key)
    {
        const response = await fetch(fileUrl(key), {headers});
        if (response.ok) return response;
        let message = '';
        try
        {
            message = (await response.json()).error || '';
        }
        catch (error)
        {
            message = '';
        }
        throw Error(message && message !== 'File not found' ? message : `File not found: ${fileLabel(key)}. It may have been moved or deleted.`);
    }

    function decodeImage(key)
    {
        return fetchFile(key).then(response => response.blob()).then(blob => createImageBitmap(blob).catch(error =>
        {
            throw Error(`Cannot display ${fileLabel(key)}: ${error.message}`);
        }));
    }

    function loadImage(key)
    {
        if (!keepsDecodedImage(key)) return decodeImage(key);
        if (!images.has(key))
        {
            images.set(key, decodeImage(key).catch(error =>
            {
                images.delete(key);
                throw error;
            }));
        }
        return images.get(key);
    }

    function releaseCamera()
    {
        if (current === cameraBitmap)
        {
            current = null;
            currentKey = '';
        }
        if (cameraBitmap) cameraBitmap.close();
        cameraBitmap = null;
    }

    function stopPage(message)
    {
        $('controls').hidden = true;
        $('stage').hidden = true;
        $('readout').hidden = true;
        $('help').hidden = true;
        $('status').textContent = scanName;
        setError(message);
    }

    function resize(canvas)
    {
        const width = Math.round(canvas.clientWidth * ratio()), height = Math.round(canvas.clientHeight * ratio());
        if (canvas.width === width && canvas.height === height) return false;
        canvas.width = width;
        canvas.height = height;
        return true;
    }

    function shader(type, source)
    {
        const result = gl.createShader(type);
        gl.shaderSource(result, source);
        gl.compileShader(result);
        if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(result));
        return result;
    }

    function prepareSphere()
    {
        program = gl.createProgram();
        gl.attachShader(program, shader(gl.VERTEX_SHADER, 'attribute vec2 p; varying vec2 q; void main() { q = p; gl_Position = vec4(p, 0.0, 1.0); }'));
        gl.attachShader(program, shader(gl.FRAGMENT_SHADER, `precision highp float;
varying vec2 q;
uniform sampler2D image;
uniform vec3 forward;
uniform vec3 right;
uniform vec3 up;
uniform vec2 scale;
const float PI = 3.141592653589793;
void main()
{
    vec3 d = normalize(forward + q.x * scale.x * right + q.y * scale.y * up);
    float u = 0.5 - atan(d.y, d.x) / (2.0 * PI);
    float v = 0.5 - atan(d.z, length(d.xy)) / PI;
    gl_FragColor = texture2D(image, vec2(fract(u), v));
}`));
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(program));
        gl.useProgram(program);
        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        const position = gl.getAttribLocation(program, 'p');
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
        locations = Object.fromEntries(['image', 'forward', 'right', 'up', 'scale'].map(name => [name, gl.getUniformLocation(program, name)]));
        texture = gl.createTexture();
    }

    function upload(bitmap, key)
    {
        if (textureKey === key) return textureNote;
        const [width, height] = textureSize(bitmap.width, bitmap.height, gl.getParameter(gl.MAX_TEXTURE_SIZE));
        let source = bitmap, note = '';
        if (width !== bitmap.width || height !== bitmap.height)
        {
            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
            source = canvas;
            note = ` · Shown at ${width}×${height} (browser limit)`;
        }
        gl.getError();
        textureKey = '';
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        if (gl.getError() !== gl.NO_ERROR || gl.isContextLost()) throw Error('The graphics card could not hold this panorama.');
        textureKey = key;
        textureNote = note;
        return note;
    }

    function normalizedAzimuth()
    {
        const value = ((view.azimuth + 180) % 360 + 360) % 360 - 180;
        return value === -180 ? 180 : value;
    }

    function drawSphere()
    {
        resize(sphere);
        gl.viewport(0, 0, sphere.width, sphere.height);
        const azimuth = view.azimuth * Math.PI / 180, elevation = view.elevation * Math.PI / 180, half = Math.tan(view.fov * Math.PI / 360);
        const forward = viewDirection(azimuth, elevation), right = [Math.sin(azimuth), -Math.cos(azimuth), 0];
        const up = [right[1] * forward[2] - right[2] * forward[1], right[2] * forward[0] - right[0] * forward[2], right[0] * forward[1] - right[1] * forward[0]];
        gl.uniform1i(locations.image, 0);
        gl.uniform3fv(locations.forward, forward);
        gl.uniform3fv(locations.right, right);
        gl.uniform3fv(locations.up, up);
        gl.uniform2f(locations.scale, half * sphere.width / Math.max(sphere.height, 1), half);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        $('readout').textContent = `Azimuth ${Math.round(normalizedAzimuth())}° · Elevation ${Math.round(view.elevation)}° · FOV ${Math.round(view.fov)}°`;
    }

    function fitFlat()
    {
        resize(flat);
        if (!current) return;
        const scale = Math.min(flat.width / current.width, flat.height / current.height);
        Object.assign(flatView, {scale, fit: scale, x: (flat.width - current.width * scale) / 2, y: (flat.height - current.height * scale) / 2, key: currentKey});
    }

    function drawFlat()
    {
        if (resize(flat) || flatView.key !== currentKey) fitFlat();
        flatContext.setTransform(1, 0, 0, 1, 0, 0);
        flatContext.fillStyle = '#05090e';
        flatContext.fillRect(0, 0, flat.width, flat.height);
        if (!current) return;
        flatContext.imageSmoothingEnabled = flatView.scale < 4;
        flatContext.drawImage(current, flatView.x, flatView.y, current.width * flatView.scale, current.height * flatView.scale);
    }

    const sphereShown = () => Boolean(active !== 'camera' && mode === 'sphere' && current && program);

    function draw()
    {
        if (sphereShown()) drawSphere();
        else drawFlat();
    }

    function redraw()
    {
        if (pending) return;
        pending = true;
        requestAnimationFrame(() =>
        {
            pending = false;
            draw();
        });
    }

    function resetView()
    {
        if (active !== 'camera' && mode === 'sphere') Object.assign(view, {azimuth: 0, elevation: 0, fov: 75});
        else fitFlat();
        redraw();
    }

    function showCanvas()
    {
        const sphereMode = sphereShown();
        sphere.hidden = !sphereMode;
        flat.hidden = sphereMode;
        $('help').textContent = sphereMode ? sphereHelp : flatHelp;
        $('readout').textContent = '';
    }

    function renderTabs()
    {
        $('tabs').replaceChildren(...tabs.map(tab =>
        {
            const item = document.createElement('button');
            item.textContent = tab.label;
            item.disabled = !tab.enabled;
            item.title = tab.reason;
            item.className = tab.id === active ? 'active' : '';
            item.setAttribute('role', 'tab');
            item.setAttribute('aria-selected', String(tab.id === active));
            item.addEventListener('click', () => select(tab.id));
            return item;
        }));
    }

    async function showPanorama(key)
    {
        $('mode-control').hidden = false;
        $('camera-list').hidden = true;
        $('camera-info').hidden = true;
        const label = tabs.find(tab => tab.id === key).label;
        releaseCamera();
        $('status').textContent = `${scanName} · Loading ${label}…`;
        try
        {
            const bitmap = await loadImage(key);
            if (active !== key) return;
            current = bitmap;
            currentKey = key;
            let note = '';
            if (mode === 'sphere' && program)
            {
                try
                {
                    note = upload(bitmap, key);
                }
                catch (error)
                {
                    mode = 'flat';
                    $('mode').value = 'flat';
                    note = ` · ${error.message} Showing Flat view.`;
                }
            }
            setError('');
            $('status').textContent = `${scanName} · ${label} · ${bitmap.width}×${bitmap.height}${note}${program ? '' : ' · WebGL is unavailable; showing Flat view.'}`;
            showCanvas();
            redraw();
        }
        catch (error)
        {
            if (active !== key) return;
            current = null;
            currentKey = '';
            $('status').textContent = scanName;
            setError(error.message);
            showCanvas();
            redraw();
        }
    }

    function renderCameraInfo(image)
    {
        const axis = cameraAxis(image.rotation);
        const number = (value, digits) => typeof value === 'number' ? value.toFixed(digits) : '—';
        const rows = [
            ['Image', image.name],
            ['Resolution', `${image.width} × ${image.height}`],
            ['fx / fy', `${number(image.fx, 1)} / ${number(image.fy, 1)}`],
            ['cx / cy', `${number(image.cx, 1)} / ${number(image.cy, 1)}`],
            ['Exposure', number(image.exposure_time, 4)],
            ['Gain R/G/B', Array.isArray(image.gain_rgb) ? image.gain_rgb.map(value => number(value, 2)).join(' / ') : '—'],
            ['Axis azimuth', `${axis.azimuth.toFixed(1)}°`],
            ['Axis elevation', `${axis.elevation.toFixed(1)}°`]
        ];
        $('camera-info').replaceChildren(...rows.flatMap(([name, value]) =>
        {
            const term = document.createElement('dt'), detail = document.createElement('dd');
            term.textContent = name;
            detail.textContent = value;
            return [term, detail];
        }));
    }

    function renderCameraList()
    {
        const groups = {Down: [], Level: [], Up: []};
        for (const image of cameras)
        {
            const axis = cameraAxis(image.rotation);
            groups[cameraGroup(axis.elevation)].push({image, axis});
        }
        const items = [];
        for (const [name, entries] of Object.entries(groups))
        {
            if (!entries.length) continue;
            const heading = document.createElement('h2');
            heading.textContent = `${name} (${Math.round(entries.reduce((sum, entry) => sum + entry.axis.elevation, 0) / entries.length)}°)`;
            items.push(heading);
            entries.sort((a, b) => a.axis.azimuth - b.axis.azimuth);
            for (const entry of entries)
            {
                const item = document.createElement('button');
                item.textContent = `${entry.image.name} · ${Math.round(entry.axis.azimuth)}°`;
                item.className = entry.image.name === selected ? 'active' : '';
                item.addEventListener('click', () => selectCamera(entry.image));
                items.push(item);
            }
        }
        $('camera-list').replaceChildren(...items);
    }

    async function selectCamera(image)
    {
        selected = image.name;
        renderCameraList();
        renderCameraInfo(image);
        setError('');
        $('status').textContent = `${scanName} · Loading ${image.name}…`;
        try
        {
            const bitmap = await loadImage(image.name);
            if (active !== 'camera' || selected !== image.name)
            {
                bitmap.close();
                return;
            }
            releaseCamera();
            cameraBitmap = bitmap;
            current = bitmap;
            currentKey = image.name;
            $('status').textContent = `${scanName} · ${image.name} · ${bitmap.width}×${bitmap.height}`;
        }
        catch (error)
        {
            if (active !== 'camera' || selected !== image.name) return;
            releaseCamera();
            $('status').textContent = scanName;
            setError(error.message);
        }
        showCanvas();
        redraw();
    }

    async function showCameras()
    {
        $('mode-control').hidden = true;
        $('camera-list').hidden = false;
        $('camera-info').hidden = false;
        current = null;
        currentKey = '';
        showCanvas();
        redraw();
        try
        {
            if (!cameras)
            {
                $('status').textContent = `${scanName} · Loading camera images…`;
                const metadata = await (await fetchFile('cameras.json')).json();
                cameras = Array.isArray(metadata.images) ? metadata.images : [];
            }
            if (active !== 'camera') return;
            if (!cameras.length) throw Error('cameras.json lists no camera images.');
            renderCameraList();
            const image = cameras.find(item => item.name === selected) || cameras.find(item => cameraGroup(cameraAxis(item.rotation).elevation) === 'Down') || cameras[0];
            await selectCamera(image);
        }
        catch (error)
        {
            if (active !== 'camera') return;
            $('status').textContent = scanName;
            setError(error.message);
        }
    }

    function select(id)
    {
        active = id;
        setError('');
        renderTabs();
        if (id === 'camera') showCameras();
        else showPanorama(id);
    }

    sphere.addEventListener('pointerdown', event =>
    {
        if (event.button !== 0) return;
        drag = {x: event.clientX, y: event.clientY};
        sphere.setPointerCapture(event.pointerId);
    });
    sphere.addEventListener('pointermove', event =>
    {
        if (!drag) return;
        const perPixel = view.fov / Math.max(sphere.clientHeight, 1);
        view.azimuth = view.azimuth + (event.clientX - drag.x) * perPixel;
        view.elevation = clamp(view.elevation + (event.clientY - drag.y) * perPixel, -89, 89);
        drag = {x: event.clientX, y: event.clientY};
        redraw();
    });
    sphere.addEventListener('wheel', event =>
    {
        event.preventDefault();
        view.fov = clamp(view.fov * Math.exp(event.deltaY * 0.001 * (event.shiftKey ? 0.25 : 1)), 30, 110);
        redraw();
    }, {passive: false});
    sphere.addEventListener('dblclick', resetView);

    flat.addEventListener('pointerdown', event =>
    {
        if (event.button !== 0) return;
        drag = {x: event.clientX, y: event.clientY};
        flat.setPointerCapture(event.pointerId);
    });
    flat.addEventListener('pointermove', event =>
    {
        const scale = ratio();
        if (drag)
        {
            flatView.x += (event.clientX - drag.x) * scale;
            flatView.y += (event.clientY - drag.y) * scale;
            drag = {x: event.clientX, y: event.clientY};
            redraw();
        }
        if (!current) return;
        const u = (event.offsetX * scale - flatView.x) / (current.width * flatView.scale);
        const v = (event.offsetY * scale - flatView.y) / (current.height * flatView.scale);
        if (u < 0 || u > 1 || v < 0 || v > 1)
        {
            $('readout').textContent = '';
            return;
        }
        if (active === 'camera')
        {
            $('readout').textContent = `Pixel ${Math.floor(u * current.width)}, ${Math.floor(v * current.height)}`;
            return;
        }
        const [azimuth, elevation] = flatAngles(u, v);
        $('readout').textContent = `Azimuth ${azimuth.toFixed(1)}° · Elevation ${elevation.toFixed(1)}°`;
    });
    flat.addEventListener('wheel', event =>
    {
        event.preventDefault();
        if (!current) return;
        const scale = ratio(), x = event.offsetX * scale, y = event.offsetY * scale;
        const next = clamp(flatView.scale * Math.exp(-event.deltaY * 0.001 * (event.shiftKey ? 0.25 : 1)), flatView.fit * 0.5, flatView.fit * 16);
        flatView.x = x - (x - flatView.x) * next / flatView.scale;
        flatView.y = y - (y - flatView.y) * next / flatView.scale;
        flatView.scale = next;
        redraw();
    }, {passive: false});
    flat.addEventListener('dblclick', resetView);
    for (const canvas of [sphere, flat])
    {
        canvas.addEventListener('pointerup', () => drag = null);
        canvas.addEventListener('pointercancel', () => drag = null);
    }
    $('reset').addEventListener('click', resetView);
    $('mode').addEventListener('change', () =>
    {
        mode = $('mode').value;
        if (active && active !== 'camera') showPanorama(active);
    });
    addEventListener('resize', () =>
    {
        if (active === 'camera' || mode === 'flat') fitFlat();
        redraw();
    });

    (async () =>
    {
        if (gl)
        {
            try
            {
                prepareSphere();
            }
            catch (error)
            {
                program = null;
            }
        }
        if (!program)
        {
            mode = 'flat';
            $('mode').value = 'flat';
            $('mode').disabled = true;
        }
        try
        {
            const response = await fetch('/api/scan/' + encodeURIComponent(scan), {headers});
            job = await response.json();
            if (!response.ok) throw Error(job.error || 'Cannot read scan record.');
        }
        catch (error)
        {
            stopPage(error.message);
            return;
        }
        scanName = String(job.name || '').replace(/\.blk$/i, '');
        if (job.status !== 'completed')
        {
            stopPage(job.message || 'This scan has not completed conversion.');
            return;
        }
        tabs = imageTabs(job.result && job.result.panorama);
        const first = firstEnabledTab(tabs);
        if (!first)
        {
            stopPage(noExports);
            return;
        }
        select(first);
    })();
})();
