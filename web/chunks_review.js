(() =>
{
    'use strict';
    const data = JSON.parse(document.getElementById('chunk-data').textContent);
    const chunks = data.chunks;
    const element = id => document.getElementById(id);
    const selector = element('chunk-select');
    const levelSelector = element('level-select');

    function option(value, text)
    {
        const node = document.createElement('option');
        node.value = value;
        node.textContent = text;
        return node;
    }

    function showAlignment()
    {
        const chunk = chunks[Number(selector.value)];
        const summary = chunk.alignment;
        const level = levelSelector.value;
        const stations = summary.stations.filter(station => level === 'all' || String(station.inferred_level) === level);
        element('stations').replaceChildren(...stations.map(station =>
        {
            const row = document.createElement('tr');
            for (const value of [station.station, station.name, `L${station.inferred_level}`, `${station.plot_xyz_m[2] >= 0 ? '+' : ''}${station.plot_xyz_m[2].toFixed(3)}`])
            {
                const cell = document.createElement('td');
                cell.textContent = value;
                row.append(cell);
            }
            row.firstChild.style.borderLeft = `8px solid ${station.color}`;
            return row;
        }));
        element('level-summary').textContent = `${stations.length} scans · ${summary.levels.length} height levels`;
        const selected = level === 'all' ? summary.images : summary.images.filter(path => path.endsWith(`level_${level.padStart(2, '0')}.png`));
        element('alignment-images').replaceChildren(...selected.map((path, index) =>
        {
            const figure = document.createElement('figure');
            const caption = document.createElement('figcaption');
            const match = path.match(/level_(\d+)\.png$/);
            const height = match && summary.levels.find(item => item.id === Number(match[1]));
            caption.textContent = height ? `L${height.id} · ${height.scanner_height_m >= 0 ? '+' : ''}${height.scanner_height_m.toFixed(2)} m · top view` : 'Alignment overview';
            const link = document.createElement('a');
            link.href = path;
            link.target = '_blank';
            link.rel = 'noopener';
            const image = document.createElement('img');
            image.src = path;
            image.alt = `Chunk ${chunk.number}: ${caption.textContent}, scan positions and names`;
            image.loading = index === 0 ? 'eager' : 'lazy';
            link.append(image);
            figure.append(caption, link);
            return figure;
        }));
    }

    function showChunk()
    {
        const index = Number(selector.value);
        const chunk = chunks[index];
        element('chunk-summary').textContent = `${chunk.rows.length} scans · ${chunk.points.toLocaleString()} preview points · chunk-local coordinates`;
        element('chunk-view').src = chunk.viewer;
        element('open-view').href = chunk.viewer;
        element('previous-chunk').disabled = index === 0;
        element('next-chunk').disabled = index === chunks.length - 1;
        levelSelector.replaceChildren(option('all', 'All levels'), ...chunk.alignment.levels.map(level =>
            option(String(level.id), `L${level.id} · ${level.scanner_height_m >= 0 ? '+' : ''}${level.scanner_height_m.toFixed(2)} m · ${level.stations.length} scans`)));
        showAlignment();
        history.replaceState(null, '', `${location.pathname}${location.search}#chunk=${chunk.number}`);
    }

    element('summary').textContent = `${chunks.length} chunks · ${chunks.reduce((count, chunk) => count + chunk.rows.length, 0)} scans`;
    selector.replaceChildren(...chunks.map((chunk, index) => option(String(index), `Chunk ${chunk.number} · ${chunk.rows.length} scans`)));
    const saved = Number(new URLSearchParams(location.hash.slice(1)).get('chunk'));
    selector.value = String(Math.max(0, chunks.findIndex(chunk => chunk.number === saved)));
    selector.onchange = showChunk;
    levelSelector.onchange = showAlignment;
    element('previous-chunk').onclick = () => { selector.value = String(Number(selector.value) - 1); showChunk(); };
    element('next-chunk').onclick = () => { selector.value = String(Number(selector.value) + 1); showChunk(); };
    showChunk();
})();
