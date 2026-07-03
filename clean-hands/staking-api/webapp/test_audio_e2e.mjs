/* Game audio E2E — the regression suite for "the music doesn't play".
 *
 * Drives /play in a real (headless) Chromium and asserts the full audio
 * contract end-to-end:
 *
 *   1. BOOT       — no uncaught page errors (the 2026-07 music outage was a
 *                   `LEVELS is not defined` ReferenceError thrown on the very
 *                   first gesture: the audio layer reached into the engine
 *                   <script>'s closure, so music NEVER played).
 *   2. FIRST TAP  — level 1's track is audibly playing right after START
 *                   (guards the warm-up race that paused the track it had
 *                   just started), and ALL level tracks are created/warmed
 *                   inside that first gesture (iOS unlock).
 *   3. SELF-HEAL  — the FIRST request for one track is aborted; the game must
 *                   retry with a ?cb= cache-buster and the retry must reach
 *                   the server. No hard refresh, ever.
 *   4. LEVEL-UP   — reaching level 2 switches to level 2's OWN track.
 *   5. FINALE     — level 11 plays the SAME track as level 1 (full circle);
 *                   levels 1-10 each use a distinct file.
 *
 * Run (CI does exactly this — see .github/workflows/ci.yml):
 *   cd clean-hands/staking-api/webapp
 *   npm install --no-save playwright && npx playwright install --with-deps chromium
 *   # start app.py on :8097, then:
 *   BASE=http://127.0.0.1:8097 node test_audio_e2e.mjs
 *
 * Local sandboxes: CLEAN_CHROMIUM=/path/to/chromium overrides the browser
 * binary; HTTPS_PROXY (if set) is bypassed for localhost automatically.
 */
import { chromium } from 'playwright';

const BASE = process.env.BASE || 'http://127.0.0.1:8097';
const BLOCKED = '/audio/Out%20Here.mp3'; // level-2 track: first plain request gets aborted

const launch = { args: ['--autoplay-policy=no-user-gesture-required'] };
if (process.env.CLEAN_CHROMIUM) launch.executablePath = process.env.CLEAN_CHROMIUM;
if (process.env.HTTPS_PROXY) launch.proxy = { server: process.env.HTTPS_PROXY, bypass: 'localhost,127.0.0.1' };

const browser = await chromium.launch(launch);
const ctx = await browser.newContext();

// 3) abort the FIRST plain request for the blocked track; let ?cb= retries through
let abortedOnce = false;
await ctx.route(`**${BLOCKED}*`, (route) => {
  if (!route.request().url().includes('cb=') && !abortedOnce) { abortedOnce = true; return route.abort(); }
  return route.continue();
});

const page = await ctx.newPage();

// record every Audio() the game creates, every play() call, and the live elements
await page.addInitScript(() => {
  window.__auLog = { created: [], els: [] };
  const N = window.Audio;
  window.Audio = function (src) {
    const a = new N(src);
    window.__auLog.els.push(a);
    if (src && !String(src).startsWith('data:')) window.__auLog.created.push(String(src));
    return a;
  };
});

const errors = [];
page.on('pageerror', (e) => errors.push((e.stack || String(e)).split('\n').slice(0, 3).join(' | ')));

const audioReqs = [];
page.on('request', (r) => { if (r.url().includes('/audio/')) audioReqs.push(r.url()); });

await page.goto(BASE + '/play', { waitUntil: 'load' });
await page.waitForSelector('#splashBtn', { timeout: 20000 });
await page.click('#splashBtn'); // real user gesture → audio unlock + startMusic
await page.waitForTimeout(9000); // warm-up + first backoff retry (fires ~4s after the load error)

// which network-served track is audibly playing right now
const nowPlaying = () => page.evaluate(() =>
  window.__auLog.els
    .filter((a) => a.src && !a.src.startsWith('data:') && !a.paused && !a.muted && a.volume > 0.01)
    .map((a) => decodeURIComponent(new URL(a.src).pathname)));

// the level table straight from the engine's public surface
const levels = await page.evaluate(() =>
  (window.__game.LEVELS || []).map((l) => decodeURIComponent(l.track || '')));

const fail = (msg) => { console.error('FAIL:', msg); process.exitCode = 1; };

// 5a) static invariant: 11 levels, 1-10 all distinct, finale === level 1
if (levels.length !== 11) fail(`expected 11 levels, got ${levels.length}`);
if (new Set(levels.slice(0, 10)).size !== 10) fail(`levels 1-10 tracks not unique: ${levels.slice(0, 10)}`);
if (levels[10] !== levels[0]) fail(`finale track ${levels[10]} !== level-1 track ${levels[0]}`);

// 2) all tracks created inside the first gesture + level 1 audible
const created = await page.evaluate(() =>
  [...new Set(window.__auLog.created.map((u) => decodeURIComponent(u).replace(/[?&]cb=[^&]*/, '')))]);
const missing = [...new Set(levels)].filter((t) => !created.some((u) => u.endsWith(t)));
if (missing.length) fail(`tracks never created on first gesture: ${missing}`);
const lv1 = await nowPlaying();
if (!lv1.some((u) => u.endsWith(levels[0]))) fail(`level-1 track not playing after START (playing: ${lv1})`);

// 3) self-heal actually happened
if (!abortedOnce) fail('test harness never aborted the blocked track (route not hit)');
const retryHit = audioReqs.find((u) => u.includes('Out%20Here') && u.includes('cb='));
if (!retryHit) fail('no cache-buster retry reached the server for the aborted track');

// 4) level-up → level 2's own track
await page.evaluate(() => { window.__game.S.total = 99999; });
await page.click('#hands');
await page.waitForTimeout(1500); // 400ms crossfade + slack
const lv2 = await nowPlaying();
if (!lv2.some((u) => u.endsWith(levels[1]))) fail(`level-2 track not playing after level-up (playing: ${lv2})`);

// 5b) finale plays level 1's track again
await page.evaluate(() => { window.__game.S.total = 999999999; });
await page.click('#hands');
await page.waitForTimeout(1500);
const atLevel = await page.evaluate(() => window.__game.S.level);
const lv11 = await nowPlaying();
if (atLevel !== 11) fail(`expected level 11 after total jump, got ${atLevel}`);
if (!lv11.some((u) => u.endsWith(levels[0]))) fail(`finale not playing level-1 track (playing: ${lv11})`);

// 1) no uncaught page errors anywhere in the flow
if (errors.length) fail(`uncaught page errors: ${JSON.stringify(errors, null, 2)}`);

await browser.close();
if (process.exitCode) process.exit(process.exitCode);
console.log('AUDIO E2E PASSED — music starts on first tap, per-level tracks switch, finale mirrors level 1, failed loads self-heal');
