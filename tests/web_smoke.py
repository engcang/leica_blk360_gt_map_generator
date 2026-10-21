import argparse
import hashlib
import json
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

from compare_ply import compareFiles, readHeader


def waitFor(_function, _timeout=180):
    deadline = time.monotonic() + _timeout
    while time.monotonic() < deadline:
        result = _function()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError("Timed out")


def run(_args):
    executable = pathlib.Path(_args.executable).resolve()
    project = pathlib.Path(_args.project).resolve()
    source = pathlib.Path(_args.input).resolve()
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_mtime = source.stat().st_mtime_ns
    output = project / "output"
    project.mkdir(parents=True, exist_ok=False)
    with open(_args.reference, "rb") as stream:
        expected_points = readHeader(stream)

    def launch():
        return subprocess.Popen([str(executable), "--no-browser", "--project", str(project)],
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    process = launch()
    try:
        session_path = project / "session.json"
        waitFor(session_path.exists, 20)
        session = json.loads(session_path.read_text())
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}

        def api(_route, _body=None, _headers=None):
            request = urllib.request.Request(base + _route,
                                             data=None if _body is None else json.dumps(_body).encode(),
                                             headers=headers if _headers is None else _headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)

        for request_headers in [{}, {**headers, "Origin": "https://example.com"}, {**headers, "Host": "example.com"}]:
            try:
                api("/api/project", _headers=request_headers)
                raise AssertionError("Unauthenticated or cross-origin request accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 403

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=_args.chrome,
                                                 headless=True,
                                                 args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1440, "height": 1100})
            page.context.add_init_script("""(() => {
                const prototype = WebGLRenderingContext.prototype;
                const draw = prototype.drawArrays;
                const upload = prototype.bufferData;
                window.pointDraws = [];
                window.currentPointDraw = 0;
                window.bufferUploads = 0;
                const clear = prototype.clear;
                prototype.clear = function(mask) {
                    window.currentPointDraw = 0;
                    return clear.call(this, mask);
                };
                prototype.drawArrays = function(mode, first, count) {
                    if (mode === this.POINTS) {
                        window.pointDraws.push(window.currentPointDraw += count);
                        if (window.pointDraws.length > 100) window.pointDraws.shift();
                    }
                    return draw.call(this, mode, first, count);
                };
                prototype.bufferData = function(target, data, usage) {
                    window.bufferUploads++;
                    if (data instanceof Float32Array && data.length === 10007 * 4)
                        window.syntheticUpload = data.slice();
                    return upload.call(this, target, data, usage);
                };
            })();""")
            errors = []
            page.on("pageerror", lambda _error: errors.append(str(_error)))
            page.goto(base + "/#" + session["token"])
            page.wait_for_function("document.querySelector('#connection').textContent.includes('Local connection')")
            page.locator("#pick-files").click()
            page.locator("#browser-path").fill(str(source.parent))
            page.locator("#browser-go").click()
            page.get_by_label(source.name, exact=True).check()
            page.locator("#use-files").click()
            page.wait_for_function("document.querySelector('#job-count').textContent === '1'", timeout=60000)
            page.locator('#source-paths').fill(str(source))
            page.locator('#add-paths').click()
            page.wait_for_function("document.querySelector('#job-count').textContent === '1'")
            page.locator('#output-path').fill(str(output))
            page.locator('#save-output').click()
            assert page.locator("#start").is_enabled()
            job = api("/api/project")["jobs"][0]
            assert pathlib.Path(job["source_path"]) == source
            assert not (project / "inputs").exists()
            assert not (project / "scans").exists()
            page.locator("#choose-output").click()
            page.locator("#browser-path").fill(str(project))
            page.locator("#browser-go").click()
            page.wait_for_function("document.querySelector('#browser-status').textContent !== 'Reading folder…'")
            page.once("dialog", lambda _dialog: _dialog.accept("output"))
            page.locator("#new-folder").click()
            page.wait_for_function("document.querySelector('#browser-path').value.endsWith('output')")
            page.locator("#use-folder").click()
            waitFor(lambda: pathlib.Path(api("/api/project")["output_directory"]) == output)
            assert page.locator("#project-name, #save-name, #export").count() == 0
            page.locator("#start").click()
            waitFor(lambda: api("/api/project")["running"])
            api("/api/cancel", {})
            waitFor(lambda: api("/api/project")["jobs"][0]["status"] == "cancelled")
            assert not list(output.iterdir()), "Cancelled conversion left temporary files"
            page.get_by_role("button", name="Retry", exact=True).click(timeout=15000)
            page.locator("#start").click()
            waitFor(lambda: api("/api/project")["jobs"][0]["status"] in ("completed", "failed"), 300)
            job = api("/api/project")["jobs"][0]
            assert job["status"] == "completed", job
            assert job["result"]["valid_points"] == expected_points
            assert "preview_points" not in job["result"]
            target = output / source.with_suffix(".ply").name
            assert pathlib.Path(job["output_path"]) == target
            assert list(output.iterdir()) == [target]
            assert target.stat().st_mtime_ns == original_mtime, "PLY did not preserve the BLK modification time"
            comparison = compareFiles(_args.reference, target)
            assert comparison["differing_points"] == 0, comparison
            page.locator("#jobs").get_by_role("link", name="View 3D").wait_for(timeout=15000)
            screenshot = pathlib.Path(_args.screenshot)
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot), full_page=True)
            with page.expect_popup() as popup:
                page.locator("#jobs").get_by_role("link", name="View 3D").click()
            viewer = popup.value
            viewer.set_viewport_size({"width": 1000, "height": 850})
            viewer.on("pageerror", lambda _error: errors.append(str(_error)))
            viewer.wait_for_function("expected => Number(document.body.dataset.loaded) === expected",
                                     arg=expected_points,
                                     timeout=240000)
            assert viewer.locator("#error").inner_text() == ""
            assert viewer.evaluate("window.blkViewer.count") == expected_points
            assert viewer.locator("#color").inner_text() == "Intensity"
            assert viewer.locator("select#color").count() == 0
            assert viewer.locator("#density").input_value() == "10"
            upload_count = viewer.evaluate("window.bufferUploads")

            def waitForDraw(_points):
                viewer.wait_for_function("expected => Number(document.body.dataset.shown) === expected && window.pointDraws.at(-1) === expected",
                                         arg=_points,
                                         timeout=30000)

            waitForDraw((expected_points + 9) // 10)
            for divisor in (5, 2, 1, 10):
                viewer.locator("#density").select_option(str(divisor))
                waitForDraw((expected_points + divisor - 1) // divisor)
                assert viewer.evaluate("window.blkViewer.count") == expected_points
            viewer.locator("#density").select_option("2")
            waitForDraw((expected_points + 1) // 2)
            before = viewer.evaluate("JSON.stringify(window.blkViewer.camera)")
            viewer.mouse.move(650, 450)
            viewer.mouse.down()
            viewer.mouse.move(780, 500, steps=3)
            waitForDraw(min(2000000, (expected_points + 1) // 2))
            viewer.wait_for_timeout(300)
            assert viewer.locator("body").get_attribute("data-interacting") == "true"
            assert int(viewer.locator("body").get_attribute("data-loaded")) == expected_points
            viewer.mouse.up()
            waitForDraw((expected_points + 1) // 2)
            assert viewer.evaluate("JSON.stringify(window.blkViewer.camera)") != before
            viewer.mouse.down(button="right")
            viewer.mouse.move(720, 475, steps=3)
            waitForDraw(min(2000000, (expected_points + 1) // 2))
            viewer.mouse.up(button="right")
            waitForDraw((expected_points + 1) // 2)
            viewer.evaluate("window.pointDraws.length = 0")
            viewer.mouse.wheel(0, -120)
            waitForDraw((expected_points + 1) // 2)
            viewer.wait_for_function("document.body.dataset.interacting === 'false'")
            assert min(2000000, (expected_points + 1) // 2) in viewer.evaluate("window.pointDraws")
            assert viewer.evaluate("window.bufferUploads") == upload_count, "Navigation or density selection reuploaded the full cloud"
            viewer.locator("#density").select_option("10")
            waitForDraw((expected_points + 9) // 10)
            viewer.screenshot(path=str(screenshot.with_name("viewer-density.png")))
            request = urllib.request.Request(base + f"/files/{job['id']}.ply", headers={**headers, "Range": "bytes=0-299"})
            with urllib.request.urlopen(request) as response:
                assert response.status == 206
                assert f"element vertex {expected_points}".encode() in response.read()
            sampling = viewer.evaluate("""async () => {
                const count = 10007;
                const header = new TextEncoder().encode('ply\\nformat binary_little_endian 1.0\\nelement vertex ' + count + '\\nproperty float x\\nproperty float y\\nproperty float z\\nproperty ushort intensity\\nend_header\\n');
                const payload = new ArrayBuffer(count * 14);
                const view = new DataView(payload);
                for (let i = 0; i < count; i++) {
                    view.setFloat32(i * 14, i, true);
                    view.setFloat32(i * 14 + 4, -i, true);
                    view.setFloat32(i * 14 + 8, i % 101, true);
                    view.setUint16(i * 14 + 12, i, true);
                }
                const file = new Blob([header, payload]);
                await window.blkViewer.loadPly(file, 'synthetic.ply', count);
                const first = window.syntheticUpload;
                const ids = new Set();
                let intact = true;
                for (let i = 0; i < count; i++) {
                    const id = first[i * 4];
                    ids.add(id);
                    intact &&= Number.isInteger(id) && id >= 0 && id < count && first[i * 4 + 1] === -id && first[i * 4 + 2] === id % 101 && first[i * 4 + 3] === id;
                }
                const bins = new Set();
                for (let i = 0; i < Math.ceil(count / 10); i++) bins.add(Math.floor(first[i * 4] / 1001));
                await window.blkViewer.loadPly(file, 'synthetic.ply', count);
                return {intact, unique: ids.size, bins: bins.size, stable: first.every((value, index) => value === window.syntheticUpload[index])};
            }""")
            assert sampling == {"intact": True, "unique": 10007, "bins": 10, "stable": True}, sampling
            waitForDraw(1001)
            viewer.close()
            page.reload()
            page.wait_for_function("document.querySelector('#job-count').textContent === '0'")
            assert page.locator("#project-name, #save-name, #export").count() == 0
            assert json.loads((project / "project.json").read_text())["jobs"] == [], "Completed history persisted"
            original_output_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            target.unlink()
            assert api(f"/api/scan/{job['id']}")["status"] == "ready", "Scan API returned a stale completion"
            page.wait_for_function("project.jobs[0].status === 'ready'")
            assert page.locator("#jobs .view").count() == 0
            assert page.locator("#start").is_disabled()
            page.locator('#source-paths').fill(str(source))
            page.locator('#add-paths').click()
            page.wait_for_function("document.querySelector('#job-count').textContent === '1'")
            page.locator('#output-path').fill(str(output))
            page.locator('#save-output').click()
            assert page.locator("#start").is_enabled()
            page.locator("#start").click()
            waitFor(lambda: api("/api/project")["jobs"][0]["status"] in ("completed", "failed"), 300)
            assert api("/api/project")["jobs"][0]["status"] == "completed"
            assert hashlib.sha256(target.read_bytes()).hexdigest() == original_output_hash
            assert target.stat().st_mtime_ns == original_mtime
            assert json.loads((project / "project.json").read_text())["jobs"] == []
            api(f"/api/remove/{job['id']}", {})
            assert target.exists() and source.exists()
            assert api("/api/add", {"paths": [str(source)]})["added"] == 1
            try:
                api("/api/start", {})
                raise AssertionError("Existing output was overwritten")
            except urllib.error.HTTPError as error:
                assert "already exists" in error.read().decode()
            assert not errors, errors
            browser.close()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
        assert source.stat().st_mtime_ns == original_mtime
        api("/api/shutdown", {})
        assert process.wait(timeout=30) == 0
        process = launch()
        waitFor(session_path.exists, 20)
        session = json.loads(session_path.read_text())
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}
        assert api("/api/project")["jobs"][0]["status"] == "ready"
        assert pathlib.Path(api("/api/project")["output_directory"]) == output
        api("/api/shutdown", {})
        assert process.wait(timeout=30) == 0
        print(json.dumps({"web_smoke": "passed", "points": expected_points, **comparison}))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--chrome", required=True)
    parser.add_argument("--screenshot", required=True)
    run(parser.parse_args())
