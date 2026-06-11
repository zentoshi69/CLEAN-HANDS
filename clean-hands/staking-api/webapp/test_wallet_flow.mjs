/*
 * test_wallet_flow.mjs — regression test for the wallet deeplink handshake.
 *
 *   node test_wallet_flow.mjs
 *
 * Simulates the real-world flow that used to break with "decrypt failed":
 * connect() starts in tab A, the wallet delivers the encrypted callback in a
 * BRAND-NEW tab B. With per-origin localStorage (current code) tab B finds
 * the ephemeral x25519 key and decrypts; with the old per-tab sessionStorage
 * semantics it cannot, and must now fail with the friendly "tap Connect once
 * more" message instead of the cryptic "decrypt failed".
 */
import { createRequire } from 'module';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';
import vm from 'vm';
import assert from 'assert';

const dir = path.dirname(fileURLToPath(import.meta.url));
const require_ = createRequire(import.meta.url);
const nacl = require_(path.join(dir, 'nacl.min.js'));
const walletSrc = readFileSync(path.join(dir, 'wallet.js'), 'utf8');

function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

// Boot one "tab": evaluates wallet.js against a fake window with the given
// storage and URL. Returns { CleanWallet, opened } where opened collects
// outbound deeplink URLs.
function bootTab({ storage, href, initData = '', fetchImpl = null, pollMs = 0 }) {
  const opened = [];
  const win = {
    localStorage: storage,
    Telegram: { WebApp: { initData, openLink: (u) => opened.push(u) } },
    CLW_POLL_MS: pollMs || undefined,
  };
  const ctx = {
    window: win,
    localStorage: storage,
    nacl,
    location: new URL(href),
    history: { replaceState: () => {} },
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    JSON,
    console,
    // host-realm constructors so arrays built inside the vm pass nacl's checks
    Uint8Array,
    Promise,
    Error,
    Date,
    setTimeout,
    clearTimeout,
    fetch: fetchImpl || (() => Promise.reject(new Error('no fetch in this tab'))),
    document: { addEventListener: () => {}, hidden: false },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(walletSrc, ctx);
  return { CleanWallet: win.CleanWallet, opened };
}

const ORIGIN = 'https://app.cleanhands.fun/';

// ---- the wallet app's side of the handshake (what Phantom does) ---------- //
function walletSideConnectCallback(connectUrl, sharedLocalStorageProbe) {
  const u = new URL(connectUrl);
  const dappPub = u.searchParams.get('dapp_encryption_public_key');
  assert.ok(dappPub, 'connect UL must carry our encryption pubkey');
  const redirect = u.searchParams.get('redirect_link');
  const b58 = sharedLocalStorageProbe; // reuse codec from a booted tab
  const wkp = nacl.box.keyPair();
  const shared = nacl.box.before(b58.b58decode(dappPub), wkp.secretKey);
  const payload = {
    public_key: 'WaLLetPubKey1111111111111111111111111111111',
    session: 'wallet-session-token',
  };
  const nonce = nacl.randomBytes(24);
  const box = nacl.box.after(
    new TextEncoder().encode(JSON.stringify(payload)),
    nonce,
    shared,
  );
  const cb = new URL(redirect);
  cb.searchParams.set('phantom_encryption_public_key', b58.b58encode(wkp.publicKey));
  cb.searchParams.set('nonce', b58.b58encode(nonce));
  cb.searchParams.set('data', b58.b58encode(box));
  return cb.toString();
}

// ---- case 1: shared localStorage (the fix) — callback in a NEW tab works -- //
{
  const shared = makeStorage();
  const tabA = bootTab({ storage: shared, href: ORIGIN });
  tabA.CleanWallet.init({});
  tabA.CleanWallet.connect('phantom');
  assert.equal(tabA.opened.length, 1, 'connect must open the wallet UL');
  const cbUrl = walletSideConnectCallback(tabA.opened[0], tabA.CleanWallet);

  const tabB = bootTab({ storage: shared, href: cbUrl }); // fresh tab, same origin storage
  let got = null;
  let err = null;
  tabB.CleanWallet.init({ onConnect: (pk) => (got = pk), onError: (e) => (err = e) });
  assert.equal(err, null, 'no error expected, got: ' + (err && err.message));
  assert.equal(got, 'WaLLetPubKey1111111111111111111111111111111');
  assert.equal(tabB.CleanWallet.currentPubkey(), got);
  console.log('localStorage handshake across tabs ✓');
}

// ---- case 2: old per-tab semantics — must fail FRIENDLY, not "decrypt failed"
{
  const tabA = bootTab({ storage: makeStorage(), href: ORIGIN });
  tabA.CleanWallet.init({});
  tabA.CleanWallet.connect('phantom');
  const cbUrl = walletSideConnectCallback(tabA.opened[0], tabA.CleanWallet);

  const tabB = bootTab({ storage: makeStorage(), href: cbUrl }); // isolated storage
  let err = null;
  tabB.CleanWallet.init({ onConnect: () => {}, onError: (e) => (err = e) });
  assert.ok(err, 'must error when the handshake key is missing');
  assert.ok(
    /Connect once more/.test(err.message),
    'must be the friendly retry message, got: ' + err.message,
  );
  assert.ok(!/decrypt failed/.test(err.message), 'cryptic error must be gone');
  console.log('cross-context landing fails with friendly retry message ✓');
}

// ---- case 3: full sign round-trip in the same browser --------------------- //
{
  const shared = makeStorage();
  const tabA = bootTab({ storage: shared, href: ORIGIN });
  tabA.CleanWallet.init({});
  tabA.CleanWallet.connect('phantom');
  const cbUrl = walletSideConnectCallback(tabA.opened[0], tabA.CleanWallet);
  const tabB = bootTab({ storage: shared, href: cbUrl });
  tabB.CleanWallet.init({ onConnect: () => {}, onError: (e) => assert.fail(e.message) });

  // request a signature; wallet answers in yet another tab
  tabB.CleanWallet.signMessage('login nonce abc', { nonce: 'abc', wallet: 'W' });
  assert.equal(tabB.opened.length, 1);
  const su = new URL(tabB.opened[0]);
  const sharedKeyB58 = shared.getItem('clw_shared');
  const sharedKey = tabB.CleanWallet.b58decode(sharedKeyB58);
  // wallet decrypts our payload, then encrypts its signature reply
  const reqBox = tabB.CleanWallet.b58decode(su.searchParams.get('payload'));
  const reqNonce = tabB.CleanWallet.b58decode(su.searchParams.get('nonce'));
  const req = JSON.parse(
    new TextDecoder().decode(nacl.box.open.after(reqBox, reqNonce, sharedKey)),
  );
  assert.equal(req.session, 'wallet-session-token', 'sign payload carries the session');
  const reply = { signature: 'FaKeSiG' };
  const rn = nacl.randomBytes(24);
  const rb = nacl.box.after(new TextEncoder().encode(JSON.stringify(reply)), rn, sharedKey);
  const scb = new URL(su.searchParams.get('redirect_link'));
  scb.searchParams.set('nonce', tabB.CleanWallet.b58encode(rn));
  scb.searchParams.set('data', tabB.CleanWallet.b58encode(rb));

  const tabC = bootTab({ storage: shared, href: scb.toString() });
  let sig = null;
  let ctx = null;
  tabC.CleanWallet.init({
    onSign: (s, c) => ((sig = s), (ctx = c)),
    onError: (e) => assert.fail(e.message),
  });
  assert.equal(sig, 'FaKeSiG');
  assert.deepEqual(ctx, { nonce: 'abc', wallet: 'W' });
  console.log('sign round-trip across three tabs ✓');
}

// ---- case 4: Telegram relay mode — callback never reaches the webview ----- //
// The webview opens the wallet with redirect_link -> /wallet-return?hid=...;
// the bounce page posts the encrypted params to /api/relay/<hid>; the webview
// polls, decrypts with its local key, and completes — no page reload at all.
{
  const relayStore = new Map();
  const fetchImpl = (url, opts) => {
    const m = String(url).match(/\/api\/relay\/(.+)$/);
    if (m && opts && opts.method === 'DELETE') {
      relayStore.delete(m[1]); // the webview's ack after processing
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    }
    if (m && relayStore.has(m[1])) {
      // peek — payload stays until acked, like the backend's get/delete pair
      const params = relayStore.get(m[1]);
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ params }) });
    }
    return Promise.resolve({ ok: false });
  };

  const tab = bootTab({
    storage: makeStorage(),
    href: ORIGIN,
    initData: 'query_id=AAA&user=stub', // we are "inside Telegram"
    fetchImpl,
    pollMs: 5,
  });
  let resolveConnect;
  const connected = new Promise((res) => (resolveConnect = res));
  tab.CleanWallet.init({
    onConnect: (pk) => resolveConnect(pk),
    onError: (e) => assert.fail('relay flow error: ' + e.message),
  });
  tab.CleanWallet.connect('phantom');
  assert.equal(tab.opened.length, 1);

  const ul = new URL(tab.opened[0]);
  const redirect = new URL(ul.searchParams.get('redirect_link'));
  assert.ok(redirect.pathname.endsWith('/wallet-return'), 'TG mode must use the bounce page');
  const hid = redirect.searchParams.get('hid');
  assert.ok(/^[1-9A-HJ-NP-Za-km-z]{8,40}$/.test(hid), 'hid must be well-formed base58');

  // wallet approves; bounce page would POST these params to /api/relay/<hid>
  const dappPub = ul.searchParams.get('dapp_encryption_public_key');
  const wkp = nacl.box.keyPair();
  const shared = nacl.box.before(tab.CleanWallet.b58decode(dappPub), wkp.secretKey);
  const payload = { public_key: 'TgRelayPubKey111111111111111111111111111111', session: 'sess' };
  const nonce = nacl.randomBytes(24);
  const box = nacl.box.after(new TextEncoder().encode(JSON.stringify(payload)), nonce, shared);
  relayStore.set(hid, {
    phantom_encryption_public_key: tab.CleanWallet.b58encode(wkp.publicKey),
    nonce: tab.CleanWallet.b58encode(nonce),
    data: tab.CleanWallet.b58encode(box),
  });

  const pk = await Promise.race([
    connected,
    new Promise((_, rej) => setTimeout(() => rej(new Error('relay poll timed out')), 2000)),
  ]);
  assert.equal(pk, 'TgRelayPubKey111111111111111111111111111111');
  assert.equal(tab.CleanWallet.currentPubkey(), pk);
  // the webview must ack so the backend can drop the payload
  await new Promise((res) => setTimeout(res, 50));
  assert.equal(relayStore.size, 0, 'payload must be acked (DELETEd) after success');
  console.log('Telegram relay handshake (peek + ack, no reload) ✓');
}

console.log('\nALL WALLET FLOW TESTS PASSED');
