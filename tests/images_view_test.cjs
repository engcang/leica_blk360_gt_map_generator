const fs = require('fs'), vm = require('vm'), path = require('path'), assert = require('assert/strict');
const web = name => fs.readFileSync(path.join(__dirname, '../web', name), 'utf8');
const near = (actual, expected, tolerance, message) => assert.ok(Math.abs(actual - expected) <= tolerance, `${message}: ${actual} != ${expected}`);

const math = vm.createContext({Math});
vm.runInContext(web('images_math.js'), math);

const texel = direction => math.panoramaTexel(direction);
near(texel([1, 0, 0])[0], 0.5, 1e-9, 'Forward is the horizontal centre');
near(texel([1, 0, 0])[1], 0.5, 1e-9, 'Horizon is the vertical centre');
near(texel([0, 1, 0])[0], 0.25, 1e-9, '+y is left of centre');
near(texel([0, -1, 0])[0], 0.75, 1e-9, '-y is right of centre');
near(texel([-1, 0, 0])[0], 0, 1e-9, 'Backward wraps to the edge');
near(texel([0, 0, 1])[1], 0, 1e-9, 'Zenith is the top row');
near(texel([0, 0, -1])[1], 1, 1e-9, 'Nadir is the bottom row');
math.viewDirection(0, 0).forEach((value, i) => near(value, [1, 0, 0][i], 1e-9, 'Initial view faces +x'));
near(texel(math.viewDirection(-Math.PI / 2, 0))[0], 0.75, 1e-9, 'Turning right moves right in the panorama');

const axis = math.cameraAxis([0, 0, 0, 0, 0, 0, -0.01, 0.82, -0.57]);
near(axis.azimuth, 90.70, 0.1, 'Camera azimuth');
near(axis.elevation, -34.75, 0.1, 'Camera elevation');
assert.equal(math.cameraGroup(-35), 'Down');
assert.equal(math.cameraGroup(13), 'Level');
assert.equal(math.cameraGroup(62), 'Up');

const panoramas = math.imageTabs({colour_path: 'a', intensity_path: 'b'});
assert.deepEqual(Array.from(panoramas, tab => tab.id), ['colour', 'intensity', 'camera']);
assert.deepEqual(Array.from(panoramas, tab => tab.enabled), [true, true, false]);
assert.equal(panoramas[2].reason, 'Convert with Export camera images to view camera images.');
assert.equal(math.firstEnabledTab(panoramas), 'colour');
const cameras = math.imageTabs({images_path: 'c', image_count: 28});
assert.equal(cameras[2].label, 'Camera images (28)');
assert.deepEqual(Array.from(cameras, tab => tab.enabled), [false, false, true]);
assert.equal(cameras[0].reason, 'Convert with Export panoramas to view panoramas.');
assert.equal(cameras[2].reason, '');
assert.equal(math.firstEnabledTab(cameras), 'camera');
const none = math.imageTabs(undefined);
assert.deepEqual(Array.from(none, tab => tab.enabled), [false, false, false]);
assert.equal(math.firstEnabledTab(none), null);

assert.deepEqual(Array.from(math.flatAngles(0.5, 0.5)), [0, 0]);
assert.deepEqual(Array.from(math.flatAngles(0, 0.5)), [180, 0]);
assert.deepEqual(Array.from(math.flatAngles(0.75, 0)), [-90, 90]);
assert.equal(math.keepsDecodedImage('colour'), true);
assert.equal(math.keepsDecodedImage('intensity'), true);
assert.equal(math.keepsDecodedImage('LDR_Image_9'), false);
assert.deepEqual(Array.from(math.textureSize(8192, 4096, 16384)), [8192, 4096]);
assert.deepEqual(Array.from(math.textureSize(16384, 8192, 16384)), [8192, 4096]);
assert.deepEqual(Array.from(math.textureSize(8192, 4096, 4096)), [4096, 2048]);

class Element
{
    constructor() { this.dataset = {}; this.children = []; this.value = ''; this.textContent = ''; this.disabled = false; this.handlers = {}; this.attributes = {}; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(type, callback) { this.handlers[type] = callback; }
}
const nodes = new Map(), element = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
const app = vm.createContext({console, setTimeout, clearTimeout, setInterval: () => {}, URLSearchParams,
    document: {getElementById: element, createElement: () => new Element(), querySelectorAll: () => [], addEventListener: () => {}},
    location: {origin: 'http://test', hash: '#tok', pathname: '/'}, history: {replaceState: () => {}},
    sessionStorage: {getItem: () => '', setItem: () => {}}, window: {addEventListener: () => {}},
    fetch: () => { throw Error('Unexpected network access'); }, confirm: () => true});
vm.runInContext(web('app.js'), app);
const renderJob = vm.runInContext('renderJob', app);
const completed = {id: 'j1', name: 'S.blk', status: 'completed', bytes: 1, output_path: '/o/S.ply', result: {valid_points: 1, panorama: {colour_path: '/o/S_panorama.jpg'}}};
const actions = renderJob(completed).children[2].children;
assert.deepEqual(actions.slice(0, 3).map(child => child.textContent), ['View 3D ↗', 'View Images ↗', 'PLY ↓']);
assert.equal(actions[1].href, '/images.html?scan=j1#tok');
assert.equal(actions[1].target, '_blank');
assert.equal(actions[1].rel, 'noopener');
assert.notEqual(actions[1].attributes['aria-disabled'], 'true');
const disabled = renderJob({...completed, result: {valid_points: 1}}).children[2].children[1];
assert.equal(disabled.textContent, 'View Images ↗');
assert.equal(disabled.href, undefined);
assert.equal(disabled.attributes['aria-disabled'], 'true');
assert.equal(disabled.title, 'Convert with Export panoramas or Export camera images to view images.');
assert.match(disabled.className, /\bdisabled\b/);
const ready = renderJob({...completed, status: 'ready', result: null});
assert.ok(!ready.children[2].children.some(child => child.textContent === 'View Images ↗'));

const page = web('images.html');
assert.doesNotMatch(page, /intensity/i);
for (const asset of ['href="viewer.css"', 'href="images.css"', 'src="images_math.js"', 'src="images.js"']) assert.ok(page.includes(asset), `${asset} missing`);
assert.ok(page.indexOf('src="images_math.js"') < page.indexOf('src="images.js"'), 'images_math.js must load before images.js');
for (const id of ['tabs', 'mode', 'reset', 'status', 'panorama', 'flat', 'camera-list', 'camera-info', 'error', 'readout', 'controls', 'stage', 'help']) assert.ok(page.includes(`id="${id}"`), `#${id} missing`);
assert.ok(page.indexOf('id="error"') < page.indexOf('id="stage"'), 'The failure reason must appear above the image area');

console.log('PASS');
