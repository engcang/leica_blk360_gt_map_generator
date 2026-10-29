'use strict';
let trajectory_state = null, trajectory_initialized = false, trajectory_pick = null, trajectory_save_timer;
const trajectory_batch_ids = new Set(), trajectory_result_ids = new Set();

async function trajectoryPicked(value)
{
    const callback = trajectory_pick; trajectory_pick = null;
    if (callback) await callback(value);
    saveTrajectoryDraft();
}

function pickTrajectory(target, mode = 'trajectory-file')
{
    trajectory_pick = typeof target === 'function' ? target : path => { target.value = path; target.dispatchEvent(new Event('change')); };
    return openBrowser(mode);
}

function setTrajectoryTimeScale(select, scale = 1)
{
    const option = [...select.options].find(item => Number(item.value) === Number(scale));
    if (!option) select.add(new Option(`${scale} s/unit`, String(scale)));
    select.value = option ? option.value : String(scale);
}

function addTrajectoryEstimate(container, value = {})
{
    const row = document.createElement('div');
    row.className = 'trajectory-estimate';
    row.innerHTML = `
        <div class="map-output"><input class="estimate-path" aria-label="Estimated trajectory file" placeholder="trajectory.tum"><button class="estimate-browse secondary">Browse…</button></div>
        <div class="registration-settings">
            <label>Display name<input class="estimate-label" placeholder="Frontend / Backend / Odometry"></label>
            <label>File format<select class="estimate-format"><option value="tum">TUM · timestamp, position, quaternion</option><option value="csv">CSV · named columns</option><option value="kitti">KITTI · pose matrices</option></select></label>
            <button class="estimate-remove quiet">Remove</button>
        </div>
        <p class="trajectory-help estimate-role"></p>
        <div class="estimate-kitti trajectory-format-options" hidden>
            <label>KITTI timestamp file</label><div class="map-output"><input class="estimate-timestamps" aria-label="KITTI timestamp file" placeholder="One timestamp per pose"><button class="estimate-time-browse secondary">Browse…</button></div>
        </div>
        <div class="estimate-csv trajectory-format-options" hidden>
            <div class="registration-settings"><label>CSV delimiter<select class="estimate-delimiter"><option value=",">Comma</option><option value=";">Semicolon</option><option value="&#9;">Tab</option><option value=" ">Space</option></select></label></div>
            <label class="trajectory-frame">CSV columns: time, x, y, z, qx, qy, qz, qw<input class="estimate-columns" value="timestamp,x,y,z,qx,qy,qz,qw"></label>
        </div>
        <details><summary>Timestamp units and pose convention</summary>
            <div class="registration-settings">
                <label>Timestamp units<select class="estimate-time"><option value="1">Seconds</option><option value="0.001">Milliseconds</option><option value="0.000001">Microseconds</option><option value="0.000000001">Nanoseconds</option></select></label>
                <label>Time offset (s)<input class="estimate-offset" type="number" step="0.001" value="0"></label>
                <label>Position scale<input class="estimate-scale" type="number" step="0.001" value="1"></label>
                <label class="estimate-invert"><input class="estimate-inverse" type="checkbox">Invert input poses</label>
            </div>
            <label class="trajectory-frame">Pose-frame-from-body transform JSON (optional)<textarea class="estimate-transform" rows="2" spellcheck="false"></textarea></label>
        </details>`;
    const get = name => row.querySelector('.estimate-'+name);
    get('label').value = value.label || (container.children.length ? 'Backend' : 'Frontend');
    for (const key of ['path', 'format', 'timestamps']) if (value[key]) get(key).value = value[key];
    setTrajectoryTimeScale(get('time'), value.time_scale);
    get('offset').value = value.time_offset_s || 0;
    get('scale').value = value.position_scale || 1;
    get('inverse').checked = !!value.inverse;
    get('delimiter').value = value.delimiter || ',';
    if (value.columns) get('columns').value = ['t', 'x', 'y', 'z', 'qx', 'qy', 'qz', 'qw'].map(k => value.columns[k]).join(',');
    if (value.T_pose_from_body) get('transform').value = JSON.stringify(value.T_pose_from_body);
    get('browse').onclick = () => pickTrajectory(get('path'));
    get('time-browse').onclick = () => pickTrajectory(get('timestamps'));
    const showFormat = () =>
    {
        get('kitti').hidden = get('format').value !== 'kitti';
        get('csv').hidden = get('format').value !== 'csv';
        get('path').placeholder = {tum:'trajectory.tum', csv:'trajectory.csv', kitti:'poses.txt'}[get('format').value];
    };
    get('format').onchange = showFormat;
    showFormat();
    get('remove').onclick = () => { row.remove(); updateTrajectoryRoles(container); saveTrajectoryDraft(); };
    row.addEventListener('input', saveTrajectoryDraft);
    container.append(row);
    updateTrajectoryRoles(container);
}

function updateTrajectoryRoles(container)
{
    const rows = [...container.children];
    const summary = container.closest('.trajectory-sequence')?.querySelector('.sequence-odometry-summary');
    if (summary)
    {
        const path = rows[0]?.querySelector('.estimate-path').value || '';
        summary.textContent = 'Input odometry / SLAM' + (path ? ' · '+path.split(/[\\/]/).pop() : ' · Select a file')
            + (rows.length > 1 ? ` + ${rows.length-1} comparison` : '');
    }
    rows.forEach((row, i) =>
    {
        row.querySelector('.estimate-role').textContent = i ? 'Comparison only · shown with its reconstructed map.' : 'Motion source · used for initialization and scan motion correction.';
        row.querySelector('.estimate-remove').disabled = rows.length === 1;
    });
}


function addTrajectorySequence(value)
{
    const row = document.createElement('article'); row.className = 'trajectory-sequence';
    row.innerHTML = `
        <div class="queue-toolbar"><h4></h4><button class="sequence-remove quiet">Remove</button></div>
        <p class="sequence-folder filepath"></p>
        <div class="sequence-scan-summary"></div>
        <details class="sequence-settings"><summary>Scan files and sensor frame</summary>
            <div class="registration-settings"><label>Scan format<select class="sequence-format"><option value="directory">Timestamped scan files</option><option value="manifest">Scan list JSON</option><option value="tartanstar">TartanStar extracted sequence</option></select></label><label>Timestamp units<select class="sequence-time"><option value="1">Seconds</option><option value="0.001">Milliseconds</option><option value="0.000001">Microseconds</option><option value="0.000000001">Nanoseconds</option></select></label></div>
            <div class="map-output"><input class="sequence-scans" aria-label="Scan files or folder"><button class="sequence-scan-browse secondary">Browse…</button></div>
            <div class="registration-settings"><label>Scan coordinate frame<select class="sequence-frame"><option value="identity">Same as odometry</option><option value="tartanstar">TartanStar OS-DOME → base_link</option><option value="file">Select sensor-to-body calibration</option></select></label></div>
            <div class="sequence-calibration-row map-output" hidden><input class="sequence-calibration" aria-label="Sensor-to-body calibration"><button class="sequence-calibration-browse secondary">Browse…</button></div>
        </details>
        <details class="sequence-odometry"><summary class="sequence-odometry-summary">Input odometry / SLAM</summary>
            <div class="queue-toolbar sequence-odometry-heading"><button class="sequence-add-estimate secondary">Add comparison</button></div>
            <div class="sequence-estimates"></div>
        </details>`;
    row.sequenceDirectory = value.sequence_directory;
    row.querySelector('h4').textContent = value.name || value.sequence_directory.split(/[\\/]/).pop();
    row.querySelector('.sequence-folder').textContent = value.sequence_directory;
    const get = name => row.querySelector('.sequence-'+name), scans = value.scans || {};
    get('scans').value = scans.path || '';
    get('format').value = scans.format || 'directory';
    setTrajectoryTimeScale(get('time'), scans.time_scale);
    get('frame').value = scans.calibration_preset ? 'tartanstar' : scans.calibration ? 'file' : 'identity';
    get('calibration').value = scans.calibration?.path || '';
    const update = () =>
    {
        get('calibration-row').hidden = get('frame').value !== 'file';
        get('scan-summary').textContent = get('scans').value
            ? `${get('format').selectedOptions[0].textContent} · All scans`
            : 'Select the scan folder or scan list below.';
        get('settings').open = !get('scans').value;
    };
    get('frame').onchange = update;
    get('format').onchange = () =>
    {
        if (get('format').value === 'tartanstar') get('frame').value = 'tartanstar';
        else if (get('frame').value === 'tartanstar') get('frame').value = 'identity';
        update();
    };
    get('scans').onchange = update;
    get('scan-browse').onclick = () => pickTrajectory(get('scans'), get('format').value === 'manifest' ? 'trajectory-file' : 'trajectory-folder');
    get('calibration-browse').onclick = () => pickTrajectory(get('calibration'));
    const container = get('estimates');
    for (const estimate of value.trajectories?.length ? value.trajectories : [{}]) addTrajectoryEstimate(container, estimate);
    get('odometry').open = !value.trajectories?.[0]?.path;
    get('add-estimate').onclick = () => { addTrajectoryEstimate(container); saveTrajectoryDraft(); };
    get('remove').onclick = () => { row.remove(); updateTrajectoryOutputs(); saveTrajectoryDraft(); };
    row.addEventListener('input', () => { updateTrajectoryRoles(container); saveTrajectoryDraft(); });
    element('trajectory-sequences').append(row); update(); updateTrajectoryOutputs();
}

function trajectorySequences()
{
    return [...element('trajectory-sequences').children].map(row =>
    {
        const get = name => row.querySelector('.sequence-'+name);
        const scans = {path:get('scans').value.trim(), format:get('format').value, time_scale:Number(get('time').value)};
        if (get('frame').value === 'tartanstar') scans.calibration_preset = 'tartanstar';
        if (get('frame').value === 'file') scans.calibration = {path:get('calibration').value.trim()};
        const trajectories = [...get('estimates').children].map(estimate =>
        {
            const field = name => estimate.querySelector('.estimate-'+name);
            const value = {label:field('label').value, path:field('path').value.trim(), format:field('format').value,
                time_scale:Number(field('time').value), time_offset_s:Number(field('offset').value),
                position_scale:Number(field('scale').value), inverse:field('inverse').checked};
            if (value.format === 'kitti') value.timestamps = field('timestamps').value.trim();
            if (value.format === 'csv')
            {
                const names = field('columns').value.split(',').map(x => x.trim());
                if (names.length !== 8 || names.some(x => !x)) throw Error('Enter eight CSV column names.');
                value.columns = Object.fromEntries(['t','x','y','z','qx','qy','qz','qw'].map((k,i) => [k,names[i]]));
                value.delimiter = field('delimiter').value;
            }
            if (field('transform').value.trim()) value.T_pose_from_body = JSON.parse(field('transform').value);
            return value;
        });
        const folder = row.sequenceDirectory.replace(/[\\/]+$/, '');
        return {name:row.querySelector('h4').textContent, sequence_directory:row.sequenceDirectory, scans, trajectories,
            output_directory:folder+'/.ground_truth', period_s:0, tls_map:element('trajectory-tls').value.trim()};
    });
}

function updateTrajectoryOutputs()
{
    const folders = [...element('trajectory-sequences').children].map(row => row.sequenceDirectory.replace(/[\\/]+$/, ''));
    element('trajectory-output-paths').replaceChildren(...folders.map(folder =>
    {
        const item = document.createElement('li'); item.className = 'filepath'; item.textContent = folder+'/ground_truth_trajectory.tum'; return item;
    }));
    element('trajectory-sequences-empty').hidden = folders.length > 0;
}

function restoreTrajectory(config)
{
    // Only restore the public batch form; internal development job configs stay out of it.
    if (config.version !== 2) return;
    element('trajectory-tls').value = config.tls_map || '';
    element('trajectory-workers').value = config.workers || '';
    element('trajectory-sequences').replaceChildren();
    for (const sequence of config.sequences || []) addTrajectorySequence(sequence);
    updateTrajectoryOutputs();
}

function saveTrajectoryDraft()
{
    clearTimeout(trajectory_save_timer);
    trajectory_save_timer = setTimeout(async () =>
    {
        try { await api('trajectory/draft', {version:2, tls_map:element('trajectory-tls').value.trim(),
            workers:Number(element('trajectory-workers').value), sequences:trajectorySequences()}); }
        catch (_) { /* Save complete settings on the next edit. */ }
    }, 600);
}

function trajectoryJobRow(job)
{
    const row = document.createElement('article'); row.className = 'registration-job';
    const name = document.createElement('h3'); name.textContent = job.name;
    const state = document.createElement('p'); state.className = 'state'; state.textContent = `${job.status} · ${job.stage || ''}`;
    const path = document.createElement('p'); path.className = 'detail filepath';
    path.textContent = job.config?.sequence_directory ? job.config.sequence_directory+'/ground_truth_trajectory.tum' : job.source_file || job.output_directory;
    row.append(name,state,path);
    const actions = document.createElement('div'); actions.className = 'registration-actions result-actions';
    if (job.viewer_available)
    {
        const link = document.createElement('a'); link.className = 'button primary review-result'; link.textContent = 'Open trajectory viewer';
        link.href = `/trajectory-viewer.html?job=${encodeURIComponent(job.id)}#${token}`; link.target = '_blank'; link.rel = 'noopener'; actions.append(link);
    }
    actions.append(button('Open folder', () => api(`trajectory/${job.id}/folder`, {})));
    if (!job.managed_externally && job.config?.scans && ['failed','cancelled','interrupted'].includes(job.status))
    {
        const resume = button('Resume', async () =>
        {
            const result = await api('trajectory/start',{resume_id:job.id});
            for (const id of result.ids) trajectory_batch_ids.add(id);
            await refreshTrajectory();
        });
        resume.disabled = trajectory_state.running; actions.append(resume);
    }
    row.append(actions); return row;
}

async function refreshTrajectory()
{
    trajectory_state = await api('trajectory');
    if (!trajectory_initialized) { restoreTrajectory(trajectory_state.draft || {}); trajectory_initialized = true; }
    if (trajectory_state.running)
        for (const job of trajectory_state.jobs)
            if (!job.managed_externally && ['running','queued'].includes(job.status)) trajectory_batch_ids.add(job.id);
    for (const id of ['trajectory-start','trajectory-check']) element(id).disabled = trajectory_state.running;
    const batch = trajectory_state.jobs.filter(job => trajectory_batch_ids.has(job.id));
    const selected = trajectory_state.jobs.filter(job => trajectory_result_ids.has(job.id));
    element('trajectory-stop').disabled = !trajectory_state.running || !batch.length;
    element('trajectory-message').textContent = batch.length ? ['running','queued','ready','completed','failed','cancelled','interrupted']
        .map(status => [status,batch.filter(job => job.status === status).length]).filter(([,n]) => n).map(([s,n]) => `${n} ${s}`).join(' · ') : '';
    element('trajectory-batch').hidden = !batch.length;
    for (const [id,jobs] of [['trajectory-batch-jobs',batch],['trajectory-jobs',selected]])
    {
        const host = element(id), signature = JSON.stringify(jobs.map(({log_tail,...job}) => job));
        if (host.dataset.signature !== signature) { host.replaceChildren(...jobs.map(trajectoryJobRow)); host.dataset.signature = signature; }
    }
    element('trajectory-results-empty').hidden = selected.length > 0;
    const latest = batch.find(job => job.status === 'running') || batch.at(-1);
    updateWorkerLog('trajectory-log', {key: latest?.id, url: latest?.log_url, tail: latest?.log_tail,
        empty: latest ? 'Waiting for worker output.' : 'No active trajectory generation in this batch.'});
}

async function openTrajectoryResults(paths)
{
    const result = await api('trajectory/open', {paths:Array.isArray(paths) ? paths : [paths]});
    trajectory_result_ids.clear();
    for (const id of result.ids) trajectory_result_ids.add(id);
    await refreshTrajectory();
}

action('trajectory-map-browse', () => pickTrajectory(element('trajectory-tls')));
action('trajectory-add-sequences', () => pickTrajectory(async paths =>
{
    const response = await api('trajectory/discover', {paths:Array.isArray(paths) ? paths : [paths]});
    const existing = new Set([...element('trajectory-sequences').children].map(row => row.sequenceDirectory));
    for (const sequence of response.sequences) if (!existing.has(sequence.sequence_directory))
    { addTrajectorySequence(sequence); existing.add(sequence.sequence_directory); }
}, 'trajectory-folders'));
action('trajectory-open', () => pickTrajectory(openTrajectoryResults,'trajectory-folder'));
action('trajectory-open-files', () => pickTrajectory(openTrajectoryResults,'trajectory-files'));
action('trajectory-clear-results', () => { trajectory_result_ids.clear(); return refreshTrajectory(); });
element('panel-trajectory').addEventListener('input', saveTrajectoryDraft);
for (const [id,inspect] of [['trajectory-check',true],['trajectory-start',false]]) action(id, async () =>
{
    const configs = trajectorySequences(), workers = Number(element('trajectory-workers').value);
    if (!element('trajectory-tls').value.trim() || !configs.length) throw Error('Choose a scanned GT map and add sequence folders.');
    if (!Number.isInteger(workers) || workers < 0 || workers > 256) throw Error('CPU threads: Auto or an integer from 1 to 256.');
    for (const config of configs)
    {
        if (!config.scans.path || !config.trajectories.length || config.trajectories.some(t => !t.path))
            throw Error(`Select scan files and input odometry for ${config.name}.`);
        if (config.scans.calibration && !config.scans.calibration.path) throw Error(`Choose the sensor calibration for ${config.name}.`);
    }
    const result = await api('trajectory/start', {configs,workers,inspect});
    trajectory_batch_ids.clear(); for (const id of result.ids) trajectory_batch_ids.add(id);
    await refreshTrajectory();
});
action('trajectory-stop', async () => { await api('trajectory/cancel',{}); await refreshTrajectory(); });
