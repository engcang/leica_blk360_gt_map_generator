'use strict';
let registration_state = null,
    registration_ids = [],
    registration_loaded = false,
    registration_signature = '',
    draft_timer,
    python_dirty = false;
const initial_placements = new Map();
let placement_window = null, placement_context = null;
let placement_automatic = false, placement_saving = false;
const identity_pose = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]];
const resume_pairs = new Map();
const registration_visible_jobs = new Set();

function registrationDraft()
{
    return {
        cloud_ids: registration_ids.slice(),
        ignore_saved_placements: placement_automatic,
        initial_guesses: registration_ids.map(id => initial_placements.get(id) || null),
        name: element('map-name').value.trim(),
        output_directory: element('map-output').value.trim(),
        voxel_m: Number(element('map-voxel').value),
        additional_pairs: element('map-pairs').value.trim()
    };
}

function updateResultNames()
{
    const name = element('map-name').value.trim() || 'reference_map';
    element('map-filename').textContent = `${name}_1cm.ply (on demand)`;
    element('map-poses-filename').textContent = 'relative_poses.json';
    element('map-review-folder').textContent = 'result_review_and_manage.html + html_source/';
}

function saveRegistrationDraft()
{
    updateResultNames();
    clearTimeout(draft_timer);
    draft_timer = setTimeout(() => api('registration/draft', registrationDraft()).catch(error => notice(error.message, true)), 300);
}

function addRegistrationScans(clouds)
{
    for (const cloud of clouds)
    {
        if (cloud.available && !registration_ids.includes(cloud.id)) registration_ids.push(cloud.id);
    }
    const byId = new Map(project.point_clouds.map(cloud => [cloud.id, cloud]));
    registration_ids.sort((a, b) =>
    {
        const first = byId.get(a), second = byId.get(b);
        const difference = (first?.modified_time ?? Infinity) - (second?.modified_time ?? Infinity);
        if (difference) return difference;
        const firstPath = first?.path || a, secondPath = second?.path || b;
        return firstPath < secondPath ? -1 : firstPath > secondPath ? 1 : 0;
    });
    if (registration_ids.some(id => initial_placements.has(id))) placement_automatic = false;
    renderRegistrationScans();
    saveRegistrationDraft();
}

function renderRegistrationScans()
{
    // Poses belong to scan IDs, not row positions. Selection edits never erase them.
    updatePlacementStatus();
    const rows = registration_ids.map((id, index) =>
    {
        const cloud = project.point_clouds.find(item => item.id === id);
        const row = document.createElement('div');
        row.className = 'registration-scan';
        const name = document.createElement('span');
        name.textContent = `${index + 1}. ${cloud?.name || 'Unavailable scan'}`;
        name.title = cloud?.path || id;
        if (!cloud?.available) name.className = 'state failed';
        const actions = document.createElement('div');
        const move = offset =>
        {
            [registration_ids[index], registration_ids[index + offset]] = [registration_ids[index + offset], registration_ids[index]];
            renderRegistrationScans();
            saveRegistrationDraft();
        };
        const up = button('Up', () => move(-1));
        up.disabled = index === 0;
        up.setAttribute('aria-label', `Move ${cloud?.name || 'scan'} up`);
        const down = button('Down', () => move(1));
        down.disabled = index === registration_ids.length - 1;
        down.setAttribute('aria-label', `Move ${cloud?.name || 'scan'} down`);
        const remove = button('Remove', () =>
        {
            registration_ids.splice(index, 1);
            renderRegistrationScans();
            saveRegistrationDraft();
        });
        actions.append(up, down, remove);
        row.append(name, actions);
        return row;
    });
    element('registration-scans').replaceChildren(...rows);
    if (!rows.length)
    {
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'Select PLY files or a folder. Input selections are kept only on this page.';
        element('registration-scans').append(empty);
    }
    element('registration-count').textContent = registration_ids.length;
    registrationControls();
}

function registrationControls()
{
    const busy = registration_state?.busy;
    element('placement-open').disabled = !!busy || registration_ids.length < 2;
    element('placement-clear').disabled = !!busy || !registration_ids.some(id => initial_placements.has(id));
    element('placement-load').disabled = !!busy || registration_ids.length < 2;
    element('registration-start').disabled = !registration_state || busy || placement_saving || registration_state.runtime.status !== 'completed' || registration_ids.length < 2;
    element('registration-stop').disabled = !busy;
    element('runtime-check').disabled = element('runtime-install').disabled = !!busy;
}

function renderRegistrationJob(job)
{
    const row = document.createElement('article');
    row.className = `registration-job ${job.status === 'completed' ? 'result-ready' : 'result-pending'}`;
    row.dataset.registrationId = job.id;
    const heading = document.createElement('h3');
    heading.textContent = `${job.name} · ${job.voxel_m * 1000} mm · ${job.scans.length} scans`;
    const state = document.createElement('p');
    state.className = `state ${job.status}`;
    state.textContent = `${job.status.replaceAll('_', ' ')} — ${job.stage || ''}`;
    const path = document.createElement('p');
    path.className = 'detail filepath';
    path.textContent = job.poses_path || job.output_path;
    const scans = document.createElement('details');
    const title = document.createElement('summary');
    title.textContent = 'Input order and connections';
    const order = document.createElement('p');
    order.className = 'hint';
    order.textContent = job.scans.map((scan, index) => `${index + 1}: ${scan.name}`).join(' · ');
    scans.append(title, order);
    if (job.groups && job.status === 'needs_connections')
    {
        const groups = document.createElement('p');
        groups.textContent = `Disconnected groups (row numbers): ${job.groups.map(group => `[${group.join(', ')}]`).join(' / ')}`;
        scans.append(groups);
        scans.open = true;
    }
    if (job.additional_pairs)
    {
        const pairs = document.createElement('p');
        pairs.className = 'hint';
        pairs.textContent = `Additional pairs tried: ${job.additional_pairs.replace(/^,/, '')}`;
        scans.append(pairs);
    }
    const actions = document.createElement('div');
    actions.className = 'registration-actions result-actions';
    if (job.chunks_available && job.status !== 'completed')
    {
        const chunks = button('View chunks', () => api(`registration/chunks/${job.id}`, {}));
        chunks.className = 'primary review-result';
        const arrange = button('Arrange chunks', () =>
        {
            window.open(`placement.html?job=${encodeURIComponent(job.id)}#${token}`, '_blank');
        });
        arrange.disabled = !!registration_state.busy;
        actions.append(chunks, arrange);
    }
    else if (job.status === 'needs_connections')
    {
        const build = button('Build chunk review', async () =>
        {
            await api('registration/start', {resume_id: job.id, review_only: true});
            await refreshRegistration();
        });
        build.disabled = !!registration_state.busy || registration_state.runtime.status !== 'completed';
        actions.append(build);
    }
    if (job.status === 'completed')
    {
        const review = button('Review and manage', () => api(`registration/review/${job.id}`, {}));
        review.className = 'primary review-result';
        actions.append(review);
        const detail = document.createElement('span');
        detail.className = 'hint';
        detail.textContent = 'Poses saved. Generate a 1 cm map, raw map, or share HTML from the management page.';
        actions.append(detail);
        if (job.export)
        {
            const progress = document.createElement('p');
            progress.className = 'hint';
            progress.textContent = `File generation: ${job.export.status} - ${job.export.stage || ''}`;
            row.append(progress);
        }
    }
    else if (['needs_connections', 'failed', 'cancelled', 'interrupted'].includes(job.status))
    {
        const pairs = document.createElement('input');
        pairs.placeholder = 'Optional extra pairs, e.g. 1:4,2:5';
        pairs.setAttribute('aria-label', `Additional pairs for ${job.name}`);
        pairs.value = resume_pairs.get(job.id) || '';
        pairs.oninput = () => resume_pairs.set(job.id, pairs.value);
        const resume = button('Resume', async () =>
        {
            await api('registration/start',
            {
                resume_id: job.id,
                additional_pairs: pairs.value
            });
            registration_visible_jobs.add(job.id);
            resume_pairs.delete(job.id);
            await refreshRegistration();
        });
        resume.disabled = !!registration_state.busy || registration_state.runtime.status !== 'completed';
        actions.append(pairs, resume);
    }
    actions.append(button('Open output folder', () => api(`registration/open/${job.id}`,
    {})));
    const initialization = document.createElement('p');
    initialization.className = 'hint';
    initialization.textContent = job.initial_placement_count === undefined
        ? 'Initial placement count: unavailable for this older job.'
        : `Initial placements used by this job: ${job.initial_placement_count} / ${job.scans.length} scans.`;
    row.append(heading, state, initialization, path, actions, scans);
    return row;
}

async function refreshRegistration()
{
    try
    {
        registration_state = await api('registration');
        for (const job of registration_state.jobs) if (job.status === 'running') registration_visible_jobs.add(job.id);
        const runtime = registration_state.runtime;
        if (!python_dirty) element('registration-python').value = registration_state.python;
        element('runtime-state').textContent = runtime.status === 'completed' ? 'Ready' : runtime.status.replaceAll('_', ' ');
        element('runtime-message').textContent = runtime.stage || 'Check an existing Python environment or install registration packages.';
        if (!registration_loaded)
        {
            registration_loaded = true;
            const draft = registration_state.draft ||
            {};
            registration_ids = [];
            element('map-name').value = 'reference_map';
            element('map-voxel').value = '0.01';
            element('map-output').value = '';
            element('map-pairs').value = '';
            updateResultNames();
            element('registration-setup').open = runtime.status !== 'completed';
            renderRegistrationScans();
        }
        registrationControls();
        const visible_jobs = registration_state.jobs.filter(job => registration_visible_jobs.has(job.id) || job.status === 'running');
        const signature = JSON.stringify([visible_jobs, registration_state.busy, runtime.status]);
        if (signature !== registration_signature)
        {
            registration_signature = signature;
            element('registration-jobs').replaceChildren(...[...visible_jobs].reverse().map(renderRegistrationJob));
            if (!visible_jobs.length)
            {
                const empty = document.createElement('p');
                empty.className = 'results-empty';
                empty.textContent = 'Your current result will appear here. Open relative_poses.json or partial_chunks.json to revisit a previous result.';
                element('registration-jobs').append(empty);
            }
        }
        element('registration-log').textContent = visible_jobs.some(job => job.status === 'running') ? (registration_state.log || 'Waiting for worker output.') : 'No active registration. Open a result explicitly to review previous work.';
        if (registration_state.storage_error) notice(registration_state.storage_error, true);
    }
    catch (error)
    {
        element('runtime-message').textContent = error.message;
        element('registration-start').disabled = true;
    }
}

for (const id of ['map-name', 'map-output', 'map-pairs', 'map-voxel']) element(id).addEventListener('input', saveRegistrationDraft);
element('registration-python').oninput = () =>
{
    python_dirty = true;
};
action('registration-pick', () => openBrowser('registration-ply'));
action('registration-folder', () => openBrowser('registration-folder'));
action('map-browse', () => openBrowser('map-output'));
action('registration-clear', () =>
{
    registration_ids = [];
    renderRegistrationScans();
    saveRegistrationDraft();
});
for (const id of ['runtime-check', 'runtime-install']) action(id, async () =>
{
    await api('registration/runtime',
    {
        python: element('registration-python').value.trim(),
        install: id === 'runtime-install'
    });
    python_dirty = false;
    await refreshRegistration();
});
action('registration-start', async () =>
{
    clearTimeout(draft_timer);
    const draft = registrationDraft();
    // The server rejects lost/stale placements before creating a job.
    await api('registration/draft', draft);
    const previous = new Set((registration_state?.jobs || []).map(job => job.id));
    await api('registration/start', draft);
    const current = await api('registration');
    for (const job of current.jobs) if (!previous.has(job.id)) registration_visible_jobs.add(job.id);
    await refreshRegistration();
});
action('registration-stop', async () =>
{
    await api('registration/cancel',
    {});
    notice('Stopping registration. Completed scan caches will remain available for resume.');
    await refreshRegistration();
});

action('open-registration-result', () => openBrowser('registration-result'));

function updatePlacementStatus()
{
    const placed = registration_ids.filter(id => initial_placements.has(id)).length;
    element('placement-status').textContent = placed
        ? `${placed} / ${registration_ids.length} selected scans have initial placements. Removing or reordering scans preserves the remaining poses.`
        : placement_automatic ? 'Automatic registration explicitly selected. Saved placements are preserved on disk.'
        : 'No placements loaded. Load saved placements or arrange scans; saved poses will be checked before registration starts.';
}
action('placement-clear', () =>
{
    for (const id of registration_ids) initial_placements.delete(id);
    placement_automatic = true;
    updatePlacementStatus();
    registrationControls();
});
action('placement-open', () =>
{
    if (!element('map-output').value.trim()) throw Error('Choose an output folder before arranging scans.');
    placement_context = registrationDraft();
    placement_window = window.open(`placement.html#${token}`, '_blank');
    if (!placement_window) throw Error('Allow a new tab to open the placement editor.');
});
window.addEventListener('message', async event =>
{
    if (event.origin !== location.origin) return;
    if (event.data?.type === 'chunk-placement-started') { refreshRegistration(); return; }
    if (event.source !== placement_window) return;
    if (event.data?.type === 'placement-ready')
    {
        try
        {
        const context = placement_context;
        const saved = context.ignore_saved_placements ? null : await api('registration/placements/load', context);
        const items = context.cloud_ids.map((id, row) =>
        {
            const cloud = project.point_clouds.find(item => item.id === id);
            return {id, name: `${row + 1}. ${cloud.name}`, pose: initial_placements.get(id) || saved?.initial_guesses[row] || null,
                scans: [{name: cloud.name, points: cloud.valid_points, base: identity_pose,
                    url: `/api/placement-sample/${id}`} ]};
        });
        event.source.postMessage({type: 'placement-input', items, context}, location.origin);
        }
        catch (error) { event.source.postMessage({type:'placement-error', error:error.message}, location.origin); }
    }
    if (event.data?.type === 'placement-save')
    {
        placement_saving = true;
        registrationControls();
        try
        {
            const values = event.data.items;
            if (!Array.isArray(values) || values.length !== placement_context.cloud_ids.length ||
                values.some((v,i) => v.id !== placement_context.cloud_ids[i] || (v.pose !== null &&
                    (!Array.isArray(v.pose) || v.pose.length !== 4 || !v.pose.every(Number.isFinite)))))
                throw Error('Invalid placement data. Nothing was applied. Keep the editor open.');
            const saved = await api('registration/placements/save', {...placement_context,
                initial_guesses: values.map(value => value.pose)});
            // Apply by ID even if rows were removed or reordered while the editor was open.
            for (const value of values)
            {
                if (value.pose) initial_placements.set(value.id, value.pose.slice());
                else initial_placements.delete(value.id);
            }
            placement_automatic = false;
            updatePlacementStatus();
            event.source.postMessage({type:'placement-saved', path:saved.path}, location.origin);
        }
        catch (error)
        {
            event.source.postMessage({type:'placement-error', error:error.message}, location.origin);
            notice(error.message, true);
        }
        finally { placement_saving = false; registrationControls(); }
    }
});

async function loadSavedPlacements()
{
    const selection = registration_ids.slice();
    const output = element('map-output').value.trim();
    if (selection.length < 2 || !output) return;
    const saved = await api('registration/placements/load', registrationDraft());
    if (output !== element('map-output').value.trim() || selection.join(',') !== registration_ids.join(',')) return;
    let restored = 0;
    saved.initial_guesses.forEach((pose, index) =>
    {
        if (pose) { initial_placements.set(selection[index], pose); restored++; }
        else initial_placements.delete(selection[index]);
    });
    if (restored) placement_automatic = false;
    updatePlacementStatus(); registrationControls();
    notice(restored ? `Restored ${restored} placements from ${saved.path}` : 'No saved placements for the selected scans in this output folder.');
}
action('placement-load', loadSavedPlacements);
