/*
 * wallet.js — Solana wallet connect + signMessage for a Telegram Mini App,
 * using the encrypted universal-link ("deeplink") protocol that Phantom defines
 * and Solflare / Backpack implement compatibly.
 *
 * Flow (mobile): we generate an ephemeral x25519 keypair, open the wallet's UL,
 * the wallet returns to our redirect_link with an encrypted payload, and we
 * decrypt it with a shared secret. connect() yields the pubkey + session;
 * signMessage() yields a base58 signature. Because the wallet round-trips through
 * the OS, each step completes on the *next* page load — we persist state in
 * sessionStorage and resolve pending steps in init().
 *
 * Requires global `nacl` (tweetnacl, loaded in index.html). base58 is inlined.
 */
(function (global) {
  'use strict';

  // ---- base58 (Bitcoin alphabet) ---------------------------------------- //
  const ALPH = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  const MAP = (() => {
    const m = {};
    for (let i = 0; i < ALPH.length; i++) m[ALPH[i]] = i;
    return m;
  })();
  function b58encode(bytes) {
    if (!bytes.length) return '';
    const digits = [0];
    for (let i = 0; i < bytes.length; i++) {
      let carry = bytes[i];
      for (let j = 0; j < digits.length; j++) {
        carry += digits[j] << 8;
        digits[j] = carry % 58;
        carry = (carry / 58) | 0;
      }
      while (carry) {
        digits.push(carry % 58);
        carry = (carry / 58) | 0;
      }
    }
    let s = '';
    for (let k = 0; bytes[k] === 0 && k < bytes.length - 1; k++) s += '1';
    for (let q = digits.length - 1; q >= 0; q--) s += ALPH[digits[q]];
    return s;
  }
  function b58decode(str) {
    if (!str.length) return new Uint8Array(0);
    const bytes = [0];
    for (let i = 0; i < str.length; i++) {
      const value = MAP[str[i]];
      if (value === undefined) throw new Error('bad base58 char');
      let carry = value;
      for (let j = 0; j < bytes.length; j++) {
        carry += bytes[j] * 58;
        bytes[j] = carry & 0xff;
        carry >>= 8;
      }
      while (carry) {
        bytes.push(carry & 0xff);
        carry >>= 8;
      }
    }
    for (let k = 0; str[k] === '1' && k < str.length - 1; k++) bytes.push(0);
    return new Uint8Array(bytes.reverse());
  }

  // ---- wallet registry -------------------------------------------------- //
  // `browse` opens THIS app inside the wallet's own in-app browser, where the
  // wallet injects its provider — so connect/sign happen with no deeplink
  // round-trip (the round-trip can't return into a Telegram Mini App webview).
  const WALLETS = {
    phantom: {
      name: 'Phantom',
      base: 'https://phantom.app/ul/v1',
      browse: (u) =>
        `https://phantom.app/ul/browse/${encodeURIComponent(u)}?ref=${encodeURIComponent(u)}`,
    },
    solflare: {
      name: 'Solflare',
      base: 'https://solflare.com/ul/v1',
      browse: (u) =>
        `https://solflare.com/ul/v1/browse/${encodeURIComponent(u)}?ref=${encodeURIComponent(u)}`,
    },
    backpack: {
      name: 'Backpack',
      base: 'https://backpack.app/ul/v1',
      browse: (u) =>
        `https://backpack.app/ul/browse/${encodeURIComponent(u)}?ref=${encodeURIComponent(u)}`,
    },
  };

  // localStorage (NOT sessionStorage): the wallet deeplink round-trip often
  // returns to a fresh webview/tab on mobile, which would wipe sessionStorage and
  // lose dapp_sk/shared/sign_ctx → decrypt fails → login impossible. localStorage
  // survives that (same origin). We still clear it in clearStep()/disconnect().
  const SS = window.localStorage;
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  function save(k, v) {
    SS.setItem('clw_' + k, v);
  }
  function load(k) {
    return SS.getItem('clw_' + k);
  }
  function clearStep() {
    ['pending', 'sign_msg', 'sign_ctx', 'state'].forEach((k) => SS.removeItem('clw_' + k));
  }

  // Per-step CSRF token. We append it to the redirect_link we hand the wallet and
  // require the callback to echo it back — so a crafted ?clw= deep link opened in
  // the Mini App webview (hostile-group threat model) can't drive a connect/sign.
  function freshState() {
    const s = b58encode(nacl.randomBytes(16));
    save('state', s);
    return s;
  }
  function stateOk(url) {
    const got = url.searchParams.get('state');
    const want = load('state');
    // FAIL CLOSED. A callback is only honored if it echoes the exact per-step
    // state we issued (and that we're still waiting on). An attacker who drops or
    // guesses `state` is rejected. This is what makes a crafted `?clw=connect…`
    // deep link (hostile-group threat model) unable to drive a connect/sign and
    // inject an attacker-chosen pubkey/session.
    return !!(want && got && got === want);
  }

  function redirectBase() {
    // strip any existing query so callbacks are clean
    return location.origin + location.pathname;
  }

  function openLink(url) {
    const tg = global.Telegram && global.Telegram.WebApp;
    if (tg && tg.openLink) tg.openLink(url, { try_instant_view: false });
    else window.location.href = url;
  }

  // ---- key management --------------------------------------------------- //
  function dappKeypair() {
    let sk = load('dapp_sk');
    if (sk) {
      const secret = b58decode(sk);
      return { publicKey: nacl.box.keyPair.fromSecretKey(secret).publicKey, secretKey: secret };
    }
    const kp = nacl.box.keyPair();
    save('dapp_sk', b58encode(kp.secretKey));
    return kp;
  }

  function sharedSecret(theirPubB58) {
    const kp = dappKeypair();
    return nacl.box.before(b58decode(theirPubB58), kp.secretKey);
  }

  function decryptPayload(dataB58, nonceB58, shared) {
    const out = nacl.box.open.after(b58decode(dataB58), b58decode(nonceB58), shared);
    if (!out) throw new Error('decrypt failed');
    return JSON.parse(dec.decode(out));
  }

  // ---- public API ------------------------------------------------------- //
  // connect(wallet): kicks off the UL; resolves after the callback on reload.
  function connect(walletId) {
    const w = WALLETS[walletId];
    if (!w) throw new Error('unknown wallet');
    save('wallet', walletId);
    save('pending', 'connect');
    // Fresh ephemeral x25519 keypair for EACH connect attempt — never reuse a key
    // persisted from a previous session (limits exposure if localStorage was read).
    const kp = nacl.box.keyPair();
    save('dapp_sk', b58encode(kp.secretKey));
    const params = new URLSearchParams({
      dapp_encryption_public_key: b58encode(kp.publicKey),
      cluster: 'mainnet-beta',
      app_url: location.origin,
      redirect_link: redirectBase() + '?clw=connect&state=' + freshState(),
    });
    openLink(`${w.base}/connect?${params.toString()}`);
  }

  // signMessage(message): encrypts payload, opens UL; resolves on reload.
  function signMessage(message, ctx) {
    const walletId = load('wallet');
    const w = WALLETS[walletId];
    const session = load('session');
    const sharedB58 = load('shared');
    if (!w || !session || !sharedB58) throw new Error('not connected');
    save('pending', 'sign');
    save('sign_ctx', ctx ? JSON.stringify(ctx) : '{}');
    const shared = b58decode(sharedB58);
    const payload = { message: b58encode(enc.encode(message)), session };
    const nonce = nacl.randomBytes(24);
    const box = nacl.box.after(enc.encode(JSON.stringify(payload)), nonce, shared);
    const params = new URLSearchParams({
      dapp_encryption_public_key: b58encode(dappKeypair().publicKey),
      nonce: b58encode(nonce),
      redirect_link: redirectBase() + '?clw=sign&state=' + freshState(),
      payload: b58encode(box),
    });
    openLink(`${w.base}/signMessage?${params.toString()}`);
  }

  // signAndSendTransaction(txBase58): wallet signs AND broadcasts a serialized
  // (legacy) transaction; resolves with the on-chain signature on reload.
  function signAndSendTransaction(txBase58, ctx) {
    const walletId = load('wallet');
    const w = WALLETS[walletId];
    const session = load('session');
    const sharedB58 = load('shared');
    if (!w || !session || !sharedB58) throw new Error('not connected');
    save('pending', 'tx');
    save('sign_ctx', ctx ? JSON.stringify(ctx) : '{}');
    const shared = b58decode(sharedB58);
    const payload = { transaction: txBase58, session };
    const nonce = nacl.randomBytes(24);
    const box = nacl.box.after(enc.encode(JSON.stringify(payload)), nonce, shared);
    const params = new URLSearchParams({
      dapp_encryption_public_key: b58encode(dappKeypair().publicKey),
      nonce: b58encode(nonce),
      redirect_link: redirectBase() + '?clw=tx&state=' + freshState(),
      payload: b58encode(box),
    });
    openLink(`${w.base}/signAndSendTransaction?${params.toString()}`);
  }

  function disconnect() {
    // Wipe the ephemeral secret key (dapp_sk) and shared secret too, so a stale
    // long-lived key never lingers in localStorage after logout.
    ['wallet', 'session', 'shared', 'pubkey', 'pending', 'sign_ctx', 'state', 'dapp_sk', 'inj_id'].forEach(
      (k) => SS.removeItem('clw_' + k),
    );
    _chosen = null; // clear active wallet reference so sign can't use a stale one
    wcDisconnect(); // also tear down any live WalletConnect relay session
  }

  function currentPubkey() {
    return load('pubkey');
  }
  function isConnected() {
    return !!(load('pubkey') && load('session'));
  }
  function listWallets() {
    return Object.entries(WALLETS).map(([id, w]) => ({ id, name: w.name }));
  }

  // Re-open this app inside the chosen wallet's in-app browser (where its provider
  // is injected). This is the reliable path from a Telegram Mini App / mobile.
  function openInWallet(walletId) {
    const w = WALLETS[walletId];
    if (!w || !w.browse) throw new Error('unknown wallet');
    openLink(w.browse(location.origin + location.pathname));
  }

  // init(): call on boot. If we returned from a wallet callback, finish the step
  // and invoke the matching handler. Returns the detected step name or null.
  function init({ onConnect, onSign, onTx, onError }) {
    const url = new URL(location.href);
    const cb = url.searchParams.get('clw');
    if (!cb) return null;

    // clean the URL so a refresh doesn't reprocess
    history.replaceState(null, '', redirectBase());

    const walletId = load('wallet');
    try {
      // Every callback must echo the per-step state we issued AND match the step
      // we're actually waiting on. Reject forged/unsolicited callbacks outright.
      if (!stateOk(url) || load('pending') !== cb) {
        // Fail CLOSED either way — but when THIS context has no handshake in
        // flight at all (webview relaunch wiped storage, or the callback landed
        // in a different browser), say so kindly instead of cryptically.
        if (!load('pending') && !load('state'))
          throw new Error('Almost there — tap Connect once more to finish.');
        throw new Error('unexpected wallet response');
      }
      const errCode = url.searchParams.get('errorCode');
      // Never surface attacker-controlled errorMessage text (social-engineering
      // vector in a hostile group) — show a fixed string keyed off the code only.
      if (errCode) throw new Error('wallet returned an error (' + errCode + ')');

      if (cb === 'connect') {
        const theirPub = url.searchParams.get('phantom_encryption_public_key');
        const data = url.searchParams.get('data');
        const nonce = url.searchParams.get('nonce');
        const shared = sharedSecret(theirPub);
        save('shared', b58encode(shared));
        const info = decryptPayload(data, nonce, shared);
        save('session', info.session);
        save('pubkey', info.public_key);
        clearStep();
        onConnect && onConnect(info.public_key);
        return 'connect';
      }
      if (cb === 'sign') {
        const data = url.searchParams.get('data');
        const nonce = url.searchParams.get('nonce');
        const shared = b58decode(load('shared'));
        const info = decryptPayload(data, nonce, shared);
        const ctx = JSON.parse(load('sign_ctx') || '{}');
        clearStep();
        onSign && onSign(info.signature, ctx); // signature is base58
        return 'sign';
      }
      if (cb === 'tx') {
        const data = url.searchParams.get('data');
        const nonce = url.searchParams.get('nonce');
        const shared = b58decode(load('shared'));
        const info = decryptPayload(data, nonce, shared);
        const ctx = JSON.parse(load('sign_ctx') || '{}');
        clearStep();
        onTx && onTx(info.signature, ctx); // on-chain tx signature (base58)
        return 'tx';
      }
    } catch (e) {
      clearStep();
      onError && onError(e);
    }
    return cb;
  }

  // ---- injected wallets (desktop extensions + in-wallet browsers) -------- //
  // Support EVERY Solana wallet, two ways:
  //   (1) Solana WALLET STANDARD — every modern wallet (Phantom, Solflare,
  //       Backpack, Glow, OKX, Coinbase, Trust, Exodus, Magic Eden, Brave, Nightly,
  //       Coin98…) announces itself to the page via standard events. We don't need
  //       to know them ahead of time.
  //   (2) LEGACY window.* injection for older builds.
  // Detection is LIVE: extensions inject *asynchronously*, so we re-scan on an
  // interval and on the standard events and tell the UI — instead of deciding once
  // at page load (that one-shot check was the desktop "nothing happens" bug).
  const _std = new Map(); // name -> Wallet Standard object
  const _detectCbs = [];
  let _chosen = null; // { id, name, kind, obj, account? }

  function onDetect(cb) {
    _detectCbs.push(cb);
  }
  function _fireDetect() {
    const list = detected();
    _detectCbs.forEach((cb) => {
      try {
        cb(list);
      } catch (e) {}
    });
  }

  // ---- account-change subscription -------------------------------------- //
  // When the user switches the active account INSIDE their wallet, the provider
  // fires an event. We surface a single normalized callback to the app: the new
  // base58 address, or null if they disconnected. The app uses it to drop the old
  // session and re-auth as the new wallet.
  const _acctCbs = [];
  // Track which providers we've already wired WITHOUT writing to them — wallet
  // extensions (Phantom/Core/MetaMask) freeze their injected provider object, so
  // adding a marker property throws "object is not extensible" and would abort
  // the entire connect. A WeakSet records the object identity externally.
  const _hooked = new WeakSet();
  function onAccountChange(cb) {
    _acctCbs.push(cb);
  }
  function _fireAccount(addr) {
    _acctCbs.forEach((cb) => {
      try {
        cb(addr);
      } catch (e) {}
    });
  }
  function _hookAccountChange(entry) {
    const o = entry.obj;
    if (!o || _hooked.has(o)) return;
    _hooked.add(o);
    try {
      if (entry.kind === 'standard') {
        const ev = o.features && o.features['standard:events'];
        if (ev && ev.on) {
          ev.on('change', (props) => {
            if (!props || !('accounts' in props)) return;
            // Only react to events from the wallet the user actually chose.
            // Without this guard, a background wallet (e.g. MetaMask while
            // Phantom is active) firing a change event would trigger a re-auth
            // attempt that signs with the wrong wallet and always fails.
            if (_chosen && _chosen.obj !== o) return;
            const acct = (o.accounts && o.accounts[0]) || (props.accounts && props.accounts[0]);
            if (acct && acct.address) {
              if (_chosen && _chosen.obj === o) _chosen.account = acct;
              save('pubkey', acct.address);
              _fireAccount(acct.address);
            } else {
              _fireAccount(null); // wallet locked / all accounts revoked
            }
          });
        }
      } else if (o.on) {
        o.on('accountChanged', (pk) => {
          // Same guard for legacy providers — ignore events from non-chosen wallets.
          if (_chosen && _chosen.obj !== o) return;
          if (pk) {
            const a = pk.toBase58 ? pk.toBase58() : pk.toString();
            save('pubkey', a);
            _fireAccount(a);
          } else {
            _fireAccount(null); // disconnected from inside the wallet
          }
        });
        o.on('disconnect', () => {
          if (_chosen && _chosen.obj !== o) return;
          _fireAccount(null);
        });
      }
    } catch (e) {}
  }
  function _isSolStd(wal) {
    try {
      const f = wal.features || {};
      const solChain = (wal.chains || []).some((c) => String(c).indexOf('solana:') === 0);
      return !!(f['solana:signMessage'] || (solChain && f['standard:connect']));
    } catch (e) {
      return false;
    }
  }
  function _registerStd(wal) {
    if (wal && wal.name && _isSolStd(wal) && !_std.has(wal.name)) {
      _std.set(wal.name, wal);
      _fireDetect();
    }
  }
  // Wallet Standard handshake — works whether the wallet loaded before or after us.
  try {
    const api = {
      register: (...ws) => {
        ws.forEach(_registerStd);
        return () => {};
      },
    };
    global.addEventListener('wallet-standard:register-wallet', (e) => {
      try {
        e.detail(api);
      } catch (x) {}
    });
    global.dispatchEvent(new CustomEvent('wallet-standard:app-ready', { detail: api }));
  } catch (e) {}

  // Legacy window.* probes (older wallet builds without the standard).
  const LEGACY = [
    ['phantom', 'Phantom', (w) => w.phantom && w.phantom.solana && w.phantom.solana],
    ['solflare', 'Solflare', (w) => w.solflare && w.solflare.isSolflare && w.solflare],
    ['backpack', 'Backpack', (w) => w.backpack && w.backpack.isBackpack && w.backpack],
    ['glow', 'Glow', (w) => w.glowSolana || (w.glow && w.glow.solana)],
    ['exodus', 'Exodus', (w) => w.exodus && w.exodus.solana],
    ['okx', 'OKX Wallet', (w) => w.okxwallet && w.okxwallet.solana],
    ['coinbase', 'Coinbase Wallet', (w) => w.coinbaseSolana],
    [
      'trust',
      'Trust',
      (w) => (w.trustwallet && w.trustwallet.solana) || (w.trustWallet && w.trustWallet.solana),
    ],
    ['brave', 'Brave Wallet', (w) => w.braveSolana],
    ['magiceden', 'Magic Eden', (w) => w.magicEden && w.magicEden.solana],
    ['nightly', 'Nightly', (w) => w.nightly && w.nightly.solana],
    ['coin98', 'Coin98', (w) => w.coin98 && w.coin98.sol],
    ['solana', 'Browser Wallet', (w) => w.solana],
  ];
  function _legacy() {
    const out = [];
    const seen = new Set();
    LEGACY.forEach(([id, name, get]) => {
      let p;
      try {
        p = get(global);
      } catch (e) {}
      if (p && !seen.has(p)) {
        seen.add(p);
        out.push({ id, name, kind: 'legacy', obj: p });
      }
    });
    return out;
  }

  // The merged, de-duped list of wallets usable right now.
  function detected() {
    const out = [];
    const names = new Set();
    _std.forEach((wal, name) => {
      out.push({ id: 'std:' + name, name, kind: 'standard', obj: wal });
      names.add(name.toLowerCase());
    });
    _legacy().forEach((w) => {
      if (!names.has(w.name.toLowerCase())) out.push(w);
    });
    return out;
  }
  // Hook account-change events on every provider detected so far — restored
  // sessions never call connect/_activate, which used to leave switches
  // inside the extension invisible to the app. WeakSet dedupes re-hooks.
  function watchAccounts() {
    detected().forEach(_hookAccountChange);
  }
  function hasInjected() {
    return detected().length > 0;
  }

  // Keep re-scanning for ~10s — desktop extensions inject after our script runs.
  let _scans = 0;
  let _lastCount = 0;
  const _iv = setInterval(() => {
    const n = detected().length;
    if (n !== _lastCount) {
      _lastCount = n;
      _fireDetect();
    }
    if (++_scans > 40) clearInterval(_iv);
  }, 250);
  global.addEventListener('load', () => setTimeout(_fireDetect, 300));

  function _pick(id) {
    const list = detected();
    if (id) {
      const m = list.find((w) => w.id === id || w.name === id);
      if (m) return m;
    }
    return list[0] || null;
  }

  async function _activate(entry) {
    if (!entry) throw new Error('no wallet found — install or open a Solana wallet');
    if (entry.kind === 'standard') {
      const wal = entry.obj;
      const cres = await wal.features['standard:connect'].connect();
      const acct = (cres && cres.accounts && cres.accounts[0]) || (wal.accounts && wal.accounts[0]);
      if (!acct) throw new Error('wallet did not return an account');
      _chosen = { id: entry.id, name: entry.name, kind: 'standard', obj: wal, account: acct };
      _hookAccountChange(entry);
      return acct.address;
    }
    const p = entry.obj;
    const resp = await p.connect();
    const pk = p.publicKey || (resp && resp.publicKey);
    if (!pk) throw new Error('wallet did not return a public key');
    _chosen = { id: entry.id, name: entry.name, kind: 'legacy', obj: p };
    _hookAccountChange(entry);
    return pk.toBase58 ? pk.toBase58() : pk.toString();
  }

  async function connectInjected(id) {
    const entry = _pick(id);
    const addr = await _activate(entry);
    save('wallet', 'injected');
    save('inj_id', entry.id);
    save('pubkey', addr);
    return addr;
  }

  async function signInjected(message) {
    if (!_chosen) await _activate(_pick(load('inj_id'))); // re-activate after a reload
    if (_chosen.kind === 'standard') {
      const out = await _chosen.obj.features['solana:signMessage'].signMessage({
        account: _chosen.account,
        message: enc.encode(message),
      });
      const sig = out && out[0] && out[0].signature;
      if (!sig) throw new Error('wallet did not return a signature');
      return b58encode(sig instanceof Uint8Array ? sig : new Uint8Array(sig));
    }
    const p = _chosen.obj;
    const res = await p.signMessage(enc.encode(message), 'utf8');
    const sig = res && (res.signature !== undefined ? res.signature : res);
    if (!sig) throw new Error('wallet did not return a signature');
    if (typeof sig === 'string') return sig; // some wallets already return base58
    return b58encode(sig instanceof Uint8Array ? sig : new Uint8Array(sig));
  }

  // ---- WalletConnect (the relay protocol — ANY wallet, any device) ------- //
  // Desktop with no extension / Telegram Desktop: we show the official
  // WalletConnect modal (QR + wallet directory). The user scans with whatever
  // wallet app they own; connect + signMessage happen over the encrypted relay.
  // SDKs are lazy-loaded from esm.sh ONLY when the user taps the button, and the
  // whole path is OFF unless the operator sets WALLETCONNECT_PROJECT_ID.
  const WC_CHAIN = 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp'; // Solana mainnet (CAIP-2)
  let _wc = null; // { provider, pubkey }

  // Two loading strategies, because webviews differ:
  //  1) dynamic import() of ESM bundles (modern browsers) — fastest;
  //  2) classic <script> UMD builds (Telegram Desktop/iOS webviews BLOCK
  //     module imports — "Importing a module script failed").
  const _timeout = (p, ms) =>
    Promise.race([
      p,
      new Promise((_, rej) => setTimeout(() => rej(new Error('module load failed (timeout)')), ms)),
    ]);
  const _imp = async (urls) => {
    let lastErr;
    for (const u of urls) {
      try {
        return await import(/* webpackIgnore: true */ u);
      } catch (e) {
        lastErr = e;
      }
    }
    throw lastErr || new Error('module load failed');
  };
  const _script = (urls) =>
    new Promise((resolve, reject) => {
      const tryOne = (i) => {
        if (i >= urls.length) return reject(new Error('script load failed'));
        const s = document.createElement('script');
        s.src = urls[i];
        s.async = true;
        s.onload = () => resolve();
        s.onerror = () => {
          s.remove();
          tryOne(i + 1);
        };
        document.head.appendChild(s);
      };
      tryOne(0);
    });
  const _wcMeta = () => ({
    name: '$CLEAN Staking',
    description: 'CLEAN soft staking — clean hands, dirty money',
    url: location.origin,
    icons: [location.origin + '/glove.png'],
  });
  const _wcAccount = (session) => {
    const accounts = (session && session.namespaces.solana.accounts) || [];
    if (!accounts.length) throw new Error('wallet did not return an account');
    return accounts[0].split(':')[2]; // 'solana:<chain>:<pubkey>'
  };

  async function _wcViaEsm(projectId) {
    const [up, wm] = await Promise.all([
      _imp([
        'https://esm.sh/@walletconnect/universal-provider@2.17.2?bundle',
        'https://esm.run/@walletconnect/universal-provider@2.17.2',
      ]),
      _imp([
        'https://esm.sh/@walletconnect/modal@2.7.0?bundle',
        'https://esm.run/@walletconnect/modal@2.7.0',
      ]),
    ]);
    const UniversalProvider = up.default || up.UniversalProvider;
    const WalletConnectModal = wm.WalletConnectModal || wm.default;
    const provider = await UniversalProvider.init({ projectId, metadata: _wcMeta() });
    const modal = new WalletConnectModal({ projectId, chains: [WC_CHAIN] });
    provider.on('display_uri', (uri) => modal.openModal({ uri }));
    try {
      await provider.connect({
        namespaces: {
          solana: { methods: ['solana_signMessage'], chains: [WC_CHAIN], events: [] },
        },
      });
    } finally {
      modal.closeModal();
    }
    const pubkey = _wcAccount(provider.session);
    return { provider, pubkey };
  }

  async function _wcViaUmd(projectId) {
    await Promise.all([
      window.SignClient
        ? 0
        : _script([
            'https://unpkg.com/@walletconnect/sign-client@2.17.2/dist/index.umd.js',
            'https://cdn.jsdelivr.net/npm/@walletconnect/sign-client@2.17.2/dist/index.umd.js',
          ]),
      window.WalletConnectModal
        ? 0
        : _script([
            'https://unpkg.com/@walletconnect/modal@2.7.0/dist/index.umd.js',
            'https://cdn.jsdelivr.net/npm/@walletconnect/modal@2.7.0/dist/index.umd.js',
          ]),
    ]);
    const SC = window.SignClient && (window.SignClient.SignClient || window.SignClient);
    const M = window.WalletConnectModal;
    const ModalCtor = M && (M.WalletConnectModal || M.default || M);
    if (!SC || !ModalCtor) throw new Error('WalletConnect UMD globals missing');
    const client = await (SC.init ? SC.init : SC)({ projectId, metadata: _wcMeta() });
    const modal = new ModalCtor({ projectId, chains: [WC_CHAIN] });
    const { uri, approval } = await client.connect({
      requiredNamespaces: {
        solana: { methods: ['solana_signMessage'], chains: [WC_CHAIN], events: [] },
      },
    });
    let session;
    try {
      if (uri) modal.openModal({ uri });
      session = await approval();
    } finally {
      modal.closeModal();
    }
    const pubkey = _wcAccount(session);
    // thin adapter so wcSign/wcDisconnect work identically on both paths
    const provider = {
      request: (args, chainId) => client.request({ topic: session.topic, chainId, request: args }),
      disconnect: () =>
        client.disconnect({ topic: session.topic, reason: { code: 6000, message: 'user' } }),
    };
    return { provider, pubkey };
  }

  async function wcConnect(projectId) {
    if (!projectId) throw new Error('WalletConnect is not configured');
    let got;
    try {
      got = await _timeout(_wcViaEsm(projectId), 30000);
    } catch (e) {
      // webview blocked module imports (or both ESM CDNs down) — UMD scripts
      got = await _wcViaUmd(projectId);
    }
    _wc = got;
    save('wallet', 'walletconnect');
    save('pubkey', got.pubkey);
    return got.pubkey;
  }

  async function wcSign(message) {
    if (!_wc) throw new Error('not connected — tap WalletConnect again');
    const res = await _wc.provider.request(
      {
        method: 'solana_signMessage',
        params: { pubkey: _wc.pubkey, message: b58encode(enc.encode(message)) },
      },
      WC_CHAIN,
    );
    const sig = res && (res.signature !== undefined ? res.signature : res);
    if (!sig) throw new Error('wallet did not return a signature');
    return typeof sig === 'string' ? sig : b58encode(new Uint8Array(sig));
  }

  function wcDisconnect() {
    try {
      _wc && _wc.provider && _wc.provider.disconnect();
    } catch (e) {}
    _wc = null;
  }

  global.CleanWallet = {
    connect,
    signMessage,
    signAndSendTransaction,
    disconnect,
    init,
    isConnected,
    currentPubkey,
    listWallets,
    openInWallet,
    hasInjected,
    detected,
    onDetect,
    onAccountChange,
    watchAccounts,
    connectInjected,
    signInjected,
    wcConnect,
    wcSign,
    wcDisconnect,
    b58encode,
    b58decode,
  };
})(window);
