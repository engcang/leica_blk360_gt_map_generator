"""Check both product resolutions through an isolated app and reopen saved products."""
import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'mapping'))
from test_products import ProductTests
from result_page import writeReviewPage


def wait(fn):
    until = time.monotonic() + 30
    while time.monotonic() < until:
        value = fn()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError('Timed out')


def main(executable):
    with tempfile.TemporaryDirectory(prefix='blk-resolution-api-') as temporary:
        work = pathlib.Path(temporary)
        output = work/'output'; output.mkdir()
        project = work/'project'; project.mkdir()
        poses, _, _, _ = ProductTests().makeResult(output, [[0,0,0],[.012,0,0],[.023,0,0],[.024,0,0]])
        assets = output/'html_source'; assets.mkdir()
        summary = {'stations': [], 'images': []}
        (assets/'review_summary.json').write_text(json.dumps(summary))
        writeReviewPage(output)
        state = {'schema_version': 1, 'python': sys.executable, 'runtime': {'status': 'completed', 'native_backend': {'test_fixture': True}}, 'jobs': []}
        (project/'registration_jobs.json').write_text(json.dumps(state))
        process = None
        base = headers = None
        def launch():
            nonlocal process, base, headers
            process = subprocess.Popen([str(pathlib.Path(executable).resolve()), '--no-browser', '--project', str(project)], stdout=subprocess.DEVNULL)
            wait(lambda: (project/'session.json').is_file())
            session = json.loads((project/'session.json').read_text())
            base = f"http://127.0.0.1:{session['port']}"
            headers = {'X-Session-Token': session['token']}
            return session
        def api(route, body=None):
            request = urllib.request.Request(base+'/api/'+route, data=None if body is None else json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        def idle():
            state = api('registration')
            return state if not state['busy'] else None
        try:
            session = launch()
            job_id = api('registration/import', {'path': str(poses)})['id']
            for kind in ('voxel', 'voxel3cm', 'share', 'share3cm'):
                api('registration/generate/'+job_id, {'product':kind})
                job = wait(idle)['jobs'][0]
                assert job['export']['status'] == 'completed', job['export']
                product = job['products'][kind]
                assert product['available'] and product['voxel_m'] == (.03 if kind.endswith('3cm') else .01)
                if kind.startswith('voxel'):
                    cloud = api('cloud/'+product['cloud_id'])
                    assert cloud['valid_points'] == product['points']
            assert job['products']['voxel3cm']['points'] < job['products']['voxel']['points']
            html = (output/'result_review_and_manage.html').read_text()
            assert 'voxel3cm' in html and 'share3cm' in html and 'download>' not in html
            with urllib.request.urlopen(base+'/') as response:
                assert 'map-voxel' not in response.read().decode()
            api('shutdown', {}); assert process.wait(timeout=15) == 0
            launch()
            api('registration/import', {'path': str(poses)})
            products = api('registration')['jobs'][0]['products']
            assert products['voxel3cm']['cloud_id'] and products['share3cm']['available']
            print('API checks passed: 1/3 cm PLY and HTML, point-cloud viewing records, fixed registration UI, restart/reimport.')
        finally:
            if process and process.poll() is None:
                api('shutdown', {})
                process.wait(timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True)
    main(parser.parse_args().executable)
