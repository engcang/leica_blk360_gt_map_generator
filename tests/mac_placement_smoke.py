"""Placement-only app: portable JSON round trip without a registration runtime.

Uses only Python's standard library; Python is a test dependency, not an app dependency.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True)
    parser.add_argument('--rgb', action='store_true')
    args = parser.parse_args()
    executable = Path(args.executable).resolve()
    with tempfile.TemporaryDirectory(prefix='blk mac placement ') as directory:
        root = Path(directory)
        project = root / 'project'
        sources = root / 'PLY scans 한글'
        sources.mkdir()
        paths = []
        for scan in range(4):
            path = sources / f'Setup {scan}.ply'
            channels = 'property uchar red\nproperty uchar green\nproperty uchar blue\n' if args.rgb else 'property ushort intensity\n'
            header = ('ply\nformat binary_little_endian 1.0\nelement vertex 1000\n'
                      'property float x\nproperty float y\nproperty float z\n' + channels + 'end_header\n').encode()
            path.write_bytes(header + b''.join(struct.pack('<fffBBB' if args.rgb else '<fffH',
                (i % 10) * .1, ((i // 10) % 10) * .1, (i // 100) * .1,
                *([255, 16, i % 256] if args.rgb else [i * 50])) for i in range(1000)))
            paths.append(path)
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        process = None

        def launch():
            nonlocal process
            session_file = project / 'session.json'
            session_file.unlink(missing_ok=True)
            process = subprocess.Popen([str(executable), '--no-browser', '--project', str(project)])
            for _ in range(200):
                if session_file.exists():
                    break
                if process.poll() is not None:
                    raise AssertionError('App exited before startup')
                time.sleep(.1)
            session = json.loads(session_file.read_text())
            base = f'http://127.0.0.1:{session["port"]}'

            def api(route, body=None, raw=False):
                request = urllib.request.Request(base + '/api/' + route,
                    data=None if body is None else json.dumps(body).encode(),
                    headers={'X-Session-Token': session['token']})
                with urllib.request.urlopen(request, timeout=20) as response:
                    return response.read() if raw else json.load(response)

            with urllib.request.urlopen(base + '/app-mode.js') as response:
                assert 'window.BLK_PLACEMENT_ONLY=true' in response.read().decode()
            return api

        try:
            api = launch()
            clouds = api('add-clouds', {'paths': [str(path) for path in paths]})['clouds']
            assert len(clouds) == 4
            ids = [cloud['id'] for cloud in clouds]
            assert api('placement-sample/' + ids[0] + '?start=0&stride=10', raw=True)
            assert not api('registration')['jobs']
            poses = [[0, 0, 0, 0], [1.25, 2, -3, .1, -.2, .3], [-4, 5, 6, -.4], [7, 8, 9, .5, .6, -.7]]
            references = [None, 3, 0, 1]  # Deliberately nonconsecutive references.
            request = {'cloud_ids': ids, 'initial_guesses': poses, 'comparison_references': references,
                       'output_directory': str(root / 'output folder'), 'name': 'manual', 'voxel_m': .01}
            saved = api('registration/placements/save', request)
            assert saved['initial_guesses'] == poses
            assert saved['comparison_references'] == references
            saved_path = Path(saved['path'])
            document = json.loads(saved_path.read_text())
            assert set(document['poses']) == {f'../{sources.name}/{path.name}' for path in paths}
            assert document['comparison_references'] == {
                f'../{sources.name}/{paths[i].name}': f'../{sources.name}/{paths[ref].name}'
                for i, ref in enumerate(references) if ref is not None}
            # A malformed update cannot damage the saved file.
            original = saved_path.read_bytes()
            try:
                api('registration/placements/save', {**request, 'initial_guesses': [[1, 2, 3]] * 4})
                raise AssertionError('Malformed pose accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 400
            assert saved_path.read_bytes() == original
            for route in ('registration/start', 'registration/setup', 'start', 'trajectory/start'):
                try:
                    api(route, {})
                    raise AssertionError(f'{route} unexpectedly available')
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                    assert 'manual PLY placement only' in error.read().decode()
            api('shutdown', {})
            process.wait(timeout=20)
            api = launch()
            request['cloud_ids'] = [cloud['id'] for cloud in api('add-clouds', {'paths': [str(path) for path in paths]})['clouds']]
            for name in ('initial_guesses', 'comparison_references'):
                assert api('registration/placements/load', request)[name] == request[name]
            # Copy scans + JSON together, then delete originals: no absolute fallback can help.
            relocated = root / 'moved dataset'
            relocated.mkdir()
            shutil.copytree(sources, relocated / sources.name)
            shutil.copytree(saved_path.parent, relocated / 'output folder')
            assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths] == hashes
            shutil.rmtree(sources)
            shutil.rmtree(saved_path.parent)
            moved = api('add-clouds', {'paths': [str(relocated / sources.name / path.name) for path in paths]})['clouds']
            request.update(cloud_ids=[cloud['id'] for cloud in moved], output_directory=str(relocated / 'output folder'))
            loaded = api('registration/placements/load', request)
            assert loaded['initial_guesses'] == poses
            assert loaded['comparison_references'] == references
            assert not (project / 'registration_runtime').exists()
            api('shutdown', {})
            process.wait(timeout=20)
            print(f'PASS: {executable.name}: save/restart/relocate, RPY, references, immutable inputs, placement-only API')
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == '__main__':
    main()
