'use strict';
const result_context = location.pathname.match(/^\/files\/result\/([0-9a-f]{64})\/([0-9a-f]{32})\//);
const product_buttons = [...document.querySelectorAll('[data-product]')];
let requesting_product = false;

async function resultApi(_path, _body)
{
    const response = await fetch(`/api/registration${_path ? '/' + _path : ''}`, {
        method: _body === undefined ? 'GET' : 'POST',
        headers: {'X-Session-Token': result_context[1], 'Content-Type': 'application/json'},
        body: _body === undefined ? undefined : JSON.stringify(_body)
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'Request failed');
    return value;
}

function resultLink(_label, _url)
{
    const link = document.createElement('a');
    link.textContent = _label;
    link.href = _url;
    link.target = '_blank';
    link.rel = 'noopener';
    return link;
}

async function refreshResult()
{
    const state = await resultApi('');
    const job = state.jobs.find(_job => _job.id === result_context[2]);
    if (!job) throw new Error('Result no longer exists in this project. Open it again in BLK Studio.');
    document.getElementById('connection-status').textContent = 'Connected to BLK Studio. Generated files are saved beside relative_poses.json. Original scans must remain at the paths stored in that JSON.';
    document.getElementById('connection-status').className = '';
    for (const button of product_buttons)
    {
        const kind = button.dataset.product;
        const product = job.products?.[kind];
        button.disabled = requesting_product || state.busy || state.runtime.status !== 'completed' || !!product?.available;
        const container = document.getElementById(`${kind}-result`);
        container.replaceChildren();
        if (product?.available)
        {
            const detail = document.createElement('p');
            detail.textContent = `${product.file} · ${Number(product.points).toLocaleString()} points · ${(product.bytes / 1e6).toFixed(1)} MB`;
            const url = `/files/result/${result_context[1]}/${job.id}/${encodeURIComponent(product.file)}`;
            container.append(detail, resultLink(kind === 'share' ? 'Open share HTML' : 'Download PLY', url));
            if (product.cloud_id)
            {
                container.append(' · ', resultLink('View in 3D', `/viewer.html?cloud=${product.cloud_id}#${result_context[1]}`));
            }
        }
    }
    const generation = job.export;
    document.getElementById('generation-status').textContent = generation ? `${generation.status}: ${generation.stage || ''}` : '';
    document.getElementById('generation-status').className = generation?.status === 'failed' ? 'error' : '';
    document.getElementById('stop-generation').hidden = !(state.busy && generation?.status === 'running');
    document.getElementById('open-output').hidden = false;
    if (state.runtime.status !== 'completed')
    {
        document.getElementById('generation-status').textContent = 'Check or install the registration runtime in BLK Studio before generating files.';
    }
}

async function performResultAction(_action)
{
    try { await _action(); await refreshResult(); }
    catch (error)
    {
        document.getElementById('generation-status').textContent = error.message;
        document.getElementById('generation-status').className = 'error';
    }
}

if (result_context && location.hostname === '127.0.0.1')
{
    for (const button of product_buttons)
    {
        button.addEventListener('click', () => performResultAction(async () =>
        {
            requesting_product = true;
            product_buttons.forEach(_button => { _button.disabled = true; });
            try { await resultApi(`generate/${result_context[2]}`, {product: button.dataset.product}); }
            finally { requesting_product = false; }
        }));
    }
    document.getElementById('stop-generation').onclick = () => performResultAction(() => resultApi('cancel', {}));
    document.getElementById('open-output').onclick = () => performResultAction(() => resultApi(`open/${result_context[2]}`, {}));
    async function pollResult()
    {
        try { await refreshResult(); }
        catch (error)
        {
            product_buttons.forEach(_button => { _button.disabled = true; });
            document.getElementById('connection-status').textContent = `${error.message} Reopen Review and manage from BLK Studio to reconnect.`;
            document.getElementById('connection-status').className = 'error';
        }
        setTimeout(pollResult, 2000);
    }
    pollResult();
}
else
{
    const saved_products = window.BLK_SAVED_PRODUCTS || {};
    const local_urls = [];
    document.getElementById('offline-help').hidden = false;
    document.getElementById('connection-status').textContent = 'Offline review: open saved files below. New file generation runs in BLK Studio.';

    function showSavedProducts(_files)
    {
        for (const url of local_urls) URL.revokeObjectURL(url);
        local_urls.length = 0;
        for (const button of product_buttons)
        {
            const kind = button.dataset.product;
            const product = saved_products[kind];
            const container = document.getElementById(`${kind}-result`);
            container.replaceChildren();
            if (!product)
            {
                container.textContent = 'No saved product recorded.';
                continue;
            }
            const selected_file = _files?.find(_file => _file.webkitRelativePath.split('/').length === 2 && _file.name === product.file && _file.size === product.bytes);
            if (_files && !selected_file)
            {
                container.textContent = `${product.file}: missing or size changed in the selected folder.`;
                continue;
            }
            const detail = document.createElement('p');
            detail.textContent = `${product.file} · ${Number(product.points).toLocaleString()} points · ${(product.bytes / 1e6).toFixed(1)} MB`;
            const url = selected_file ? URL.createObjectURL(selected_file) : encodeURIComponent(product.file);
            if (selected_file) local_urls.push(url);
            const link = resultLink(kind === 'share' ? 'Open share HTML' : 'Open PLY file', url);
            if (kind !== 'share') link.download = product.file;
            container.append(detail, link);
        }
    }

    for (const button of product_buttons)
    {
        button.disabled = false;
        button.title = 'Requires BLK Studio. Click for instructions.';
        button.addEventListener('click', () => document.getElementById('app-required').showModal());
    }
    document.getElementById('check-local-files').onclick = () => document.getElementById('local-folder').click();
    document.getElementById('local-folder').onchange = _event =>
    {
        const files = [..._event.target.files];
        if (!files.length) return;
        const correct_folder = files.some(_file => _file.name === 'relative_poses.json' && _file.webkitRelativePath.split('/').length === 2);
        if (!correct_folder)
        {
            document.getElementById('local-files-status').textContent = 'Select the output folder containing relative_poses.json.';
            return;
        }
        showSavedProducts(files);
        document.getElementById('local-files-status').textContent = 'Checked saved product filenames and sizes in the selected folder. No files were uploaded or copied.';
    };
    showSavedProducts();
}
