/*
 * test_tg_e2e.mjs — CROSS-IMPLEMENTATION end-to-end of the Telegram wallet
 * handshake: this script plays Phantom using the real tweetnacl (the same
 * crypto library Phantom ships), over real HTTP, against the real server
 * (PyNaCl on the other side). Catches any interop drift that same-language
 * unit tests cannot: base58 alphabets, nonce handling, box framing, signed
 * message bytes.
 *
 *   # terminal 1
 *   STAKE_ENV=dev STAKE_PORT=8096 TG_COMMUNITY_TOKEN=123456:TEST_token \
 *     MINIAPP_URL=http://127.0.0.1:8096 python app.py
 *   # terminal 2
 *   BASE=http://127.0.0.1:8096 TG_COMMUNITY_TOKEN=123456:TEST_token \
 *     node webapp/test_tg_e2e.mjs
 */
import { createRequire } from 'module';
import { fileURLToPath } from 'url';
import path from 'path';
import crypto from 'crypto';
import assert from 'assert';

const dir = path.dirname(fileURLToPath(import.meta.url));
const nacl = createRequire(import.meta.url)(path.join(dir, 'nacl.min.js'));

const BASE = process.env.BASE || 'http://127.0.0.1:8096';
const BOT_TOKEN = process.env.TG_COMMUNITY_TOKEN || '123456:TEST_token';

// ---- base58 (Bitcoin alphabet) — standalone copy for the test ------------- //
const ALPH = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function b58e(bytes) {
  const d = [0];
  for (const b of bytes) {
    let c = b;
    for (let j = 0; j < d.length; j++) {
      c += d[j] << 8;
      d[j] = c % 58;
      c = (c / 58) | 0;
    }
    while (c) {
      d.push(c % 58);
      c = (c / 58) | 0;
    }
  }
  let s = '';
  for (let k = 0; bytes[k] === 0 && k < bytes.length - 1; k++) s += '1';
  for (let q = d.length - 1; q >= 0; q--) s += ALPH[d[q]];
  return s;
}
function b58d(str) {
  const MAP = {};
  for (let i = 0; i < ALPH.length; i++) MAP[ALPH[i]] = i;
  const bytes = [0];
  for (const ch of str) {
    let c = MAP[ch];
    if (c === undefined) throw new Error('bad b58');
    for (let j = 0; j < bytes.length; j++) {
      c += bytes[j] * 58;
      bytes[j] = c & 0xff;
      c >>= 8;
    }
    while (c) {
      bytes.push(c & 0xff);
      c >>= 8;
    }
  }
  for (let k = 0; str[k] === '1' && k < str.length - 1; k++) bytes.push(0);
  return new Uint8Array(bytes.reverse());
}

// ---- forge valid Telegram initData (same HMAC the server verifies) -------- //
function initData(uid) {
  const pairs = {
    user: JSON.stringify({ id: uid, username: 'e2e' + uid }),
    auth_date: String(Math.floor(Date.now() / 1000)),
  };
  const dcs = Object.keys(pairs)
    .sort()
    .map((k) => `${k}=${pairs[k]}`)
    .join('\n');
  const secret = crypto.createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
  pairs.hash = crypto.createHmac('sha256', secret).update(dcs).digest('hex');
  return new URLSearchParams(pairs).toString();
}

const enc = new TextEncoder();
const dec = new TextDecoder();
const idata = initData(880099);

// the "wallet": an ed25519 signing key (the Solana keypair) + x25519 box key
const solKp = nacl.sign.keyPair();
const walletAddr = b58e(solKp.publicKey);
const boxKp = nacl.box.keyPair();

const j = async (r) => {
  if (!r.ok) assert.fail(`${r.url} -> ${r.status} ${await r.text().catch(() => '')}`);
  return r.json();
};

// 1) start
const start = await j(
  await fetch(BASE + '/api/tg/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: idata, wallet: 'phantom' }),
  }),
);
assert.ok(start.sid && start.dapp_pub, 'start returns sid + server pubkey');
const shared = nacl.box.before(b58d(start.dapp_pub), boxKp.secretKey);

// 2) connect callback — encrypt {public_key, session} exactly like Phantom
{
  const nonce = nacl.randomBytes(24);
  const ct = nacl.box.after(
    enc.encode(JSON.stringify({ public_key: walletAddr, session: 'phantom-sess' })),
    nonce,
    shared,
  );
  const u = new URL(BASE + '/api/tg/connect/' + start.sid);
  u.searchParams.set('phantom_encryption_public_key', b58e(boxKp.publicKey));
  u.searchParams.set('nonce', b58e(nonce));
  u.searchParams.set('data', b58e(ct));
  const html = await (await fetch(u)).text();
  assert.ok(html.includes('phantom://ul/v1/signMessage?'), 'scheme button present');
  const m = html.match(/https:\/\/phantom\.app\/ul\/v1\/signMessage\?[^"']+/);
  assert.ok(m, 'fallback https UL present');

  // 3) decrypt the server-built signMessage payload, sign it, hit the sign cb
  const q = new URL(m[0]).searchParams;
  const payload = nacl.box.open.after(b58d(q.get('payload')), b58d(q.get('nonce')), shared);
  assert.ok(payload, 'wallet can decrypt the server payload (PyNaCl -> tweetnacl)');
  const info = JSON.parse(dec.decode(payload));
  assert.equal(info.session, 'phantom-sess', 'session echoed through');
  const msgBytes = b58d(info.message);
  assert.ok(dec.decode(msgBytes).startsWith('CLEAN soft-staking login'), 'human-readable message');
  const sig = nacl.sign.detached(msgBytes, solKp.secretKey);

  const n2 = nacl.randomBytes(24);
  const ct2 = nacl.box.after(
    enc.encode(JSON.stringify({ signature: b58e(sig) })),
    n2,
    shared,
  );
  const s = new URL(new URL(m[0]).searchParams.get('redirect_link'));
  s.searchParams.set('nonce', b58e(n2));
  s.searchParams.set('data', b58e(ct2));
  const signedHtml = await (await fetch(s)).text();
  assert.ok(signedHtml.includes('Signed'), 'sign callback verifies the tweetnacl signature');
}

// 4) the webview's poll completes with a working session
const poll = await j(
  await fetch(BASE + '/api/tg/poll', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: idata, sid: start.sid }),
  }),
);
assert.equal(poll.status, 'done');
assert.equal(poll.profile.wallet, walletAddr);
const prof = await j(
  await fetch(BASE + '/api/profile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: poll.token }),
  }),
);
assert.equal(prof.wallet, walletAddr);

// 5) sid-less recovery (cold webview relaunch)
const cold = await j(
  await fetch(BASE + '/api/tg/poll', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: idata }),
  }),
);
assert.equal(cold.status, 'done', 'sid-less poll recovers via the per-user pointer');

console.log('CROSS-IMPL E2E PASSED — tweetnacl (Phantom) ⇄ PyNaCl (server), full handshake over HTTP');
