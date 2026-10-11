'use strict';
let registration_state = null,
    registration_ids = [],
    registration_loaded = false,
    registration_signature = '',
    draft_timer,
    python_dirty = false;
const resume_pairs = new Map();

function registrationDraft()
{
    return {
        cloud_ids: registration_ids,
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
    renderRegistrationScans();
    saveRegistrationDraft();
}

function renderRegistrationScans()
{
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
        empty.textContent = 'Add individual scans from Saved point clouds, or use the converted scans in queue order.';
        element('registration-scans').append(empty);
    }
    element('registration-count').textContent = registration_ids.length;
    registrationControls();
}

function registrationControls()
{
    const busy = registration_state?.busy;
    element('registration-start').disabled = !registration_state || busy || registration_state.runtime.status !== 'completed' || registration_ids.length < 2;
    element('registration-stop').disabled = !busy;
    element('runtime-check').disabled = element('runtime-install').disabled = !!busy;
}

function renderRegistrationJob(job)
{
    const row = document.createElement('article');
    row.className = 'registration-job';
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
    actions.className = 'registration-actions';
    if (job.status === 'completed')
    {
        actions.append(button('Review and manage', () => api(`registration/review/${job.id}`, {})));
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
            resume_pairs.delete(job.id);
            await refreshRegistration();
        });
        resume.disabled = !!registration_state.busy || registration_state.runtime.status !== 'completed';
        actions.append(pairs, resume);
    }
    actions.append(button('Open output folder', () => api(`registration/open/${job.id}`,
    {})));
    row.append(heading, state, path, scans, actions);
    return row;
}

async function refreshRegistration()
{
    try
    {
        registration_state = await api('registration');
        const runtime = registration_state.runtime;
        if (!python_dirty) element('registration-python').value = registration_state.python;
        element('runtime-state').textContent = runtime.status === 'completed' ? 'Ready' : runtime.status.replaceAll('_', ' ');
        element('runtime-message').textContent = runtime.stage || 'Check an existing Python environment or install registration packages.';
        if (!registration_loaded)
        {
            registration_loaded = true;
            const draft = registration_state.draft ||
            {};
            registration_ids = draft.cloud_ids || [];
            element('map-name').value = draft.name || 'reference_map';
            element('map-voxel').value = '0.01';
            element('map-output').value = draft.output_directory || project.output_directory || '';
            element('map-pairs').value = draft.additional_pairs || '';
            updateResultNames();
            element('registration-setup').open = runtime.status !== 'completed';
            renderRegistrationScans();
        }
        registrationControls();
        const signature = JSON.stringify([registration_state.jobs, registration_state.busy, runtime.status]);
        if (signature !== registration_signature)
        {
            registration_signature = signature;
            element('registration-jobs').replaceChildren(...[...registration_state.jobs].reverse().map(renderRegistrationJob));
        }
        element('registration-log').textContent = registration_state.log || 'No worker output yet.';
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
action('registration-converted', () =>
{
    const normalized = path => path.replace(/\\/g, '/');
    const scans = project.jobs.filter(job => job.status === 'completed').map(job => project.point_clouds.find(cloud => normalized(cloud.path) === normalized(job.output_path))).filter(Boolean);
    if (!scans.length) throw new Error('No completed conversions in this project. Use Add PLY files or Add to map instead.');
    registration_ids = [];
    addRegistrationScans(scans);
    notice('Scans selected in conversion queue order. Confirm that this matches acquisition order.');
});
action('registration-pick', () => openBrowser('registration-ply'));
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
    await api('registration/draft', draft);
    await api('registration/start', draft);
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
