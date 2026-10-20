'use strict';
let registration_state = null,
    registration_ids = [],
    registration_loaded = false,
    registration_signature = '',
    draft_timer,
    python_dirty = false;
let initial_placements = new Map();
const placement_maps = new Map([['', initial_placements]]);
let placement_output = '', placement_restore_error = '', registration_starting = false;
function selectPlacementOutput()
{
    const output = element('map-output').value.trim();
    if (output === placement_output) return;
    placement_output = output;
    if (!placement_maps.has(output)) placement_maps.set(output, new Map());
    initial_placements = placement_maps.get(output);
    placement_restore_error = '';
}
let placement_window = null, placement_context = null;
const placement_pending_outputs = new Set();
const chunk_windows = new Map(), chunk_pending = new Set(), chunk_locks = new Map();
let latest_worker_log = 'No active registration.';
const placementSaving = () => placement_pending_outputs.has(element('map-output').value.trim());
let placement_restoring = false;
let placement_restore_serial = 0;
const identity_pose = [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]];
const registration_visible_jobs = new Set();

function registrationDraft()
{
    selectPlacementOutput();
    return {
        cloud_ids: registration_ids.slice(),
        initial_guesses: registration_ids.map(id => initial_placements.get(id) || null),
        name: element('map-name').value.trim(),
        output_directory: element('map-output').value.trim(),
        voxel_m: Number(element('map-voxel').value)
    };
}

function saveRegistrationDraft()
{
    selectPlacementOutput();

    ++placement_restore_serial;
    updatePlacementStatus();
    registrationControls();
    clearTimeout(draft_timer);
    draft_timer = setTimeout(() => loadSavedPlacements().catch(error => notice(error.message, true)), 300);
}

function defaultRegistrationOutput()
{
    if (element('map-output').value.trim()) return;
    const cloud = registration_ids.map(id => project.point_clouds.find(item => item.id === id))
        .find(item => item?.available && item.path);
    if (!cloud) return;
    const end = Math.max(cloud.path.lastIndexOf('/'), cloud.path.lastIndexOf(String.fromCharCode(92)));
    if (end < 0) return;
    element('map-output').value = cloud.path.slice(0, end + 1);
    selectPlacementOutput();
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
    defaultRegistrationOutput();
    renderRegistrationScans();
    saveRegistrationDraft();
    return loadSavedPlacements();
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
        empty.textContent = 'Select PLY files or a folder.';
        element('registration-scans').append(empty);
    }
    element('registration-count').textContent = registration_ids.length;
    registrationControls();
}

function registrationControls()
{
    const busy = registration_state?.busy || registration_starting;
    element('placement-open').disabled = !!busy || registration_ids.length < 2;
    element('registration-start').disabled = !registration_state || busy || placementSaving() || placement_restoring || registration_state.runtime.status !== 'completed' || registration_ids.length < 2 || !element('map-output').value.trim() || !!placement_restore_error;
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
    const actions = document.createElement('div');
    actions.className = 'registration-actions result-actions';
    if (job.chunks_available && job.status !== 'completed')
    {
        const chunks = button('View chunks', () => api(`registration/chunks/${job.id}`, {}));
        chunks.className = 'primary review-result';
        const arrange = button('Arrange chunks', () =>
        {
            const existing = chunk_windows.get(job.id);
            if (existing && !existing.closed) { existing.focus(); return; }
            const editor = window.open(`placement.html?job=${encodeURIComponent(job.id)}#${token}`, '_blank');
            if (!editor) throw Error('Allow a new tab to open the chunk editor.');
            chunk_windows.set(job.id, editor);
        });
        arrange.disabled = !!registration_state.busy;
        const refine = document.createElement('input');
        refine.type = 'checkbox';
        refine.checked = false;
        refine.disabled = !!registration_state.busy || !registration_state.individual_scan_refinement_option;
        const option = document.createElement('label');
        option.className = 'chunk-refinement-option';
        const caption = document.createElement('span');
        caption.textContent = 'Refine individual scans afterward (slower; changes internal chunk alignment)';
        if (!registration_state.individual_scan_refinement_option) option.title = 'Available after starting the updated app.';
        option.append(refine, caption);
        option.hidden = job.kind === "additional";
        const register = button('Register chunks', () => startChunkRegistration(job.id, false, refine.checked));
        register.className = 'primary';
        register.disabled = !!registration_state.busy || registration_starting || chunk_pending.has(job.id) || registration_state.runtime.status !== 'completed';
        actions.append(chunks, arrange, register, option);
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
        const resume = button('Resume', async () =>
        {
            await startChunkRegistration(job.id, true);
            return;
        });
        resume.disabled = !!registration_state.busy || registration_starting || chunk_pending.has(job.id) || registration_state.runtime.status !== 'completed';
        actions.append(resume);
    }
    actions.append(button('Open output folder', () => api(`registration/open/${job.id}`,
    {})));
    const initialization = document.createElement('p');
    initialization.className = 'hint';
    initialization.textContent = job.initial_placement_count === undefined
        ? 'Initial placement count: unavailable for this older job.'
        : job.kind === "additional" ? `${job.initial_placement_count} / ${job.new_scan_count} new scan placements used; original reference poses remain fixed.`
        : `Initial placements used by this job: ${job.initial_placement_count} / ${job.scans.length} scans.`;
    row.append(heading, state, initialization, path, actions, scans);
    return row;
}

async function startChunkRegistration(id, automatic = false, refineScans = false)
{
    if (registration_starting || registration_state?.busy) return;
    if (chunk_pending.has(id)) throw Error('Wait for chunk placements to finish saving.');
    registration_starting = true;
    registrationControls();
    const editor = chunk_windows.get(id);
    let started = false;
    try
    {
        if (editor && !editor.closed)
        {
            await new Promise((resolve, reject) =>
            {
                const timer = setTimeout(() => { chunk_locks.delete(id); reject(Error('The chunk editor did not respond. Keep it open and check that edits are saved.')); }, 10000);
                chunk_locks.set(id, result => { clearTimeout(timer); chunk_locks.delete(id); result.error ? reject(Error(result.error)) : resolve(); });
                editor.postMessage({type:'placement-lock'}, location.origin);
            });
        }
        const request = {resume_id: id};
        if (!automatic)
        {
            const layout = await api('registration/placement/' + encodeURIComponent(id));
            if (layout.busy) throw Error('Wait for the active registration to finish.');
            const keys = layout.items.map(item => item.id);
            const saved = await api('registration/placements/load', {resume_id: id, chunk_keys: keys});
            if (saved.initial_guesses.length !== keys.length || (saved.chunk_keys && JSON.stringify(saved.chunk_keys) !== JSON.stringify(keys)))
                throw Error('Chunks changed. Reopen Arrange chunks to inspect the current result.');
            if (saved.initial_guesses.filter(Boolean).length < 2)
                throw Error('Place at least two chunks in Arrange chunks first, or use Resume for automatic matching.');
            request.refine_individual_scans = refineScans;
            request.chunk_initial_guesses = Object.fromEntries(keys.map((key, index) => [key, saved.initial_guesses[index]]));
        }
        // The editor can report a final pending edit while these reads are in flight.
        if (chunk_pending.has(id)) throw Error('Wait for chunk placements to finish saving.');
        await api('registration/start', request);
        started = true;
        if (registration_state.jobs.find(job => job.id === id)?.kind === 'additional' && typeof additional_visible_jobs !== 'undefined') additional_visible_jobs.add(id);
        else registration_visible_jobs.add(id);
        if (registration_state.jobs.find(job => job.id === id)?.kind === 'additional' && typeof additional_log !== 'undefined') additional_log = 'Waiting for worker output.';
        else latest_worker_log = 'Waiting for worker output.';
    }
    finally
    {
        registration_starting = false;
        if (editor && !editor.closed) editor.postMessage({type: started ? 'placement-started' : 'placement-unlock'}, location.origin);
        if (started) chunk_windows.delete(id);
        await refreshRegistration();
    }
}

async function refreshRegistration()
{
    try
    {
        registration_state = await api('registration');
        for (const job of registration_state.jobs) if (job.status === 'running' && job.kind !== 'additional') registration_visible_jobs.add(job.id);
        const runtime = registration_state.runtime;
        if (!python_dirty) element('registration-python').value = registration_state.python;
        element('runtime-state').textContent = runtime.status === 'completed' ? 'Ready' : runtime.status.replaceAll('_', ' ');
        element('runtime-message').textContent = runtime.stage || 'Check an existing Python environment or install registration packages.';
        if (!registration_loaded)
        {
            registration_loaded = true;
            registration_ids = [];
            element('map-name').value = 'reference_map';
            element('map-voxel').value = '0.01';
            element('map-output').value = '';

            element('registration-setup').open = runtime.status !== 'completed';
            renderRegistrationScans();
        }
        registrationControls();
        const visible_jobs = registration_state.jobs.filter(job => registration_visible_jobs.has(job.id) || (job.status === 'running' && job.kind !== 'additional'));
        const signature = JSON.stringify([visible_jobs, registration_state.busy, runtime.status, registration_starting, [...chunk_pending]]);
        if (signature !== registration_signature)
        {
            registration_signature = signature;
            element('registration-jobs').replaceChildren(...[...visible_jobs].reverse().map(renderRegistrationJob));
            if (!visible_jobs.length)
            {
                const empty = document.createElement('p');
                empty.className = 'results-empty';
                empty.textContent = 'No result selected.';
                element('registration-jobs').append(empty);
            }
        }
        if (visible_jobs.some(job => job.status === 'running'))
            latest_worker_log = registration_state.log || 'Waiting for worker output.';
        else if (registration_state.log && latest_worker_log !== 'No active registration.' &&
            (registration_state.log_job_id === undefined || visible_jobs.some(job => job.id === registration_state.log_job_id)))
            latest_worker_log = registration_state.log;
        const log = element('registration-log');
        if (log.textContent !== latest_worker_log) log.textContent = latest_worker_log;
        log.scrollTop = log.scrollHeight;
        if (typeof refreshAdditional === 'function') refreshAdditional();
        if (registration_state.storage_error) notice(registration_state.storage_error, true);
    }
    catch (error)
    {
        element('runtime-message').textContent = error.message;
        element('registration-start').disabled = true;
    }
}

for (const id of ['map-name', 'map-output', 'map-voxel']) element(id).addEventListener('input', saveRegistrationDraft);
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
    if (registration_starting || placementSaving() || registration_state?.busy) return;
    registration_starting = true;
    registrationControls();
    clearTimeout(draft_timer);
    try
    {
        const selection = JSON.stringify(registrationDraft());
        if (!await loadSavedPlacements()) throw Error('Selection changed or is incomplete. Review selected scans and the output folder before starting.');
        // Loading poses may change guesses, but must not switch the requested job inputs.
        const before = JSON.parse(selection), draft = registrationDraft();
        delete before.initial_guesses;
        const inputs = {...draft}; delete inputs.initial_guesses;
        if (JSON.stringify(before) !== JSON.stringify(inputs)) throw Error('Registration inputs changed while checking saved placements. Review them before starting.');
        const previous = new Set((registration_state?.jobs || []).map(job => job.id));
        await api('registration/start', draft);
        const current = await api('registration');
        for (const job of current.jobs) if (!previous.has(job.id)) registration_visible_jobs.add(job.id);
        await refreshRegistration();
    }
    finally { registration_starting = false; registrationControls(); }
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
    selectPlacementOutput();
    const placed = registration_ids.filter(id => initial_placements.has(id)).length;
    element('placement-status').hidden = !registration_ids.length && !placement_restore_error;
    element('placement-status').textContent = placement_restore_error
        ? `Saved placements could not be verified: ${placement_restore_error}. Registration is blocked until they can be read.`
        : placementSaving() ? 'Saving placements...'
        : placement_restoring ? 'Checking saved placements?'
        : !registration_ids.length ? 'Select PLY files or a folder to begin.'
        : !element('map-output').value.trim() ? 'Choose a map output folder.'
        : placed
        ? `${placed} / ${registration_ids.length} saved placements`
        : 'No saved placements ? automatic initialization.';
}
function openPlacementEditor()
{
    if (placement_window && !placement_window.closed) { placement_window.focus(); return; }
    if (registration_state?.busy || registration_starting) throw Error('Wait for the active registration to finish.');
    if (registration_ids.length < 2) throw Error('Select at least two PLY scans.');
    defaultRegistrationOutput();
    if (!element('map-output').value.trim()) throw Error('Cannot determine the selected PLY folder. Choose a map output folder.');
    updatePlacementStatus();
    registrationControls();
    placement_context = registrationDraft();
    placement_window = window.open(`placement.html#${token}`, '_blank');
    if (!placement_window) throw Error('Allow a new tab to open the placement editor.');
}
action('placement-open', openPlacementEditor);
window.addEventListener('message', async event =>
{
    if (event.origin !== location.origin) return;
    if (event.data?.job && event.source === chunk_windows.get(event.data.job))
    {
        if (event.data.type === 'placement-locked') { chunk_locks.get(event.data.job)?.(event.data); return; }
        if (event.data.type === 'placement-saving') chunk_pending.add(event.data.job);
        else if (event.data.type === 'placement-autosaved') chunk_pending.delete(event.data.job);
        else return;
        await refreshRegistration();
        return;
    }
    if (event.source !== placement_window) return;
    if (event.data?.type === 'placement-ready')
    {
        try
        {
        const context = placement_context;
        const saved = await api('registration/placements/load', context);
        const items = context.cloud_ids.map((id, row) =>
        {
            const cloud = project.point_clouds.find(item => item.id === id);
            return {id, name: `${row + 1}. ${cloud.name}`, pose: saved.initial_guesses[row] || null,
                scans: [{name: cloud.name, points: cloud.valid_points, base: identity_pose,
                    url: `/api/placement-sample/${id}`} ]};
        });
        event.source.postMessage({type: 'placement-input', items, context}, location.origin);
        }
        catch (error) { event.source.postMessage({type:'placement-error', error:error.message}, location.origin); }
    }
    if (event.data?.type === 'placement-saving' || event.data?.type === 'placement-autosaved')
    {
        const output = placement_context.output_directory.trim();
        if (event.data.type === 'placement-saving')
        {
            placement_pending_outputs.add(output);
            ++placement_restore_serial;
            placement_restoring = false;
        }
        else
        {
            placement_pending_outputs.delete(output);
            if (output === element('map-output').value.trim())
            {
                try { await loadSavedPlacements(); }
                catch (error) { notice(error.message, true); }
            }
        }
        updatePlacementStatus();
        registrationControls();
    }
});

async function loadSavedPlacements()
{
    selectPlacementOutput();
    const serial = ++placement_restore_serial;
    const selection = registration_ids.slice();
    const output = element('map-output').value.trim();
    placement_restoring = false;
    if (selection.length < 2 || !output)
    {
        updatePlacementStatus();
        registrationControls();
        return false;
    }
    placement_restore_error = '';
    placement_restoring = true;
    updatePlacementStatus();
    registrationControls();
    try
    {
        const saved = await api('registration/placements/load', registrationDraft());
        if (serial !== placement_restore_serial ||
            output !== element('map-output').value.trim() || selection.join(',') !== registration_ids.join(',')) return false;
        // Only selected scans are updated; removed scans remain saved on disk.
        saved.initial_guesses.forEach((pose, index) =>
        {
            if (pose) initial_placements.set(selection[index], pose);
            else initial_placements.delete(selection[index]);
        });
        return true;
    }
    catch (error)
    {
        if (serial === placement_restore_serial) placement_restore_error = error.message;
        throw error;
    }
    finally
    {
        if (serial === placement_restore_serial)
        {
            placement_restoring = false;
            updatePlacementStatus();
            registrationControls();
        }
    }
}
