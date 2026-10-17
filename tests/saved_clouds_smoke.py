"""Exercise explicit PLY selection, tabs, and result reopening against the real app."""

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from compare_ply import readHeader


def waitFor(_function, _timeout=30):
    deadline = time.monotonic() + _timeout
    while time.monotonic() < deadline:
        result = _function()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError("Timed out waiting for the app")


def run(_args):
    root = pathlib.Path(_args.project).resolve()
    source, extra = [pathlib.Path(path).resolve() for path in (_args.ply, _args.extra)]
    root.mkdir(parents=True, exist_ok=False)
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, extra)}
    with source.open("rb") as stream:
        expected = readHeader(stream)
    jobs = [{"id": "2" * 32, "name": "converted.blk", "source_path": str(root / "old.blk"),
             "bytes": 0, "status": "completed", "message": "Completed", "progress": 1,
             "output_path": str(source), "result": {"valid_points": expected}}]
    (root / "project.json").write_text(json.dumps({"schema_version": 2, "name": "Selection test", "units": "metres",
        "output_directory": str(source.parent), "jobs": jobs,
        "point_clouds": [{"id": "a" * 32, "path": str(source)}]}))
    (root / "registration_jobs.json").write_text(json.dumps({"schema_version": 1, "python": "python",
        "runtime": {"status": "unchecked"}, "jobs": [], "draft": {"cloud_ids": ["a" * 32]}}))
    result_dir = root / "result"
    (result_dir / "html_source").mkdir(parents=True)
    map_path = result_dir / "reference_map_1cm.ply"
    shutil.copyfile(source, map_path)
    poses = result_dir / "relative_poses.json"
    poses.write_text(json.dumps({"schema_version": 3, "result_name": "Selection result", "registration_voxel_m": .01,
                                "scans": [{"name": source.name, "source_path": str(source)}]}))
    (result_dir / "result_review_and_manage.html").write_text("<!doctype html><title>Test review</title>")
    (result_dir / "html_source/products.json").write_text(json.dumps({"voxel": {"file": map_path.name,
        "points": expected, "bytes": map_path.stat().st_size, "sha256": hashes[source]}}))
    session_path = root / "session.json"
    process, base, headers = None, "", {}

    def launch():
        nonlocal process, base, headers
        process = subprocess.Popen([str(pathlib.Path(_args.executable).resolve()), "--no-browser", "--project", str(root)],
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        waitFor(session_path.exists)
        session = json.loads(session_path.read_text())
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}
        return base + "/#" + session["token"]

    def api(route, body=None):
        request = urllib.request.Request(base + "/api/" + route, data=None if body is None else json.dumps(body).encode(), headers=headers)
        return json.load(urllib.request.urlopen(request, timeout=30))

    def shutdown():
        api("shutdown", {})
        assert process.wait(timeout=30) == 0
        assert not session_path.exists()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_args.chrome, headless=True,
                args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(launch())
            page.wait_for_function("project !== null && registration_state !== null")
            assert page.locator("#panel-convert").is_visible()
            assert not page.locator("#panel-clouds").is_visible()
            assert not page.locator("#panel-register").is_visible()
            assert api("project")["point_clouds"] == []
            api("refresh-clouds", {})
            assert api("project")["point_clouds"] == []
            assert "point_clouds" not in json.loads((root / "project.json").read_text())
            assert "cloud_ids" not in api("registration")["draft"]
            before_browse = (root / "project.json").read_bytes()
            page.screenshot(path=str(root / "convert-tab.png"), full_page=True)

            def choose(button, folder, name=None):
                page.locator(button).click()
                page.locator("#browser-path").fill(str(folder))
                page.locator("#browser-go").click()
                page.wait_for_function(r"p => browser_state?.path.replaceAll('\\', '/') === p", arg=str(folder).replace("\\", "/"))
                if name:
                    page.get_by_label(name, exact=True).check()
                    page.locator("#use-files").click()
                else:
                    page.locator("#use-folder").click()

            page.locator("#tab-clouds").click()
            choose("#pick-ply-folder", source.parent)
            page.wait_for_function("document.querySelectorAll('#clouds .job').length > 0")
            cloud = next(c for c in api("project")["point_clouds"] if pathlib.Path(c["path"]) == source)
            with page.expect_popup() as popup:
                page.locator(f"[data-cloud-id='{cloud['id']}']").get_by_role("link", name="View 3D").click()
            popup.value.wait_for_function("n => blkViewer.count === n", arg=expected, timeout=180000)
            popup.value.close()
            choose("#pick-ply", extra.parent, extra.name)
            page.wait_for_function("document.querySelectorAll('#clouds .job').length === 1")
            page.wait_for_function("name => document.querySelector('#clouds').textContent.includes(name)", arg=extra.name)
            page.screenshot(path=str(root / "clouds-tab.png"), full_page=True)
            page.locator("#clear-clouds").click()
            page.wait_for_function("document.querySelectorAll('#clouds .job').length === 0")
            page.locator("#tab-register").click()
            choose("#registration-folder", source.parent)
            page.wait_for_function("registration_ids.length > 0")
            first_count = page.locator(".registration-scan").count()
            choose("#registration-pick", extra.parent, extra.name)
            page.wait_for_function("n => registration_ids.length === n", arg=first_count + 1)
            page.locator("#tab-convert").click()
            page.locator("#tab-register").click()
            assert page.locator(".registration-scan").count() == first_count + 1
            waitFor(lambda: "cloud_ids" not in json.loads((root / "registration_jobs.json").read_text()).get("draft", {}))
            assert (root / "project.json").read_bytes() == before_browse, "Viewing must not write a library cache"
            page.reload()
            page.wait_for_function("project !== null && registration_state !== null")
            assert page.locator("#panel-convert").is_visible()
            assert page.locator("#clouds .job").count() == 0
            assert page.locator(".registration-scan").count() == 0
            page.locator("#tab-register").click()
            # Keep external browser launch out of the test; the import itself uses the real API.
            page.route("**/api/registration/review/*", lambda route: route.fulfill(json={"ok": True}))
            choose("#open-registration-result", result_dir, poses.name)
            page.locator(".review-result").wait_for()
            assert page.locator(".registration-results .result-ready").count() == 1
            assert page.locator(".result-actions").get_by_role("button", name="Open output folder").is_visible()
            assert page.locator(".review-result").evaluate("e => getComputedStyle(e).backgroundColor") != "rgba(0, 0, 0, 0)"
            page.screenshot(path=str(root / "register-tab.png"), full_page=True)
            page.set_viewport_size({"width": 420, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "register-mobile.png"), full_page=True)
            imported = api("registration/import", {"path": str(poses)})["id"]
            cloud_id = next(j for j in api("registration")["jobs"] if j["id"] == imported)["products"]["voxel"]["cloud_id"]
            assert api("cloud/" + cloud_id)["valid_points"] == expected
            shutdown()
            page.goto(launch())
            page.wait_for_function("project !== null && registration_state !== null")
            assert api("project")["point_clouds"] == []
            assert page.locator(".registration-job").count() == 0
            assert page.locator(".registration-scan").count() == 0
            # Reopening an existing result must assign valid current-session cloud IDs.
            assert api("registration/import", {"path": str(poses)})["id"] == imported
            new_id = next(j for j in api("registration")["jobs"] if j["id"] == imported)["products"]["voxel"]["cloud_id"]
            assert new_id != cloud_id and api("cloud/" + new_id)["valid_points"] == expected
            assert api("project")["jobs"] == [], "Completed conversion history was restored"
            for route in [f"/api/cloud/{new_id}", f"/files/cloud/{new_id}.ply"]:
                try:
                    urllib.request.urlopen(base + route, timeout=10)
                    raise AssertionError("Cloud accessible without a session token")
                except urllib.error.HTTPError as error:
                    assert error.code == 403
            assert not errors, errors
            browser.close()
            shutdown()
        assert all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in hashes.items())
        print(json.dumps({"selection_tabs_restart_results": "passed", "sources_unchanged": True}))
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ["executable", "project", "ply", "extra", "chrome"]:
        parser.add_argument("--" + name, required=True)
    run(parser.parse_args())
