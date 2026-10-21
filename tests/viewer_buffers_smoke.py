"""Exercise full-cloud uploads across GPU buffers in an isolated headless browser."""

import argparse
import pathlib
import subprocess
import tempfile


def run(chrome):
    root = pathlib.Path(__file__).resolve().parents[1]
    web = root / "web"
    prelude = """<script>
window.uploadSizes = []; window.pointDraws = []; window.failUpload = false;
const originalUpload = WebGLRenderingContext.prototype.bufferData;
WebGLRenderingContext.prototype.bufferData = function(target, data, usage) {
    const bytes = typeof data === 'number' ? data : data.byteLength;
    if (bytes > 16 * 1024 * 1024) throw Error('Oversized GPU buffer');
    if (bytes > 1024) {
        window.uploadSizes.push(bytes);
        if (window.failUpload) throw Error('Simulated GPU allocation failure');
    }
    return originalUpload.call(this, target, data, usage);
};
const originalDraw = WebGLRenderingContext.prototype.drawArrays;
WebGLRenderingContext.prototype.drawArrays = function(mode, start, count) {
    if (mode === this.POINTS) window.pointDraws.push(count);
    return originalDraw.call(this, mode, start, count);
};
</script>"""
    checks = r"""<script>
addEventListener('load', async () => {
    const check = (value, message) => { if (!value) throw Error(message); };
    const result = document.createElement('p'); result.id = 'buffer-test'; document.body.append(result);
    try {
        check(window.blkViewer, 'Viewer did not initialize');
        const sourceFrom = (header, body) => {
            const prefix = new TextEncoder().encode(header), bytes = new Uint8Array(prefix.length + body.byteLength);
            bytes.set(prefix); bytes.set(new Uint8Array(body.buffer || body), prefix.length);
            return {size: bytes.length, slice: (start, end) => ({arrayBuffer: async () => bytes.slice(start, end).buffer})};
        };
        const count = 1048576 + 37;
        const header = `ply\nformat binary_little_endian 1.0\nelement vertex ${count}\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n`;
        const raw = new ArrayBuffer(count * 14), view = new DataView(raw);
        for (let i = 0; i < count; ++i) {
            view.setFloat32(i * 14, (i % 100) * .01, true);
            view.setFloat32(i * 14 + 4, (Math.floor(i / 100) % 100) * .01, true);
            view.setFloat32(i * 14 + 8, (Math.floor(i / 10000) % 100) * .01, true);
            view.setUint16(i * 14 + 12, i % 65536, true);
        }
        const source = sourceFrom(header, raw);
        await window.blkViewer.loadPly(source, 'multi-buffer.ply', count);
        check(document.getElementById('error').textContent === '', 'Load error');
        check(window.blkViewer.count === count, 'Lost points across buffers');
        window.pointDraws = [];
        window.blkViewer.draw();
        check(JSON.stringify(window.pointDraws) === JSON.stringify([1048576, 37]), 'Incorrect full draw batches');
        document.getElementById('density').value = '10';
        window.pointDraws = [];
        window.blkViewer.draw();
        check(window.pointDraws[0] === Math.ceil(count / 10), 'Density crosses buffer boundaries incorrectly');
        check(Number(document.body.dataset.loaded) === count, 'Density discarded loaded points');
        window.failUpload = true;
        await window.blkViewer.loadPly(source, 'failed.ply', count);
        check(window.blkViewer.count === 0 && document.body.dataset.loaded === '0', 'Failed load retained partial cloud');
        check(document.getElementById('error').textContent.includes('Simulated GPU allocation failure'), 'Underlying error was hidden');
        window.failUpload = false;
        const rgbHeader = 'ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n';
        const rgb = new Uint8Array(15); rgb.set([10, 20, 30], 12);
        await window.blkViewer.loadPly(sourceFrom(rgbHeader, rgb), 'rgb.ply', 1);
        check(window.blkViewer.count === 1 && document.getElementById('color').textContent === 'RGB', 'RGB reload failed');
        result.textContent = 'PASS';
    } catch (error) { result.textContent = 'FAIL: ' + error.message; }
});
</script>"""
    document = (web / "viewer.html").read_text(encoding="utf-8")
    document = document.replace('<link rel="stylesheet" href="viewer.css"><script src="viewer.js" defer></script>',
                                '<style>' + (web / "viewer.css").read_text() + '</style>' + prelude)
    document = document.replace('</body>', '<script>' + (web / "viewer.js").read_text() + '</script>' + checks + '</body>')
    with tempfile.TemporaryDirectory(prefix="blk-viewer-buffers-") as temporary:
        directory = pathlib.Path(temporary)
        page = directory / "test.html"
        page.write_text(document, encoding="utf-8")
        result = subprocess.run([chrome, '--headless', '--no-sandbox', '--no-first-run',
                                 '--disable-background-networking', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
                                 '--user-data-dir=' + str(directory / 'profile'), '--virtual-time-budget=15000',
                                 '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=60)
        if result.returncode or '<p id="buffer-test">PASS</p>' not in result.stdout:
            import re
            detail = re.search(r'<p id="buffer-test">(.*?)</p>', result.stdout)
            raise AssertionError(detail[1] if detail else result.stderr[-3000:])
    print('Viewer buffers passed: every point drawn, density counts, failed-load cleanup, RGB reload.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', required=True)
    run(parser.parse_args().chrome)
