"""Exercise selection, image loading, and layout of a saved chunk review."""
import argparse
from pathlib import Path
import json

from playwright.sync_api import sync_playwright


def run(path, chrome, screenshots=None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chrome, headless=True,
            args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(path.resolve().as_uri(), wait_until='load')
        data = page.locator('#chunk-data').text_content()
        chunks = json.loads(data)['chunks']
        assert page.locator('iframe').count() == 1
        assert page.locator('a[download], button[download]').count() == 0
        for index, chunk in enumerate(chunks):
            page.locator('#chunk-select').select_option(str(index))
            assert page.locator('#stations tr').count() == len(chunk['scans'])
            frame = page.locator('#chunk-view')
            assert frame.get_attribute('src') == chunk['viewer']
            assert frame.bounding_box()['width'] > 1200
            frame.content_frame.locator('body[data-loaded]').wait_for(timeout=60000)
            for level in chunk['alignment']['levels']:
                page.locator('#level-select').select_option(str(level['id']))
                assert page.locator('#stations tr').count() == len(level['stations'])
                assert page.locator('#alignment-images img').count() == 1
                page.wait_for_function("Array.from(document.querySelectorAll('#alignment-images img')).every(i => i.complete && i.naturalWidth > 0)")
            assert page.locator('#previous-chunk').is_disabled() == (index == 0)
            assert page.locator('#next-chunk').is_disabled() == (index == len(chunks)-1)
        page.locator('#previous-chunk').click()
        assert page.locator('#chunk-select').input_value() == str(len(chunks)-2)
        page.reload(wait_until='load')
        assert page.locator('#chunk-select').input_value() == str(len(chunks)-2)
        page.locator('#chunk-select').select_option('0')
        page.locator('#chunk-view').content_frame.locator('body[data-loaded]').wait_for(timeout=60000)
        if screenshots:
            screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshots/'chunk-review.png'))
            levels = chunks[0]['alignment']['levels']
            page.locator('#level-select').select_option(str(levels[min(1,len(levels)-1)]['id']))
            page.locator('#alignment-images img').scroll_into_view_if_needed()
            page.locator('#alignment-images').screenshot(path=str(screenshots/'chunk-level.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function('document.documentElement.scrollWidth <= window.innerWidth')
        assert not errors, errors
        print(f'PASS: {len(chunks)} chunks, all height-level images, single 3D view, navigation, reload and mobile layout')
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('page', type=Path)
    parser.add_argument('--chrome', default='/usr/bin/google-chrome')
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    run(args.page, args.chrome, args.screenshots)
