import argparse
import hashlib
import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from compare_ply import readHeader


def waitFor(_function, _timeout=30):
    deadline = time.monotonic() + _timeout
    while time.monotonic() < deadline:
        if _function():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for the app")


def run(_args):
    project = pathlib.Path(_args.project).resolve()
    source = pathlib.Path(_args.ply).resolve()
    extra = pathlib.Path(_args.extra).resolve()
    project.mkdir(parents=True, exist_ok=False)
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, extra)}
    with source.open("rb") as stream:
        expected_points = readHeader(stream)
    jobs = [{"id": "1" * 32, "name": "unconverted.blk", "source_path": str(project / "missing.blk"),
             "bytes": 0, "status": "ready", "message": "Ready", "progress": 0, "output_path": "", "result": None},
            {"id": "2" * 32, "name": "converted.blk", "source_path": str(project / "missing-converted.blk"),
             "bytes": 0, "status": "completed", "message": "Completed", "progress": 1,
             "output_path": str(source), "result": {"valid_points": expected_points}}]
    jobs.append({**jobs[1], "id": "3" * 32, "output_path": str(project / "deleted-output.ply")})
    manifest = {"schema_version": 2, "name": "Saved PLY restart test", "units": "metres",
                "output_directory": str(source.parent), "jobs": jobs}
    (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    session_path = project / "session.json"
    process = None
    base = ""
    headers = {}

    def launch():
        nonlocal process, base, headers
        process = subprocess.Popen([str(pathlib.Path(_args.executable).resolve()), "--no-browser", "--project", str(project)],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        waitFor(session_path.exists)
        session = json.loads(session_path.read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}
        return base + "/#" + session["token"]

    def api(_route, _body=None):
        request = urllib.request.Request(base + _route,
                                         data=None if _body is None else json.dumps(_body).encode(),
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def shutdown():
        api("/api/shutdown", {})
        assert process.wait(timeout=30) == 0
        assert not session_path.exists()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=_args.chrome,
                                                 headless=True,
                                                 args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on("pageerror", lambda _error: errors.append(str(_error)))
            page.goto(launch())
            page.locator("#clouds .job").first.wait_for()
            state = api("/api/project")
            cloud = next(item for item in state["point_clouds"] if pathlib.Path(item["path"]) == source)
            assert state["jobs"] == jobs
            assert not state["point_cloud_errors"]
            count = len(state["point_clouds"])
            assert cloud["available"] and cloud["valid_points"] == expected_points
            row = page.locator(f"#clouds [data-cloud-id='{cloud['id']}']")
            with page.expect_popup() as popup:
                row.get_by_role("link", name="View 3D").click()
            viewer = popup.value
            viewer.wait_for_function("count => Number(document.body.dataset.loaded) === count", arg=expected_points, timeout=180000)
            assert viewer.locator("#error").inner_text() == ""
            assert viewer.locator("#color").inner_text() == "Intensity"
            assert viewer.locator("select#color").count() == 0
            viewer.close()
            page.locator("#pick-ply").click()
            page.locator("#browser-path").fill(str(extra.parent))
            page.locator("#browser-go").click()
            page.get_by_label(extra.name, exact=True).check()
            page.locator("#use-files").click()
            page.wait_for_function("count => Number(document.querySelector('#cloud-count').textContent) === count", arg=count + 1)
            extra_cloud = next(item for item in api("/api/project")["point_clouds"] if pathlib.Path(item["path"]) == extra)
            assert api("/api/add-clouds", {"paths": [str(extra)]})["skipped"] == 1
            temporary = project / "temporary.ply"
            temporary.write_bytes(b"ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n" + bytes(14))
            assert api("/api/add-clouds", {"paths": [str(temporary)]})["added"] == 1
            page.wait_for_function("count => Number(document.querySelector('#cloud-count').textContent) === count", arg=count + 2)
            temporary.unlink()
            page.wait_for_function("count => Number(document.querySelector('#cloud-count').textContent) === count", arg=count + 1)
            api("/api/refresh-clouds", {})
            assert not api("/api/project")["point_cloud_errors"]
            shutdown()
            page.goto(launch())
            page.wait_for_function("count => Number(document.querySelector('#cloud-count').textContent) === count", arg=count + 1)
            state = api("/api/project")
            assert state["jobs"] == jobs
            assert any(item["id"] == extra_cloud["id"] for item in state["point_clouds"])
            assert api(f"/api/cloud/{cloud['id']}")["valid_points"] == expected_points
            assert api(f"/api/scan/{jobs[1]['id']}")["status"] == "completed"
            with page.expect_popup() as popup:
                page.locator(f"#clouds [data-cloud-id='{cloud['id']}']").get_by_role("link", name="View 3D").click()
            viewer = popup.value
            viewer.wait_for_function("count => Number(document.body.dataset.loaded) === count", arg=expected_points, timeout=180000)
            assert viewer.locator("#error").inner_text() == ""
            viewer.close()
            for route in [f"/api/cloud/{cloud['id']}", f"/files/cloud/{cloud['id']}.ply"]:
                try:
                    urllib.request.urlopen(base + route, timeout=10)
                    raise AssertionError("Saved cloud accessible without session token")
                except urllib.error.HTTPError as error:
                    assert error.code == 403
            missing = project / "missing.ply"
            assert api("/api/add-clouds", {"paths": [str(missing)]})["errors"]
            page.screenshot(path=str(project / "saved-clouds.png"), full_page=True)
            assert not errors, errors
            browser.close()
            shutdown()
        assert not (project / "inputs").exists() and not (project / "scans").exists()
        for path, expected in hashes.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        print(json.dumps({"saved_clouds_smoke": "passed", "discovered": count, "imported": 1,
                         "points_loaded_before_and_after_restart": expected_points, "original_files_unchanged": True}))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--ply", required=True)
    parser.add_argument("--extra", required=True)
    parser.add_argument("--chrome", required=True)
    run(parser.parse_args())
