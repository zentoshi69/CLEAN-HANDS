/*
 * app.js — CLEAN soft-staking Mini App logic.
 * Ties the wallet deeplink flow (wallet.js) to the staking API (same origin).
 *
 * This file is the reusable "engine": all business logic, the secure login
 * round-trip, and the API calls live here. The look is driven entirely by
 * index.html + CSS variables, so the whole app rebrands for a new client by
 * swapping the logo, the palette, and the copy — no logic changes.
 *
 * Login is a two-hop wallet round-trip:
 *   connect  -> (reload) onConnect -> GET /api/nonce -> signMessage
 *   sign     -> (reload) onSign    -> POST /api/login -> token -> app
 */
(function (global) {
  'use strict';
  const tg = global.Telegram && global.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor && tg.setHeaderColor('#F4FAFF');
      tg.setBackgroundColor && tg.setBackgroundColor('#F4FAFF');
    } catch (e) {}
  }
  const haptic = (t) => {
    try {
      tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(t || 'light');
    } catch (e) {}
  };
  const initData = (tg && tg.initData) || '';
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
  // localStorage so the session token + wallet state survive the deeplink
  // round-trip (mobile may return to a fresh webview that has no sessionStorage).
  const SS = global.localStorage;

  let TOKEN = SS.getItem('clw_token') || '';
  let PROFILE = null;
  // Active-wallet balances for the MM liquidity form: SOL read live via RPC,
  // $CLEAN from the profile the server already read on-chain. The form only ever
  // deals in these two assets ($CLEAN CA + SOL).
  const MM_BAL = { sol: 0, clean: 0, solLoaded: false };
  // Guards the irreversible signing paths (burn / MM deposit) from an accidental
  // double-tap during the async build window.
  let SIGNING = false;
  let PRICE = null;
  let MINT = '';
  let CONFIG = {};
  let _jupLoaded = false;

  // ---- helpers ---------------------------------------------------------- //
  const $ = (id) => document.getElementById(id);
  let _toastT;
  function toast(m) {
    const t = $('toast');
    if (!t) return;
    t.textContent = m;
    t.classList.add('show');
    clearTimeout(_toastT);
    _toastT = setTimeout(() => t.classList.remove('show'), 2400);
  }
  function fmt(n) {
    n = Number(n || 0);
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(2) + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  function pct(x) {
    return '+' + Math.round(Number(x || 0) * 100) + '%';
  }
  function escapeScoreLabel(score) {
    score = Number(score || 0);
    if (!score) return '—';
    return '×' + (score >= 10 ? score.toFixed(0) : score.toFixed(2));
  }
  function escapeBoostLabel(a) {
    const score = Number((a && a.escape_score) || 0);
    const raw = Number((a && a.escape_raw_score) || 0);
    const boost = Number((a && a.escape_boost) || 0);
    const rawBoost = Number((a && a.escape_boost_unscaled) || boost);
    const socialCount = Number((a && a.social_verified_count) || 0);
    const socialNeed = Number((a && a.social_required_count) || 3);
    const scale = a && a.escape_boost_scale != null ? Number(a.escape_boost_scale) : 1;
    const status = (a && a.escape_status) || '';
    if (status === 'paused') return 'paused by ops';
    if (status === 'review') return raw ? 'under review' : '—';
    if (status === 'verifying') return escapeScoreLabel(raw) + ' verifying';
    if (status === 'telegram_required') return 'open in Telegram';
    if (!score) return 'play to unlock';
    if (rawBoost && scale <= 0) return escapeScoreLabel(score) + ' locked · socials 0/' + socialNeed;
    if (rawBoost && scale < 1)
      return escapeScoreLabel(score) + ' → ' + pct(boost) + ' · socials ' + socialCount + '/' + socialNeed;
    return escapeScoreLabel(score) + ' → ' + pct(boost);
  }
  function socialActivationLabel(p) {
    const s = (p && p.socials) || {};
    const need = Number(s.required || (p && p.apr && p.apr.social_required_count) || 3);
    const count = Number(s.verified_count || (p && p.apr && p.apr.social_verified_count) || 0);
    const active = need ? Math.round((count / need) * 100) : 0;
    return count + '/' + need + ' · ' + active + '% active';
  }
  function socialPlatformLabel(p, key) {
    const platforms = ((p && p.socials && p.socials.platforms) || {});
    const s = platforms[key] || {};
    if (s.verified) return 'verified';
    if (s.status === 'pending') return 'pending review';
    if (s.status === 'rejected') return 'rejected';
    return key === 'tg' ? 'open in Telegram' : 'missing';
  }
  function esc(s) {
    return String(s).replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
    );
  }
  // Copy arbitrary text with the house haptic + toast (used by the portfolio
  // address chips, alongside the connected-wallet copyAddr).
  function copyText(text, msg) {
    if (text && navigator.clipboard) navigator.clipboard.writeText(text);
    haptic();
    toast(msg || 'Copied ✦');
  }
  async function api(path, body) {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 22000);
    try {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal: ctrl.signal,
      });
      clearTimeout(tid);
      if (!res.ok) {
        let detail = res.status;
        try {
          detail = (await res.json()).detail || detail;
        } catch (e) {}
        throw new Error(detail);
      }
      return res.json();
    } catch (e) {
      clearTimeout(tid);
      if (e.name === 'AbortError') throw new Error('Server timed out — try again');
      throw e;
    }
  }
  function authedBody(extra) {
    return Object.assign({ token: TOKEN }, extra || {});
  }
  // Fetch a login nonce, surfacing real errors (429/400) instead of letting an
  // error body flow through and make us sign the literal string "undefined".
  async function getNonce(pubkey) {
    const ctrl = new AbortController();
    const tid = setTimeout(() => ctrl.abort(), 10000);
    try {
      const r = await fetch('/api/nonce?wallet=' + encodeURIComponent(pubkey), { signal: ctrl.signal });
      clearTimeout(tid);
      if (!r.ok) {
        let d = r.status;
        try {
          d = (await r.json()).detail || d;
        } catch (e) {}
        throw new Error(d);
      }
      return r.json();
    } catch (e) {
      clearTimeout(tid);
      if (e.name === 'AbortError') throw new Error('Server timed out — try again');
      throw e;
    }
  }
  // Wrap a promise with a hard timeout; rejects with a friendly message on expiry.
  function withTimeout(p, ms, label) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error((label || 'Request') + ' timed out — try again')), ms);
      p.then(
        (v) => { clearTimeout(t); resolve(v); },
        (e) => { clearTimeout(t); reject(e); },
      );
    });
  }
  // Friendly message for server errors so "504" doesn't show raw to users.
  function _loginErrMsg(raw) {
    const s = String(raw || '');
    if (/50[234]|timeout|timed out/i.test(s)) return 'Server busy — please try again in a moment';
    if (/429|rate.?limit/i.test(s)) return 'Too many attempts — wait a minute and try again';
    if (/401|signature|nonce/i.test(s)) return 'Signature rejected — try connecting again';
    return 'Connect failed: ' + s;
  }
  // Only ever talk to an https RPC. If the configured swapRpc is missing or not
  // https (tampered /api/economics, misconfig), fall back to the public endpoint
  // rather than letting an attacker-chosen host build the burn/swap transaction.
  const DEFAULT_RPC = 'https://api.mainnet-beta.solana.com';
  function safeRpc() {
    const u = (CONFIG && CONFIG.swapRpc) || '';
    try {
      if (u && new URL(u).protocol === 'https:') return u;
    } catch (e) {}
    return DEFAULT_RPC;
  }
  const INVITE_TITLE = 'CLEAN HANDS DIRTY MONEY';
  const INVITE_TEXT = 'CLEAN HANDS DIRTY MONEY — play, stake & boost yield with my referral 🧤';
  function refCode() {
    const code = String((PROFILE && PROFILE.ref_code) || '').trim().toUpperCase();
    return /^[A-Z0-9]{4,12}$/.test(code) ? code : '';
  }
  function telegramStartLink(payload) {
    const bot = (CONFIG && CONFIG.botUsername) || 'YOUR_BOT';
    const short = (CONFIG && CONFIG.appShortName) || 'app';
    const suffix = payload ? `?startapp=${encodeURIComponent(payload)}` : '';
    return `https://t.me/${bot}/${short}${suffix}`;
  }
  function inviteLink() {
    const code = refCode();
    if (code) {
      const origin = (CONFIG && CONFIG.miniappUrl) || location.origin;
      return `${String(origin).replace(/\/$/, '')}/g/${encodeURIComponent(code)}`;
    }
    const fallback = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    return telegramStartLink(fallback);
  }
  function inviteLabel() {
    const code = refCode();
    return code ? `CLEAN referral · ${code}` : inviteLink().replace(/^https?:\/\//, '');
  }

  // ---- screens ---------------------------------------------------------- //
  function showApp() {
    $('connect').classList.add('hide');
    $('app').classList.remove('hide');
    loadPrice();
  }
  function showConnect(note) {
    $('app').classList.add('hide');
    $('connect').classList.remove('hide');
    $('connect-spin').classList.add('hide');
    if (note) $('connect-note').textContent = note;
    renderWallets();
  }
  function show(v) {
    ['stake', 'trade', 'boost', 'game', 'meme', 'board', 'invite', 'folio'].forEach((n) => {
      const el = $('view-' + n);
      if (el) el.hidden = n !== v;
    });
    document
      .querySelectorAll('.tabbtn')
      .forEach((b) => b.classList.toggle('on', b.dataset.tab === v));
    // Full-screen game: hide our app header (the embedded game brings its own).
    document.body.classList.toggle('game-mode', v === 'game');
    // Portfolio lives in the header (not the tab bar), so light its chip here.
    const fc = $('folio-chip');
    if (fc) fc.classList.toggle('on', v === 'folio');
    const sc = $('scroll');
    if (sc) {
      // Element.scrollTo(options) is missing in older webviews; never let
      // scroll sugar break navigation (or the boot session restore).
      try {
        sc.scrollTo({ top: 0, behavior: 'smooth' });
      } catch (_) {
        sc.scrollTop = 0;
      }
    }
    if (v === 'game') loadGame();
    haptic();
    if (v === 'board') loadBoard();
    if (v === 'boost') loadMmBalances();
    if (v === 'trade') loadPrice();
  }

  // GAME: lazy-load the embedded game on first open (so it never costs anything
  // until tapped), with an external fallback link if the host refuses framing.
  function loadGame() {
    const url = (CONFIG && CONFIG.gameUrl) || '/play';
    const f = $('game-frame');
    if (f && !f.getAttribute('src')) {
      // Play the loading intro: the glow rings the circle (~1.25s), the logo
      // shines once (~0.85s @1.2s). Reveal the game only once BOTH the shine has
      // finished (~2.15s min) AND the iframe has actually loaded — so we never
      // flash a half-loaded game. A 9s safety cap never traps the user.
      const intro = $('game-intro');
      if (intro) intro.classList.remove('done');
      let loaded = false,
        minElapsed = false;
      const reveal = () => {
        if (loaded && minElapsed && intro) intro.classList.add('done');
      };
      f.addEventListener('load', () => {
        loaded = true;
        // Hand the game our verified Telegram identity so it can cloud-save and
        // rank on the leaderboard. targetOrigin pins it to our own origin, so a
        // game served from a different origin (CONFIG.gameUrl) never sees initData.
        try {
          if (initData) f.contentWindow.postMessage({ type: 'clean:auth', initData }, location.origin);
        } catch (_) {}
        reveal();
      });
      setTimeout(() => {
        minElapsed = true;
        reveal();
      }, 2150);
      setTimeout(() => {
        if (intro) intro.classList.add('done');
      }, 9000);
      f.setAttribute('src', url); // game loads behind the intro
    }
  }

  // Invite lives in EVERY tab (no standalone invite section): clone a compact
  // referral card into each view. Class-based so there are no duplicate IDs;
  // paint() fills .inv-link / .inv-count.
  function injectInvite() {
    const tpl =
      '<div class="card panel inv-card"><h3>🤝 Invite &amp; earn</h3>' +
      '<p class="desc">Each active referral adds <b>+2% APR</b> (up to +30%). You have <b class="inv-count">0</b> active.</p>' +
      '<div class="ca"><span class="lab">REF</span><code class="inv-link">—</code>' +
      '<span class="cp" onclick="App.copyLink()">copy</span></div>' +
      '<button class="btn btn-solid" style="margin-top:10px" onclick="App.invite()">Share invite ✦</button></div>';
    ['stake', 'board', 'boost', 'trade'].forEach((n) => {
      const v = $('view-' + n);
      if (v && !v.querySelector('.inv-card')) {
        const d = document.createElement('div');
        d.innerHTML = tpl;
        const card = d.firstChild;
        if (!card) return;
        // on TOP for the leaderboard (board), at the BOTTOM for every other tab
        if (n === 'board') v.insertBefore(card, v.firstChild);
        else v.appendChild(card);
      }
    });
  }

  function addBtn(el, label, onclick, ghost) {
    const b = document.createElement('button');
    b.className = 'btn ' + (ghost ? 'btn-ghost' : 'btn-solid');
    b.style.marginTop = '10px';
    b.textContent = label;
    b.onclick = onclick;
    el.appendChild(b);
  }
  function setNote(msg) {
    const n = $('connect-note');
    if (n) n.textContent = msg;
  }
  // Visible diagnostic on the connect screen so we can see the real runtime state
  // (environment, injected wallet, crypto lib, last error) instead of guessing.
  function diag(extra) {
    let el = $('diag');
    if (!el) {
      el = document.createElement('div');
      el.id = 'diag';
      el.style.cssText = 'margin-top:16px;font-size:11px;opacity:.5;word-break:break-word';
      ($('connect') || document.body).appendChild(el);
    }
    const plat = (tg && tg.platform) || 'web';
    const inj = window.CleanWallet && CleanWallet.hasInjected() ? 'yes' : 'no';
    const naclOk = typeof window.nacl !== 'undefined' ? 'yes' : 'no';
    el.textContent =
      'env:' + plat + ' · wallet:' + inj + ' · crypto:' + naclOk + (extra ? ' · ' + extra : '');
  }

  // WalletConnect is the universal fallback: works on EVERY device with EVERY
  // wallet (QR scan / wallet directory via the official modal). Appended to every
  // branch below so no environment is ever a dead end.
  function addWalletConnect(el) {
    if (!(CONFIG && CONFIG.wcProjectId)) return;
    addBtn(el, '🔗 WalletConnect — any wallet', () => loginWalletConnect(), true);
  }

  function renderWallets() {
    const el = $('wallet-list');
    el.innerHTML = '';
    diag();

    // CASE 1 — one or more wallet providers detected (desktop extension via the
    // Wallet Standard / legacy injection, OR we're inside a wallet's in-app
    // browser). List every one we found and connect directly to the chosen one.
    const found = (CleanWallet.detected && CleanWallet.detected()) || [];
    if (found.length) {
      const last = SS.getItem('clw_wallet');
      found.forEach((w) =>
        addBtn(el, 'Connect ' + w.name + (w.id === last ? ' · last used' : ''), () =>
          loginInjected(w.id),
        ),
      );
      addWalletConnect(el);
      setNote(
        found.length === 1
          ? 'Approve the connect + sign request in your wallet.'
          : 'Choose your wallet to connect.',
      );
      return;
    }

    const plat = (tg && tg.platform) || '';
    const isTgDesktop = ['tdesktop', 'macos', 'weba', 'webk', 'web'].indexOf(plat) >= 0;

    if (isTgDesktop) {
      // CASE 2 — Telegram Desktop: its webview can't reach a browser extension.
      // Best path: WalletConnect QR (scan with the phone wallet, no app-switch).
      // Fallback: open the app in the real browser where the extension lives.
      addWalletConnect(el);
      addBtn(el, 'Open in my browser', () => openExt(location.origin + location.pathname), true);
      setNote(
        CONFIG && CONFIG.wcProjectId
          ? 'Scan the WalletConnect QR with your phone wallet, or open in your browser.'
          : 'On desktop, open the app in Chrome/Brave where your wallet extension is, then Connect Wallet.',
      );
      return;
    }

    // CASE 3 — Telegram mobile / mobile browser with no injected wallet.
    if (freshInitData()) {
      // Inside Telegram: the SERVER-mediated handshake is the robust path — the
      // wallet hop round-trips through our server and the webview just polls, so
      // it survives Telegram's iOS webview relaunch (and logs every step).
      Object.keys(TG_WALLETS).forEach((id) =>
        addBtn(el, 'Connect ' + TG_WALLETS[id].name, () => loginTg(id)),
      );
      addWalletConnect(el);
      CleanWallet.listWallets().forEach((w) =>
        addBtn(el, 'Open in ' + w.name + ' instead', () => CleanWallet.openInWallet(w.id), true),
      );
      setNote('Tap your wallet — approve connect + sign, then you land back here automatically.');
      return;
    }
    // Plain mobile browser (not Telegram): re-open the app INSIDE the wallet's
    // browser, where its provider is injected; WalletConnect covers the rest.
    CleanWallet.listWallets().forEach((w) => {
      addBtn(el, 'Open in ' + w.name, () => CleanWallet.openInWallet(w.id), true);
    });
    addWalletConnect(el);
    setNote('Tap your wallet — the app reopens inside it, then tap Connect Wallet.');
  }

  // ---- render profile --------------------------------------------------- //
  function paint(p) {
    const _prevBurn = PROFILE && PROFILE.apr ? Number(PROFILE.apr.burn_bonus_apr) || 0 : null;
    PROFILE = p;
    // sliding sessions: the server hands us a fresh token as the old one ages —
    // store it silently so active users never see a login screen again
    if (p.refreshed_token) {
      TOKEN = p.refreshed_token;
      SS.setItem('clw_token', TOKEN);
    }
    const a = p.apr || {};
    $('apr').textContent =
      (a.effective_apr_pct != null
        ? a.effective_apr_pct
        : Math.round((a.effective_apr || 0) * 100)) + '%';
    $('staked').textContent = fmt(p.staked);
    $('pending').textContent = fmt(p.pending_rewards);
    $('rank').textContent = '#' + fmt(p.rank);
    $('balance').textContent = fmt(p.balance) + ' $CLEAN';
    $('claimable').textContent = fmt(p.pending_rewards);
    paintClaimState(p);
    $('burned').textContent = fmt(p.total_burned) + ' $CLEAN';
    $('ref-count').textContent = fmt(p.active_referrals);
    $('inv-earned').textContent = fmt(p.active_referrals);
    $('b-base').textContent = pct(a.base);
    $('b-amount').textContent = pct(a.amount_boost);
    $('b-loyalty').textContent = pct(a.loyalty_boost);
    $('b-ref').textContent = pct(a.referral_boost);
    if ($('b-social')) $('b-social').textContent = socialActivationLabel(p);
    $('b-escape').textContent = pct(a.escape_boost);
    $('b-burn').textContent = pct(a.burn_bonus_apr);
    $('inv-bonus').textContent = pct(a.referral_boost);
    // grey out boosters that are still zero (matches the design's .zero style)
    [
      ['b-amount', a.amount_boost],
      ['b-loyalty', a.loyalty_boost],
      ['b-ref', a.referral_boost],
      ['b-social', (p.socials && p.socials.verified_count) || 0],
      ['b-escape', a.escape_boost],
      ['b-burn', a.burn_bonus_apr],
    ].forEach(([id, v]) => {
      const el = $(id);
      if (el) el.classList.toggle('zero', !Number(v));
    });
    paintSocials(p);
    // burn gauge
    $('boost-now').textContent = pct(a.burn_bonus_apr);
    const cap = Number((CONFIG && CONFIG.burn_cap_apr) || 2.0) || 2.0;
    $('boost-bar').style.width = Math.min(100, ((Number(a.burn_bonus_apr) || 0) / cap) * 100) + '%';
    paintBoostLab(a, _prevBurn);
    const pk = CleanWallet.currentPubkey() || p.wallet || '';
    const chipAddr = $('wallet-chip-addr');
    if (chipAddr) chipAddr.textContent = pk ? pk.slice(0, 4) + '…' + pk.slice(-4) : '—';
    const chip = $('wallet-chip');
    if (chip) chip.classList.toggle('connected', !!pk);
    if (MINT) $('ca-text').textContent = MINT;
    $('ref-text').textContent = inviteLabel();
    // invite cards injected into every tab (class-based, no duplicate IDs)
    const _invl = inviteLabel();
    document.querySelectorAll('.inv-link').forEach((e) => (e.textContent = _invl));
    document.querySelectorAll('.inv-count').forEach((e) => (e.textContent = fmt(p.active_referrals)));
    updatePortfolio();
    startTicker();
  }

  function paintSocials(p) {
    const s = (p && p.socials) || {};
    const platforms = s.platforms || {};
    const set = (id, text, on) => {
      const el = $(id);
      if (!el) return;
      el.textContent = text;
      el.classList.toggle('zero', !on);
    };
    set('soc-total', socialActivationLabel(p), Number(s.verified_count || 0) > 0);
    set('soc-tg', socialPlatformLabel(p, 'tg'), platforms.tg && platforms.tg.verified);
    set('soc-x', socialPlatformLabel(p, 'x'), platforms.x && platforms.x.verified);
    set('soc-discord', socialPlatformLabel(p, 'discord'), platforms.discord && platforms.discord.verified);
  }

  async function socialClaim(platform) {
    if (!TOKEN) return toast('Connect your wallet first');
    try {
      const input = $('social-' + platform + '-handle');
      const handle = input ? input.value.trim() : '';
      if (platform !== 'tg' && !handle) return toast('Add your ' + platform + ' handle first');
      const r = await api('/api/social/claim', authedBody({ platform, handle }));
      if (r.profile) paint(r.profile);
      haptic('medium');
      toast(
        platform === 'tg'
          ? 'Telegram verified ✦'
          : platform.toUpperCase() + ' submitted for review ✦',
      );
    } catch (e) {
      toast('Social verify failed: ' + String(e.message || e));
    }
  }

  function paintClaimState(p) {
    const note = $('claim-note');
    const btn = $('claim-btn');
    const setup = $('payout-setup');
    const locked = !!(p.claim_locked && p.claim_lock_days > 0);
    const needsPayout = !!(p.payout_setup_open && !p.payout_confirmed);
    if (setup) setup.classList.toggle('hide', !needsPayout);
    if (btn) btn.disabled = locked || needsPayout;
    if (!note) return;
    if (locked) {
      note.textContent =
        '🔒 Rewards unlock after ' +
        p.claim_lock_days +
        ' continuous days — ' +
        p.claim_unlock_in_days +
        'd to go. Unstaking resets pending rewards.';
    } else if (needsPayout) {
      note.textContent = '💳 Confirm a payout wallet before requesting rewards.';
    } else if (Number(p.claim_fee_usd) > 0) {
      note.textContent =
        'Payouts are manual requests. A $' +
        p.claim_fee_usd +
        ' processing fee is deducted in $CLEAN.';
    } else {
      note.textContent = 'Payouts are manual requests from the treasury.';
    }
  }

  let _tick = null;
  let _tickBase = null;
  function startTicker() {
    clearInterval(_tick);
    if (!PROFILE || !PROFILE.apr) return;
    const apr = Number(PROFILE.apr.effective_apr) || 0;
    const eff =
      Number(PROFILE.staked_effective != null ? PROFILE.staked_effective : PROFILE.staked) || 0;
    if (apr <= 0 || eff <= 0) return;
    _tickBase = { pending: Number(PROFILE.pending_rewards) || 0, t: Date.now() };
    const perSec = (eff * apr) / (365 * 24 * 3600);
    _tick = setInterval(() => {
      if (!_tickBase || document.hidden) return;
      const live = _tickBase.pending + perSec * ((Date.now() - _tickBase.t) / 1000);
      const a = $('pending');
      const b = $('claimable');
      const s = live >= 1000 ? fmt(live) : live.toFixed(4);
      if (a) a.textContent = s;
      if (b) b.textContent = s;
    }, 1000);
  }

  async function refresh() {
    try {
      const p = await api('/api/profile', authedBody());
      if (sessionStale(p)) {
        await reauthLive();
        return;
      }
      paint(p);
    } catch (e) {
      if (String(e.message).indexOf('session') >= 0 || e.message === 401) {
        TOKEN = '';
        SS.removeItem('clw_token');
        showConnect('Session expired — reconnect your wallet.');
      } else toast(String(e.message));
    }
  }

  let _escapeProfileRefresh = 0;
  window.addEventListener('message', (e) => {
    if (e.origin !== window.location.origin) return;
    const d = e.data || {};
    if (d.type !== 'clean:escape-saved') return;
    if (!TOKEN) return;
    const now = Date.now();
    if (now - _escapeProfileRefresh < 1500) return;
    _escapeProfileRefresh = now;
    setTimeout(refresh, 350);
  });

  // The saved session token authenticated a DIFFERENT, EMPTY wallet than the one
  // live in the extension right now. Classic Brave case: its built-in wallet
  // auto-injects and signs in first, so the token is for an unused address while
  // Phantom is what's actually connected — the chip shows Phantom (live pubkey)
  // but the balance reflects the token's empty wallet, i.e. "connected but 0".
  // Only ever fire when the session wallet has NOTHING staked/held/burned, so a
  // real linked/primary wallet (a deliberate multi-wallet portfolio) is never
  // hijacked.
  function sessionStale(p) {
    if (LINKING) return false; // mid multi-wallet link: divergence is intentional
    if (SS.getItem('clw_wallet') !== 'injected') return false; // extensions only
    const live = (CleanWallet.currentPubkey && CleanWallet.currentPubkey()) || '';
    if (!live || !p || !p.wallet || live === p.wallet) return false;
    return (
      !Number(p.balance) &&
      !Number(p.staked) &&
      !Number(p.pending_rewards) &&
      !Number(p.total_burned) &&
      !Number(p.active_referrals)
    );
  }

  // Re-authenticate as the wallet that is actually connected now, so the balances
  // on screen always belong to the address in the chip.
  let _reauthing = false;
  async function reauthLive() {
    if (_reauthing) return false;
    _reauthing = true;
    TOKEN = '';
    SS.removeItem('clw_token');
    const pk = (CleanWallet.currentPubkey && CleanWallet.currentPubkey()) || '';
    try {
      if (!pk) throw new Error('no live wallet');
      const { nonce, message } = await getNonce(pk);
      const sig = await CleanWallet.signInjected(message);
      const r = await api('/api/login', {
        wallet: pk,
        signature: sig,
        nonce: nonce,
        initData: initData || null,
        ref: startParam || null,
      });
      TOKEN = r.token;
      SS.setItem('clw_token', TOKEN);
      paint(r.profile);
      showApp();
      show('stake');
      return true;
    } catch (e) {
      showConnect('Reconnect your wallet to load your balances.');
      diag('reauth: ' + ((e && e.message) || e));
      return false;
    } finally {
      _reauthing = false;
    }
  }

  async function loadBoard() {
    try {
      const { leaderboard } = await api('/api/leaderboard', authedBody());
      const el = $('board-list');
      el.innerHTML = '';
      leaderboard.forEach((r) => {
        const d = document.createElement('div');
        d.className = 'lb';
        // Escape the rank too: it lands in innerHTML, and a non-numeric value from
        // the API would otherwise be an XSS sink (name is already escaped).
        const medal =
          r.rank === 1 ? '🥇' : r.rank === 2 ? '🥈' : r.rank === 3 ? '🥉' : esc(String(r.rank));
        const top = r.rank <= 3 ? ' top' : '';
        d.innerHTML = `<div class="rk${top}">${medal}</div><div class="ad">${r.me ? 'You' : esc(r.name)}</div><div class="amt">${fmt(r.staked)}</div>`;
        el.appendChild(d);
      });
    } catch (e) {
      toast(String(e.message));
    }
  }

  // ---- actions ---------------------------------------------------------- //
  let STAKE_PCT = 100;
  function wirePctRow() {
    const row = $('pct-row');
    if (!row) return;
    row.querySelectorAll('.pctbtn').forEach((b) => {
      b.onclick = () => {
        STAKE_PCT = +b.dataset.pct || 100;
        row.querySelectorAll('.pctbtn').forEach((x) => x.classList.toggle('on', x === b));
        const sb = $('stake-btn');
        if (sb) sb.textContent = STAKE_PCT === 100 ? 'Stake my $CLEAN' : 'Stake ' + STAKE_PCT + '%';
        haptic();
      };
    });
  }
  async function stake() {
    try {
      const prev = (PROFILE && Number(PROFILE.staked)) || 0;
      const p = await api('/api/stake', authedBody({ percent: STAKE_PCT }));
      paint(p);
      haptic('medium');
      // re-staking after buying more $CLEAN stacks the new tokens on top (the
      // server now reads the live balance), so call out the increase explicitly.
      const added = (Number(p.staked) || 0) - prev;
      if (prev > 0 && added > 0) {
        toast('Added ' + fmt(added) + ' $CLEAN to your stake ✦');
      } else {
        toast(STAKE_PCT === 100 ? 'Soft-staked ✦' : 'Soft-staked ' + STAKE_PCT + '% of your bag ✦');
      }
    } catch (e) {
      toast(String(e.message));
    }
  }
  async function unstake() {
    try {
      paint(await api('/api/unstake', authedBody()));
      toast('Unstaked');
    } catch (e) {
      toast(String(e.message));
    }
  }
  async function claim() {
    try {
      const r = await api('/api/claim', authedBody());
      paint(r.profile);
      haptic('medium');
      toast('Payout requested: ' + fmt(r.requested || r.claimed) + ' $CLEAN ✦');
    } catch (e) {
      toast(String(e.message));
    }
  }
  async function confirmPayout() {
    if (!PROFILE || !TOKEN) return toast('Connect your wallet first');
    const raw = ($('payout-addr') && $('payout-addr').value.trim()) || '';
    const requested = raw || PROFILE.wallet;
    const live = (CleanWallet.currentPubkey && CleanWallet.currentPubkey()) || '';
    if (live && PROFILE.wallet && live !== PROFILE.wallet)
      return toast('Reconnect the staking wallet before changing payout.');
    try {
      const n = await api('/api/payout/nonce', authedBody({ address: requested }));
      const mode = SS.getItem('clw_wallet') || '';
      let sig = '';
      if (mode === 'injected' && CleanWallet.signInjected) {
        sig = await withTimeout(CleanWallet.signInjected(n.message), 60000, 'Wallet sign');
      } else if (mode === 'walletconnect' && CleanWallet.wcSign) {
        sig = await withTimeout(CleanWallet.wcSign(n.message), 60000, 'WalletConnect sign');
      } else {
        throw new Error(
          'Fresh wallet signature required — reconnect in a wallet/browser session, then confirm payout.',
        );
      }
      const p = await api(
        '/api/payout',
        authedBody({ address: n.address || requested, nonce: n.nonce, signature: sig }),
      );
      paint(p);
      haptic('medium');
      toast('Payout wallet confirmed ✦');
    } catch (e) {
      toast('Payout setup failed: ' + String(e.message || e));
    }
  }
  async function submitBurn() {
    const sig = $('burn-sig').value.trim();
    if (!sig) return toast('Paste your burn tx signature');
    try {
      const r = await api('/api/burn', authedBody({ signature: sig }));
      paint(r.profile);
      $('burn-sig').value = '';
      toast('🔥 Burn credited: ' + fmt(r.burned));
    } catch (e) {
      toast(String(e.message));
    }
  }
  function invite() {
    const url = inviteLink();
    if (tg && tg.openTelegramLink)
      tg.openTelegramLink(
        `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(INVITE_TEXT)}`,
      );
    else if (navigator.share) navigator.share({ title: INVITE_TITLE, text: INVITE_TEXT, url }).catch(() => {});
    else copyLink();
  }
  function copyLink() {
    const url = inviteLink();
    navigator.clipboard && navigator.clipboard.writeText(`${INVITE_TEXT}\n${url}`);
    haptic();
    toast('Clean invite copied ✦');
  }
  function copyCA() {
    if (!MINT) return toast('Token still loading…');
    navigator.clipboard && navigator.clipboard.writeText(MINT);
    haptic();
    toast('Contract copied ✦');
  }
  function logout() {
    clearInterval(_tick);
    hideWalletMenu();
    CleanWallet.disconnect();
    TOKEN = '';
    SS.removeItem('clw_token');
    showConnect('Wallet disconnected.');
  }
  function hideWalletMenu() {
    const m = $('wmenu');
    if (m) m.classList.add('hide');
  }
  function walletMenu(ev) {
    ev && ev.stopPropagation();
    const m = $('wmenu');
    if (!m) return;
    const pk = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    $('wmenu-addr').textContent = pk || 'Not connected';
    m.classList.toggle('hide');
  }
  function copyAddr() {
    const pk = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    if (pk && navigator.clipboard) navigator.clipboard.writeText(pk);
    hideWalletMenu();
    toast('Address copied ✦');
  }
  function changeWallet() {
    hideWalletMenu();
    CleanWallet.disconnect();
    TOKEN = '';
    SS.removeItem('clw_token');
    PROFILE = null;
    showConnect('Pick a wallet to connect.');
  }
  document.addEventListener('click', (ev) => {
    const m = $('wmenu');
    if (m && !m.classList.contains('hide') && !ev.target.closest('.wallet-wrap')) hideWalletMenu();
  });

  // ---- AI meme generator ------------------------------------------------- //
  let MEME_DATA = null;
  async function genMeme() {
    const idea = ($('meme-idea').value || '').trim();
    if (!idea) return toast('Describe your meme idea');
    $('meme-spin').classList.remove('hide');
    $('meme-out').classList.add('hide');
    try {
      const r = await api('/api/meme', authedBody({ idea }));
      // Only ever load a data:image or https: URL into the <img>. Assigning an
      // unvalidated API string to .src is an injection foothold (e.g. an
      // attacker-influenced javascript:/text payload) — coerce + allowlist.
      const img = String(r.image || '');
      if (!/^(data:image\/(png|jpe?g|gif|webp|svg\+xml);|https:\/\/)/i.test(img)) {
        throw new Error('unexpected image format');
      }
      MEME_DATA = img;
      $('meme-img').src = img;
      $('meme-out').classList.remove('hide');
      if (r.remaining != null) $('meme-left').textContent = r.remaining + ' left today';
    } catch (e) {
      toast(String(e.message));
    } finally {
      $('meme-spin').classList.add('hide');
    }
  }
  function downloadMeme() {
    if (!MEME_DATA) return;
    const a = document.createElement('a');
    a.href = MEME_DATA;
    a.download = 'clean-meme.png';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  async function shareMeme() {
    if (!MEME_DATA) return;
    try {
      const blob = await (await fetch(MEME_DATA)).blob();
      const file = new File([blob], 'clean-meme.png', { type: 'image/png' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], text: 'Made with $CLEAN 🧤' });
        return;
      }
    } catch (e) {}
    downloadMeme();
    toast('Saved — share it in the group!');
  }

  // ---- market / trade --------------------------------------------------- //
  function usd(n) {
    const v = (Number(n) || 0) * (PRICE ? PRICE.price_usd : 0);
    return '$' + (v >= 1 ? fmt(v) : v.toPrecision(2));
  }
  async function loadPrice() {
    try {
      PRICE = await (await fetch('/api/price')).json();
      MINT = PRICE.mint || MINT;
      if (MINT) $('ca-text').textContent = MINT;
      if (!PRICE.available) {
        $('m-price').textContent = 'no data';
        return;
      }
      const p = PRICE.price_usd;
      $('m-price').textContent = '$' + (p < 1 ? p.toPrecision(3) : fmt(p));
      $('m-change').textContent =
        PRICE.change_24h != null
          ? (PRICE.change_24h >= 0 ? '+' : '') + Number(PRICE.change_24h).toFixed(1) + '%'
          : '—';
      $('m-mc').textContent = PRICE.market_cap ? '$' + fmt(PRICE.market_cap) : '—';
      $('m-liq').textContent = PRICE.liquidity_usd ? '$' + fmt(PRICE.liquidity_usd) : '—';
      $('m-vol').textContent = PRICE.volume_24h ? '$' + fmt(PRICE.volume_24h) : '—';
      updatePortfolio();
      // If the price lands while the portfolio is open, repaint it so the USD
      // columns fill in instead of staying blank until the next link/unlink.
      const fv = $('view-folio');
      if (TOKEN && fv && !fv.hidden) loadFolio();
    } catch (e) {}
  }
  function updatePortfolio() {
    if (!PRICE || !PRICE.available || !PROFILE) return;
    const el = $('m-portfolio');
    if (el)
      el.textContent = `Your stake ${usd(PROFILE.staked)} · pending ${usd(PROFILE.pending_rewards)} · wallet ${usd(PROFILE.balance)}`;
  }
  function openExt(u) {
    if (tg && tg.openLink) tg.openLink(u);
    else window.open(u, '_blank');
  }
  function appSurfaceUrl(serverPath, localFile) {
    if (location.protocol !== 'file:') return location.origin + serverPath;
    try {
      const here = new URL(location.href);
      here.hash = '';
      here.search = '';
      here.pathname = here.pathname.replace(/[^/]*$/, localFile);
      return here.href;
    } catch (e) {
      return localFile;
    }
  }
  function buy() {
    if (MINT)
      openExt(`https://jup.ag/swap?sell=So11111111111111111111111111111111111111112&buy=${MINT}`);
    else toast('Token still loading…');
  }
  function chart() {
    openExt(PRICE && PRICE.url ? PRICE.url : `https://dexscreener.com/solana/${MINT}`);
  }
  function birdeye() {
    if (MINT) openExt(`https://birdeye.so/token/${MINT}?chain=solana`);
  }
  function whitepaper() {
    openExt(appSurfaceUrl('/whitepaper', 'whitepaper.html'));
  }
  function loadSwap() {
    if (!MINT) return toast('Price still loading…');
    if (_jupLoaded) return toast('Swap widget already loaded below.');
    const s = document.createElement('script');
    s.src = 'https://plugin.jup.ag/plugin-v1.js';
    s.async = true;
    s.onload = () => {
      try {
        window.Jupiter.init({
          displayMode: 'integrated',
          integratedTargetId: 'jup-terminal',
          endpoint: safeRpc(),
          formProps: {
            initialInputMint: 'So11111111111111111111111111111111111111112',
            initialOutputMint: MINT,
          },
        });
        _jupLoaded = true;
      } catch (e) {
        toast('Swap widget unavailable — use “Buy $CLEAN”.');
      }
    };
    s.onerror = () => toast('Swap widget failed to load — use “Buy $CLEAN”.');
    document.body.appendChild(s);
  }

  // ---- injected (extension) login: connect -> nonce -> sign -> login ----- //
  let CONNECTING = false;
  async function loginInjected(id) {
    if (CONNECTING) return; // prevent double-tap / concurrent connect attempts
    try {
      CONNECTING = true;
      $('connect-spin').classList.remove('hide');
      const pk = await withTimeout(CleanWallet.connectInjected(id), 60000, 'Wallet connect');
      const { nonce, message } = await getNonce(pk);
      const sig = await withTimeout(CleanWallet.signInjected(message), 60000, 'Wallet sign');
      const r = await api('/api/login', {
        wallet: pk,
        signature: sig,
        nonce: nonce,
        initData: initData || null,
        ref: startParam || null,
      });
      TOKEN = r.token;
      SS.setItem('clw_token', TOKEN);
      paint(r.profile);
      showApp();
      show('stake');
      CONNECTING = false;
    } catch (e) {
      CONNECTING = false;
      const msg = String(e.message || e);
      toast(_loginErrMsg(msg));
      showConnect();
      diag('connect err: ' + msg);
    }
  }

  // ---- live wallet-account switching ------------------------------------ //
  // The user changed the active account inside their wallet. Drop the old
  // session immediately (never keep showing wallet A's data while wallet B is
  // active) and seamlessly re-authenticate as the new wallet — which needs one
  // signature, since the server only mints a session after verifying ownership.
  let _switching = false;
  async function onWalletSwitch(newAddr) {
    // a portfolio link flow connects a SECOND wallet on purpose — that is
    // not an account switch; don't tear the session down mid-link
    if (LINKING) return;
    if (!newAddr) {
      // Wallets emit 'disconnect' on every page reload — that is NOT the user
      // logging out. Keep the server session; the wallet re-attaches lazily the
      // next time a signature is needed. Only land on the connect screen when
      // there is no session to keep.
      if (TOKEN) return;
      showConnect('Wallet disconnected.');
      return;
    }
    if (newAddr === (PROFILE && PROFILE.wallet)) return; // same account, no-op
    // PORTFOLIO MODE: with the All-wallets screen open, switching accounts
    // inside the extension means "add this one" — the only way to reach a
    // 2nd/3rd account of the SAME extension (connect() always returns the
    // active account, so a plain Link can never see the others).
    const folioView = $('view-folio');
    if (TOKEN && folioView && !folioView.hidden) {
      if (_switching) return;
      _switching = true;
      try {
        toast('Adding ' + newAddr.slice(0, 4) + '… — sign once to prove it\'s yours');
        const { nonce, message } = await getNonce(newAddr);
        const sig = await CleanWallet.signInjected(message);
        const r = await api('/api/link', authedBody({ wallet: newAddr, signature: sig, nonce: nonce }));
        renderFolio(r);
        toast('🧤 ' + newAddr.slice(0, 4) + '…' + newAddr.slice(-4) + ' added to your portfolio');
      } catch (e) {
        toast('Add failed: ' + (e.message || e));
      } finally {
        _switching = false;
      }
      return;
    }
    if (_switching) return;
    _switching = true;
    // keep the CURRENT session alive until the new wallet actually signs in —
    // declining the signature must not log the user out of wallet A
    const oldTok = TOKEN;
    const oldProf = PROFILE;
    try {
      toast('Wallet switched — sign to continue as ' + newAddr.slice(0, 4) + '…');
      const { nonce, message } = await getNonce(newAddr);
      const sig = await withTimeout(CleanWallet.signInjected(message), 60000, 'Wallet sign');
      const r = await api('/api/login', {
        wallet: newAddr,
        signature: sig,
        nonce: nonce,
        initData: initData || null,
        ref: startParam || null,
      });
      TOKEN = r.token;
      SS.setItem('clw_token', TOKEN);
      paint(r.profile);
      showApp();
      show('stake');
      toast('Switched to ' + newAddr.slice(0, 4) + '…' + newAddr.slice(-4));
    } catch (e) {
      TOKEN = oldTok;
      PROFILE = oldProf;
      if (TOKEN) {
        SS.setItem('clw_token', TOKEN);
        toast('Switch cancelled — still signed in as ' + (oldProf && oldProf.wallet ? oldProf.wallet.slice(0, 4) + '…' : 'before'));
      } else {
        showConnect('Wallet changed — reconnect to continue.');
      }
    } finally {
      _switching = false;
    }
  }

  // ---- WalletConnect login: QR/relay -> nonce -> sign -> login ----------- //
  async function loginWalletConnect() {
    if (CONNECTING) return; // prevent double-tap / concurrent connect attempts
    try {
      CONNECTING = true;
      $('connect-spin').classList.remove('hide');
      toast('Opening WalletConnect…');
      const pk = await withTimeout(CleanWallet.wcConnect(CONFIG.wcProjectId), 120000, 'WalletConnect');
      const { nonce, message } = await getNonce(pk);
      const sig = await withTimeout(CleanWallet.wcSign(message), 60000, 'WalletConnect sign');
      const r = await api('/api/login', {
        wallet: pk,
        signature: sig,
        nonce: nonce,
        initData: initData || null,
        ref: startParam || null,
      });
      TOKEN = r.token;
      SS.setItem('clw_token', TOKEN);
      paint(r.profile);
      showApp();
      show('stake');
      CONNECTING = false;
    } catch (e) {
      CONNECTING = false;
      const msg = String(e.message || e);
      // A failed SDK fetch / user-closed modal shouldn't read like a crash —
      // most desktop users have an extension and never need this path.
      const friendly =
        /dynamically imported module|Importing a module|Failed to fetch|module load|script load/i.test(
          msg,
        )
          ? 'WalletConnect is busy — use a wallet extension above, or try again.'
          : /closed|reject|cancel/i.test(msg)
            ? 'WalletConnect cancelled.'
            : /50[234]|timeout|timed out/i.test(msg)
              ? 'Server busy — please try again in a moment'
              : 'WalletConnect: ' + msg;
      toast(friendly);
      showConnect();
      diag('wc err: ' + msg);
    }
  }

  // ---- login round-trip (mobile deeplink) ------------------------------- //
  async function afterConnect(pubkey) {
    try {
      $('connect-spin').classList.remove('hide');
      const { nonce, message } = await getNonce(pubkey);
      CleanWallet.signMessage(message, { nonce: nonce, wallet: pubkey });
    } catch (e) {
      CONNECTING = false;
      toast('Login failed: ' + e.message);
      showConnect();
    }
  }
  async function afterSign(signatureB58, ctx) {
    try {
      if (ctx && ctx.mode === 'link') {
        const r = await api('/api/link', authedBody({ wallet: ctx.wallet, signature: signatureB58, nonce: ctx.nonce }));
        showApp();
        show('folio');
        renderFolio(r);
        toast('🧤 Wallet linked');
        return;
      }
      const r = await api('/api/login', {
        wallet: ctx.wallet,
        signature: signatureB58,
        nonce: ctx.nonce,
        initData: initData || null,
        ref: startParam || null,
      });
      TOKEN = r.token;
      SS.setItem('clw_token', TOKEN);
      paint(r.profile);
      showApp();
      show('stake');
      CONNECTING = false;
    } catch (e) {
      CONNECTING = false;
      toast('Login failed: ' + e.message);
      showConnect('Login failed — try again.');
    }
  }

  // ---- Telegram server-mediated wallet handshake (mobile-robust) --------- //
  // The wallet hop round-trips through OUR server (/api/tg/connect -> /api/tg/
  // sign) and the webview just POLLS by its verified Telegram identity, so it
  // completes no matter how often Telegram relaunches the iOS webview. The
  // server logs every step ([tg-handshake]). This is the reliable mobile path.
  const TG_WALLETS = {
    phantom: { name: 'Phantom', base: 'https://phantom.app/ul/v1' },
    solflare: { name: 'Solflare', base: 'https://solflare.com/ul/v1' },
    backpack: { name: 'Backpack', base: 'https://backpack.app/ul/v1' },
  };
  let _tgPoll = null;
  let _tgDeadline = 0;
  function stopTgPoll() {
    if (_tgPoll) {
      clearTimeout(_tgPoll);
      _tgPoll = null;
    }
  }
  // Re-read initData on EVERY call — Telegram refreshes it on a webview relaunch,
  // so a value cached at boot can age past the server's initData TTL mid-flow.
  function freshInitData() {
    return (tg && tg.initData) || initData || '';
  }
  async function loginTg(walletId) {
    const w = TG_WALLETS[walletId];
    if (!w) return loginWalletConnect();
    const id = freshInitData();
    if (!id) return toast('Open this inside Telegram to connect this way.');
    CONNECTING = true;
    $('connect-spin').classList.remove('hide');
    setNote('Opening ' + w.name + ' — approve the connect, then the signature.');
    try {
      const r = await api('/api/tg/start', { initData: id, wallet: walletId });
      SS.setItem('clw_tgsid', r.sid);
      const params = new URLSearchParams({
        dapp_encryption_public_key: r.dapp_pub,
        cluster: 'mainnet-beta',
        app_url: location.origin,
        redirect_link: location.origin + '/api/tg/connect/' + r.sid,
      });
      const ul = w.base + '/connect?' + params.toString();
      if (tg && tg.openLink) tg.openLink(ul);
      else window.location.href = ul;
      pollTg(true);
    } catch (e) {
      CONNECTING = false;
      $('connect-spin').classList.add('hide');
      toast('Couldn’t start sign-in: ' + (e.message || e));
      diag('tg start err: ' + (e.message || e));
    }
  }
  function pollTg(fresh) {
    stopTgPoll();
    if (fresh || Date.now() > _tgDeadline) _tgDeadline = Date.now() + 5 * 60 * 1000;
    const tick = async () => {
      if (Date.now() > _tgDeadline) {
        stopTgPoll();
        CONNECTING = false;
        $('connect-spin').classList.add('hide');
        setNote('Didn’t hear back from your wallet — tap your wallet to try again.');
        return;
      }
      let r = null;
      try {
        r = await api('/api/tg/poll', { initData: freshInitData(), sid: SS.getItem('clw_tgsid') || '' });
      } catch (e) {
        /* transient network/429 — keep polling until the deadline */
      }
      if (r && r.status === 'done') {
        stopTgPoll();
        SS.removeItem('clw_tgsid');
        TOKEN = r.token;
        SS.setItem('clw_token', TOKEN);
        CONNECTING = false;
        try {
          paint(r.profile);
        } catch (_) {}
        showApp();
        show('stake');
        return;
      }
      if (r && r.status === 'error') {
        stopTgPoll();
        SS.removeItem('clw_tgsid');
        CONNECTING = false;
        $('connect-spin').classList.add('hide');
        setNote('Sign-in problem: ' + (r.detail || 'try again'));
        return;
      }
      _tgPoll = setTimeout(tick, 2000);
    };
    _tgPoll = setTimeout(tick, 1500);
  }

  // ---- in-app burn (beta, flag-gated; burn is irreversible) ------------- //
  async function afterBurnTx(sig, ctx) {
    try {
      const r = await api('/api/burn', authedBody({ signature: sig }));
      paint(r.profile);
      toast('🔥 Burn credited: ' + fmt(r.burned));
    } catch (e) {
      toast('Burn submit failed: ' + String(e.message));
    }
  }
  async function burnInApp() {
    const amt = parseFloat(($('burn-amount') || {}).value);
    if (!amt || amt <= 0) return toast('Enter an amount to burn');
    if (!MINT) return toast('Token not loaded yet');
    const pk = CleanWallet.currentPubkey();
    if (!pk) return toast('Connect your wallet first');
    if (SIGNING) return toast('Hold on — still finishing the last action…');
    SIGNING = true;
    toast('Building burn…');
    try {
      // Pinned to a minor line (not a bare major) to shrink the moving-target
      // surface on this signing path. Durable fix: self-host these bundles —
      // dynamic import() can't carry SRI. (Gated behind CONFIG.inAppBurn.)
      const web3 = await import('https://esm.sh/@solana/web3.js@1.95');
      const splt = await import('https://esm.sh/@solana/spl-token@0.4');
      const conn = new web3.Connection(safeRpc());
      const owner = new web3.PublicKey(pk);
      const mint = new web3.PublicKey(MINT);
      const ata = await splt.getAssociatedTokenAddress(mint, owner);
      const dec = (CONFIG && CONFIG.decimals) || 6;
      const raw = BigInt(Math.round(amt * 10 ** dec));
      const tx = new web3.Transaction().add(
        splt.createBurnCheckedInstruction(ata, mint, owner, raw, dec),
      );
      tx.feePayer = owner;
      tx.recentBlockhash = (await conn.getLatestBlockhash()).blockhash;
      const ser = tx.serialize({ requireAllSignatures: false, verifySignatures: false });
      CleanWallet.signAndSendTransaction(CleanWallet.b58encode(new Uint8Array(ser)), {
        amount: amt,
      });
    } catch (e) {
      toast('Burn build failed: ' + (e.message || e));
    } finally {
      SIGNING = false;
    }
  }

  // Burn quick-chips: prefill the in-app burn amount when that beta path is on,
  // otherwise point the user at the always-available paste-signature flow.
  function wireBurnChips() {
    document.querySelectorAll('.chip[data-burn]').forEach((b) => {
      b.onclick = () => {
        const amt = b.dataset.burn;
        if (CONFIG.inAppBurn) {
          const el = $('burn-amount');
          if (el) el.value = amt;
          burnInApp();
        } else {
          toast(
            'Burn ' + fmt(+amt) + ' $CLEAN from your wallet, then paste the tx signature below',
          );
          const s = $('burn-sig');
          if (s) s.focus();
        }
      };
    });
  }

  // ---- Boost Lab -------------------------------------------------------- //
  // Drives the booster dial + milestones from REAL profile data. Boosters are
  // additive: multiplier = 1 + amount + wallet + loyalty + referral + liquidity
  // + Escape. Burn is a flat APR bonus on top (shown separately).
  function paintBoostLab(a, prevBurn) {
    const add =
      (Number(a.amount_boost) || 0) +
      (Number(a.wallet_boost) || 0) +
      (Number(a.loyalty_boost) || 0) +
      (Number(a.referral_boost) || 0) +
      (Number(a.liquidity_boost) || 0) +
      (Number(a.escape_boost) || 0) +
      (Number(a.vip_boost) || 0);
    const mult = 1 + add;
    const set = (id, v) => {
      const e = $(id);
      if (e) e.textContent = v;
    };
    set('bl-mult', mult.toFixed(2) + '×');
    const dial = $('bl-dial');
    if (dial) dial.style.setProperty('--blp', Math.max(0, Math.min(100, ((mult - 1) / 2) * 100)));
    set(
      'bl-apr',
      (a.effective_apr_pct != null
        ? a.effective_apr_pct
        : Math.round((a.effective_apr || 0) * 100)) + '%',
    );
    set('bl-amount', pct(a.amount_boost));
    set('bl-wallet', pct(a.wallet_boost));
    set('bl-loyalty', pct(a.loyalty_boost));
    set('bl-ref', pct(a.referral_boost));
    set('bl-lp', a.liquidity_boost ? pct(a.liquidity_boost) : 'soon');
    set('bl-social', socialActivationLabel(PROFILE));
    set('bl-escape', escapeBoostLabel(a));
    set('bl-vip', a.vip_boost ? '3× locked ✦' : '—');
    set('bl-burn', pct(a.burn_bonus_apr));
    [
      ['bl-amount', a.amount_boost],
      ['bl-wallet', a.wallet_boost],
      ['bl-loyalty', a.loyalty_boost],
      ['bl-ref', a.referral_boost],
      ['bl-lp', a.liquidity_boost],
      ['bl-social', (PROFILE && PROFILE.socials && PROFILE.socials.verified_count) || 0],
      ['bl-escape', a.escape_boost],
      ['bl-vip', a.vip_boost],
      ['bl-burn', a.burn_bonus_apr],
    ].forEach(([id, v]) => {
      const e = $(id);
      if (e) e.classList.toggle('zero', !Number(v));
    });
    document.querySelectorAll('#bl-miles .bl-mile').forEach((el) => {
      el.classList.toggle('lit', mult >= parseFloat(el.getAttribute('data-m')) - 1e-9);
    });
    // Fire the wash-cycle celebration whenever a real burn just credited.
    if (prevBurn != null && (Number(a.burn_bonus_apr) || 0) > prevBurn + 1e-9) celebrateBoost();
  }

  function celebrateBoost() {
    const box = $('bl-burst');
    if (box) {
      for (let i = 0; i < 24; i++) {
        const s = document.createElement('span');
        s.className = 'bl-spark';
        s.textContent = '✦';
        s.style.left = 20 + Math.random() * 60 + '%';
        s.style.top = 18 + Math.random() * 50 + '%';
        s.style.fontSize = 10 + Math.random() * 20 + 'px';
        const ang = Math.random() * 6.283;
        const d = 50 + Math.random() * 90;
        s.style.setProperty('--dx', (Math.cos(ang) * d).toFixed(0) + 'px');
        s.style.setProperty('--dy', (Math.sin(ang) * d).toFixed(0) + 'px');
        box.appendChild(s);
        setTimeout(() => s.parentNode && s.parentNode.removeChild(s), 950);
      }
    }
    const dial = $('bl-dial');
    if (dial) {
      dial.classList.remove('bl-pop');
      void dial.offsetWidth;
      dial.classList.add('bl-pop');
    }
    haptic();
  }

  function initBoostLab() {
    const box = $('bl-bubbles');
    if (!box || box.childElementCount) return;
    for (let i = 0; i < 9; i++) {
      const b = document.createElement('i');
      const sz = 7 + Math.random() * 22;
      b.style.width = b.style.height = sz + 'px';
      b.style.left = Math.random() * 100 + '%';
      b.style.animationDuration = 6 + Math.random() * 7 + 's';
      b.style.animationDelay = -Math.random() * 10 + 's';
      box.appendChild(b);
    }
  }

  // Market-maker liquidity. After the wallet signs+sends the deposit, the
  // on-chain signature returns via onTx -> afterMmTx, which credits the booster.
  async function afterMmTx(sig, ctx) {
    try {
      const r = await api('/api/mm/add', authedBody({ signature: sig }));
      paint(r.profile);
      loadMmBalances();
      if (typeof celebrateBoost === 'function') celebrateBoost();
      toast('💧 Liquidity credited: +$' + fmt(r.added_usd));
    } catch (e) {
      toast('Liquidity submit failed: ' + String(e.message));
    }
  }

  // Deposit SOL (+ optional $CLEAN) to the MM reserve. SOL is required; $CLEAN is
  // optional but never SOL-less. Server enforces the USD min/max; we pre-check
  // with a live quote so we don't make the user sign a deposit it would reject.
  async function addLiquidity() {
    if (!CONFIG.mmEnabled || !CONFIG.mmWallet) return toast('Liquidity booster — coming soon ✦');
    const pk = CleanWallet.currentPubkey();
    if (!pk) return toast('Connect your wallet first');
    const solAmt = parseFloat(($('bl-lp-sol') || {}).value) || 0;
    const cleanAmt = parseFloat(($('bl-lp-clean') || {}).value) || 0;
    const min = CONFIG.mmMinUsd || 50, max = CONFIG.mmMaxUsd || 500;
    if (solAmt <= 0) return toast('Add SOL — the SOL leg is required (min $' + min + ')');
    // Refresh the live wallet balance and never let the user sign a deposit
    // bigger than they hold ($CLEAN + SOL only).
    await loadMmBalances();
    if (MM_BAL.solLoaded) {
      if (solAmt > MM_BAL.sol)
        return toast('Not enough SOL — you have ' + MM_BAL.sol.toFixed(4));
      if (solAmt > MM_BAL.sol - 0.01)
        return toast('Leave ~0.01 SOL in the wallet for the network fee');
    }
    if (PROFILE && cleanAmt > MM_BAL.clean)
      return toast('Not enough $CLEAN — you have ' + fmt(MM_BAL.clean));
    if (SIGNING) return toast('Hold on — still finishing the last deposit…');
    SIGNING = true;
    try {
      const q = await fetch('/api/mm/quote').then((r) => r.json());
      const solUsd = solAmt * (q.sol_usd || 0);
      const cleanUsd = cleanAmt * (q.clean_usd || 0);
      if (q.sol_usd && solUsd < min) return toast('SOL leg must be ≥ $' + min + ' (≈ $' + solUsd.toFixed(0) + ')');
      if (cleanAmt > 0 && q.clean_usd) {
        if (cleanUsd < min) return toast('If you add $CLEAN it must be ≥ $' + min);
        if (cleanUsd >= max) return toast('$CLEAN leg must be under $' + max);
      }
    } catch (e) {}
    toast('Building deposit…');
    try {
      const web3 = await import('https://esm.sh/@solana/web3.js@1.95');
      const owner = new web3.PublicKey(pk);
      const reserve = new web3.PublicKey(CONFIG.mmWallet);
      const conn = new web3.Connection(safeRpc());
      const tx = new web3.Transaction();
      tx.add(web3.SystemProgram.transfer({
        fromPubkey: owner, toPubkey: reserve, lamports: Math.round(solAmt * 1e9),
      }));
      if (cleanAmt > 0 && MINT) {
        const splt = await import('https://esm.sh/@solana/spl-token@0.4');
        const mint = new web3.PublicKey(MINT);
        const dec = (CONFIG && CONFIG.decimals) || 6;
        const fromAta = await splt.getAssociatedTokenAddress(mint, owner);
        const toAta = await splt.getAssociatedTokenAddress(mint, reserve, true);
        tx.add(splt.createAssociatedTokenAccountIdempotentInstruction(owner, toAta, reserve, mint));
        tx.add(splt.createTransferCheckedInstruction(
          fromAta, mint, toAta, owner, BigInt(Math.round(cleanAmt * 10 ** dec)), dec,
        ));
      }
      tx.feePayer = owner;
      tx.recentBlockhash = (await conn.getLatestBlockhash()).blockhash;
      const ser = tx.serialize({ requireAllSignatures: false, verifySignatures: false });
      CleanWallet.signAndSendTransaction(CleanWallet.b58encode(new Uint8Array(ser)), { kind: 'mm' });
    } catch (e) {
      toast('Deposit build failed: ' + (e.message || e));
    } finally {
      SIGNING = false;
    }
  }

  // Read the active wallet's balances for the MM form: SOL live via RPC, $CLEAN
  // from the profile the server already read on-chain for this wallet.
  async function loadMmBalances() {
    if (!CONFIG || !CONFIG.mmEnabled) return;
    MM_BAL.solLoaded = false;
    MM_BAL.clean = (PROFILE && Number(PROFILE.balance)) || 0;
    const cb = $('bl-lp-clean-bal');
    if (cb) cb.textContent = 'Balance: ' + fmt(MM_BAL.clean);
    const pk = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    const sb = $('bl-lp-sol-bal');
    if (!pk) {
      if (sb) sb.textContent = '';
      return;
    }
    try {
      const web3 = await import('https://esm.sh/@solana/web3.js@1.95');
      const conn = new web3.Connection(safeRpc());
      const lamports = await conn.getBalance(new web3.PublicKey(pk));
      MM_BAL.sol = lamports / 1e9;
      MM_BAL.solLoaded = true;
      if (sb) sb.textContent = 'Balance: ' + MM_BAL.sol.toFixed(4) + ' SOL';
    } catch (e) {
      if (sb) sb.textContent = '';
    }
  }
  // "Max" fills each leg from the live balance: $CLEAN in full; SOL keeps a small
  // ~0.01 SOL cushion for the network fee.
  function wireMmMax() {
    const cm = $('bl-lp-clean-max');
    if (cm)
      cm.onclick = () => {
        const i = $('bl-lp-clean');
        if (i) i.value = MM_BAL.clean > 0 ? String(MM_BAL.clean) : '';
      };
    const sm = $('bl-lp-sol-max');
    if (sm)
      sm.onclick = () => {
        const i = $('bl-lp-sol');
        if (!i) return;
        const usable = Math.max(0, MM_BAL.sol - 0.01);
        i.value = usable > 0 ? usable.toFixed(4) : '';
      };
  }

  // ---- boot ------------------------------------------------------------- //
  async function boot() {
    try {
      CONFIG = await fetch('/api/economics').then((r) => r.json());
    } catch (e) {}
    CONFIG.botUsername = CONFIG.botUsername || '';
    MINT = CONFIG.mint || '';
    if (MINT) {
      const ca = $('ca-text');
      if (ca) ca.textContent = MINT;
    }

    if (CONFIG.inAppBurn) {
      const el = $('burn-inapp');
      if (el) el.classList.remove('hide');
    }
    // Reveal the Meme tab only when the (paid) generator is enabled server-side.
    if (CONFIG.memeEnabled) {
      const mt = document.querySelector('.tabbtn[data-tab="meme"]');
      if (mt) mt.classList.remove('hide');
    }
    wireBurnChips();
    wirePctRow();
    initBoostLab();
    wireMmMax();
    injectInvite();

    // Live wallet detection: extensions inject asynchronously on desktop, so
    // re-render the connect screen whenever a new wallet announces itself.
    CleanWallet.onDetect &&
      CleanWallet.onDetect(() => {
        if (!CONNECTING && $('connect') && !$('connect').classList.contains('hide'))
          renderWallets();
      });

    // Re-auth automatically when the user switches accounts inside their wallet.
    CleanWallet.onAccountChange && CleanWallet.onAccountChange(onWalletSwitch);
    if (CleanWallet.watchAccounts) {
      CleanWallet.watchAccounts();
      CleanWallet.onDetect && CleanWallet.onDetect(() => CleanWallet.watchAccounts());
    }

    // Resolve any wallet callback first (we just came back from a wallet app).
    const step = CleanWallet.init({
      onConnect: afterConnect,
      onSign: afterSign,
      onTx: function (s, c) {
        return c && c.kind === 'mm' ? afterMmTx(s, c) : afterBurnTx(s, c);
      },
      onError: (e) => {
        CONNECTING = false;
        toast('Wallet: ' + (e.message || e));
        showConnect();
        diag('wallet err: ' + (e.message || e));
      },
    });

    if (step) return; // a callback handler is driving the flow

    // Resume / recover an in-flight Telegram wallet handshake after a webview
    // relaunch. One poll: if it already completed server-side we log straight in
    // (the server recovers a lost sid via its per-user pointer); if it's still
    // in flight we keep polling; otherwise fall through to the normal connect.
    if (!TOKEN && freshInitData()) {
      try {
        const r = await api('/api/tg/poll', {
          initData: freshInitData(),
          sid: SS.getItem('clw_tgsid') || '',
        });
        if (r.status === 'done') {
          SS.removeItem('clw_tgsid');
          TOKEN = r.token;
          SS.setItem('clw_token', TOKEN);
          try {
            paint(r.profile);
          } catch (_) {}
          showApp();
          show('stake');
          return;
        }
        if (r.status === 'error' || SS.getItem('clw_tgsid')) {
          showConnect();
          if (r.status === 'error') {
            setNote('Sign-in problem: ' + (r.detail || 'try again'));
          } else {
            $('connect-spin').classList.remove('hide');
            setNote('Finishing sign-in…');
            pollTg(true);
          }
          return;
        }
      } catch (e) {
        /* not in Telegram, or transient — fall through to normal boot */
      }
    }

    // Returning user with a live token?
    if (TOKEN) {
      let p = null;
      try {
        p = await api('/api/profile', authedBody());
      } catch (e) {
        // session rejected or network down — only now is reconnect required
        TOKEN = '';
        SS.removeItem('clw_token');
      }
      if (p) {
        if (sessionStale(p)) {
          // stale token from a different (empty) wallet — re-auth as the live one
          // so the balances always match the connected account.
          diag('stale session — re-authing live wallet');
          await reauthLive();
          return;
        }
        try {
          paint(p);
        } catch (e) {
          // never swallow silently: a paint failure here is exactly how "in the
          // app but balances blank" used to happen with zero diagnostics.
          console.error('paint failed on restore:', e);
          diag('paint err: ' + ((e && e.message) || e));
        }
        showApp();
        show('stake');
        return;
      }
    }
    showConnect();
  }


  // ---- portfolio: all linked wallets, one dashboard ---------------------- //
  let LINKING = false;
  function renderFolio(r) {
    const t = r.totals || {};
    const priced = !!(PRICE && PRICE.available);
    const setUsd = (id, amt) => {
      const el = $(id);
      if (el) el.textContent = priced ? usd(amt || 0) : '';
    };
    $('f-holdings').textContent = fmt(t.holdings || 0);
    $('f-usd').textContent = priced ? usd(t.holdings || 0) : '';
    $('f-staked').textContent = fmt(t.staked || 0);
    $('f-pend').textContent = fmt(t.pending_rewards || 0);
    $('f-burned').textContent = fmt(t.total_burned || 0);
    setUsd('f-staked-usd', t.staked);
    setUsd('f-pend-usd', t.pending_rewards);
    setUsd('f-burned-usd', t.total_burned);

    const wallets = r.wallets || [];
    // Blended APR: stake-weighted across the cluster. With nothing staked yet
    // there is no weight, so show the best rate any wallet would earn instead.
    const totalStaked = wallets.reduce((s, w) => s + (Number(w.staked) || 0), 0);
    const blended =
      totalStaked > 0
        ? wallets.reduce((s, w) => s + (Number(w.apr_pct) || 0) * (Number(w.staked) || 0), 0) /
          totalStaked
        : wallets.reduce((m, w) => Math.max(m, Number(w.apr_pct) || 0), 0);
    const aprEl = $('f-apr');
    if (aprEl) aprEl.textContent = (Math.round(blended * 10) / 10).toLocaleString() + '%';

    const totalHoldings = Number(t.holdings) || 0;
    const box = $('folio-rows');
    box.innerHTML =
      `<div class="lbl folio-count">${r.count || 0} of ${r.limit || 20} wallets — linked forever until you remove them</div>` +
      wallets
        .map((w) => {
          const hold = (Number(w.balance) || 0) + (Number(w.pending_rewards) || 0);
          const share =
            totalHoldings > 0 ? Math.round((hold / totalHoldings) * 1000) / 10 : 0;
          // "Effective" stake = boost-adjusted base used for rewards; only worth
          // a tile when a burn has actually lifted it above the recorded stake.
          const boosted = (Number(w.staked_effective) || 0) > (Number(w.staked) || 0) + 1e-9;
          return `<div class="card fwal">
        <div class="fwal-top">
          <button class="fwal-addr" data-copy="${esc(w.wallet)}" title="Tap to copy full address">${esc(
            w.wallet.slice(0, 4) + '…' + w.wallet.slice(-4),
          )}<span class="fwal-copy">⧉</span></button>
          ${w.anchor ? '<span class="fpill">⚓ anchor</span>' : ''}
          ${w.me ? '<span class="fpill on">this session</span>' : ''}
          ${w.anchor ? '' : `<button class="funlink" data-unlink="${esc(w.wallet)}">✕ Remove</button>`}
        </div>
        <div class="fwal-share" title="${share}% of portfolio"><div class="fwal-share-bar" style="width:${Math.min(100, Math.max(share, 2))}%"></div></div>
        <div class="fwal-share-lbl">${share}% of portfolio · ${fmt(hold)} $CLEAN holdings</div>
        <div class="fwal-stats">
          <div><span class="fv">${fmt(w.staked)}</span><span class="fk">Staked</span></div>
          <div><span class="fv">${fmt(w.pending_rewards)}</span><span class="fk">Pending</span></div>
          <div><span class="fv">${Number(w.apr_pct) || 0}%</span><span class="fk">APR</span></div>
          <div><span class="fv">${fmt(w.total_burned)}</span><span class="fk">Burned</span></div>
          ${boosted ? `<div><span class="fv">${fmt(w.staked_effective)}</span><span class="fk">Effective</span></div>` : ''}
        </div>
        <div class="fwal-foot">
          <span class="fwal-usd">${priced ? '≈ ' + usd(hold) + ' holdings' : ''}</span>
          <button class="fwal-link" data-explore="${esc(w.wallet)}">Solscan ↗</button>
        </div>
      </div>`;
        })
        .join('');
    box.querySelectorAll('[data-unlink]').forEach((b) => {
      b.onclick = () => unlinkWallet(b.dataset.unlink);
    });
    box.querySelectorAll('[data-copy]').forEach((b) => {
      b.onclick = () => copyText(b.dataset.copy, 'Address copied ✦');
    });
    box.querySelectorAll('[data-explore]').forEach((b) => {
      b.onclick = () => openExt('https://solscan.io/account/' + b.dataset.explore);
    });
  }
  async function loadFolio() {
    try {
      renderFolio(await api('/api/portfolio', authedBody()));
    } catch (e) {
      toast('Portfolio: ' + (e.message || e));
    }
  }
  function portfolio() {
    hideWalletMenu();
    show('folio');
    loadFolio();
  }
  async function _finishLink(pk, sign) {
    if (PROFILE && pk === PROFILE.wallet)
      throw new Error(
        "that's your signed-in wallet — for another account of the SAME extension, keep this screen open and switch accounts inside the wallet; it links automatically",
      );
    const { nonce, message } = await getNonce(pk);
    const sig = await sign(message);
    const r = await api('/api/link', authedBody({ wallet: pk, signature: sig, nonce: nonce }));
    renderFolio(r);
    toast('🧤 Wallet linked');
  }
  function linkPicker() {
    const box = $('f-picker');
    if (!box) return;
    if (!box.classList.contains('hide')) {
      box.classList.add('hide');
      return;
    }
    const list = CleanWallet.detected();
    box.innerHTML = list.length
      ? '<div class="lbl" style="width:100%">Pick the wallet to add — approve in THAT extension:</div>' +
        list
          .map(
            (w) => `<button class="btn btn-ghost" data-linkid="${esc(w.id)}" style="padding:9px 14px">${esc(w.name)}</button>`,
          )
          .join('')
      : '<p class="lbl">No wallet extension detected here — use WalletConnect, or open the app inside the wallet\'s own browser.</p>';
    box.querySelectorAll('[data-linkid]').forEach((b) => {
      b.onclick = () => {
        box.classList.add('hide');
        linkWallet(b.dataset.linkid);
      };
    });
    box.classList.remove('hide');
  }
  async function linkWallet(id) {
    if (LINKING) return;
    LINKING = true;
    try {
      toast('Approve in the wallet you want to add…');
      const pk = await CleanWallet.connectInjected(id);
      await _finishLink(pk, (m) => CleanWallet.signInjected(m));
    } catch (e) {
      toast('Link failed: ' + (e.message || e));
    } finally {
      LINKING = false;
    }
  }
  async function linkWalletWC() {
    if (LINKING) return;
    if (!CONFIG.wcProjectId) return toast('WalletConnect is not configured');
    LINKING = true;
    try {
      const pk = await CleanWallet.wcConnect(CONFIG.wcProjectId);
      await _finishLink(pk, (m) => CleanWallet.wcSign(m));
    } catch (e) {
      toast('Link failed: ' + (e.message || e));
    } finally {
      LINKING = false;
    }
  }
  async function unlinkWallet(w) {
    try {
      renderFolio(await api('/api/unlink', authedBody({ wallet: w })));
      toast('Unlinked ' + w.slice(0, 4) + '…');
    } catch (e) {
      toast('Unlink failed: ' + (e.message || e));
    }
  }

  // ---- No Stains Bridge --------------------------------------------------- //
  // Two modes, chosen by the server (CONFIG.bridgeMode):
  //   "api"  — the real in-app swap form. The API key lives on OUR server; the
  //            browser only calls /api/bridge/*, so it's a true white-label.
  //   "link" — fallback when no exchange API key is set: a branded launch card
  //            (or inline iframe) that opens an external URL. Unchanged.
  let _bridgeLoaded = false; // link-mode one-shot guard
  let _bridgeAppBuilt = false; // api-mode build-once guard
  function loadBridge() {
    const host = $('bridge-host');
    const note = $('bridge-note');
    if (!host) return;
    if (CONFIG && CONFIG.bridgeMode === 'api') {
      buildBridgeApp(host, note);
      return;
    }
    if (_bridgeLoaded) return;
    loadBridgeLink(host, note);
  }

  function loadBridgeLink(host, note) {
    // Always resolve to a working destination — never a "coming soon" dead-end.
    // Defaults to the CLEAN EasyBit ref so the bridge works out of the box;
    // operators override with MINIAPP_BRIDGE_URL (or set EASYBIT_API_KEY for the
    // fully in-app white-label swap).
    const url = (CONFIG && CONFIG.bridgeUrl) || 'https://easybit.com/?ref_id=d4RqwQRDBs';
    const ok = /^https:\/\//.test(url);
    if (ok && CONFIG.bridgeEmbed) {
      const f = document.createElement('iframe');
      f.src = url;
      f.title = 'No Stains Bridge';
      f.loading = 'lazy';
      f.setAttribute('allow', 'clipboard-write; payment');
      f.setAttribute('referrerpolicy', 'no-referrer');
      host.innerHTML = '';
      host.appendChild(f);
      if (note)
        note.textContent =
          'Quotes and routing come from the exchange. Always confirm the destination address in your wallet.';
      _bridgeLoaded = true;
      return;
    }
    if (ok) {
      host.innerHTML =
        '<div class="bridge-empty">' +
        '<img class="glove" src="/glove.png" alt="">' +
        '<div class="t">No Stains Bridge</div>' +
        '<div class="d">Swap and bridge across chains — wallet-to-wallet, never through us. Opens the spotless exchange in a new tab.</div>' +
        '<button class="btn btn-solid" id="bridge-go" style="margin-top:6px;min-width:200px">🧤 Open No Stains Bridge ↗</button>' +
        '<div class="d" style="font-size:.78rem">Your funds stay in your wallet</div>' +
        '</div>';
      const go = $('bridge-go');
      if (go) go.onclick = () => openExt(url);
      if (note) note.textContent = '';
      _bridgeLoaded = true;
      return;
    }
  }

  // ---- No Stains Bridge — API mode (in-app swap) -------------------------- //
  const BR = {
    coins: [], // [{coin,name,networks:[{network,name}]}]
    quote: null,
    stage: 'quote', // quote -> confirm
    addrOk: null,
    addrChecking: false,
  };
  let _quoteT, _addrT, _bridgePoll;

  function stopBridgePoll() {
    if (_bridgePoll) {
      clearTimeout(_bridgePoll);
      _bridgePoll = null;
    }
  }

  // Same-origin GET helper that surfaces the server's error detail (api() is POST).
  async function bridgeGet(path) {
    const r = await fetch(path);
    if (!r.ok) {
      let d = r.status;
      try {
        d = (await r.json()).detail || d;
      } catch (e) {}
      throw new Error(d);
    }
    return r.json();
  }

  function bridgeCopy(t) {
    try {
      navigator.clipboard.writeText(t);
      toast('Copied');
    } catch (e) {
      toast('Copy failed');
    }
    haptic();
  }

  function buildBridgeApp(host, note) {
    if (note) note.textContent = '';
    if (_bridgeAppBuilt) return;
    _bridgeAppBuilt = true;
    const minUsd = (CONFIG && CONFIG.bridgeMinUsd) || 55;
    const feeUsd = (CONFIG && CONFIG.bridgeFeeUsd) || 5;
    host.innerHTML =
      '<div class="bridge-form">' +
      '  <div class="bridge-leg">' +
      '    <div class="top-l"><span>You send</span><span>min $' +
      esc(String(minUsd)) +
      '</span></div>' +
      '    <div class="lrow"><input class="amt" id="bf-amt" inputmode="decimal" placeholder="0.0" autocomplete="off" />' +
      '      <select class="coinsel" id="bf-send" aria-label="Send coin"></select></div>' +
      '    <div class="bridge-net"><select class="netsel" id="bf-send-net" aria-label="Send network"></select></div>' +
      '  </div>' +
      '  <div class="bridge-swap"><button id="bf-flip" title="Flip" aria-label="Flip direction">⇅</button></div>' +
      '  <div class="bridge-leg">' +
      '    <div class="top-l"><span>You get (estimated)</span><span id="bf-rate"></span></div>' +
      '    <div class="lrow"><input class="amt" id="bf-recv-amt" disabled placeholder="0.0" />' +
      '      <select class="coinsel" id="bf-recv" aria-label="Receive coin"></select></div>' +
      '    <div class="bridge-net"><select class="netsel" id="bf-recv-net" aria-label="Receive network"></select></div>' +
      '  </div>' +
      '  <div class="field" style="margin-top:12px">' +
      '    <div class="top-l"><span>Destination address</span><span id="bf-addr-chk"></span></div>' +
      '    <div class="input"><input id="bf-addr" placeholder="Where you receive the coins" autocomplete="off" spellcheck="false" /></div>' +
      '  </div>' +
      '  <div class="field"><div class="top-l"><span>Memo / Tag (only if your coin needs one)</span></div>' +
      '    <div class="input"><input id="bf-tag" placeholder="Optional" autocomplete="off" spellcheck="false" /></div></div>' +
      '  <div class="bridge-sum" id="bf-sum"></div>' +
      '  <div class="bridge-msg" id="bf-msg"></div>' +
      '  <button class="btn btn-solid" id="bf-go" disabled>Review swap</button>' +
      '  <div class="bridge-warn">A flat $' +
      esc(String(feeUsd)) +
      ' service fee is included in your quote. Always verify the deposit address shown in this app before sending — sending the wrong coin or network can mean permanent loss.</div>' +
      '</div>' +
      '<div id="bf-result"></div>';

    // Wire events (addEventListener, not inline handlers — CSP-clean).
    $('bf-amt').addEventListener('input', onBridgeInput);
    $('bf-addr').addEventListener('input', onAddrInput);
    $('bf-tag').addEventListener('input', () => setStage('quote'));
    $('bf-send').addEventListener('change', () => onCoinChange('send'));
    $('bf-recv').addEventListener('change', () => onCoinChange('recv'));
    $('bf-send-net').addEventListener('change', onBridgeInput);
    $('bf-recv-net').addEventListener('change', () => {
      BR.addrOk = null;
      onBridgeInput();
      checkAddr();
    });
    $('bf-flip').addEventListener('click', flipBridge);
    $('bf-go').addEventListener('click', onBridgePrimary);

    loadBridgeCoins();
  }

  async function loadBridgeCoins() {
    const msg = $('bf-msg');
    try {
      const r = await bridgeGet('/api/bridge/currencies');
      BR.coins = (r && r.currencies) || [];
    } catch (e) {
      if (msg) {
        msg.className = 'bridge-msg err';
        msg.textContent = 'Could not load coins: ' + e.message;
      }
      return;
    }
    if (!BR.coins.length) {
      if (msg) {
        msg.className = 'bridge-msg err';
        msg.textContent = 'No coins available right now — try again shortly.';
      }
      return;
    }
    fillCoinSelect($('bf-send'), 'USDC');
    fillCoinSelect($('bf-recv'), 'SOL');
    // Never default both legs to the same coin (tiny exchanges may lack USDC/SOL).
    if ($('bf-send').value === $('bf-recv').value && BR.coins.length > 1) {
      const alt = BR.coins.find((c) => c.coin !== $('bf-send').value);
      if (alt) $('bf-recv').value = alt.coin;
    }
    onCoinChange('send');
    onCoinChange('recv');
  }

  function fillCoinSelect(sel, prefer) {
    sel.innerHTML = '';
    let preferIdx = 0;
    BR.coins.forEach((c, i) => {
      const o = new Option(c.coin, c.coin);
      sel.add(o);
      if (c.coin === prefer) preferIdx = i;
    });
    sel.selectedIndex = preferIdx;
  }

  function coinByCode(code) {
    return BR.coins.find((c) => c.coin === code) || null;
  }

  function fillNetSelect(sel, networks) {
    sel.innerHTML = '';
    const nets = networks || [];
    if (!nets.length) {
      sel.style.visibility = 'hidden';
      return;
    }
    sel.style.visibility = 'visible';
    nets.forEach((n) => sel.add(new Option(n.name || n.network, n.network)));
  }

  function onCoinChange(side) {
    const c = coinByCode($(side === 'send' ? 'bf-send' : 'bf-recv').value);
    fillNetSelect($(side === 'send' ? 'bf-send-net' : 'bf-recv-net'), c && c.networks);
    if (side === 'recv') BR.addrOk = null;
    onBridgeInput();
    if (side === 'recv') checkAddr();
  }

  function setSelIfPresent(sel, val) {
    if (!val) return;
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === val) {
        sel.value = val;
        return;
      }
    }
  }

  function flipBridge() {
    const sCoin = $('bf-send').value,
      sNet = $('bf-send-net').value;
    const rCoin = $('bf-recv').value,
      rNet = $('bf-recv-net').value;
    $('bf-send').value = rCoin;
    $('bf-recv').value = sCoin;
    onCoinChange('send'); // refills bf-send-net for the new send coin
    onCoinChange('recv'); // refills bf-recv-net for the new receive coin
    // Carry each leg's chosen network across the flip when still offered.
    setSelIfPresent($('bf-send-net'), rNet);
    setSelIfPresent($('bf-recv-net'), sNet);
    onBridgeInput();
    haptic();
  }

  function bridgeFields() {
    return {
      send: $('bf-send').value,
      receive: $('bf-recv').value,
      sendNetwork: $('bf-send-net').value || '',
      receiveNetwork: $('bf-recv-net').value || '',
      amount: ($('bf-amt').value || '').trim(),
    };
  }

  function onBridgeInput() {
    setStage('quote');
    clearTimeout(_quoteT);
    _quoteT = setTimeout(doQuote, 400);
  }

  function onAddrInput() {
    setStage('quote');
    BR.addrOk = null;
    $('bf-addr-chk').textContent = '';
    clearTimeout(_addrT);
    _addrT = setTimeout(checkAddr, 600);
  }

  async function doQuote() {
    const f = bridgeFields();
    const sum = $('bf-sum'),
      msg = $('bf-msg'),
      recv = $('bf-recv-amt');
    if (!f.amount || Number(f.amount) <= 0) {
      sum.innerHTML = '';
      recv.value = '';
      msg.textContent = '';
      $('bf-rate').textContent = '';
      updatePrimary();
      return;
    }
    msg.className = 'bridge-msg';
    msg.textContent = 'Getting best rate…';
    try {
      const q = await api('/api/bridge/quote', f);
      BR.quote = q;
      recv.value = q.receiveAmount != null ? String(q.receiveAmount) : '';
      renderQuoteSummary(q);
      msg.textContent = '';
    } catch (e) {
      BR.quote = null;
      recv.value = '';
      sum.innerHTML = '';
      $('bf-rate').textContent = '';
      msg.className = 'bridge-msg err';
      msg.textContent = e.message || 'Could not get a quote.';
    }
    updatePrimary();
  }

  function renderQuoteSummary(q) {
    const rows = [];
    if (q.rate != null && isFinite(+q.rate))
      $('bf-rate').textContent = '1 ' + q.send + ' ≈ ' + (+q.rate).toPrecision(6) + ' ' + q.receive;
    if (q.feeUsd != null)
      rows.push(['Service fee', '$' + (+q.feeUsd).toFixed(2) + ' (' + q.feePct + '%)']);
    if (q.networkFee != null && q.networkFee !== '')
      rows.push(['Network fee', String(q.networkFee) + ' ' + q.receive]);
    if (q.sendUsd != null) rows.push(['Order value', '≈ $' + (+q.sendUsd).toFixed(2)]);
    if (q.min != null) rows.push(['Exchange min', String(q.min) + ' ' + q.send]);
    const sum = $('bf-sum');
    sum.innerHTML = '';
    rows.forEach(([name, v]) => {
      const row = document.createElement('div');
      row.className = 'row';
      const n = document.createElement('span');
      n.className = 'name';
      n.textContent = name;
      const val = document.createElement('span');
      val.className = 'v';
      val.textContent = v;
      row.appendChild(n);
      row.appendChild(val);
      sum.appendChild(row);
    });
  }

  async function checkAddr() {
    const addr = ($('bf-addr').value || '').trim();
    const chk = $('bf-addr-chk');
    if (!addr) {
      chk.textContent = '';
      BR.addrOk = null;
      BR.addrChecking = false;
      updatePrimary();
      return;
    }
    BR.addrChecking = true; // block confirm until this resolves
    updatePrimary();
    try {
      const r = await api('/api/bridge/validate-address', {
        currency: $('bf-recv').value,
        network: $('bf-recv-net').value || '',
        address: addr,
      });
      BR.addrOk = !!r.valid;
      chk.textContent = r.valid ? '✓ valid' : '✗ invalid';
      chk.style.color = r.valid ? '#1c7a3f' : '#b3261e';
    } catch (e) {
      BR.addrOk = null;
      chk.textContent = '';
    }
    BR.addrChecking = false;
    updatePrimary();
  }

  function setStage(stage) {
    BR.stage = stage;
    updatePrimary();
  }

  function updatePrimary() {
    const go = $('bf-go');
    if (!go) return;
    const addr = ($('bf-addr').value || '').trim();
    const ready = BR.quote && addr && BR.addrOk !== false && !BR.addrChecking;
    go.disabled = !ready;
    go.textContent = BR.stage === 'confirm' ? 'Confirm — get deposit address' : 'Review swap';
  }

  async function onBridgePrimary() {
    if (!BR.quote) return;
    if (BR.stage === 'quote') {
      // Validate the address before committing, if not already known-good.
      if (BR.addrOk == null) await checkAddr();
      if (BR.addrOk === false) {
        toast('Destination address looks invalid');
        return;
      }
      setStage('confirm');
      const msg = $('bf-msg');
      msg.className = 'bridge-msg';
      msg.textContent = 'Double-check the destination address, then confirm to get your deposit address.';
      return;
    }
    await createBridgeOrder();
  }

  async function createBridgeOrder() {
    const go = $('bf-go');
    const msg = $('bf-msg');
    const f = bridgeFields();
    f.receiveAddress = ($('bf-addr').value || '').trim();
    const tag = ($('bf-tag').value || '').trim();
    if (tag) f.receiveTag = tag;
    go.disabled = true;
    go.textContent = 'Opening order…';
    msg.className = 'bridge-msg';
    msg.textContent = '';
    try {
      const o = await api('/api/bridge/order', f);
      renderDeposit(o);
      haptic('medium');
    } catch (e) {
      msg.className = 'bridge-msg err';
      msg.textContent = e.message || 'Could not open the order.';
      setStage('confirm');
      go.disabled = false;
    }
  }

  function renderDeposit(o) {
    stopBridgePoll();
    const wrap = $('bf-result');
    const net = o.sendNetwork ? ' (' + esc(o.sendNetwork) + ')' : '';
    wrap.innerHTML =
      '<div class="deposit-box">' +
      '  <div class="row"><span class="name">Send exactly</span><span class="v" id="dep-amt"></span></div>' +
      '  <div class="top-l" style="margin-top:8px">Deposit address' +
      net +
      '</div>' +
      '  <div class="addr" id="dep-addr"></div>' +
      '  <button class="btn btn-ghost" id="dep-copy">Copy address</button>' +
      '  <div id="dep-tag-wrap" hidden><div class="top-l" style="margin-top:8px">Deposit memo/tag</div><div class="addr" id="dep-tag"></div></div>' +
      '  <div class="row" style="margin-top:8px"><span class="name">You receive</span><span class="v" id="dep-recv"></span></div>' +
      '  <div class="row"><span class="name">Order</span><span class="v" id="dep-id"></span></div>' +
      '  <div class="row"><span class="name">Status</span><span class="statusbadge" id="dep-status">…</span></div>' +
      '  <button class="btn btn-ghost" id="dep-new" style="margin-top:10px">Start another swap</button>' +
      '</div>';
    // External/dynamic values via textContent — never innerHTML (no injection).
    $('dep-amt').textContent = (o.sendAmount != null ? o.sendAmount : '') + ' ' + o.send;
    $('dep-addr').textContent = o.depositAddress || '';
    $('dep-recv').textContent = '~ ' + (o.receiveAmount != null ? o.receiveAmount : '') + ' ' + o.receive;
    $('dep-id').textContent = o.orderId || '';
    if (o.depositTag) {
      $('dep-tag-wrap').hidden = false;
      $('dep-tag').textContent = o.depositTag;
    }
    setStatusBadge($('dep-status'), o.status, o.phase);
    $('dep-copy').addEventListener('click', () => bridgeCopy(o.depositAddress || ''));
    $('dep-new').addEventListener('click', resetBridge);
    wrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (o.orderId) pollStatus(o.orderId);
  }

  function setStatusBadge(el, status, phase) {
    if (!el) return;
    el.textContent = status || 'pending';
    el.className = 'statusbadge' + (phase ? ' ' + phase : '');
  }

  function pollStatus(orderId) {
    stopBridgePoll();
    const tick = async () => {
      const el = $('dep-status');
      if (!el) return; // panel gone (user reset/left)
      try {
        const s = await bridgeGet('/api/bridge/order/' + encodeURIComponent(orderId));
        setStatusBadge(el, s.status, s.phase);
        if (s.phase === 'done' || s.phase === 'refund' || s.phase === 'failed') {
          if (s.phase === 'done') toast('Swap complete 🧤');
          return; // terminal — stop polling
        }
      } catch (e) {
        /* transient — keep polling */
      }
      _bridgePoll = setTimeout(tick, 9000);
    };
    _bridgePoll = setTimeout(tick, 5000);
  }

  function resetBridge() {
    stopBridgePoll();
    $('bf-result').innerHTML = '';
    $('bf-amt').value = '';
    $('bf-recv-amt').value = '';
    $('bf-addr').value = '';
    $('bf-tag').value = '';
    $('bf-addr-chk').textContent = '';
    $('bf-sum').innerHTML = '';
    $('bf-rate').textContent = '';
    $('bf-msg').textContent = '';
    BR.quote = null;
    BR.addrOk = null;
    setStage('quote');
  }
  global.App = {
    show,
    stake,
    unstake,
    claim,
    socialClaim,
    confirmPayout,
    submitBurn,
    invite,
    copyLink,
    copyCA,
    logout,
    walletMenu,
    portfolio,
    linkPicker,
    linkWallet,
    linkWalletWC,
    copyAddr,
    changeWallet,
    hideWalletMenu,
    refresh,
    buy,
    chart,
    birdeye,
    whitepaper,
    loadSwap,
    burnInApp,
    addLiquidity,
    genMeme,
    shareMeme,
    downloadMeme,
  };
  boot();
})(window);
