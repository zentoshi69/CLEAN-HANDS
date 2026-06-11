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
  const listeners = {};
  const win = {
    localStorage: storage,
    Telegram: { WebApp: { initData, openLink: (u) => opened.push(u) } },
    CLW_POLL_MS: pollMs || undefined,
    // minimal event bus so the Wallet Standard register/app-ready protocol runs
    addEventListener: (t, fn) => (listeners[t] = listeners[t] || []).push(fn),
    dispatchEvent: (ev) => ((listeners[ev.type] || []).forEach((fn) => fn(ev)), true),
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
    Array,
    Date,
    setTimeout,
    clearTimeout,
    fetch: fetchImpl || (() => Promise.reject(new Error('no fetch in this tab'))),
    document: { addEventListener: () => {}, hidden: false },
    CustomEvent: class {
      constructor(type, opts) {
        this.type = type;
        this.detail = opts && opts.detail;
      }
    },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(walletSrc, ctx);
  return { CleanWallet: win.CleanWallet, opened, win, ctx };
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

// ---- case 5: Telegram server-side handshake (client view) ----------------- //
// In Telegram, connect() calls POST /api/tg/start, opens the wallet UL pointed
// at the SERVER, and polls POST /api/tg/poll until it gets {status:done,...},
// then fires onSession. No in-webview crypto, survives relaunches.
{
  let started = 0;
  const fetchImpl = (url, opts) => {
    if (/\/api\/tg\/start$/.test(url)) {
      started++;
      const body = JSON.parse(opts.body);
      assert.ok(body.initData && body.wallet === 'phantom', 'start carries initData + wallet');
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ sid: 'SID123', dapp_pub: 'PUB' }) });
    }
    if (/\/api\/tg\/poll$/.test(url)) {
      // first poll pending, then done — exercises the retry loop
      started++;
      const done = started > 2;
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve(
            done
              ? { status: 'done', token: 'TOK. SIG', profile: { wallet: 'Wxyz' } }
              : { status: 'connected' },
          ),
      });
    }
    return Promise.resolve({ ok: false });
  };

  const tab = bootTab({ storage: makeStorage(), href: ORIGIN, initData: 'user=stub', fetchImpl, pollMs: 5 });
  let session = null;
  tab.CleanWallet.init({ onSession: (tok, prof) => (session = { tok, prof }), onError: (e) => assert.fail(e.message) });
  const mode = tab.CleanWallet.connect('phantom');
  assert.equal(mode, 'tg', 'inside Telegram, connect uses the server handshake');
  assert.equal(tab.opened.length, 0, 'connect UL opens only after /api/tg/start resolves');

  await new Promise((res) => setTimeout(res, 1300)); // first poll fires at 800ms
  const redirect = tab.opened[0] && new URL(tab.opened[0]).searchParams.get('redirect_link');
  assert.ok(
    redirect && redirect.endsWith('/api/tg/connect/SID123'),
    'wallet UL redirect_link points at the server handshake: ' + redirect,
  );
  assert.ok(session && session.tok === 'TOK. SIG', 'onSession fired with the server token');
  assert.equal(session.prof.wallet, 'Wxyz');
  console.log('Telegram server-side handshake (client) ✓');
}

// ---- case 6: Wallet Standard — any modern wallet announces itself --------- //
// A wallet registers through the real event protocol; it must appear in the
// picker (deduped against the deeplink trio), and connect+signMessage must
// flow through its standard features.
{
  const tab = bootTab({ storage: makeStorage(), href: ORIGIN });
  const signed = [];
  const mockWallet = {
    name: 'OKX Wallet',
    icon: 'data:image/png;base64,AAAA',
    chains: ['solana:mainnet'],
    accounts: [],
    features: {
      'standard:connect': {
        connect: async () => ({ accounts: [{ address: 'OkxAddr1111111111111111111111111111111111111' }] }),
      },
      'solana:signMessage': {
        signMessage: async ({ account, message }) => {
          signed.push({ account, message });
          return [{ signature: new Uint8Array([1, 2, 3, 4]) }];
        },
      },
    },
  };
  // wallet-side registration, exactly as the standard specifies
  tab.win.dispatchEvent(
    new tab.ctx.CustomEvent('wallet-standard:register-wallet', {
      detail: (api) => api.register(mockWallet),
    }),
  );

  const list = tab.CleanWallet.listWallets();
  assert.ok(list.find((w) => w.id === 'ws:OKX Wallet' && w.icon), 'standard wallet listed with icon');
  assert.ok(list.find((w) => w.id === 'phantom'), 'deeplink trio still offered');
  assert.equal(list.filter((w) => w.name.toLowerCase() === 'okx wallet').length, 1, 'deduped');

  let pk = null;
  let sig = null;
  tab.CleanWallet.init({
    onConnect: (p) => (pk = p),
    onSign: (s) => (sig = s),
    onError: (e) => assert.fail('std flow error: ' + e.message),
  });
  assert.equal(tab.CleanWallet.connect('ws:OKX Wallet'), 'std');
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(pk, 'OkxAddr1111111111111111111111111111111111111');
  tab.CleanWallet.signMessage('login msg', { nonce: 'n' });
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(sig && sig.length > 0, 'b58 signature delivered');
  assert.equal(signed[0].account.address, pk, 'signed with the authorized account');
  console.log('Wallet Standard discovery + connect + sign ✓');
}

console.log('\nALL WALLET FLOW TESTS PASSED');
