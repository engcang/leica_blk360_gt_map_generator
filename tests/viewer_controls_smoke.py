"""Exercise camera navigation in an actual browser without scan conversion."""

import argparse
import functools
import http.server
import json
import pathlib
import struct
import threading

from playwright.sync_api import sync_playwright


def run(_chrome):
    root = pathlib.Path(__file__).resolve().parents[1]
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root / "web"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=_chrome, headless=True,
                                                 args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda _error: errors.append(str(_error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/viewer.html")
            payload = b"ply\nformat binary_little_endian 1.0\nelement vertex 4000\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n"
            payload += b"".join(struct.pack("<fffH", index % 20 * 0.1, index // 20 % 20 * 0.1, index // 400 * 0.1, index * 13) for index in range(4000))
            page.locator("#file").set_input_files({"name": "navigation.ply", "mimeType": "application/octet-stream", "buffer": payload})
            page.wait_for_function("window.blkViewer?.count === 4000")
            canvas = page.locator("#cloud")
            canvas.scroll_into_view_if_needed()
            canvas.focus()
            before = page.evaluate("JSON.stringify(blkViewer.camera)")
            page.keyboard.press("f")
            page.keyboard.press("Shift+F")
            assert page.evaluate("JSON.stringify(blkViewer.camera)") == before
            eye_before = page.evaluate("blkViewer.cameraEye()")
            canvas.dblclick()
            eye_after = page.evaluate("blkViewer.cameraEye()")
            assert max(abs(a - b) for a, b in zip(eye_before, eye_after)) < 1e-10
            before = page.evaluate("JSON.stringify(blkViewer.camera)")
            assert page.evaluate("document.getElementById('cloud').ondblclick === null")
            assert "Double-click" not in page.locator("body").inner_text()
            canvas.hover()
            page.mouse.wheel(0, -120)
            page.wait_for_function("old => JSON.stringify(blkViewer.camera) !== old", arg=before)
            initial = json.loads(before)
            current = page.evaluate("blkViewer.camera")
            assert current["distance"] == initial["distance"]
            assert current["target"] != initial["target"]
            result = page.evaluate("""() => {
                const viewer = blkViewer, canvas = document.getElementById('cloud');
                const radius = viewer.camera.distance;
                const start = viewer.camera.target.slice();
                for (let i = 0; i < 2000; ++i) canvas.dispatchEvent(new WheelEvent('wheel', {deltaY: -120, cancelable: true}));
                const moved = viewer.camera.target.slice();
                for (let i = 0; i < 2000; ++i) canvas.dispatchEvent(new WheelEvent('wheel', {deltaY: 120, cancelable: true}));
                const recovered = viewer.camera.target.slice();
                viewer.orbit(25, 10);
                viewer.panBy(10, 10);
                return {radius, finalRadius: viewer.camera.distance, moved: Math.hypot(...moved.map((v, i) => v - start[i])),
                        recoveryError: Math.hypot(...recovered.map((v, i) => v - start[i])),
                        finite: Array.from(viewer.matrices().mvp).every(Number.isFinite)};
            }""")
            assert result["radius"] == result["finalRadius"]
            assert result["moved"] > 10 and result["recoveryError"] < 1e-8 and result["finite"]
            page.locator("#projection").select_option("orthographic")
            assert page.evaluate("Array.from(blkViewer.matrices().mvp).every(Number.isFinite)")
            page.locator("#reset").click()
            wall_points = [(x, y * .02, z * .02) for x in (4.5, 0) for y in range(-50, 51) for z in range(-50, 51)]
            header = f"ply\nformat binary_little_endian 1.0\nelement vertex {len(wall_points)}\nproperty float x\nproperty float y\nproperty float z\nproperty ushort intensity\nend_header\n".encode()
            walls = header + b"".join(struct.pack("<fffH", *point, 30000) for point in wall_points)
            page.locator("#file").set_input_files({"name": "near-walls.ply", "mimeType": "application/octet-stream", "buffer": walls})
            page.wait_for_function("n => blkViewer.count === n", arg=len(wall_points))
            page.locator("#density").select_option("1")
            page.locator("#projection").select_option("perspective")
            page.evaluate("""() => {
                Object.assign(blkViewer.camera, {target: [-45, 0, 0], distance: 50, azimuth: 0, elevation: 0, projection: 'perspective'});
                blkViewer.draw();
            }""")
            old_eye = page.evaluate("blkViewer.cameraEye()")
            canvas.hover()
            page.mouse.down()
            focused = page.evaluate("({eye: blkViewer.cameraEye(), target: blkViewer.camera.target, radius: blkViewer.camera.distance})")
            assert max(abs(a - b) for a, b in zip(old_eye, focused["eye"])) < 1e-10
            assert abs(focused["radius"] - .5) < 1e-6, focused
            assert max(abs(a - b) for a, b in zip(focused["target"], [4.5, 0, 0])) < 1e-6
            box = canvas.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2 + 10, box["y"] + box["height"] / 2)
            rotated = page.evaluate("""() => {
                const target = blkViewer.camera.target, m = blkViewer.matrices().mvp;
                const clip = [0, 1, 2, 3].map(row => m[row] * target[0] + m[4 + row] * target[1] + m[8 + row] * target[2] + m[12 + row]);
                return {eye: blkViewer.cameraEye(), target, center: [clip[0] / clip[3], clip[1] / clip[3]]};
            }""")
            page.mouse.up()
            assert rotated["target"] == focused["target"]
            assert sum((a - b)**2 for a, b in zip(old_eye, rotated["eye"]))**.5 < .05
            assert max(abs(v) for v in rotated["center"]) < 1e-5
            fallback = page.evaluate("""() => {
                Object.assign(blkViewer.camera, {target: [10, 0, 0], distance: 1, azimuth: Math.PI, elevation: 0});
                const before = blkViewer.cameraEye();
                blkViewer.focusView(); blkViewer.draw(); blkViewer.orbit(100, 20);
                blkViewer.draw(); blkViewer.orbit(40, -10);
                return Math.hypot(...blkViewer.cameraEye().map((v, i) => v - before[i]));
            }""")
            assert fallback < 1e-10
            ortho = page.evaluate("""() => {
                Object.assign(blkViewer.camera, {target: [-45, 0, 0], distance: 50, azimuth: 0, elevation: 0,
                    projection: 'orthographic', orthographicScale: 2});
                const before = Array.from(blkViewer.matrices().mvp);
                blkViewer.focusView();
                const after = Array.from(blkViewer.matrices().mvp);
                return {scale: blkViewer.camera.orthographicScale,
                    xyError: Math.max(...[0,1,4,5,8,9,12,13].map(i => Math.abs(before[i] - after[i])))};
            }""")
            assert ortho["scale"] == 2 and ortho["xyError"] < 1e-8
            print(json.dumps({"near_surface_radius": focused["radius"], "pivot_screen_position": rotated["center"],
                              "empty_view_eye_displacement": fallback, "orthographic": ortho}, indent=2))
            assert not errors, errors
            print(json.dumps(result, indent=2))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    run(parser.parse_args().chrome)
