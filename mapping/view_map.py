import argparse
import html
import http.server
import json
import pathlib
import re
import secrets
import urllib.parse
import webbrowser


def createViewer(_directory):
    assets = pathlib.Path(__file__).resolve().parents[1] / "web"
    metadata = json.loads((_directory / "map.json").read_text(encoding="utf-8"))
    registration_path = _directory / "registration.json"
    focus = None
    if registration_path.exists():
        registration = json.loads(registration_path.read_text(encoding="utf-8"))
        stations = [[record["transform_map_from_scan"][axis][3] for axis in range(3)] for record in registration["scans"]]
        minimum = [min(point[axis] for point in stations) for axis in range(3)]
        maximum = [max(point[axis] for point in stations) for axis in range(3)]
        focus = {"target": [(a + b) / 2 for a, b in zip(minimum, maximum)],
                 "distance": max(10, 1.7 * sum((b - a) ** 2 for a, b in zip(minimum, maximum)) ** 0.5)}
    document = (assets / "viewer.html").read_text(encoding="utf-8")
    document = document.replace('<link rel="stylesheet" href="viewer.css"><script src="viewer.js" defer></script>',
                                "<style>" + (assets / "viewer.css").read_text(encoding="utf-8") + "</style>")
    document = document.replace("scanner-local coordinates in metres", "registered map coordinates in metres")
    document = document.replace("PLY coordinates in metres", "registered map coordinates in metres")
    document = document.replace("Experimental decoder. No leveling or registration. Accuracy against an official export has not been verified.",
                                "Experimental registered reference map. Scanner-frame axes; no surveyed leveling. Independent accuracy has not been established.")
    document = document.replace("Coordinates are displayed as stored in the PLY. Accuracy against an official export has not been verified.",
                                "Experimental registered reference map. Scanner-frame axes; no surveyed leveling. Independent accuracy has not been established.")
    document = document.replace("Full-resolution point cloud</h1>", html.escape(metadata["file"]) + "</h1>")
    summary = f'<p>{metadata["points"]:,} map points | {metadata["voxel_m"] * 100:g} cm merge voxels | Intensity preserved by averaging. Open the map PLY below when using this HTML offline.</p>'
    document = document.replace('<div class="load-controls">', summary + '<div class="load-controls">')
    bootstrap = """
    (async () =>
    {
        const token = new URLSearchParams(location.hash.slice(1)).get('token');
        if (!token || location.protocol === 'file:') return;
        try
        {
            const headers = {'X-Map-Token': token};
            const response = await fetch('/map.json', {headers});
            if (!response.ok) throw Error('Unable to read map metadata.');
            const map = await response.json();
            const head = await fetch('/map.ply', {method: 'HEAD', headers});
            if (!head.ok) throw Error('Unable to open the map PLY.');
            const size = Number(head.headers.get('Content-Length'));
            const source = {
                size,
                slice: (start, end) => ({
                    arrayBuffer: async () => {
                        const response = await fetch('/map.ply', {
                            headers: {...headers, Range: `bytes=${start}-${Math.min(end, size) - 1}`}
                        });
                        if (response.status !== 206) throw Error('Map range read failed.');
                        return response.arrayBuffer();
                    }
                })
            };
            await window.blkViewer.loadPly(source, map.file, map.points);
            const focus = MAP_FOCUS;
            if (focus && window.blkViewer.count)
            {
                window.blkViewer.camera.target = focus.target;
                window.blkViewer.camera.distance = focus.distance;
                window.blkViewer.draw();
            }
        }
        catch (error)
        {
            document.getElementById('error').textContent = error.message;
        }
    })();
    """
    bootstrap = bootstrap.replace("MAP_FOCUS", json.dumps(focus))
    script = (assets / "viewer.js").read_text(encoding="utf-8")
    document = document.replace("</body>", "<script>" + script + "</script><script>" + bootstrap + "</script></body>")
    target = _directory / "map.html"
    target.write_text(document, encoding="utf-8")
    return target


def serve(_directory, _open_browser=True):
    directory = _directory.resolve()
    viewer = createViewer(directory)
    metadata = json.loads((directory / "map.json").read_text(encoding="utf-8"))
    ply = (directory / metadata["file"]).resolve()
    if ply.parent != directory or ply.suffix != ".ply" or not ply.is_file():
        raise ValueError("The map must be a PLY directly inside the selected directory")
    token = secrets.token_urlsafe(32)
    routes = {"/": viewer, "/map.html": viewer, "/map.ply": ply, "/map.json": directory / "map.json"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def logMessage(self, _format, *_args):
            pass

        def respond(self, _body):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if self.headers.get("Host") != expected_host or (origin and origin != f"http://{expected_host}"):
                self.send_error(403)
                return
            route = urllib.parse.urlsplit(self.path).path
            path = routes.get(route)
            if path is None:
                self.send_error(404)
                return
            if route in ("/map.ply", "/map.json") and not secrets.compare_digest(self.headers.get("X-Map-Token", ""), token):
                self.send_error(403)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                if not match:
                    self.send_error(416)
                    return
                start = int(match[1])
                end = min(int(match[2]) if match[2] else end, end)
                if start > end:
                    self.send_error(416)
                    return
            self.send_response(206 if requested else 200)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Type", {".html": "text/html; charset=utf-8", ".json": "application/json"}.get(path.suffix, "application/octet-stream"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if _body:
                try:
                    with path.open("rb") as stream:
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            block = stream.read(min(1024 * 1024, remaining))
                            if not block:
                                break
                            self.wfile.write(block)
                            remaining -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def doGet(self):
            self.respond(True)

        def doHead(self):
            self.respond(False)

        #Codex: BaseHTTPRequestHandler requires these protocol callback names.
        do_GET = doGet
        do_HEAD = doHead
        log_message = logMessage

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    session = directory / "viewer_session.json"
    session.write_text(json.dumps({"url": url}) + "\n", encoding="utf-8")
    print(url, flush=True)
    if _open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        session.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Open a registered PLY map in the local BLK Studio viewer")
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    serve(args.directory, not args.no_browser)


if __name__ == "__main__":
    main()
