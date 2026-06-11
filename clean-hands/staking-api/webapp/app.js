/*
 * app.js — CLEAN soft-staking Mini App logic.
 * Ties the wallet deeplink flow (wallet.js) to the staking API (same origin).
 *
 * Login is a two-hop wallet round-trip:
 *   connect  -> (reload) onConnect -> GET /api/nonce -> signMessage
 *   sign     -> (reload) onSign    -> POST /api/login -> token -> app
 *
 * Native polish layered on top (all feature-detected, degrade gracefully):
 *   - Telegram theme-matched chrome + haptics + BackButton + closing confirm
 *   - a real-time rewards ticker that accrues between server syncs
 *   - periodic background re-sync + double-submit guards on money actions
 */
(function (global) {
  'use strict';
  const tg = global.Telegram && global.Telegram.WebApp;
  const initData = (tg && tg.initData) || '';
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
  const SS = global.sessionStorage;
  const BRAND_BG = '#0d1320';
  const SECONDS_PER_YEAR = 365 * 24 * 3600;

  let TOKEN = SS.getItem('clw_token') || '';
  let PROFILE = null;
  let PRICE = null;
  let MINT = '';
  let _jupLoaded = false;
  let _pollTimer = null;

  // ---- helpers ---------------------------------------------------------- //
  const $ = (id) => document.getElementById(id);
  function toast(m) {
    const t = $('toast');
    t.textContent = m;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2200);
  }
  function haptic(kind) {
    try {
      const h = tg && tg.HapticFeedback;
      if (!h) return;
      if (kind === 'success' || kind === 'error' || kind === 'warning') h.notificationOccurred(kind);
      else h.impactOccurred(kind || 'light');
    } catch (e) {}
  }
  function fmt(n) {
    n = Number(n || 0);
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(2) + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  // Like fmt() but keeps fractional motion visible for the live ticker.
  function fmtLive(n) {
    n = Number(n || 0);
    if (n >= 1e6) return fmt(n);
    if (n >= 1e3) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (n >= 1) return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
    return n.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 6 });
  }
  function pct(x) {
    return '+' + Math.round(Number(x || 0) * 100) + '%';
  }
  function esc(s) {
    return String(s).replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
    );
  }
  async function api(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) {
      let detail = res.status;
      try {
        detail = (await res.json()).detail || detail;
      } catch (e) {}
      throw new Error(detail);
    }
    return res.json();
  }
  function authedBody(extra) {
    return Object.assign({ token: TOKEN }, extra || {});
  }
  // Prevent double-submits on money actions (also avoids racing the backend).
  async function withBusy(el, fn) {
    if (el && el.dataset.busy) return;
    if (el) {
      el.dataset.busy = '1';
      el.dataset.label = el.textContent;
      el.disabled = true;
      el.style.opacity = '0.6';
    }
    try {
      return await fn();
    } finally {
      if (el) {
        delete el.dataset.busy;
        el.disabled = false;
        el.style.opacity = '';
      }
    }
  }

  // ---- live rewards ticker ---------------------------------------------- //
  // Server returns pending_rewards at sync time; we interpolate forward using
  // the effective APR so the user watches yield accrue in real time. Each
  // refresh reseeds from server truth, so this never drifts unboundedly.
  const accrual = { base: 0, rate: 0, t0: 0 };
  let _raf = null;
  function seedAccrual(p) {
    const a = (p && p.apr) || {};
    const apr =
      a.effective_apr != null
        ? Number(a.effective_apr)
        : a.effective_apr_pct != null
          ? Number(a.effective_apr_pct) / 100
          : 0;
    const eff = Math.max(0, Math.min(Number((p && p.staked) || 0), Number((p && p.balance) || 0)));
    accrual.base = Number((p && p.pending_rewards) || 0);
    accrual.rate = apr > 0 && eff > 0 ? (eff * apr) / SECONDS_PER_YEAR : 0;
    accrual.t0 = performance.now();
    renderRateNote();
    if (accrual.rate > 0) startTicker();
    else stopTicker(true);
  }
  function livePending() {
    return accrual.base + accrual.rate * ((performance.now() - accrual.t0) / 1000);
  }
  function renderRateNote() {
    const el = $('rate-note');
    const dot = $('live-dot');
    if (!el) return;
    if (accrual.rate > 0) {
      el.textContent = '⚡ Earning ~' + fmtLive(accrual.rate * 86400) + ' $CLEAN/day, live';
      el.classList.remove('hide');
      if (dot) dot.classList.remove('hide');
    } else {
      el.classList.add('hide');
      if (dot) dot.classList.add('hide');
    }
  }
  function paintPending(v) {
    const s = fmtLive(v);
    if ($('pending')) $('pending').textContent = s;
    if ($('claimable')) $('claimable').textContent = s;
  }
  function startTicker() {
    if (_raf) return;
    let last = 0;
    const step = (ts) => {
      if (!PROFILE || !TOKEN || accrual.rate <= 0) {
        _raf = null;
        return;
      }
      if (ts - last > 200) {
        // ~5fps is plenty for a smooth count-up and saves battery
        paintPending(livePending());
        last = ts;
      }
      _raf = requestAnimationFrame(step);
    };
    _raf = requestAnimationFrame(step);
  }
  function stopTicker(freeze) {
    if (_raf) {
      cancelAnimationFrame(_raf);
      _raf = null;
    }
    if (freeze && PROFILE) paintPending(accrual.base);
  }

  // ---- Telegram-native chrome ------------------------------------------ //
  function applyChrome() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor && tg.setHeaderColor(BRAND_BG);
      tg.setBackgroundColor && tg.setBackgroundColor(BRAND_BG);
      if (tg.BackButton) {
        tg.BackButton.onClick(() => show('stake'));
      }
    } catch (e) {}
  }
  function syncBackButton(v) {
    try {
      if (!tg || !tg.BackButton) return;
      if (v && v !== 'stake') tg.BackButton.show();
      else tg.BackButton.hide();
    } catch (e) {}
  }
  function setClosingConfirm(on) {
    try {
      if (!tg) return;
      if (on) tg.enableClosingConfirmation && tg.enableClosingConfirmation();
      else tg.disableClosingConfirmation && tg.disableClosingConfirmation();
    } catch (e) {}
  }

  // ---- screens ---------------------------------------------------------- //
  function showApp() {
    $('connect').classList.add('hide');
    $('app').classList.remove('hide');
    setClosingConfirm(true);
    startPolling();
    loadPrice();
  }
  function showConnect(note) {
    $('app').classList.add('hide');
    $('connect').classList.remove('hide');
    $('connect-spin').classList.add('hide');
    stopTicker();
    stopPolling();
    setClosingConfirm(false);
    syncBackButton('stake');
    if (note) $('connect-note').textContent = note;
    renderWallets();
  }
  function show(v) {
    ['stake', 'trade', 'boost', 'board', 'invite'].forEach((n) =>
      $('view-' + n).classList.toggle('hide', n !== v),
    );
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('sel', t.dataset.v === v));
    syncBackButton(v);
    haptic('light');
    if (v === 'board') loadBoard();
    if (v === 'trade') loadPrice();
  }

  function renderWallets() {
    const el = $('wallet-list');
    el.innerHTML = '';
    CleanWallet.listWallets().forEach((w) => {
      const b = document.createElement('button');
      b.className = 'wallet-btn';
      b.textContent = 'Connect ' + w.name;
      b.onclick = () => {
        haptic('light');
        $('connect-spin').classList.remove('hide');
        try {
          CleanWallet.connect(w.id);
        } catch (e) {
          toast(String(e.message || e));
          $('connect-spin').classList.add('hide');
        }
      };
      el.appendChild(b);
    });
  }

  // ---- render profile --------------------------------------------------- //
  function paint(p) {
    PROFILE = p;
    const a = p.apr || {};
    $('apr').textContent =
      (a.effective_apr_pct != null
        ? a.effective_apr_pct
        : Math.round((a.effective_apr || 0) * 100)) + '%';
    $('staked').textContent = fmt(p.staked);
    $('rank').textContent = '#' + fmt(p.rank);
    $('balance').textContent = fmt(p.balance) + ' $CLEAN';
    $('burned').textContent = fmt(p.total_burned) + ' $CLEAN';
    $('ref-count').textContent = fmt(p.active_referrals);
    $('b-base').textContent = pct(a.base);
    $('b-amount').textContent = pct(a.amount_boost);
    $('b-loyalty').textContent = pct(a.loyalty_boost);
    $('b-ref').textContent = pct(a.referral_boost);
    $('b-burn').textContent = pct(a.burn_bonus_apr);
    const pk = CleanWallet.currentPubkey() || p.wallet || '';
    $('wallet-chip').textContent = pk ? pk.slice(0, 4) + '…' + pk.slice(-4) : '—';
    seedAccrual(p); // reseed the live ticker from server truth + paint pending now
    paintPending(accrual.base);
    updatePortfolio();
  }

  async function refresh() {
    try {
      paint(await api('/api/profile', authedBody()));
    } catch (e) {
      if (String(e.message).indexOf('session') >= 0 || e.message === 401) {
        TOKEN = '';
        SS.removeItem('clw_token');
        showConnect('Session expired — reconnect your wallet.');
      } else toast(String(e.message));
    }
  }

  // Keep the UI honest with the backend without hammering it.
  function startPolling() {
    if (_pollTimer) return;
    _pollTimer = setInterval(() => {
      if (TOKEN && PROFILE && !document.hidden) refresh();
    }, 30000);
  }
  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && TOKEN && PROFILE) refresh();
  });

  async function loadBoard() {
    try {
      const { leaderboard } = await api('/api/leaderboard', authedBody());
      const el = $('board-list');
      el.innerHTML = '';
      leaderboard.forEach((r) => {
        const d = document.createElement('div');
        d.className = 'lb-row' + (r.me ? ' me' : '');
        const medal = r.rank === 1 ? '🥇' : r.rank === 2 ? '🥈' : r.rank === 3 ? '🥉' : r.rank;
        d.innerHTML = `<div class="rank">${medal}</div><div class="name">${r.me ? 'You' : esc(r.name)}</div><div class="pts">${fmt(r.staked)}</div>`;
        el.appendChild(d);
      });
    } catch (e) {
      toast(String(e.message));
    }
  }

  // ---- actions ---------------------------------------------------------- //
  async function stake(el) {
    return withBusy(el, async () => {
      try {
        paint(await api('/api/stake', authedBody()));
        haptic('success');
        toast('Staked ✅');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  }
  async function unstake(el) {
    return withBusy(el, async () => {
      try {
        paint(await api('/api/unstake', authedBody()));
        haptic('warning');
        toast('Unstaked');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  }
  async function claim(el) {
    return withBusy(el, async () => {
      try {
        const r = await api('/api/claim', authedBody());
        paint(r.profile);
        haptic('success');
        toast('Claimed ' + fmt(r.claimed) + ' $CLEAN');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  }
  async function submitBurn(el) {
    const sig = $('burn-sig').value.trim();
    if (!sig) return toast('Paste your burn tx signature');
    return withBusy(el, async () => {
      try {
        const r = await api('/api/burn', authedBody({ signature: sig }));
        paint(r.profile);
        $('burn-sig').value = '';
        haptic('success');
        toast('🔥 Burn credited: ' + fmt(r.burned));
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  }
  function invite() {
    const pk = CleanWallet.currentPubkey();
    const bot = (CONFIG && CONFIG.botUsername) || 'YOUR_BOT';
    const short = (CONFIG && CONFIG.appShortName) || 'app';
    const url = `https://t.me/${bot}/${short}?startapp=${pk}`;
    const text = 'Stake $CLEAN with me — clean hands, dirty money 🧤';
    haptic('light');
    if (tg && tg.openTelegramLink)
      tg.openTelegramLink(
        `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`,
      );
    else App.copyLink();
  }
  function copyLink() {
    const pk = CleanWallet.currentPubkey();
    const bot = (CONFIG && CONFIG.botUsername) || 'YOUR_BOT';
    const short = (CONFIG && CONFIG.appShortName) || 'app';
    navigator.clipboard &&
      navigator.clipboard.writeText(`https://t.me/${bot}/${short}?startapp=${pk}`);
    haptic('success');
    toast('Link copied');
  }
  function logout() {
    CleanWallet.disconnect();
    TOKEN = '';
    PROFILE = null;
    SS.removeItem('clw_token');
    showConnect('Wallet disconnected.');
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
    } catch (e) {}
  }
  function updatePortfolio() {
    if (!PRICE || !PRICE.available || !PROFILE) return;
    $('m-portfolio').textContent =
      `Your stake ${usd(PROFILE.staked)} · pending ${usd(PROFILE.pending_rewards)} · wallet ${usd(PROFILE.balance)}`;
  }
  function openExt(u) {
    if (tg && tg.openLink) tg.openLink(u);
    else window.open(u, '_blank');
  }
  function buy() {
    if (MINT) openExt(`https://jup.ag/swap/SOL-${MINT}`);
  }
  function chart() {
    openExt(PRICE && PRICE.url ? PRICE.url : `https://dexscreener.com/solana/${MINT}`);
  }
  function birdeye() {
    if (MINT) openExt(`https://birdeye.so/token/${MINT}?chain=solana`);
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
          endpoint: (CONFIG && CONFIG.swapRpc) || 'https://api.mainnet-beta.solana.com',
          formProps: {
            initialInputMint: 'So11111111111111111111111111111111111111112',
            initialOutputMint: MINT,
          },
        });
        _jupLoaded = true;
      } catch (e) {
        toast('Swap widget unavailable — use “Buy on Jupiter”.');
      }
    };
    s.onerror = () => toast('Swap widget failed to load — use “Buy on Jupiter”.');
    document.body.appendChild(s);
  }

  // ---- login round-trip ------------------------------------------------- //
  async function afterConnect(pubkey) {
    try {
      $('connect-spin').classList.remove('hide');
      const { nonce, message } = await fetch(
        '/api/nonce?wallet=' + encodeURIComponent(pubkey),
      ).then((r) => r.json());
      // store nonce to use after the sign round-trip
      CleanWallet.signMessage(message, { nonce: nonce, wallet: pubkey });
    } catch (e) {
      toast('Login failed: ' + e.message);
      showConnect();
    }
  }
  async function afterSign(signatureB58, ctx) {
    try {
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
      haptic('success');
    } catch (e) {
      toast('Login failed: ' + e.message);
      showConnect('Login failed — try again.');
    }
  }

  // ---- in-app burn (beta, flag-gated; burn is irreversible) ------------- //
  async function afterBurnTx(sig, ctx) {
    try {
      const r = await api('/api/burn', authedBody({ signature: sig }));
      paint(r.profile);
      haptic('success');
      toast('🔥 Burn credited: ' + fmt(r.burned));
    } catch (e) {
      haptic('error');
      toast('Burn submit failed: ' + String(e.message));
    }
  }
  async function burnInApp() {
    const amt = parseFloat(($('burn-amount') || {}).value);
    if (!amt || amt <= 0) return toast('Enter an amount to burn');
    if (!MINT) return toast('Token not loaded yet');
    const pk = CleanWallet.currentPubkey();
    if (!pk) return toast('Connect your wallet first');
    // Burning is irreversible — confirm through the native dialog when available.
    const proceed = await new Promise((resolve) => {
      if (tg && tg.showConfirm)
        tg.showConfirm('Burn ' + amt + ' $CLEAN? This is permanent and cannot be undone.', resolve);
      else resolve(global.confirm('Burn ' + amt + ' $CLEAN? This is permanent.'));
    });
    if (!proceed) return;
    toast('Building burn…');
    try {
      const web3 = await import('https://esm.sh/@solana/web3.js@1');
      const splt = await import('https://esm.sh/@solana/spl-token@0.4');
      const conn = new web3.Connection(
        (CONFIG && CONFIG.swapRpc) || 'https://api.mainnet-beta.solana.com',
      );
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
    }
  }

  // ---- boot ------------------------------------------------------------- //
  let CONFIG = {};
  async function boot() {
    applyChrome();
    try {
      CONFIG = await fetch('/api/economics').then((r) => r.json());
    } catch (e) {}
    try {
      CONFIG.botUsername = CONFIG.botUsername || '';
    } catch (e) {}

    if (CONFIG.inAppBurn) {
      const el = $('burn-inapp');
      if (el) el.classList.remove('hide');
    }

    // Resolve any wallet callback first (we just came back from a wallet app).
    const step = CleanWallet.init({
      onConnect: afterConnect,
      onSign: afterSign,
      onTx: afterBurnTx,
      onError: (e) => {
        toast('Wallet: ' + (e.message || e));
        showConnect();
      },
    });

    if (step) return; // a callback handler is driving the flow

    // Returning user with a live token?
    if (TOKEN) {
      try {
        paint(await api('/api/profile', authedBody()));
        showApp();
        show('stake');
        return;
      } catch (e) {
        TOKEN = '';
        SS.removeItem('clw_token');
      }
    }
    showConnect();
  }

  global.App = {
    show,
    stake,
    unstake,
    claim,
    submitBurn,
    invite,
    copyLink,
    logout,
    refresh,
    buy,
    chart,
    birdeye,
    loadSwap,
    burnInApp,
  };
  boot();
})(window);
