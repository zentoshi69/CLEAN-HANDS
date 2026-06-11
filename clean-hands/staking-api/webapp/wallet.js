/*
 * wallet.js — Solana wallet connect + signMessage for the CLEAN app.
 *
 * Two transports, picked automatically per wallet:
 *   1. Injected extension provider (desktop: Phantom / Solflare / Backpack)
 *      — direct async calls, no page reload.
 *   2. Encrypted universal-link deeplinks (mobile) — the wallet round-trips
 *      through the OS and each step completes on the NEXT page load.
 *
 * Handshake state (ephemeral x25519 key, wallet session, shared secret) lives
 * in localStorage, NOT sessionStorage: wallets routinely deliver the callback
 * in a brand-new tab or webview, and sessionStorage does not survive that hop
 * — that was the cause of "decrypt failed" on the website. localStorage is
 * shared per-origin across tabs of the same browser, so the callback always
 * finds the keys it needs. Nothing stored is a long-term secret: the x25519
 * key is per-handshake transport encryption, the wallet session is issued by
 * the wallet app, and both are cleared on disconnect.
 *
 * Requires global `nacl` (tweetnacl, served same-origin as /nacl.min.js).
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
    if (!str || !str.length) return new Uint8Array(0);
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

  // ---- wallet registry --------------------------------------------------- //
  const WALLETS = {
    phantom: { name: 'Phantom', base: 'https://phantom.app/ul/v1' },
    solflare: { name: 'Solflare', base: 'https://solflare.com/ul/v1' },
    backpack: { name: 'Backpack', base: 'https://backpack.app/ul/v1' },
  };

  const LS = global.localStorage;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  let HANDLERS = {};

  function save(k, v) {
    LS.setItem('clw_' + k, v);
  }
  function load(k) {
    return LS.getItem('clw_' + k);
  }
  function clearStep() {
    ['pending', 'sign_msg', 'sign_ctx'].forEach((k) => LS.removeItem('clw_' + k));
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

  function fail(e) {
    clearStep();
    HANDLERS.onError && HANDLERS.onError(e instanceof Error ? e : new Error(String(e)));
  }

  // ---- injected extension providers (desktop) ---------------------------- //
  function extProvider(walletId) {
    try {
      if (walletId === 'phantom') {
        const p = (global.phantom && global.phantom.solana) || global.solana;
        if (p && p.isPhantom) return p;
      }
      if (walletId === 'solflare' && global.solflare && global.solflare.isSolflare)
        return global.solflare;
      if (walletId === 'backpack' && global.backpack) return global.backpack;
    } catch (e) {}
    return null;
  }
  function normSig(res) {
    const s = res && res.signature !== undefined ? res.signature : res;
    if (typeof s === 'string') return s; // already base58
    return b58encode(s instanceof Uint8Array ? s : new Uint8Array(s));
  }

  // ---- key management ----------------------------------------------------- //
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

  // ---- public API ---------------------------------------------------------- //
  // connect(wallet): extension -> resolves via onConnect immediately;
  // deeplink -> opens the UL and resolves after the callback on reload.
  function connect(walletId) {
    const w = WALLETS[walletId];
    if (!w) throw new Error('unknown wallet');
    const ext = extProvider(walletId);
    if (ext) {
      save('wallet', walletId);
      save('mode', 'ext');
      Promise.resolve(ext.connect())
        .then((res) => {
          const pk = (res && res.publicKey ? res.publicKey : ext.publicKey).toString();
          save('pubkey', pk);
          HANDLERS.onConnect && HANDLERS.onConnect(pk);
        })
        .catch(fail);
      return 'ext';
    }
    save('wallet', walletId);
    save('mode', 'link');
    save('pending', 'connect');
    const kp = dappKeypair();
    const params = new URLSearchParams({
      dapp_encryption_public_key: b58encode(kp.publicKey),
      cluster: 'mainnet-beta',
      app_url: location.origin,
      redirect_link: redirectBase() + '?clw=connect',
    });
    openLink(`${w.base}/connect?${params.toString()}`);
    return 'link';
  }

  // signMessage(message): extension -> signs in-page; deeplink -> UL round-trip.
  function signMessage(message, ctx) {
    const walletId = load('wallet');
    if (load('mode') === 'ext') {
      const ext = extProvider(walletId);
      if (!ext) return fail(new Error('wallet extension not available'));
      Promise.resolve(ext.signMessage(enc.encode(message), 'utf8'))
        .then((res) => HANDLERS.onSign && HANDLERS.onSign(normSig(res), ctx || {}))
        .catch(fail);
      return;
    }
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
      redirect_link: redirectBase() + '?clw=sign',
      payload: b58encode(box),
    });
    openLink(`${w.base}/signMessage?${params.toString()}`);
  }

  // signAndSendTransaction(txBase58, ctx, txObj): wallet signs AND broadcasts.
  // txObj (a @solana/web3.js Transaction) is used by the extension path;
  // the deeplink path uses the base58 serialization.
  function signAndSendTransaction(txBase58, ctx, txObj) {
    const walletId = load('wallet');
    if (load('mode') === 'ext') {
      const ext = extProvider(walletId);
      if (!ext) return fail(new Error('wallet extension not available'));
      if (!txObj) return fail(new Error('missing transaction object'));
      Promise.resolve(ext.signAndSendTransaction(txObj))
        .then((res) => HANDLERS.onTx && HANDLERS.onTx(normSig(res), ctx || {}))
        .catch(fail);
      return;
    }
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
      redirect_link: redirectBase() + '?clw=tx',
      payload: b58encode(box),
    });
    openLink(`${w.base}/signAndSendTransaction?${params.toString()}`);
  }

  function disconnect() {
    ['wallet', 'mode', 'session', 'shared', 'pubkey', 'pending', 'sign_ctx', 'dapp_sk'].forEach(
      (k) => LS.removeItem('clw_' + k),
    );
  }

  function currentPubkey() {
    return load('pubkey');
  }
  function isConnected() {
    return !!(load('pubkey') && (load('session') || load('mode') === 'ext'));
  }
  function listWallets() {
    return Object.entries(WALLETS).map(([id, w]) => ({ id, name: w.name }));
  }

  // init(): call on boot. If we returned from a wallet callback, finish the
  // step and invoke the matching handler. Returns the step name or null.
  function init(handlers) {
    HANDLERS = handlers || {};
    const url = new URL(location.href);
    const cb = url.searchParams.get('clw');
    if (!cb) return null;

    // clean the URL so a refresh doesn't reprocess
    history.replaceState(null, '', redirectBase());

    try {
      const errCode = url.searchParams.get('errorCode');
      if (errCode)
        throw new Error(url.searchParams.get('errorMessage') || 'wallet error ' + errCode);

      if (cb === 'connect') {
        if (!load('dapp_sk'))
          throw new Error(
            'Connect started in another app — tap Connect once more to finish here.',
          );
        const theirPub = url.searchParams.get('phantom_encryption_public_key');
        const data = url.searchParams.get('data');
        const nonce = url.searchParams.get('nonce');
        if (!theirPub || !data || !nonce) throw new Error('incomplete wallet callback');
        const shared = sharedSecret(theirPub);
        save('shared', b58encode(shared));
        const info = decryptPayload(data, nonce, shared);
        save('session', info.session);
        save('pubkey', info.public_key);
        clearStep();
        HANDLERS.onConnect && HANDLERS.onConnect(info.public_key);
        return 'connect';
      }
      if (cb === 'sign' || cb === 'tx') {
        if (!load('shared'))
          throw new Error(
            'Signature finished in another app — tap Connect once more to finish here.',
          );
        const data = url.searchParams.get('data');
        const nonce = url.searchParams.get('nonce');
        if (!data || !nonce) throw new Error('incomplete wallet callback');
        const shared = b58decode(load('shared'));
        const info = decryptPayload(data, nonce, shared);
        const ctx = JSON.parse(load('sign_ctx') || '{}');
        clearStep();
        if (cb === 'sign') HANDLERS.onSign && HANDLERS.onSign(info.signature, ctx);
        else HANDLERS.onTx && HANDLERS.onTx(info.signature, ctx);
        return cb;
      }
    } catch (e) {
      fail(e);
    }
    return cb;
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
    b58encode,
    b58decode,
  };
})(typeof window !== 'undefined' ? window : globalThis);
