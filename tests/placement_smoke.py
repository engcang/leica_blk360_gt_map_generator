"""Exercise scan/chunk placement in a real browser and native registration worker."""
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import urllib.error
import numpy as np
from playwright.sync_api import sync_playwright


def waitFor(function, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(.2)
    raise AssertionError("Timed out waiting for placement test")


def run(args):
    repo = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo / "mapping"))
    from manual_placement import placementMatrix
    from register_scans import writePly
    root = pathlib.Path(args.project).resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "sources"
    source.mkdir()
    rng = np.random.default_rng(73)
    surfaces = []
    for axis, limits in enumerate(((-3, 4), (-2, 3), (0, 2.7))):
        for level in limits:
            pts = rng.uniform([-3, -2, 0], [4, 3, 2.7], (1800, 3))
            pts[:, axis] = level
            surfaces.append(pts)
    world = np.concatenate(surfaces)
    poses = [[0, 0, 0, 0], [1.2, .5, .1, .15], [2.2, -.3, .2, -.1]]
    paths, hashes, locals_ = [], [], []
    for index, pose in enumerate(poses):
        matrix = placementMatrix(pose)
        xyz = (world - matrix[:3, 3]) @ matrix[:3, :3]
        path = source / f"Setup{[44, 511, 100][index]}.ply"
        writePly(path, xyz, np.arange(len(xyz)) * 5)
        os.utime(path, (1700000000 + index, 1700000000 + index))
        paths.append(path)
        locals_.append(xyz)
        hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
    output = root / "chunks-output"
    output.mkdir()
    report = root / "registration_records" / ("a" * 32)
    report.mkdir(parents=True)
    chunks = []
    for number, rows in enumerate(([0, 1], [2]), 1):
        scans = []
        for index in rows:
            scans.append({"row": index + 1, "name": paths[index].name, "source_path": str(paths[index]),
                          "source_sha256": hashes[index], "original_points": len(world),
                          "transform_chunk_from_scan": (placementMatrix(poses[index]) if number == 1 else np.eye(4)).tolist()})
        chunks.append({"key": f"chunk-{number}", "number": number, "rows": [i+1 for i in rows], "scans": scans})
    (output / "partial_chunks.json").write_text(json.dumps({"chunks": chunks}), encoding="utf-8")
    config = {"name": "manual_chunks", "paths": list(map(str, paths)), "voxel_m": .01,
              "output_path": str(output / "manual_chunks_1cm.ply"), "poses_path": str(output / "relative_poses.json"),
              "review_directory": str(output), "additional_pairs": ""}
    (report / "config.json").write_text(json.dumps(config), encoding="utf-8")
    job = {"id": "a" * 32, "name": "Manual chunks", "status": "needs_connections", "stage": "Test fixture",
           "report_directory": str(report), "output_path": config["output_path"], "poses_path": config["poses_path"],
           "voxel_m": .01, "scans": [{"name": p.name, "path": str(p)} for p in paths]}
    state = {"schema_version": 1, "python": sys.executable, "runtime": {"status": "unchecked"}, "jobs": [job]}
    (root / "registration_jobs.json").write_text(json.dumps(state), encoding="utf-8")
    process = subprocess.Popen([str(pathlib.Path(args.executable).resolve()), "--no-browser", "--project", str(root)],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        waitFor(lambda: (root / "session.json").exists(), 30)
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}
        def api(route, body=None, raw=False):
            request = urllib.request.Request(base + "/api/" + route, data=None if body is None else json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read() if raw else json.load(response)
        def idle():
            state = api("registration")
            return state if not state["busy"] else None
        api("registration/runtime", {"python": sys.executable, "install": False})
        assert waitFor(idle)["runtime"]["status"] == "completed"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chrome, headless=True,
                        args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/#" + session["token"])
            page.locator("#tab-register").click()
            page.evaluate("paths => openCloudPaths(paths, true)", [str(p) for p in reversed(paths)])
            page.wait_for_function("document.querySelectorAll('.registration-scan').length === 3")
            assert [line.split('. ', 1)[1] for line in page.locator('.registration-scan > span').all_text_contents()] == [p.name for p in paths]
            ids = page.evaluate("registration_ids")
            for stride in (1, 2, 5, 10, 50):
                block = np.frombuffer(api(f"placement-sample/{ids[0]}?start=1&stride={stride}", raw=True), dtype="<f4").reshape(-1,4)
                np.testing.assert_allclose(block[:, :3], locals_[0][stride::stride], atol=1e-6)
            page.locator("#map-output").fill(str(root / "raw-placements"))
            with page.expect_popup() as popup:
                page.locator("#placement-open").click()
            editor = popup.value
            editor.on("pageerror", lambda error: errors.append(str(error)))
            editor.wait_for_function("window.blkPlacement?.items().length === 3 && document.body.dataset.loaded === 'true'")
            assert editor.locator("#density").input_value() == "1"
            assert int(editor.locator("body").get_attribute("data-shown")) == 2 * len(world)
            editor.locator("#mode-place").click()
            before = editor.evaluate("blkPlacement.items()[1].pose.slice()")
            box = editor.locator("#scene").bounding_box()
            x, y = box['x'] + box['width']*.5, box['y'] + box['height']*.5
            editor.mouse.move(x, y)
            editor.mouse.down()
            editor.mouse.move(x + 80, y - 40, steps=4)
            editor.mouse.up()
            after = editor.evaluate("blkPlacement.items()[1].pose.slice()")
            assert after[0] > before[0] and after[1] > before[1] and after[2:] == before[2:]
            editor.locator("#undo").click()
            assert editor.evaluate("blkPlacement.items()[1].pose") == before
            for tool, axis in (("x",0),("y",1),("z",2),("yaw",3)):
                editor.locator("#tool").select_option(tool)
                before = editor.evaluate("blkPlacement.items()[1].pose.slice()")
                editor.mouse.move(x,y); editor.mouse.down(); editor.mouse.move(x+30,y-30,steps=3); editor.mouse.up()
                after = editor.evaluate("blkPlacement.items()[1].pose.slice()")
                assert abs(after[axis]-before[axis])>1e-5, (tool,before,after)
                assert all(abs(after[i]-before[i])<1e-8 for i in range(4) if i!=axis)
            for id_, value in zip(("x","y","z","yaw"), (1.2,.5,.1,np.degrees(.15))):
                editor.locator('#'+id_).fill(str(value)); editor.locator('#'+id_).press('Tab')
            editor.locator("#next").click()
            editor.wait_for_function("document.body.dataset.loaded === 'true' && document.querySelector('#reference').value === '1'")
            for id_, value in zip(("x","y","z","yaw"), (2.2,-.3,.2,np.degrees(-.1))):
                editor.locator('#'+id_).fill(str(value)); editor.locator('#'+id_).press('Tab')
            editor.locator("#oblique").click()
            editor.locator("#fit").click()
            editor.screenshot(path=str(root / "placement.png"))
            assert editor.locator("#apply").count() == 0
            editor.wait_for_function("document.querySelector('#save-status').textContent.startsWith('Saved on disk:')")
            page.wait_for_function("initial_placements.size === 3")
            values = page.evaluate("registrationDraft().initial_guesses")
            np.testing.assert_allclose(values, poses, atol=1e-8)
            editor.locator("#moving").select_option("0")
            assert editor.locator("#x").is_disabled() and editor.locator("#yaw").is_disabled()
            assert page.locator("#placement-clear").count() == 0
            np.testing.assert_allclose(page.evaluate("registrationDraft().initial_guesses"), poses, atol=1e-8)
            editor.close()
            # Verify initial guesses persist into a real new registration, not only a UI object.
            raw_output = root / "raw-output"
            request = {"cloud_ids": ids, "initial_guesses": poses, "name": "manual_scans", "output_directory": str(raw_output), "voxel_m": .01}
            bad = {**request, "initial_guesses": [[0,0,0]]*3}
            try:
                api("registration/start", bad)
                raise AssertionError("Invalid placements accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 400
            assert not raw_output.exists(), "Invalid placement created output folders"
            api("registration/start", request)
            completed = waitFor(idle, 180)
            created = next(item for item in completed['jobs'] if item['id'] != job['id'])
            assert created['status'] == 'completed', created
            raw_report = pathlib.Path(created['report_directory'])
            actual = json.loads((raw_report/'config.json').read_text(encoding='utf-8'))
            assert actual['initial_guesses'] == poses
            report_doc = json.loads((raw_report/'manual_placement_report.json').read_text(encoding='utf-8'))
            assert report_doc['accepted_pairs'] >= 2
            assert all(e['initialization'] == 'pose_prior' for e in report_doc['pairs'])
            # Resume independent chunks using the same editor and real native worker.
            chunk_editor = browser.new_page(viewport={"width":1500,"height":1000})
            chunk_editor.on("pageerror", lambda error: errors.append(str(error)))
            chunk_editor.goto(base + '/placement.html?job=' + job['id'] + '#' + session['token'])
            chunk_editor.wait_for_function("window.blkPlacement?.items().length === 2 && document.body.dataset.loaded === 'true'")
            assert int(chunk_editor.locator('body').get_attribute('data-shown')) == 3 * len(world)
            for id_,value in zip(('x','y','z','yaw'),(2.2,-.3,.2,np.degrees(-.1))):
                chunk_editor.locator('#'+id_).fill(str(value));chunk_editor.locator('#'+id_).press('Tab')
            chunk_editor.wait_for_function("document.querySelector('#save-status').textContent.startsWith('Saved on disk:')")
            page.evaluate("id => startChunkRegistration(id)", job['id'])
            completed = waitFor(idle, 180)
            resumed = next(item for item in completed['jobs'] if item['id']==job['id'])
            assert resumed['status']=='completed', resumed
            updated = json.loads((report/'config.json').read_text(encoding='utf-8'))
            assert updated['manual_chunks'] == chunks
            np.testing.assert_allclose(updated['chunk_initial_guesses']['chunk-2'],poses[2],atol=1e-8)
            for result_dir in (raw_report, report):
                result = json.loads((result_dir/'registration.json').read_text(encoding='utf-8'))
                for record,pose in zip(result['scans'],poses):
                    np.testing.assert_allclose(record['transform_map_from_scan'],placementMatrix(pose),atol=.002)
            assert hashes == [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
            assert not errors, errors
            browser.close()
        print('Placement smoke passed: sampling, drag axes/yaw, undo, order, apply, native scan and chunk registration, unchanged sources.')
        api('shutdown', {})
        assert process.wait(timeout=30)==0
    finally:
        if process.poll() is None:
            process.terminate();process.wait(timeout=30)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--executable',required=True)
    parser.add_argument('--project',required=True)
    parser.add_argument('--chrome',default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    run(parser.parse_args())
