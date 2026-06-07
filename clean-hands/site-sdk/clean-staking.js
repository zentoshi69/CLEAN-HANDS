/*
 * clean-staking.js — drop-in browser client for the CLEAN staking API.
 *
 * Embed this on your WEBSITE so it calls the EXACT same endpoints as the Telegram
 * Mini App → the two are always in sync (one backend = one source of truth).
 *
 * No dependencies. Works with any Solana wallet that exposes
 * `publicKey` + `signMessage` (Phantom/Solflare/Backpack browser extensions, or
 * the wallet-standard adapter). Token is held in memory (and optionally
 * localStorage) — never a secret, just a short-lived session.
 *
 *   <script src="/clean-staking.js"></script>
 *   const clean = new CleanStaking("https://app.cleanhands.fun");
 *   await clean.login(window.solana);     // Phantom
 *   const me = await clean.profile();
 *   await clean.stake();  await clean.claim();
 */
(function (global) {
  'use strict';

  // -- base58 (for encoding the signature) -------------------------------- //
  const ALPH = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  function b58encode(bytes) {
    if (!bytes || !bytes.length) return '';
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

  const enc = new TextEncoder();

  async function _err(r) {
    try {
      const j = await r.json();
      return new Error(j.detail || 'HTTP ' + r.status);
    } catch (e) {
      return new Error('HTTP ' + r.status);
    }
  }

  class CleanStaking {
    constructor(apiBase, opts) {
      opts = opts || {};
      this.api = (apiBase || '').replace(/\/$/, '');
      this.persist = !!opts.persist; // optionally keep the session token in localStorage
      this.token =
        opts.token ||
        (this.persist && global.localStorage && localStorage.getItem('clean_token')) ||
        null;
    }

    async _get(path) {
      const r = await fetch(this.api + path);
      if (!r.ok) throw await _err(r);
      return r.json();
    }
    async _post(path, body) {
      const r = await fetch(this.api + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      if (!r.ok) throw await _err(r);
      return r.json();
    }
    _auth(extra) {
      if (!this.token) throw new Error('not logged in — call login() first');
      return Object.assign({ token: this.token }, extra || {});
    }

    // public reads
    economics() {
      return this._get('/api/economics');
    }
    price() {
      return this._get('/api/price');
    }

    // wallet login: provider must expose publicKey + signMessage
    async login(provider, opts) {
      opts = opts || {};
      if (!provider || !provider.publicKey || !provider.signMessage) {
        throw new Error('wallet provider missing publicKey/signMessage');
      }
      const wallet = provider.publicKey.toString();
      const { nonce, message } = await this._get('/api/nonce?wallet=' + encodeURIComponent(wallet));
      let sig = await provider.signMessage(enc.encode(message), 'utf8');
      if (sig && sig.signature) sig = sig.signature; // Phantom returns { signature }
      const signature = b58encode(sig instanceof Uint8Array ? sig : new Uint8Array(sig));
      const r = await this._post('/api/login', {
        wallet,
        signature,
        nonce,
        initData: opts.initData || null,
        ref: opts.ref || null,
      });
      this.token = r.token;
      if (this.persist && global.localStorage) localStorage.setItem('clean_token', this.token);
      return r.profile;
    }

    logout() {
      this.token = null;
      if (this.persist && global.localStorage) localStorage.removeItem('clean_token');
    }

    // authenticated actions (identical to the Mini App)
    profile() {
      return this._post('/api/profile', this._auth());
    }
    stake() {
      return this._post('/api/stake', this._auth());
    }
    unstake() {
      return this._post('/api/unstake', this._auth());
    }
    claim() {
      return this._post('/api/claim', this._auth());
    }
    burn(signature) {
      return this._post('/api/burn', this._auth({ signature }));
    }
    leaderboard() {
      return this._post('/api/leaderboard', this._auth());
    }
    referrals() {
      return this._post('/api/referrals', this._auth());
    }
  }

  global.CleanStaking = CleanStaking;
  if (typeof module !== 'undefined' && module.exports) module.exports = CleanStaking;
})(typeof window !== 'undefined' ? window : globalThis);
