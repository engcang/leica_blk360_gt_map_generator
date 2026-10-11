"""Exercise registration, cancellation, persistence, and viewing through the local app."""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright


def waitFor(_function, _timeout=45):
    deadline = time.monotonic() + _timeout
    while time.monotonic() < deadline:
        result = _function()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for registration")


def hashFile(_path):
    with _path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(_args):
    root = pathlib.Path(_args.project).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sources = [pathlib.Path(value).resolve() for value in _args.ply]
    source_hashes = [hashFile(path) for path in sources]
    output = root / "Maps with spaces"
    output.mkdir()
    session_path = root / "session.json"
    process = None
    base = ""
    headers = {}

    def launch():
        nonlocal process, base, headers
        process = subprocess.Popen([str(pathlib.Path(_args.executable).resolve()), "--no-browser", "--project", str(root)],
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        waitFor(session_path.exists)
        session = json.loads(session_path.read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}
        return base + "/#" + session["token"]

    def api(_route, _body=None):
        request = urllib.request.Request(base + "/api/" + _route,
                                         data=None if _body is None else json.dumps(_body).encode(),
                                         headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def expectError(_body):
        try:
            api("registration/start", _body)
        except urllib.error.HTTPError as error:
            assert error.code == 400
            return
        raise AssertionError("Invalid registration request was accepted")

    def idle():
        state = api("registration")
        return state if not state["busy"] else None

    def shutdown():
        api("shutdown", {})
        assert process.wait(timeout=30) == 0

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=_args.chrome,
                                                 headless=True,
                                                 args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1440, "height": 1200})
            errors = []
            page.on("pageerror", lambda _error: errors.append(str(_error)))
            page.goto(launch())
            try:
                urllib.request.urlopen(base + "/api/registration")
                raise AssertionError("Unauthenticated registration access succeeded")
            except urllib.error.HTTPError as error:
                assert error.code == 403
            api("registration/runtime", {"python": str(root / "missing-python"), "install": False})
            assert waitFor(idle)["runtime"]["status"] == "failed"
            page.locator("#registration-setup").evaluate("node => node.open = true")
            page.locator("#registration-python").fill(sys.executable)
            page.locator("#runtime-check").click()
            page.wait_for_function("document.querySelector('#runtime-state').textContent === 'Ready'", timeout=60000)
            added = api("add-clouds", {"paths": [str(path) for path in sources]})
            assert not added["errors"]
            page.wait_for_function("n => document.querySelectorAll('#clouds .job').length === n", arg=len(sources))
            for path in sources:
                cloud = next(item for item in api("project")["point_clouds"] if pathlib.Path(item["path"]) == path)
                page.locator(f"[data-cloud-id='{cloud['id']}']").get_by_role("button", name="Add to map").click()
            original = page.locator(".registration-scan > span").all_text_contents()
            page.locator(".registration-scan").nth(1).get_by_role("button", name="Move", exact=False).first.click()
            assert page.locator(".registration-scan > span").all_text_contents() != original
            page.locator(".registration-scan").first.get_by_role("button", name="Move", exact=False).nth(1).click()
            assert page.locator(".registration-scan > span").all_text_contents() == original
            page.locator("#map-name").fill("Integration map")
            page.locator("#map-voxel").select_option("0.01")
            page.locator("#map-browse").click()
            page.locator("#browser-path").fill(str(output))
            page.locator("#browser-go").click()
            page.wait_for_function("p => document.querySelector('#browser-path').value.replaceAll('\\\\','/') === p", arg=output.as_posix())
            page.locator("#use-folder").click()
            waitFor(lambda: api("registration").get("draft", {}).get("name") == "Integration map")
            config = api("registration")["draft"]
            expectError({**config, "name": "../outside"})
            expectError({**config, "voxel_m": 0})
            expectError({**config, "additional_pairs": "0:2"})
            expectError({**config, "cloud_ids": [config["cloud_ids"][0]] * 2})
            protected = output / "existing_1cm.ply"
            protected.write_bytes(b"preserve existing output")
            expectError({**config, "name": "existing"})
            assert protected.read_bytes() == b"preserve existing output"
            protected.unlink()
            protected_poses = output / "relative_poses.json"
            protected_poses.write_text("preserve existing poses")
            expectError({**config, "name": "poses_conflict"})
            assert protected_poses.read_text() == "preserve existing poses"
            protected_poses.unlink()
            (output / "html_source").mkdir()
            expectError({**config, "name": "review_conflict_map"})
            (output / "html_source").rmdir()
            page.locator("#registration-start").click()
            page.locator("#registration-stop").click()
            state = waitFor(idle)
            assert state["jobs"][0]["status"] == "cancelled", state
            job_id = state["jobs"][0]["id"]
            page.get_by_role("button", name="Resume", exact=True).wait_for()
            page.get_by_role("button", name="Resume", exact=True).click()
            deadline = time.monotonic() + 1200
            previous = ""
            while time.monotonic() < deadline:
                state = api("registration")
                job = state["jobs"][0]
                if job.get("stage") != previous:
                    previous = job.get("stage")
                    print(previous, flush=True)
                if not state["busy"]:
                    break
                time.sleep(2)
            assert job["status"] == "completed", state
            assert "cloud_id" not in job
            assert {path.name for path in output.iterdir()} == {"relative_poses.json", "result_review_and_manage.html", "html_source"}
            records = pathlib.Path(job["report_directory"])
            registration = json.loads((records / "registration.json").read_text(encoding="utf-8"))
            assert registration["registration_voxel_m"] == 0.01
            assert not registration["independently_validated"]
            assert registration["global_alignment"] == "KISS-Matcher"
            assert registration["local_alignment"] == "Nano-GICP"
            assert (records / "poses.json").is_file()
            assert job["review_available"]
            review_path = pathlib.Path(job["review_path"])
            assert review_path == output / "result_review_and_manage.html"
            review = json.loads((review_path.parent / "html_source/review_summary.json").read_text(encoding="utf-8"))
            assert len(review["stations"]) == len(sources)
            assert all((review_path.parent / "html_source" / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for name in review["images"])
            page.get_by_role("button", name="Review and manage", exact=True).wait_for()
            review_page = browser.new_page()
            review_page.goto(review_path.as_uri())
            review_page.get_by_role("button", name="Generate raw PLY").click()
            assert review_page.locator("#app-required").is_visible()
            review_page.get_by_role("button", name="Got it").click()
            review_page.locator("img").evaluate_all("images => images.forEach(image => image.loading = 'eager')")
            review_page.wait_for_function("[...document.images].every(image => image.complete && image.naturalWidth > 0)")
            assert review_page.locator("tbody tr").count() == len(sources)
            quality = json.loads((records / "quality.json").read_text(encoding="utf-8"))
            assert quality["schema_version"] == 2 and quality["usage"] == "diagnostic_only"
            panel = review_page.frame_locator("iframe[title='Scan-pair quality metrics']")
            assert panel.locator("#threshold").input_value() == "0.03"
            assert panel.locator("#pairs tr").count() == len(quality["pairs"])
            panel.locator("#threshold").select_option("0.01")
            assert "Threshold: 1 cm" in panel.locator("#summary").inner_text()
            assert [pathlib.Path(scan["source_path"]) for scan in registration["scans"]] == sources
            assert [scan["source_sha256"] for scan in registration["scans"]] == source_hashes
            manifest_path = pathlib.Path(job["poses_path"])
            assert manifest_path == output / "relative_poses.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert len(manifest["scans"]) == len(sources) and "map" not in manifest
            manifest_hash = hashFile(manifest_path)
            relocated = root / "removed working records"
            assert records.resolve().is_relative_to(root) and relocated.resolve().is_relative_to(root)
            records.rename(relocated)
            manager_url = base + "/files/result/" + headers["X-Session-Token"] + "/" + job_id + "/result_review_and_manage.html"
            review_page.goto(manager_url)
            review_page.wait_for_function("!document.querySelector('[data-product=raw]').disabled")
            for asset in ("html_source/result_manager.js", "html_source/quality.html", "relative_poses.json"):
                with urllib.request.urlopen(manager_url.rsplit("/", 1)[0] + "/" + asset) as response:
                    assert response.status == 200
            for path in ("/files/result/" + "0" * 64 + "/" + job_id + "/relative_poses.json",
                         manager_url.removeprefix(base).replace("result_review_and_manage.html", "html_source/%2e%2e/%2e%2e/session.json")):
                try:
                    urllib.request.urlopen(base + path)
                    raise AssertionError("Unauthorized result asset was exposed")
                except urllib.error.HTTPError as error:
                    assert error.code in (400, 403, 404)
            for kind in ("raw", "voxel", "share"):
                review_page.locator(f"[data-product='{kind}']").click()
                state = waitFor(idle, 300)
                generated = state["jobs"][0]["export"]
                assert generated["status"] == "completed", generated
                assert generated["product"] == kind
                assert pathlib.Path(generated["output_path"]).is_file()
                assert generated["poses_sha256"] == manifest_hash
                if kind == "raw":
                    assert generated["points"] == sum(scan["points"] for scan in manifest["scans"])
                if kind == "voxel":
                    cloud_id = generated["cloud_id"]
                    points = generated["points"]
                    target = pathlib.Path(generated["output_path"])
                    assert generated["sha256"] == hashFile(target)
                    assert generated["voxel_m"] == 0.01
                review_page.wait_for_function("kind => document.getElementById(kind + '-result').textContent.includes('points')", arg=kind)
            assert hashFile(manifest_path) == manifest_hash
            assert not list(output.glob(".product-*"))
            assert not list(output.glob("*.zip"))
            review_page.screenshot(path=str(root / "result-review.png"))
            with review_page.expect_popup() as popup:
                review_page.locator("#voxel-result").get_by_role("link", name="View in 3D").click()
            viewer = popup.value
            viewer.wait_for_function("n => Number(document.body.dataset.loaded) === n", arg=points, timeout=180000)
            assert viewer.locator("#error").inner_text() == ""
            assert viewer.locator("#color").inner_text() == "Intensity"
            viewer.screenshot(path=str(root / "registered-map.png"))
            viewer.close()
            shared = browser.new_page()
            shared.goto(pathlib.Path(generated["output_path"]).as_uri())
            shared.wait_for_function("n => Number(document.body.dataset.loaded) === n", arg=points, timeout=180000)
            assert shared.locator("#error").inner_text() == ""
            shared.close()
            raw_path = output / "Integration map_raw.ply"
            raw_backup = root / "saved_raw.ply"
            raw_path.rename(raw_backup)
            review_page.wait_for_function("!document.querySelector('[data-product=raw]').disabled")
            review_page.locator("[data-product=raw]").click()
            waitFor(lambda: bool(list(output.glob(".product-*"))), 30)
            api("registration/cancel", {})
            cancelled = waitFor(idle)["jobs"][0]["export"]
            assert cancelled["status"] == "cancelled", cancelled
            assert not list(output.glob(".product-*")) and not raw_path.exists()
            raw_backup.rename(raw_path)
            review_page.close()
            expectError(config)
            expectError({"resume_id": job_id})
            shutdown()
            page.goto(launch())
            page.get_by_role("button", name="Review and manage").wait_for()
            state = api("registration")
            assert state["jobs"][0]["products"]["voxel"]["cloud_id"] == cloud_id
            assert state["draft"]["cloud_ids"] == config["cloud_ids"]
            assert page.locator(".registration-scan > span").all_text_contents() == original
            assert api(f"cloud/{cloud_id}")["valid_points"] == points
            assert state["jobs"][0]["review_path"] == str(review_path)
            assert not records.exists(), "Viewing/export recreated the deleted registration folder"
            assert source_hashes == [hashFile(path) for path in sources]
            assert not errors, errors
            shutdown()
            state_path = root / "registration_jobs.json"
            saved_state = json.loads(state_path.read_text(encoding="utf-8"))
            saved_state["jobs"] = []
            state_path.write_text(json.dumps(saved_state), encoding="utf-8")
            page.goto(launch())
            imported = api("registration/import", {"path": str(manifest_path)})
            assert imported["id"] != job_id
            imported_job = api("registration")["jobs"][0]
            assert imported_job["products"]["voxel"]["cloud_id"] == cloud_id
            assert imported_job["products"]["share"]["available"]
            assert "report_directory" not in imported_job
            assert api("registration/import", {"path": str(manifest_path)})["id"] == imported["id"]
            page.get_by_role("button", name="Review and manage").wait_for()
            shutdown()
            browser.close()
            print(json.dumps({"status": "passed", "points": points, "output": str(target)}), flush=True)
    finally:
        if process is not None and process.poll() is None:
            try:
                shutdown()
            except Exception:
                process.kill()
                process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--ply", nargs="+", required=True)
    parser.add_argument("--chrome", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
