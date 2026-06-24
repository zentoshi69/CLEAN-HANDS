/*
 * test_play_wallet_fallback.mjs — guards the game Buy sheet wallet fallback.
 *
 * The preview/Codex browser often cannot access desktop wallet extensions even
 * when the user has Phantom installed in Chrome/Brave. The game must not treat
 * that as "go install Phantom" and navigate to Phantom's download page.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';
import assert from 'assert';

const dir = path.dirname(fileURLToPath(import.meta.url));
const play = readFileSync(path.join(dir, 'play.html'), 'utf8');

assert.ok(
  !play.includes('phantom.app/download'),
  'desktop/preview fallback must not open Phantom download',
);
assert.ok(
  play.includes('window.phantom&&window.phantom.solana'),
  'Phantom provider must be checked explicitly before generic window.solana',
);
assert.ok(
  play.includes('This preview browser can’t access wallet extensions'),
  'preview users need a clear extension-injection explanation',
);
assert.ok(
  play.includes("btn.textContent='Copy app link'"),
  'preview fallback should let the user copy/open the app link instead of leaving',
);

console.log('play wallet preview fallback ✓');
