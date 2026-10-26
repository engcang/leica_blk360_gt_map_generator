"""Check conversion selection, output defaults, and submission in a real browser."""

import argparse
import http.server
import pathlib
import re
import subprocess
import tempfile
import threading


PRELUDE = r"""
let mockProject = {jobs: [], point_clouds: [], running: false, output_directory: '/old/output'};
let requests = [], disconnected = false, outputFailure = false;
async function refreshRegistration() {}
async function refreshTrajectory() {}
window.fetch = async (url, options = {}) => {
    const path = String(url).replace('/api/', '');
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({path, body});
    if (disconnected) throw Error('Offline');
    let result = {}, status = 200;
    if (path === 'project') result = structuredClone(mockProject);
    else if (path === 'add') {
        const ids = []; let added = 0;
        for (const source of body.paths) {
            let job = mockProject.jobs.find(item => item.source_path === source);
            if (!job) {
                job = {id: 'scan-' + mockProject.jobs.length, source_path: source,
                       name: source.split(/[/\\]/).pop(), status: 'ready', bytes: 100, progress: 0};
                mockProject.jobs.push(job); ++added;
            }
            ids.push(job.id);
        }
        result = {ids, added, skipped: ids.length - added, errors: []};
    } else if (path === 'output') {
        if (outputFailure) { status = 400; result = {error: 'Output folder unavailable'}; }
        else mockProject.output_directory = body.path;
    } else if (path === 'start') {
        for (const job of mockProject.jobs) if (body.ids.includes(job.id)) job.status = 'queued';
    } else throw Error('Unexpected request: ' + path);
    return {ok: status === 200, status, json: async () => structuredClone(result)};
};
"""

CHECKS = r"""
addEventListener('load', async () => {
    const result = document.createElement('p'); result.id = 'conversion-test'; document.body.append(result);
    const check = (value, message) => { if (!value) throw Error(message); };
    const input = document.getElementById('output-path'), start = document.getElementById('start');
    const edit = value => { input.value = value; input.dispatchEvent(new Event('input')); };
    const settle = async () => { for (let i = 0; i < 40; ++i) await Promise.resolve(); };
    const reset = async () => {
        conversion_ids.clear(); output_dirty = false; input.value = '';
        mockProject.jobs = []; mockProject.running = false;
        mockProject.output_directory = '/old/output'; requests = [];
        await refresh();
    };
    try {
        await reset();
        check(input.value === '' && start.disabled, 'Startup restored an old output or enabled conversion');
        await addPaths(['/tmp/My scans/Setup2.blk']);
        check(input.value === '/tmp/My scans/output' && !start.disabled, 'Ubuntu output default missing');
        start.click(); await settle();
        const output = requests.find(item => item.path === 'output');
        const conversion = requests.find(item => item.path === 'start');
        check(output?.body.path === '/tmp/My scans/output', 'Destination not saved before conversion');
        check(conversion?.body.ids.join() === 'scan-0', 'Selected scans not submitted');
        check(requests.indexOf(output) < requests.indexOf(conversion), 'Conversion preceded destination save');
        check(start.disabled, 'Queued scans remained convertible');

        await reset();
        await addPaths(['C:\\Scans\\Setup2.blk']);
        check(input.value === 'C:\\Scans\\output' && !start.disabled, 'Windows output default missing');
        edit(''); check(start.disabled, 'Clearing output did not disable conversion immediately');
        await addPaths(['C:\\Scans\\Setup3.blk']);
        check(input.value === '' && start.disabled, 'Explicitly cleared output was replaced');
        edit('D:\\Custom output'); check(!start.disabled, 'Typing output did not enable conversion immediately');
        await addPaths(['C:\\Scans\\Setup4.blk']);
        check(input.value === 'D:\\Custom output', 'Chosen output was replaced');

        await reset(); edit('/tmp/custom');
        await addPaths(['/tmp/new/Setup2.blk']);
        check(input.value === '/tmp/custom' && !start.disabled, 'Output chosen before scans was replaced');
        conversion_ids.clear(); input.value = ''; output_dirty = false;
        await refresh();
        check(input.value === '' && start.disabled && element('job-count').textContent === '0', 'Old selection was restored');
        await addPaths(['/tmp/new/Setup2.blk']);
        check(input.value === '/tmp/new/output' && !start.disabled, 'Reselecting known scans did not suggest output');

        disconnected = true; await refresh(); edit('/tmp/other');
        check(start.disabled, 'Editing output enabled disconnected conversion');
        disconnected = false; await refresh();
        check(!start.disabled, 'Reconnect did not enable conversion');
        outputFailure = true; requests = []; start.click(); await settle();
        check(!requests.some(item => item.path === 'start'), 'Conversion started after destination save failed');
        check(element('notice').textContent === 'Output folder unavailable', 'Destination error was hidden');
        result.textContent = 'PASS';
    } catch (error) { result.textContent = 'FAIL: ' + error.message; }
});
"""


def run(chrome):
    web = pathlib.Path(__file__).resolve().parents[1] / 'web'
    document = (web / 'index.html').read_text(encoding='utf-8')
    document = document.replace('<script src="/app.js" defer></script>',
                                '<script>' + PRELUDE + '</script><script src="/app.js" defer></script>')
    for script in ('registration', 'additional', 'trajectory'):
        document = document.replace(f'<script src="/{script}.js" defer></script>', '')
    document = document.replace('</body>', '<script>' + CHECKS + '</script></body>')
    routes = {'/': document.encode(), '/app.js': (web / 'app.js').read_bytes(),
              '/app.css': (web / 'app.css').read_bytes()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = routes.get(self.path)
            self.send_response(200 if payload is not None else 404)
            self.send_header('Content-Type', 'application/javascript' if self.path.endswith('.js') else
                             'text/css' if self.path.endswith('.css') else 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(payload or b'')

        def log_message(self, *_args):
            pass

    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='blk-conversion-controls-') as temporary:
                result = subprocess.run([chrome, '--headless', '--no-sandbox', '--no-first-run',
                                         '--disable-background-networking', '--disable-gpu',
                                         '--user-data-dir=' + temporary, '--virtual-time-budget=3000',
                                         '--dump-dom', f'http://127.0.0.1:{server.server_port}/'],
                                        capture_output=True, text=True, timeout=45)
                if result.returncode or '<p id="conversion-test">PASS</p>' not in result.stdout:
                    detail = re.search(r'<p id="conversion-test">(.*?)</p>', result.stdout)
                    raise AssertionError(detail[1] if detail else result.stderr[-3000:])
        finally:
            server.shutdown()
            thread.join()
    print('Conversion controls passed: Ubuntu/Windows defaults, explicit output, immediate enablement, submission, and errors.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', required=True)
    run(parser.parse_args().chrome)
