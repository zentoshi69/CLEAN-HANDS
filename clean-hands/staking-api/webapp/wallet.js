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

  // ---- wallet registry (all share Phantom's UL protocol) ---------------- //
  const WALLETS = {
    phantom: { name: 'Phantom', base: 'https://phantom.app/ul/v1' },
    solflare: { name: 'Solflare', base: 'https://solflare.com/ul/v1' },
    backpack: { name: 'Backpack', base: 'https://backpack.app/ul/v1' },
  };

  const SS = window.sessionStorage;
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  function save(k, v) {
    SS.setItem('clw_' + k, v);
  }
  function load(k) {
    return SS.getItem('clw_' + k);
  }
  function clearStep() {
    ['pending', 'sign_msg', 'sign_ctx'].forEach((k) => SS.removeItem('clw_' + k));
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
    const kp = dappKeypair();
    const params = new URLSearchParams({
      dapp_encryption_public_key: b58encode(kp.publicKey),
      cluster: 'mainnet-beta',
      app_url: location.origin,
      redirect_link: redirectBase() + '?clw=connect',
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
      redirect_link: redirectBase() + '?clw=sign',
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
      redirect_link: redirectBase() + '?clw=tx',
      payload: b58encode(box),
    });
    openLink(`${w.base}/signAndSendTransaction?${params.toString()}`);
  }

  function disconnect() {
    ['wallet', 'session', 'shared', 'pubkey', 'pending', 'sign_ctx'].forEach((k) =>
      SS.removeItem('clw_' + k),
    );
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
      const errCode = url.searchParams.get('errorCode');
      if (errCode)
        throw new Error(url.searchParams.get('errorMessage') || 'wallet error ' + errCode);

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
})(window);
