'use strict';
window.blkTrajectoryToken = location.hash.slice(1) || sessionStorage.getItem('blk-session') || '';
if (window.blkTrajectoryToken) sessionStorage.setItem('blk-session', window.blkTrajectoryToken);
if (window.blkTrajectoryToken && !location.hash)
    history.replaceState(null, '', `${location.pathname}${location.search}#${window.blkTrajectoryToken}`);

window.ViewerPreferences = {async open(scope)
{
    const key = 'blk-trajectory-viewer-v1';
    let state = {common:{}, views:{}};
    try { state = JSON.parse(localStorage.getItem(key)) || state; } catch (_) {}
    state.common ||= {}; state.views ||= {};
    const set = (common = {}, values = {}) =>
    {
        Object.assign(state.common, common);
        state.views[scope] = {...state.views[scope], ...values};
        try { localStorage.setItem(key, JSON.stringify(state)); } catch (_) {}
    };
    const api = {common:() => state.common, values:() => state.views[scope] || {}, set,
        bind(commonIds, scopedIds)
        {
            for (const [ids, common] of [[commonIds,true], [scopedIds,false]]) for (const id of ids)
            {
                const el = document.getElementById(id), value = (common ? state.common : state.views[scope] || {})[id];
                if (!el) continue;
                if (el.type === 'checkbox') { if (typeof value === 'boolean') el.checked = value; }
                else if (value !== undefined && (el.tagName !== 'SELECT' || [...el.options].some(o => o.value === String(value))))
                {
                    const before = el.value; el.value = String(value);
                    if (!el.checkValidity() || !el.value) el.value = before;
                }
                for (const event of ['input','change']) el.addEventListener(event, () =>
                {
                    if (!el.checkValidity()) return;
                    const value = el.type === 'checkbox' ? el.checked : el.value;
                    set(common ? {[id]:value} : {}, common ? {} : {[id]:value});
                });
            }
        }, reset(){ try { localStorage.removeItem(key); } catch (_) {} location.reload(); }};
    document.getElementById('reset-settings').onclick = api.reset;
    return api;
}};

window.blkTrajectoryScene = async function()
{
    const id = new URLSearchParams(location.search).get('job');
    if (!id) throw Error('Open this viewer from Generate trajectory.');
    const response = await fetch(`/api/trajectory/${encodeURIComponent(id)}/viewer`, {headers:{'X-Session-Token':window.blkTrajectoryToken}});
    if (!response.ok) throw Error('Cannot open trajectory result ('+response.status+').');
    const scene = await response.json();
    document.querySelector('h1').textContent = scene.name;
    document.title = scene.name+' · BLK Studio';
    const cfg = {settings_scope:'trajectory', sequence:scene.name, maps:{}, styles:{}, transforms:{},
        trajectories:{}, trajectory_gap_s:{}, trajectory_break_before:{}, path_bounds:{}, default_map:'tls_10cm'};
    const quality = scene.quality || [], inputGaps = scene.input_quality?.skipped_scan_intervals || [];
    const ordered = new Map(inputGaps.map(q => [q.scan_index, {index:q.scan_index, accepted:false}]));
    for (const q of quality) ordered.set(q.index, q);
    const breaks = []; let acceptedIndex = 0, missing = false;
    for (const q of [...ordered.values()].sort((a,b) => a.index-b.index))
    {
        if (!q.accepted) { missing = true; continue; }
        if (missing && acceptedIndex) breaks.push(acceptedIndex);
        acceptedIndex++; missing = false;
    }
    cfg.trajectory_break_before.tls_gt = breaks;
    const mapSelect = document.getElementById('map');
    for (const item of scene.maps)
    {
        // Older results still reference the original 1 cm registration input.
        if (item.key === 'tls_1cm') continue;
        const label = {tls_10cm:'Scanned GT map · 10 cm', tls_gt:'Generated ground-truth trajectory map · 10 cm'}[item.key] || item.label;
        cfg.maps[item.key] = {count:item.points, bounds:item.bounds, color_bounds:item.bounds, url:item.url};
        cfg.styles[item.key] = {label, mapLabel:label, color:item.color};
        mapSelect.add(new Option(label, item.key));
    }
    mapSelect.add(new Option('Hide map', 'none'));
    document.getElementById('color-by').value = 'solid';
    document.getElementById('solid').value = '#ffffff';
    for (const layer of scene.trajectories)
    {
        if (!layer.rows.length) continue;
        if (layer.key === 'tls_gt') layer.label = 'Generated ground-truth trajectory';
        if (layer.key === 'tls_gt' && layer.color.toUpperCase() === '#FFFFFF') layer.color = '#FF9B54';
        cfg.trajectories[layer.key] = layer.rows.map(row => row.slice(0, 4));
        cfg.trajectory_gap_s[layer.key] = layer.gap_s ?? scene.gap_s;
        cfg.styles[layer.key] = {...cfg.styles[layer.key], label:layer.label, color:layer.color, control:'path-'+layer.key, width:2.3};
        const low = [Infinity,Infinity,Infinity], high = [-Infinity,-Infinity,-Infinity];
        for (const row of layer.rows) for (let j = 0; j < 3; j++) { low[j] = Math.min(low[j], row[j+1]); high[j] = Math.max(high[j], row[j+1]); }
        cfg.path_bounds[layer.key] = [low, high];
        const label = document.createElement('label'); label.className = 'check';
        const check = document.createElement('input'); check.type = 'checkbox'; check.checked = true; check.id = 'path-'+layer.key;
        const color = document.createElement('i'); color.style.background = layer.color;
        label.append(check, color, document.createTextNode(layer.label));
        document.getElementById('trajectory-paths').append(label);
    }
    // Renderer expects controls only for trajectory styles.
    for (const [key, style] of Object.entries(cfg.styles)) if (!style.control)
    {
        const hidden = document.createElement('input'); hidden.type = 'checkbox'; hidden.hidden = true; hidden.id = 'map-style-'+key;
        document.body.append(hidden); style.control = hidden.id;
    }
    const accepted = quality.filter(q => q.accepted);
    const rms = accepted.map(q => q.plane_rms_m).filter(Number.isFinite).sort((a,b) => a-b);
    const skipped = scene.input_quality?.skipped_scan_intervals?.length || 0;
    document.getElementById('trajectory-summary').textContent = `${accepted.length.toLocaleString()} / ${quality.length.toLocaleString()} accepted poses`+(rms.length ? ` · median residual ${(rms[Math.floor(rms.length/2)]*100).toFixed(2)} cm` : '')+` · ${skipped} skipped scans`;
    const reasons = new Map();
    for (const q of quality.filter(q => !q.accepted)) reasons.set(q.reason, (reasons.get(q.reason) || 0)+1);
    const table = document.createElement('table');
    for (const [reason, count] of reasons)
    {
        const row = document.createElement('tr'), label = document.createElement('th'), value = document.createElement('td');
        label.textContent = String(reason || 'Unresolved').replaceAll('_',' '); value.textContent = count.toLocaleString(); row.append(label,value); table.append(row);
    }
    document.getElementById('trajectory-quality-table').append(table);
    return cfg;
};
