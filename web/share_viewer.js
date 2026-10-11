(async () =>
{
    'use strict';
    const metadata = JSON.parse(document.getElementById('shared-map').textContent);
    if (!metadata.embedded) return;
    try
    {
        if (!window.blkViewer) throw Error('WebGL is unavailable. Use a browser with WebGL enabled.');
        if (!window.DecompressionStream) throw Error('This browser cannot decode the embedded map. Use a current Chrome, Edge, Firefox, or Safari.');
        document.getElementById('file').parentElement.style.display = 'none';
        let cached_index = -1, cached_bytes = null;

        async function block(index)
        {
            if (cached_index === index) return cached_bytes;
            const element = document.getElementById(`ply-block-${index}`);
            if (!element) throw Error('Embedded PLY block is missing. Download the complete HTML file again.');
            const encoded = atob(element.textContent.trim());
            const compressed = new Uint8Array(encoded.length);
            for (let i = 0; i < encoded.length; i++) compressed[i] = encoded.charCodeAt(i);
            const stream = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
            const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
            const expected = Math.min(metadata.chunk_bytes, metadata.bytes - index * metadata.chunk_bytes);
            if (bytes.length !== expected) throw Error('Embedded PLY block size mismatch.');
            element.remove();
            cached_index = index;
            cached_bytes = bytes;
            return bytes;
        }

        const source = {
            size: metadata.bytes,
            slice: (start, end) => ({
                arrayBuffer: async () =>
                {
                    end = Math.min(end, metadata.bytes);
                    const result = new Uint8Array(end - start);
                    let position = start;
                    while (position < end)
                    {
                        const index = Math.floor(position / metadata.chunk_bytes);
                        const bytes = await block(index);
                        const offset = position - index * metadata.chunk_bytes;
                        const take = Math.min(end - position, bytes.length - offset);
                        result.set(bytes.subarray(offset, offset + take), position - start);
                        position += take;
                    }
                    return result.buffer;
                }
            })
        };
        await window.blkViewer.loadPly(source, metadata.file, metadata.points);
        cached_bytes = null;
        if (window.blkViewer.count === metadata.points && metadata.focus)
        {
            window.blkViewer.camera.target = metadata.focus.target;
            window.blkViewer.camera.distance = metadata.focus.distance;
            window.blkViewer.draw();
        }
    }
    catch (error)
    {
        document.getElementById('error').textContent = error.message;
    }
})();
