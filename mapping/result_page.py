"""Render a portable registration review with optional BLK Studio generation controls."""

import html
import json
import pathlib
import shutil


def writeProductSnapshot(_directory):
    assets = _directory / "html_source"
    assets.mkdir(parents=True, exist_ok=True)
    manifest = assets / "products.json"
    products = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    saved = {}
    for kind in ("raw", "voxel", "share", "voxel3cm", "share3cm"):
        product = products.get(kind)
        if not product:
            continue
        filename = product.get("file", "")
        if not filename or pathlib.Path(filename).name != filename or "/" in filename or "\\" in filename:
            continue
        path = _directory / filename
        if not path.is_file() or path.stat().st_size != product.get("bytes"):
            continue
        saved[kind] = {key: product[key] for key in ("file", "points", "bytes")}
    #Codex: A local script can load under file:// without fetch permissions or session credentials.
    script = "window.BLK_SAVED_PRODUCTS = " + json.dumps(saved, ensure_ascii=True, allow_nan=False) + ";\n"
    pending = assets / "products.js.pending"
    pending.write_text(script, encoding="utf-8")
    pending.replace(assets / "products.js")


def writeReviewPage(_directory):
    assets = _directory / "html_source"
    summary = json.loads((assets / "review_summary.json").read_text(encoding="utf-8"))
    if (assets / "quality.json").is_file():
        from registration_quality import writeQualityPage
        writeQualityPage(json.loads((assets / "quality.json").read_text(encoding="utf-8")), assets)
    web = pathlib.Path(__file__).resolve().parents[1] / "web"
    shutil.copyfile(web / "result_manager.js", assets / "result_manager.js")
    shutil.copyfile(web / "result_manager.css", assets / "result_manager.css")
    writeProductSnapshot(_directory)
    stations = summary["stations"]
    rows = "".join(f"<tr><td style='border-left:8px solid {station['color']}'>{station['station']}</td><td>{html.escape(station['name'])}</td><td>L{station['inferred_level']}</td><td>{station['plot_xyz_m'][2]:+.3f}</td></tr>" for station in stations)
    images = "".join(f'<a href="html_source/{html.escape(name, quote=True)}"><img src="html_source/{html.escape(name, quote=True)}" alt="{html.escape(name, quote=True)}" loading="lazy"></a>' for name in summary["images"])
    quality = ("<p><a href='html_source/quality.html' target='_blank' rel='noopener'>Open full quality table</a></p>"
               "<iframe title='Scan-pair quality metrics' src='html_source/quality.html'></iframe>"
               if (assets / "quality.html").is_file() else "<p>Quality metrics are unavailable for this registration.</p>")
    document = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Registration review and management</title><link rel='stylesheet' href='html_source/result_manager.css'>"
                "<script src='html_source/products.js' defer></script><script src='html_source/result_manager.js' defer></script></head><body>"
                f"<h1>Registration review and management</h1><p>{len(stations)} registered scans &middot; "
                "<a href='#products'>Generate files</a> &middot; <a href='#quality'>Quality metrics</a> &middot; <a href='#alignment'>Alignment images</a></p>"
                "<section id='products'><h2>Files</h2>"
                "<div id='offline-help' hidden>"
                "<button id='check-local-files'>Check output folder</button><input id='local-folder' type='file' webkitdirectory multiple hidden>"
                "<p id='local-files-status' role='status'></p></div>"
                "<div class='products'><article><h3>Merged PLY</h3>"
                "<label>Resolution <select id='voxel-resolution'><option value='voxel'>1 cm</option><option value='voxel3cm'>3 cm</option></select></label>"
                "<button data-product='voxel' disabled>Generate 1 cm PLY</button><div id='voxel-result'></div></article>"
                "<article><h3>Full raw merged PLY</h3>"
                "<button data-product='raw' disabled>Generate raw PLY</button><div id='raw-result'></div></article>"
                "<article><h3>Standalone share HTML</h3>"
                "<label>Resolution <select id='share-resolution'><option value='share'>1 cm</option><option value='share3cm'>3 cm</option></select></label>"
                "<button data-product='share' disabled>Generate share HTML</button><div id='share-result'></div></article></div>"
                "<p id='connection-status' role='status'></p>"
                "<p id='generation-status' role='status'></p><button id='stop-generation' hidden>Stop generation</button>"
                "<button id='open-output' hidden>Open output folder</button></section>"
                "<dialog id='app-required'><h2>Open in BLK Studio</h2>"
                "<p>Choose Review and manage, or Open existing result and select relative_poses.json, then generate the file.</p>"
                "<form method='dialog'><button>Got it</button></form></dialog>"
                f"<section id='quality'><h2>Scan-pair quality</h2>{quality}</section>"
                "<section id='alignment'><h2>Alignment and station positions</h2>"
                f"<table><thead><tr><th>Station</th><th>Scan</th><th>Inferred level</th><th>Relative height (m)</th></tr></thead><tbody>{rows}</tbody></table>"
                f"{images}</section></body></html>")
    pending = _directory / "result_review_and_manage.html.pending"
    pending.write_text(document, encoding="utf-8")
    pending.replace(_directory / "result_review_and_manage.html")
