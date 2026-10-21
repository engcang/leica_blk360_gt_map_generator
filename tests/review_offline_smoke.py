"""Check direct-file review, saved products, and local folder verification in a browser."""

import argparse
import json
import pathlib
import tempfile

from playwright.sync_api import sync_playwright


def run(_args):
    review = pathlib.Path(_args.review).resolve(strict=True)
    products = json.loads((review.parent / "html_source/products.json").read_text(encoding="utf-8"))
    errors = []
    network = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=_args.chrome, headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda _error: errors.append(str(_error)))
        page.on("request", lambda _request: network.append(_request.url) if _request.url.startswith(("http:", "https:")) else None)
        page.goto(review.as_uri())
        assert page.locator("#offline-help").is_visible()
        assert page.locator("a[download]").count() == 0

        def selectProduct(kind):
            base = kind.removesuffix("3cm")
            if base != "raw":
                page.locator(f"#{base}-resolution").select_option(kind)
            return page.locator(f"#{base}-result")

        for kind, product in products.items():
            if not (review.parent / product["file"]).is_file():
                continue
            container = selectProduct(kind)
            assert product["file"] in container.inner_text()
            if kind.startswith("share"):
                assert container.locator("a").evaluate("node => node.href") == (review.parent / product["file"]).as_uri()
            else:
                assert container.locator("a").count() == 0
        for kind in ("raw", "voxel", "share"):
            page.locator(f"[data-product={kind}]").click()
            assert page.locator("#app-required").is_visible()
            page.get_by_role("button", name="Got it").click()
        page.locator("#local-folder").set_input_files(str(review.parent))
        page.wait_for_function("document.getElementById('local-files-status').textContent.startsWith('Checked saved')")
        for kind, product in products.items():
            if (review.parent / product["file"]).is_file():
                container = selectProduct(kind)
                if kind.startswith("share"):
                    assert container.locator("a").evaluate("node => node.href").startswith("blob:")
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "relative_poses.json").write_text("{}", encoding="utf-8")
            page.locator("#local-folder").set_input_files(directory)
            for kind in products:
                container = selectProduct(kind)
                container.get_by_text("missing or size changed", exact=False).wait_for()
                assert container.locator("a").count() == 0
        assert not errors, errors
        assert not network, "Offline review made network requests"
        browser.close()
    print(f"Offline review passed: {len(products)} saved products, generation instructions, local folder verification, missing-file detection; no network requests.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True)
    parser.add_argument("--chrome", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
