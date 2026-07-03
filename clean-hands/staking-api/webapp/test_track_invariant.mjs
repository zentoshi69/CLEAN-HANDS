/* Per-level music INVARIANT lock — the guard that makes the fix permanent.
 *
 * The rule, stated once and enforced forever:
 *   • exactly 11 levels
 *   • levels 1–10 each play their OWN track (10 distinct files, each used once)
 *   • level 11 (the finale) reuses level 1's track
 *   • every track file actually exists under webapp/audio/
 *   • the wiring that made music work is intact: window.__game exports LEVELS,
 *     and the audio layer reads it via G.LEVELS (NOT a cross-<script> closure).
 *     This is the exact bug that shipped silent-music to production
 *     ("LEVELS is not defined" on the first tap) — pinning it here means it can
 *     never regress again.
 *
 * Pure static analysis: no browser, no server, no deps — so it always runs in
 * CI and can never flake. If someone edits the LEVELS table or the __game
 * surface in a way that breaks the rule, the build goes red before it can ship.
 *
 * Run:  node test_track_invariant.mjs        (checks play.html + standalone.html)
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const AUDIO_DIR = join(HERE, 'audio');

// play.html is canonical; standalone.html is its enforced mirror. Check both so
// a hand-edit to either is caught.
const TARGETS = [
  { label: 'play.html', path: join(HERE, 'play.html') },
  { label: 'standalone.html', path: join(HERE, '..', '..', '..', 'clean-hands-game', 'standalone.html') },
];

let failures = 0;
const fail = (label, msg) => { console.error(`✗ ${label}: ${msg}`); failures++; };

function extractLevels(src) {
  const block = src.match(/const\s+LEVELS\s*=\s*\[([\s\S]*?)\];/);
  if (!block) return null;
  // one {track:'...'} per level, in order
  return [...block[1].matchAll(/track\s*:\s*'([^']*)'/g)].map((m) => m[1]);
}

for (const { label, path } of TARGETS) {
  if (!existsSync(path)) { fail(label, `file not found at ${path}`); continue; }
  const src = readFileSync(path, 'utf8');

  const tracks = extractLevels(src);
  if (!tracks) { fail(label, 'could not find the const LEVELS=[...] table'); continue; }

  // 1) exactly 11 levels
  if (tracks.length !== 11) fail(label, `expected 11 levels, found ${tracks.length}`);

  // 2) levels 1–10 all distinct
  const firstTen = tracks.slice(0, 10);
  const distinct = new Set(firstTen);
  if (firstTen.length === 10 && distinct.size !== 10) {
    const seen = {}, dupes = [];
    for (const t of firstTen) { seen[t] = (seen[t] || 0) + 1; if (seen[t] === 2) dupes.push(t); }
    fail(label, `levels 1–10 must be 10 DISTINCT tracks; duplicated: ${dupes.join(', ')}`);
  }

  // 3) finale (level 11) reuses level 1
  if (tracks.length === 11 && tracks[10] !== tracks[0]) {
    fail(label, `level 11 must reuse level 1's track — level1='${tracks[0]}' level11='${tracks[10]}'`);
  }

  // 4) every referenced track file exists (check against play.html's sibling audio dir)
  if (label === 'play.html') {
    for (const t of [...new Set(tracks)]) {
      const name = decodeURIComponent(t.replace(/^\/audio\//, ''));
      if (!existsSync(join(AUDIO_DIR, name))) fail(label, `track file missing on disk: audio/${name}`);
    }
  }

  // 5) the wiring that makes music actually play — the regression that shipped silence
  const gameSurface = src.match(/window\.__game\s*=\s*\{([\s\S]*?)\};/);
  if (!gameSurface) fail(label, 'window.__game surface not found');
  else if (!/\bLEVELS\b/.test(gameSurface[1])) {
    fail(label, 'window.__game must export LEVELS (its absence is the "LEVELS is not defined" silent-music bug)');
  }
  if (!/\bLEVELS\s*=\s*G\.LEVELS\b/.test(src)) {
    fail(label, 'audio layer must read the level table via G.LEVELS, not a cross-<script> closure');
  }

  if (!failures) console.log(`✓ ${label}: 11 levels, 1–10 distinct, finale=level 1, files present, wiring intact`);
}

if (failures) {
  console.error(`\nPER-LEVEL MUSIC INVARIANT BROKEN (${failures}). This must never ship — fix the LEVELS table / __game surface above.`);
  process.exit(1);
}
console.log('\nPER-LEVEL MUSIC INVARIANT OK — 10 tracks for levels 1–10, level 11 == level 1, wiring locked.');
