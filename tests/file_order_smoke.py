"""Check chronological selection across all tabs with misleading scan filenames."""

import argparse
import json
import os
import pathlib
import shutil
import struct
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright


def run(args):
    root = pathlib.Path(args.project).resolve()
    root.mkdir(parents=True, exist_ok=False)
    scans, clouds = root / "scans", root / "clouds"
    scans.mkdir()
    clouds.mkdir()
    expected = ["Setup511", "Setup52", "Setup51"]
    timestamp = 1700000000
    ply = (b"ply\nformat binary_little_endian 1.0\nelement vertex 1\n"
           b"property float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n"
           + struct.pack("<fffH", 1, 2, 3, 100))
    for index, name in enumerate(expected):
        source = scans / (name + ".blk")
        shutil.copyfile(args.input, source)
        cloud = clouds / (name + ".ply")
        cloud.write_bytes(ply)
        for path in (source, cloud):
            os.utime(path, (timestamp + index * 100, timestamp + index * 100))
    process = subprocess.Popen([str(pathlib.Path(args.executable).resolve()), "--no-browser", "--project", str(root)],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 30
        while not (root / "session.json").exists():
            assert time.monotonic() < deadline
            time.sleep(.1)
        session = json.loads((root / "session.json").read_text())
        base = f"http://127.0.0.1:{session['port']}"
        headers = {"X-Session-Token": session["token"]}

        def api(route, body=None):
            request = urllib.request.Request(base + "/api/" + route, headers=headers,
                data=None if body is None else json.dumps(body).encode())
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)

        # Separate additions and reversed file selection must still produce one time order.
        api("add", {"paths": [str(scans / "Setup51.blk")]})
        api("add", {"paths": [str(scans / "Setup52.blk"), str(scans / "Setup511.blk")]})
        assert [pathlib.Path(j["name"]).stem for j in api("project")["jobs"]] == expected
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.chrome, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/#" + session["token"])
            page.wait_for_function("project !== null && registration_state !== null")
            assert page.locator("#tab-clouds").inner_text() == "View saved point clouds"
            assert page.locator("#jobs .filename").all_text_contents() == [n + ".blk" for n in expected]
            page.locator("#pick-files").click()
            page.locator("#browser-path").fill(str(scans))
            page.locator("#browser-go").click()
            page.wait_for_function("document.querySelectorAll('#browser-list input[type=checkbox]').length === 3")
            assert [pathlib.Path(p).stem for p in page.locator("#browser-list input[type=checkbox]").evaluate_all("nodes => nodes.map(n => n.dataset.path)")] == expected
            page.locator("#close-browser").click()
            page.evaluate("p => openCloudPaths([p])", str(clouds))
            assert page.locator("#clouds .filename").all_text_contents() == [n + ".ply" for n in expected]
            page.evaluate("paths => openCloudPaths(paths, true)", [str(clouds / (n + ".ply")) for n in reversed(expected)])
            assert page.locator(".registration-scan > span").all_text_contents() == [f"{i + 1}. {n}.ply" for i, n in enumerate(expected)]
            page.locator(".registration-scan").nth(1).get_by_role("button", name="Move Setup52.ply up", exact=True).click()
            page.evaluate("refresh()")
            assert page.locator(".registration-scan > span").first.inner_text() == "1. Setup52.ply"
            page.locator("#tab-clouds").click()
            page.set_viewport_size({"width": 420, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(root / "tabs-mobile.png"))
            assert not errors, errors
            browser.close()
        api("shutdown", {})
        assert process.wait(timeout=30) == 0
        print("Chronological files, folder selection, all tabs, and manual override passed")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for option in ("executable", "project", "input", "chrome"):
        parser.add_argument("--" + option, required=True)
    run(parser.parse_args())
