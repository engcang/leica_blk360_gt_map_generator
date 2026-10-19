'use strict';
const element = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem('blk-session') || '';
if (token) sessionStorage.setItem('blk-session', token);
history.replaceState(null, '', '/');
let closed = false,
    adding = false,
    last_signature = '',
    last_cloud_signature = '',
    notice_timer, output_dirty = false;
let viewed_cloud_ids = new Set();
const conversion_ids = new Set();
let project = null,
    browser_state = null,
    browser_mode = 'files',
    selected = new Set();
const number = value => Number(value).toLocaleString('en-US');
const size = value => `${(value / 1024 ** 2).toFixed(1)} MiB`;
const labels = {
    ready: 'Ready',
    queued: 'Queued',
    running: 'Converting',
    completed: 'Completed',
    failed: 'Failed',
    cancelled: 'Stopped',
    interrupted: 'Interrupted'
};

function selectTab(name, focus = false)
{
    for (const tab of document.querySelectorAll('[role="tab"]'))
    {
        const active = tab.id === `tab-${name}`;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        element(tab.getAttribute('aria-controls')).hidden = !active;
        if (active && focus) tab.focus();
    }
    element('open-folder').hidden = name !== 'convert';
}
for (const tab of document.querySelectorAll('[role="tab"]'))
{
    tab.onclick = () => selectTab(tab.id.slice(4));
    tab.onkeydown = event =>
    {
        const tabs = [...document.querySelectorAll('[role="tab"]')];
        let index = tabs.indexOf(tab);
        if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
        else if (event.key === 'Home') index = 0;
        else if (event.key === 'End') index = tabs.length - 1;
        else return;
        event.preventDefault();
        selectTab(tabs[index].id.slice(4), true);
    };
}

function notice(message, error = false)
{
    element('notice').textContent = message;
    element('notice').className = error ? 'error' : '';
    element('notice').hidden = false;
    clearTimeout(notice_timer);
    notice_timer = setTimeout(() =>
    {
        element('notice').hidden = true;
    }, error ? 14000 : 5000);
}
async function api(path, body)
{
    const response = await fetch(`/api/${path}`,
    {
        method: body === undefined ? 'GET' : 'POST',
        headers:
        {
            'X-Session-Token': token,
            'Content-Type': 'application/json'
        },
        body: body === undefined ? undefined : JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
}

function action(id, handler)
{
    element(id).addEventListener('click', async () =>
    {
        try
        {
            await handler();
        }
        catch (error)
        {
            notice(error.message, true);
        }
    });
}

function button(text, handler)
{
    const node = document.createElement('button');
    node.textContent = text;
    node.onclick = async () =>
    {
        try
        {
            await handler();
        }
        catch (error)
        {
            notice(error.message, true);
        }
    };
    return node;
}

function renderJob(job)
{
    const row = document.createElement('article');
    row.className = 'job';
    row.dataset.id = job.id;
    const identity = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'filename';
    name.textContent = job.name;
    const detail = document.createElement('div');
    detail.className = 'detail';
    detail.textContent = `${size(job.bytes)}${job.result ? ` · ${number(job.result.valid_points)} points` : ''}`;
    const source = document.createElement('div');
    source.className = 'detail filepath';
    source.textContent = job.source_path;
    source.title = job.source_path;
    identity.append(name, detail, source);
    const status = document.createElement('div');
    const state = document.createElement('div');
    state.className = `state ${job.status}`;
    state.textContent = `${labels[job.status] || job.status}${job.status === 'running' ? ` · ${Math.floor(job.progress * 100)}%` : ''}`;
    const message = document.createElement('div');
    message.className = 'detail filepath';
    message.textContent = job.status === 'completed' ? job.output_path : job.message;
    status.append(state, message);
    if (['running', 'queued'].includes(job.status))
    {
        const progress = document.createElement('progress');
        progress.max = 1;
        progress.value = job.progress;
        status.append(progress);
    }
    const actions = document.createElement('div');
    actions.className = 'job-actions';
    if (job.status === 'completed')
    {
        const view = document.createElement('a');
        view.href = `/viewer.html?scan=${job.id}#${token}`;
        view.target = '_blank';
        view.rel = 'noopener';
        view.textContent = 'View 3D ↗';
        view.className = 'view';
        const download = document.createElement('a');
        download.href = `/files/${job.id}.ply?token=${token}`;
        download.download = job.name.replace(/\.blk$/i, '.ply');
        download.textContent = 'PLY ↓';
        actions.append(view, download);
    }
    if (['failed', 'cancelled', 'interrupted'].includes(job.status)) actions.append(button('Retry', async () =>
    {
        await api(`retry/${job.id}`,
        {});
        await refresh();
    }));
    if (!['running', 'queued'].includes(job.status)) actions.append(button('Remove', async () =>
    {
        await api(`remove/${job.id}`,
        {});
        await refresh();
    }));
    row.append(identity, status, actions);
    return row;
}

function renderCloud(cloud)
{
    const row = document.createElement('article');
    row.className = 'job';
    row.dataset.cloudId = cloud.id;
    const identity = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'filename';
    name.textContent = cloud.name;
    const path = document.createElement('div');
    path.className = 'detail filepath';
    path.textContent = cloud.path;
    identity.append(name, path);
    const detail = document.createElement('div');
    detail.className = 'detail';
    detail.textContent = cloud.available ? `${number(cloud.valid_points)} points · ${size(cloud.bytes)}` : cloud.error;
    const actions = document.createElement('div');
    actions.className = 'job-actions';
    if (cloud.available)
    {
        const view = document.createElement('a');
        view.href = `/viewer.html?cloud=${cloud.id}#${token}`;
        view.target = '_blank';
        view.rel = 'noopener';
        view.textContent = 'View 3D ↗';
        view.className = 'view';
        actions.append(view);
        actions.append(button('Add to map', async () => { await addRegistrationScans([cloud]); selectTab('register'); }));
    }
    else
    {
        const missing = document.createElement('span');
        missing.className = 'state failed';
        missing.textContent = 'Unavailable';
        actions.append(missing);
    }
    row.append(identity, detail, actions);
    return row;
}
async function refresh()
{
    if (closed) return;
    try
    {
        project = await api('project');
        element('connection').textContent = '● Local connection · files read in place';
        for (const job of project.jobs) if (['running', 'queued'].includes(job.status)) conversion_ids.add(job.id);
        const selectedJobs = project.jobs.filter(job => conversion_ids.has(job.id));
        element('job-count').textContent = selectedJobs.length;
        const count = status => selectedJobs.filter(job => job.status === status).length;
        element('open-folder').disabled = !element('output-path').value.trim();
        element('summary').textContent = selectedJobs.length ? `${count('completed')} completed · ${count('ready') + count('queued')} waiting · ${count('running')} processing` : 'Selected scans will appear here.';
        element('start').disabled = adding || !count('ready') || !element('output-path').value.trim();
        element('cancel').disabled = !project.running && !count('queued');
        const clouds = (project.point_clouds || []).filter(cloud => viewed_cloud_ids.has(cloud.id));
        element('cloud-count').textContent = clouds.length;
        const cloud_signature = JSON.stringify(clouds);
        if (cloud_signature !== last_cloud_signature)
        {
            last_cloud_signature = cloud_signature;
            element('clouds').replaceChildren(...clouds.map(renderCloud));
            if (!clouds.length)
            {
                const empty = document.createElement('div');
                empty.className = 'empty';
                empty.textContent = 'Select PLY files or a folder to view. Nothing is loaded automatically.';
                element('clouds').append(empty);
            }
        }
        const cloud_errors = viewed_cloud_ids.size ? (project.point_cloud_errors || []) : [];
        element('cloud-errors').hidden = !cloud_errors.length;
        element('cloud-errors').textContent = cloud_errors.map(item => `${item.path}: ${item.error}`).join('\n');
        const signature = JSON.stringify(selectedJobs);
        if (signature !== last_signature)
        {
            last_signature = signature;
            element('jobs').replaceChildren(...selectedJobs.map(renderJob));
            if (!selectedJobs.length)
            {
                const empty = document.createElement('div');
                empty.className = 'empty';
                empty.textContent = 'No scans selected. Select BLK files or a folder to get started.';
                element('jobs').append(empty);
            }
        }
        if (project.storage_error) notice(`Project save error: ${project.storage_error}`, true);
        await refreshRegistration();
        if (project.migration_notice && !sessionStorage.getItem('migration-notice'))
        {
            notice(project.migration_notice);
            sessionStorage.setItem('migration-notice', 'shown');
        }
    }
    catch (error)
    {
        element('connection').textContent = 'Disconnected · reopen BLK Studio to reconnect';
        element('start').disabled = element('cancel').disabled = true;
    }
}
async function addPaths(paths)
{
    if (adding || !paths.length) return;
    adding = true;
    element('start').disabled = true;
    element('import-status').textContent = 'Checking selected BLK files…';
    try
    {
        const result = await api('add',
        {
            paths
        });
        for (const id of result.ids) conversion_ids.add(id);
        element('import-status').textContent = `${result.added} scans added · ${result.skipped} already selected · ${result.errors.length} errors. No files copied.`;
        if (result.errors.length) notice(result.errors.map(item => `${item.path}: ${item.error}`).join('\n'), true);
    }
    finally
    {
        adding = false;
        await refresh();
    }
}
async function saveOutput()
{
    await api('output',
    {
        path: element('output-path').value.trim()
    });
    output_dirty = false;
    await refresh();
}
async function browse(path = '')
{
    element('browser-status').textContent = 'Reading folder…';
    for (const id of ['browser-path', 'browser-go', 'browser-up', 'use-folder', 'new-folder']) element(id).disabled = true;
    try
    {
        browser_state = await api('browse?path=' + encodeURIComponent(path) + (browser_mode === 'registration-result' ? '&type=json' : ['ply', 'ply-folder', 'registration-ply', 'registration-folder'].includes(browser_mode) ? '&type=ply' : ''));
        selected.clear();
        element('browser-path').value = browser_state.path;
        element('browser-up').disabled = browser_state.parent === browser_state.path;
        element('select-all').checked = false;
        const roots = browser_state.roots.map(root =>
        {
            const node = button(root.name, () => browse(root.path));
            node.className = 'quiet';
            return node;
        });
        element('browser-roots').replaceChildren(...roots);
        const rows = [];
        for (const entry of browser_state.entries)
        {
            if (!entry.directory && !['files', 'ply', 'registration-ply', 'registration-result'].includes(browser_mode)) continue;
            const row = document.createElement('div');
            row.className = 'browser-entry';
            if (entry.directory)
            {
                const open = button('▸ ' + entry.name, () => browse(entry.path));
                open.className = 'directory-entry';
                row.append(open);
            }
            else
            {
                const label = document.createElement('label');
                const check = document.createElement('input');
                check.type = 'checkbox';
                check.dataset.path = entry.path;
                check.onchange = () =>
                {
                    if (check.checked) selected.add(entry.path);
                    else selected.delete(entry.path);
                    updateSelection();
                };
                label.append(check, document.createTextNode(entry.name));
                row.append(label);
            }
            rows.push(row);
        }
        element('browser-list').replaceChildren(...rows);
        element('browser-status').textContent = rows.length ? `${rows.length} items` : 'No matching items in this folder.';
        updateSelection();
    }
    catch (error)
    {
        element('browser-status').textContent = error.message;
        browser_state = null;
        selected.clear();
        element('browser-list').replaceChildren();
        updateSelection();
    }
    finally
    {
        for (const id of ['browser-path', 'browser-go', 'new-folder']) element(id).disabled = false;
        element('browser-up').disabled = !browser_state || browser_state.parent === browser_state.path;
        element('use-folder').disabled = !browser_state;
    }
}

function updateSelection()
{
    element('use-files').disabled = !selected.size || (browser_mode === 'registration-result' && selected.size !== 1);
    element('use-files').textContent = `${browser_mode === 'registration-result' ? 'Open selected poses JSON' : ['ply', 'ply-folder', 'registration-ply', 'registration-folder'].includes(browser_mode) ? 'Open selected PLY files' : 'Add selected files'}${selected.size ? ` (${selected.size})` : ''}`;
}
async function openBrowser(mode)
{
    browser_mode = mode;
    const ply_mode = ['ply', 'ply-folder', 'registration-ply', 'registration-folder'].includes(mode);
    const output_mode = ['output', 'map-output'].includes(mode);
    element('browser-title').textContent = mode === 'registration-result' ? 'Open relative_poses.json or partial_chunks.json' : ply_mode ? 'Open existing PLY files' : output_mode ? 'Choose output folder' : mode === 'folder' ? 'Select a scan folder' : 'Select BLK files';
    element('use-files').hidden = !['files', 'ply', 'registration-ply', 'registration-result'].includes(mode);
    element('select-all').parentElement.hidden = !['files', 'ply', 'registration-ply'].includes(mode);
    element('select-all-label').textContent = ply_mode ? 'Select all PLY files' : 'Select all BLK files';
    element('use-folder').hidden = ['ply', 'registration-ply', 'registration-result'].includes(mode);
    element('new-folder').hidden = !output_mode;
    element('use-folder').textContent = output_mode ? 'Use this output folder' : ply_mode ? 'Open PLY files in this folder' : 'Add this folder';
    element('browser').showModal();
    let path = '';
    if (mode === 'output') path = element('output-path').value.trim();
    if (mode === 'map-output' && element('map-output').value) path = element('map-output').value;
    await browse(path);
}
action('pick-files', () => openBrowser('files'));
action('pick-ply', () => openBrowser('ply'));
action('pick-ply-folder', () => openBrowser('ply-folder'));
action('clear-clouds', async () =>
{
    viewed_cloud_ids.clear();
    await refresh();
});
action('pick-folder', () => openBrowser('folder'));
action('choose-output', () => openBrowser('output'));
action('close-browser', () => element('browser').close());
action('browser-go', () => browse(element('browser-path').value.trim().replace(/^"|"$/g, '')));
element('browser-path').onkeydown = event =>
{
    if (event.key === 'Enter')
    {
        event.preventDefault();
        element('browser-go').click();
    }
};
action('browser-up', () => browse(browser_state.parent));
element('select-all').onchange = event =>
{
    for (const check of element('browser-list').querySelectorAll('input[type=checkbox]'))
    {
        check.checked = event.target.checked;
        if (check.checked) selected.add(check.dataset.path);
        else selected.delete(check.dataset.path);
    }
    updateSelection();
};
async function openCloudPaths(paths, registration = false)
{
    const result = await api('add-clouds', {paths});
    if (!registration) viewed_cloud_ids = new Set(result.clouds.map(cloud => cloud.id));
    await refresh();
    if (registration) await addRegistrationScans(result.clouds);
    selectTab(registration ? 'register' : 'clouds');
    if (result.errors.length) notice(result.errors.map(item => `${item.path}: ${item.error}`).join('\n'), true);
    else notice(result.clouds.length ? `${result.clouds.length} PLY files selected. No files copied.` : 'No PLY files found in this folder.');
}

action('use-files', async () =>
{
    const paths = [...selected];
    element('browser').close();
    if (browser_mode === 'registration-result')
    {
        const result = await api('registration/import', {path: paths[0]});
        registration_visible_jobs.add(result.id);
        await refreshRegistration();
        const job = registration_state.jobs.find(item => item.id === result.id);
        if (job?.status === 'completed') await api(`registration/review/${result.id}`, {});
        else if (job?.chunks_available) await api(`registration/chunks/${result.id}`, {});
        return;
    }
    if (['ply', 'registration-ply'].includes(browser_mode)) await openCloudPaths(paths, browser_mode === 'registration-ply');
    else await addPaths(paths);
});
action('use-folder', async () =>
{
    if (!browser_state) return;
    const path = browser_state.path;
    element('browser').close();
    if (browser_mode === 'output')
    {
        element('output-path').value = path;
        await saveOutput();
    }
    else if (browser_mode === 'map-output')
    {
        element('map-output').value = path;
        saveRegistrationDraft();
        await loadSavedPlacements();
    }
    else if (['ply-folder', 'registration-folder'].includes(browser_mode)) await openCloudPaths([path], browser_mode === 'registration-folder');
    else await addPaths([path]);
});
action('new-folder', async () =>
{
    const name = prompt('New folder name', 'output');
    if (!name) return;
    const result = await api('create-folder',
    {
        parent: browser_state.path,
        name
    });
    await browse(result.path);
});
action('add-paths', () => addPaths(element('source-paths').value.split(/\r?\n/).map(path => path.trim().replace(/^"|"$/g, '')).filter(Boolean)));
element('output-path').oninput = () =>
{
    output_dirty = true;
};
action('save-output', async () =>
{
    await saveOutput();
    notice('Output folder saved.');
});
action('start', async () =>
{
    await saveOutput();
    await api('start', {ids: [...conversion_ids]});
    await refresh();
});
action('cancel', async () =>
{
    await api('cancel',
    {});
    notice('Stop requested. The current compressed block will finish before cleanup.');
    await refresh();
});
action('open-folder', async () =>
{
    await saveOutput();
    await api('open-folder', {path: element('output-path').value.trim()});
});
action('quit', async () =>
{
    if ((project?.running || registration_state?.busy) && !confirm('Stop active conversion/registration work and quit?')) return;
    await api('shutdown',
    {});
    closed = true;
    element('connection').textContent = 'App closed. You can close this tab.';
    element('start').disabled = element('cancel').disabled = true;
});
document.addEventListener('DOMContentLoaded', refresh);
setInterval(refresh, 1200);
