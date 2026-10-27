'use strict';
let trajectory_state = null, trajectory_initialized = false, trajectory_pick = null, trajectory_signature = '', trajectory_save_timer;

async function trajectoryPicked(path)
{
    if (trajectory_pick) await trajectory_pick(path);
    trajectory_pick = null;
    saveTrajectoryDraft();
}

function pickTrajectory(target, folder = false)
{
    trajectory_pick = typeof target === 'function' ? target : path => { target.value = path; };
    return openBrowser(folder ? 'trajectory-folder' : 'trajectory-file');
}

function setTrajectoryTimeScale(select, scale = 1)
{
    const option = [...select.options].find(item => Number(item.value) === Number(scale));
    if (!option) select.add(new Option(`${scale} s/unit`, String(scale)));
    select.value = option ? option.value : String(scale);
}

function addTrajectoryEstimate(value = {})
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
    get('label').value = value.label || (element('trajectory-estimates').children.length ? 'Backend' : 'Frontend');
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
    get('remove').onclick = () => { row.remove(); updateTrajectoryRoles(); saveTrajectoryDraft(); };
    row.addEventListener('input', saveTrajectoryDraft);
    element('trajectory-estimates').append(row);
    updateTrajectoryRoles();
}

function updateTrajectoryRoles()
{
    const rows = [...element('trajectory-estimates').children];
    rows.forEach((row, i) =>
    {
        row.querySelector('.estimate-role').textContent = i ? 'Comparison only · shown with its reconstructed map.' : 'Motion source · used for initialization and scan motion correction.';
        row.querySelector('.estimate-remove').disabled = rows.length === 1;
    });
}

function updateTrajectoryScanFormat()
{
    const format = element('trajectory-scan-format').value;
    element('trajectory-scans').placeholder = {
        directory:'Folder containing timestamped scan files',
        manifest:'Scan manifest JSON file',
        tartanstar:'Extracted sequence folder, such as 2026-10-12-13-37-16'
    }[format];
    element('trajectory-scan-help').textContent = {
        directory:'Choose the dataset’s individual LiDAR scans. Filenames must be timestamps, such as 1720000000.100.ply.',
        manifest:'Choose a JSON file listing each scan’s timestamp and file path.',
        tartanstar:'Choose one extracted sequence folder containing lidar/. Sensor calibration is filled in automatically.'
    }[format];
}

function trajectoryConfig()
{
    const scans = {path:element('trajectory-scans').value.trim(), format:element('trajectory-scan-format').value, time_scale:Number(element('trajectory-scan-time').value)};
    const calibration = element('trajectory-calibration-mode').value;
    if (calibration === 'tartanstar') scans.calibration_preset = 'tartanstar';
    if (calibration === 'file' && element('trajectory-calibration').value.trim()) scans.calibration = {path:element('trajectory-calibration').value.trim()};
    const trajectories = [...element('trajectory-estimates').children].map(row =>
    {
        const get = name => row.querySelector('.estimate-'+name);
        const value = {label:get('label').value, path:get('path').value.trim(), format:get('format').value,
            time_scale:Number(get('time').value), time_offset_s:Number(get('offset').value), position_scale:Number(get('scale').value), inverse:get('inverse').checked};
        if (value.format === 'kitti') value.timestamps = get('timestamps').value.trim();
        if (value.format === 'csv')
        {
            value.delimiter = get('delimiter').value;
            const names = get('columns').value.split(',').map(x => x.trim());
            if (names.length !== 8 || names.some(x => !x)) throw Error('Enter eight CSV column names in the displayed order.');
            value.columns = Object.fromEntries(['t', 'x', 'y', 'z', 'qx', 'qy', 'qz', 'qw'].map((k, i) => [k, names[i]]));
        }
        if (get('transform').value.trim()) value.T_pose_from_body = JSON.parse(get('transform').value);
        return value;
    });
    const options = JSON.parse(element('trajectory-options').value || '{}');
    const config = {name:element('trajectory-name').value.trim() || 'tls_trajectory', tls_map:element('trajectory-tls').value.trim(),
        output_directory:element('trajectory-output').value.trim(), scans, trajectories,
        period_s:Number(element('trajectory-period').value), workers:Number(element('trajectory-workers').value), ...options};
    config.scans = {...scans, ...(options.scans || {})};
    for (const [field, key] of [['initial','initial_alignment'], ['baseline','baseline_directory'], ['cache','cache_directory']])
        if (element('trajectory-'+field).value.trim()) config[key] = element('trajectory-'+field).value.trim();
    return config;
}

function restoreTrajectory(config)
{
    for (const [field,key] of [['name','name'], ['tls','tls_map'], ['output','output_directory'], ['period','period_s'], ['workers','workers'], ['initial','initial_alignment'], ['baseline','baseline_directory'], ['cache','cache_directory']])
        if (typeof config[key] !== 'undefined' && typeof config[key] !== 'object') element('trajectory-'+field).value = config[key];
    element('trajectory-workers').value = config.workers || '';
    if (config.scans)
    {
        element('trajectory-scans').value = config.scans.path || '';
        element('trajectory-scan-format').value = config.scans.format || 'directory';
        setTrajectoryTimeScale(element('trajectory-scan-time'), config.scans.time_scale);
        element('trajectory-calibration-mode').value = config.scans.calibration_preset ? 'tartanstar' : config.scans.calibration ? 'file' : 'identity';
        element('trajectory-calibration').value = config.scans.calibration?.path || '';
    }
    element('trajectory-estimates').replaceChildren();
    for (const value of config.trajectories?.length ? config.trajectories : [{}]) addTrajectoryEstimate(value);
    const extra = {...config}, scanExtra = {...(config.scans || {})};
    for (const key of ['name','tls_map','output_directory','period_s','workers','initial_alignment','baseline_directory','cache_directory','scans','trajectories']) delete extra[key];
    for (const key of ['path','format','time_scale','calibration','calibration_preset']) delete scanExtra[key];
    if (Object.keys(scanExtra).length) extra.scans = scanExtra;
    element('trajectory-options').value = Object.keys(extra).length ? JSON.stringify(extra, null, 2) : '';
    element('trajectory-calibration-row').hidden = element('trajectory-calibration-mode').value !== 'file';
    updateTrajectoryScanFormat();
}

function saveTrajectoryDraft()
{
    clearTimeout(trajectory_save_timer);
    trajectory_save_timer = setTimeout(async () =>
    {
        try { await api('trajectory/draft', trajectoryConfig()); }
        catch (_) { /* An unfinished JSON field is saved after it becomes valid. */ }
    }, 600);
}

function visibleTrajectoryJobs(jobs)
{
    const latest = new Map();
    const identity = job =>
    {
        const config = job.config || {};
        return config.scans?.path && config.tls_map
            ? JSON.stringify([config.scans.path.replace(/[\\/]+$/, ''), config.tls_map]) : job.id;
    };
    for (const job of jobs)
        if (!job.inspect) latest.set(identity(job), job.id);
    return jobs.filter(job => !job.inspect && latest.get(identity(job)) === job.id);
}

async function refreshTrajectory()
{
    trajectory_state = await api('trajectory');
    if (!trajectory_initialized)
    {
        restoreTrajectory(trajectory_state.draft || {});
        trajectory_initialized = true;
    }
    for (const id of ['trajectory-start','trajectory-check']) element(id).disabled = trajectory_state.running;
    element('trajectory-stop').disabled = !trajectory_state.running;
    const latest = trajectory_state.jobs.at(-1);
    const monitored = trajectory_state.jobs.filter(job => job.managed_externally);
    element('trajectory-message').textContent = monitored.length
        ? ['running','queued','auditing','completed','failed','cancelled','interrupted'].map(status => [status,monitored.filter(job => job.status === status).length])
            .filter(([,count]) => count).map(([status,count]) => `${count} ${status}`).join(' · ')
        : latest?.stage || '';
    const log = element('trajectory-log');
    if (latest?.log_tail && log.textContent !== latest.log_tail) { log.textContent = latest.log_tail; log.scrollTop = log.scrollHeight; }
    const jobs = visibleTrajectoryJobs(trajectory_state.jobs);
    const signature = JSON.stringify(jobs.map(({log_tail, ...job}) => job));
    if (signature === trajectory_signature) return;
    trajectory_signature = signature;
    element('trajectory-jobs').replaceChildren(...jobs.map(job =>
    {
        const row = document.createElement('article'); row.className = `registration-job ${job.viewer_available ? 'result-ready' : 'result-pending'}`;
        const name = document.createElement('h3'); name.textContent = job.name;
        const status = document.createElement('p'); status.className = 'state'; status.textContent = `${job.status} · ${job.stage || ''}`;
        const path = document.createElement('p'); path.className = 'detail filepath'; path.textContent = job.output_directory;
        row.append(name, status, path);
        const actions = document.createElement('div'); actions.className = 'registration-actions result-actions';
        if (job.viewer_available)
        {
            const link = document.createElement('a'); link.className = 'button primary review-result'; link.textContent = 'Open trajectory viewer';
            link.href = `/trajectory-viewer.html?job=${encodeURIComponent(job.id)}#${token}`; link.target = '_blank'; link.rel = 'noopener'; actions.append(link);
        }
        actions.append(button('Open output folder', async () => { await api(`trajectory/${job.id}/folder`, {}); }));
        if (!job.managed_externally && ['failed','cancelled','interrupted'].includes(job.status))
        {
            const resume = button('Resume', async () => { await api('trajectory/start', {resume_id:job.id}); await refreshTrajectory(); });
            resume.disabled = trajectory_state.running; actions.append(resume);
        }
        row.append(actions);
        return row;
    }));
}

for (const node of document.querySelectorAll('[data-trajectory-browse]')) node.onclick = () => pickTrajectory(element(node.dataset.trajectoryBrowse));
for (const node of document.querySelectorAll('[data-trajectory-folder]')) node.onclick = () => pickTrajectory(element(node.dataset.trajectoryFolder), true);
action('trajectory-scan-browse', () => pickTrajectory(element('trajectory-scans'), element('trajectory-scan-format').value !== 'manifest'));
action('trajectory-add-estimate', () => { addTrajectoryEstimate(); saveTrajectoryDraft(); });
action('trajectory-open', () => pickTrajectory(async path => { await api('trajectory/open', {path}); await refreshTrajectory(); }, true));
element('trajectory-calibration-mode').onchange = () => { element('trajectory-calibration-row').hidden = element('trajectory-calibration-mode').value !== 'file'; saveTrajectoryDraft(); };
element('trajectory-scan-format').onchange = () =>
{
    if (element('trajectory-scan-format').value === 'tartanstar') element('trajectory-calibration-mode').value = 'tartanstar';
    else if (element('trajectory-calibration-mode').value === 'tartanstar') element('trajectory-calibration-mode').value = 'identity';
    updateTrajectoryScanFormat();
    element('trajectory-calibration-row').hidden = element('trajectory-calibration-mode').value !== 'file'; saveTrajectoryDraft();
};
element('panel-trajectory').addEventListener('input', saveTrajectoryDraft);
for (const [id, inspect] of [['trajectory-check',true], ['trajectory-start',false]]) action(id, async () =>
{
    const config = trajectoryConfig();
    if (!config.tls_map || !config.scans.path || !config.output_directory || !config.trajectories.length || config.trajectories.some(t => !t.path)) throw Error('Complete steps 1–4: scanned GT map, sequence scans, estimated trajectory, and output folder.');
    if (element('trajectory-calibration-mode').value === 'file' && !config.scans.calibration?.path) throw Error('Choose the sensor-to-body calibration file in step 2.');
    await api('trajectory/start', {config, inspect}); await refreshTrajectory();
});
action('trajectory-stop', async () => { await api('trajectory/cancel', {}); await refreshTrajectory(); });
