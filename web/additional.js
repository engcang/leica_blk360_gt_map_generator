"use strict";
let additional_reference = null, additional_ids = [], additional_poses = [], additional_error = '';
let additional_restoring = false, additional_starting = false, additional_serial = 0;
let additional_editor = null, additional_context = null, additional_editor_items = null;
let additional_pending = false, additional_lock = null, additional_signature = '', additional_log = 'No active registration.';
const additional_visible_jobs = new Set();

function additionalDraft()
{
    return {reference_poses: additional_reference?.path, reference_sha256: additional_reference?.sha256,
        cloud_ids: additional_ids.slice(), output_directory: element('additional-output').value.trim(),
        name: element('additional-name').value.trim(), voxel_m: .01};
}
function additionalControls()
{
    const busy = !!registration_state?.busy || additional_starting;
    const capable = !!registration_state?.additional_registration;
    const selected = additional_reference && additional_ids.length && element('additional-output').value.trim();
    element('additional-arrange').disabled = busy || !capable || !selected || additional_restoring || !!additional_error;
    element('additional-start').disabled = busy || !capable || !selected || additional_restoring || additional_pending || !!additional_error || registration_state?.runtime.status !== 'completed';
    element('additional-stop').disabled = !registration_state?.jobs.some(job => job.kind === 'additional' && job.status === 'running');
    element('additional-setup').hidden = registration_state?.runtime.status === 'completed';
    element('additional-message').textContent = !capable ? 'Start the updated app to use Additional Register.' : additional_error;
    element('additional-placement-status').textContent = additional_pending ? 'Saving placements...' : additional_restoring ? 'Checking saved placements...' :
        additional_poses.slice(additional_reference?.items?.length || 1).some(Boolean) ? `${additional_poses.slice(additional_reference?.items?.length || 1).filter(Boolean).length} / ${additional_ids.length} new scan placements saved` : '';
    element('additional-message').hidden = !element('additional-message').textContent;
    element('additional-placement-status').hidden = !element('additional-placement-status').textContent;
    // Freeze selection while starting, including during the editor's final save handshake.
    for (const id of ['additional-reference','additional-pick','additional-folder','additional-clear','additional-browse','additional-output','additional-name'])
        element(id).disabled = additional_starting;
    for (const node of element('additional-scans').querySelectorAll('button')) node.disabled = additional_starting;
}
async function loadAdditionalReference(path)
{
    if (additional_starting) return;
    const result = await api('registration/reference', {reference_poses:path});
    additional_reference = result;
    additional_poses = [];
    element('additional-reference-status').textContent = result.partial
        ? `${result.items.length} chunks · ${result.scans.length} scans · internal chunk poses preserved — ${result.path}`
        : `${result.scans.length} fixed scans - ${result.path}`;
    await refresh();
    await loadAdditionalPlacements();
}
async function addAdditionalPaths(paths)
{
    if (additional_starting) return;
    const result = await api('add-clouds', {paths});
    await refresh();
    for (const cloud of result.clouds) if (cloud.available && !additional_ids.includes(cloud.id)) additional_ids.push(cloud.id);
    const byId = new Map(project.point_clouds.map(cloud => [cloud.id,cloud]));
    additional_ids.sort((a,b) => (byId.get(a)?.modified_time ?? Infinity) - (byId.get(b)?.modified_time ?? Infinity) ||
        ((byId.get(a)?.path || a) < (byId.get(b)?.path || b) ? -1 : 1));
    renderAdditionalScans();
    await loadAdditionalPlacements();
    if (result.errors.length) notice(result.errors.map(item => `${item.path}: ${item.error}`).join('\n'),true);
}
function renderAdditionalScans()
{
    const rows = additional_ids.map((id,index) =>
    {
        const cloud = project.point_clouds.find(item => item.id === id);
        const row = document.createElement('div'); row.className = 'registration-scan';
        const name = document.createElement('span'); name.textContent = `${index+1}. ${cloud?.name || 'Unavailable scan'}`; name.title = cloud?.path || id;
        const remove = button('Remove', async () =>
        {
            if (additional_starting) return;
            additional_ids = additional_ids.filter(value => value !== id);
            renderAdditionalScans(); await loadAdditionalPlacements();
        });
        row.append(name,remove); return row;
    });
    element('additional-scans').replaceChildren(...rows);
    additionalControls();
}
async function loadAdditionalPlacements()
{
    const serial = ++additional_serial, draft = additionalDraft();
    additional_error = ''; additional_poses = []; additional_restoring = false;
    if (!draft.reference_poses || !draft.cloud_ids.length || !draft.output_directory) { additionalControls(); return false; }
    additional_restoring = true; additionalControls();
    try
    {
        const saved = await api('registration/placements/load',draft);
        if (serial !== additional_serial || JSON.stringify(draft) !== JSON.stringify(additionalDraft())) return false;
        additional_poses = saved.initial_guesses;
        return true;
    }
    catch (error) { if (serial === additional_serial) additional_error = error.message; throw error; }
    finally { if (serial === additional_serial) { additional_restoring = false; additionalControls(); } }
}
function refreshAdditional()
{
    for (const job of registration_state.jobs) if (job.kind === 'additional' && job.status === 'running') additional_visible_jobs.add(job.id);
    const jobs = registration_state.jobs.filter(job => additional_visible_jobs.has(job.id));
    const signature = JSON.stringify([jobs,registration_state.busy,registration_starting,[...chunk_pending]]);
    if (signature !== additional_signature)
    {
        additional_signature = signature;
        element('additional-jobs').replaceChildren(...jobs.slice().reverse().map(renderRegistrationJob));
    }
    if (jobs.some(job => job.status === 'running')) additional_log = registration_state.log || 'Waiting for worker output.';
    else if (!registration_state.busy && additional_log !== 'No active registration.' && registration_state.log && jobs.some(job => job.id === registration_state.log_job_id)) additional_log = registration_state.log;
    const log = element('additional-log');
    log.textContent = additional_log; log.scrollTop = log.scrollHeight;
    additionalControls();
}
async function openAdditionalEditor()
{
    if (additional_editor && !additional_editor.closed) { additional_editor.focus(); return; }
    if (!await loadAdditionalPlacements()) throw Error('Select a reference, new scans and a separate output folder first.');
    additional_context = additionalDraft();
    additional_editor_items = [...structuredClone(additional_reference.items || [additional_reference.item]), ...additional_ids.map((id,index) =>
    {
        const cloud = project.point_clouds.find(item => item.id === id);
        return {id,name:`${index+1}. ${cloud.name}`,scans:[{name:cloud.name,points:cloud.valid_points,base:identity_pose,url:`/api/placement-sample/${id}`}]};
    })];
    additional_editor = window.open(`placement.html#${token}`,'_blank');
    if (!additional_editor) throw Error('Allow a new tab to open the placement editor.');
}
window.addEventListener('message', async event =>
{
    if (event.origin !== location.origin || event.source !== additional_editor) return;
    const type = event.data?.type;
    if (type === 'placement-locked') { additional_lock?.(event.data); return; }
    if (type === 'placement-ready')
    {
        try
        {
            const saved = await api('registration/placements/load',additional_context);
            const items = additional_editor_items.map((item,index) => ({...item,pose:saved.initial_guesses[index]}));
            event.source.postMessage({type:'placement-input',items,context:additional_context,comparison_references:saved.comparison_references},location.origin);
        }
        catch (error) { event.source.postMessage({type:'placement-error',error:error.message},location.origin); }
    }
    if (type === 'placement-saving') { additional_pending = true; additionalControls(); }
    if (type === 'placement-autosaved')
    {
        additional_pending = false;
        try { await loadAdditionalPlacements(); } catch (error) { notice(error.message,true); }
    }
});
async function startAdditional()
{
    if (additional_starting || registration_state?.busy) return;
    additional_starting = true; additionalControls();
    const editor = additional_editor, before = JSON.stringify(additionalDraft());
    let started = false;
    try
    {
        if (editor && !editor.closed)
        {
            await new Promise((resolve,reject) =>
            {
                const timer = setTimeout(() => { additional_lock = null; reject(Error('Editor did not confirm its final save. Keep it open and try again.')); },30000);
                additional_lock = message => { clearTimeout(timer); additional_lock = null; message.error ? reject(Error(message.error)) : resolve(); };
                editor.postMessage({type:'placement-lock'},location.origin);
            });
        }
        if (!await loadAdditionalPlacements() || before !== JSON.stringify(additionalDraft())) throw Error('Selection changed while checking saved placements. Try again.');
        const previous = new Set(registration_state.jobs.map(job => job.id));
        await api('registration/start',{...additionalDraft(),initial_guesses:additional_poses});
        started = true; additional_log = 'Waiting for worker output.';
        const current = await api('registration');
        for (const job of current.jobs) if (!previous.has(job.id)) additional_visible_jobs.add(job.id);
    }
    finally
    {
        additional_starting = false;
        if (editor && !editor.closed) editor.postMessage({type:started ? 'placement-started' : 'placement-unlock'},location.origin);
        if (started) { additional_editor = null; additional_pending = false; }
        await refreshRegistration();
    }
}
action('additional-reference', () => openBrowser('additional-reference'));
action('additional-pick', () => openBrowser('additional-ply'));
action('additional-folder', () => openBrowser('additional-folder'));
action('additional-browse', () => openBrowser('additional-output'));
action('additional-result', () => openBrowser('additional-result'));
action('additional-arrange',openAdditionalEditor);
action('additional-start',startAdditional);
action('additional-stop',async () => { await api('registration/cancel',{}); await refreshRegistration(); });
action('additional-setup',() => { selectTab('register'); element('registration-setup').open = true; element('registration-setup').scrollIntoView(); });
action('additional-clear',async () =>
{
    additional_ids = []; additional_reference = null; additional_poses = [];
    element('additional-reference-status').textContent = 'Select relative_poses.json or partial_chunks.json.';
    renderAdditionalScans(); await loadAdditionalPlacements();
});
element('additional-output').addEventListener('input',() => loadAdditionalPlacements().catch(error => notice(error.message,true)));
additionalControls();
