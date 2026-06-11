/*
 * app.js — CLEAN soft-staking Mini App logic ("sky" design, matches the site).
 * Ties the wallet deeplink flow (wallet.js) to the staking API (same origin).
 *
 * Login is a two-hop wallet round-trip:
 *   connect  -> (reload) onConnect -> GET /api/nonce -> signMessage
 *   sign     -> (reload) onSign    -> POST /api/login -> token -> app
 *
 * Everything renders from server truth: /api/economics for the rules,
 * /api/profile for the wallet's numbers, /api/price for the market. The
 * pending-rewards ticker interpolates between syncs using the effective APR
 * and reseeds on every refresh, so it never drifts.
 */
(function (global) {
  'use strict';
  const tg = global.Telegram && global.Telegram.WebApp;
  const initData = (tg && tg.initData) || '';
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
  const SS = global.localStorage; // shared across tabs — survives wallet round-trips
  const BRAND_BG = '#F4FAFF';
  const SECONDS_PER_YEAR = 365 * 24 * 3600;

  let TOKEN = SS.getItem('clw_token') || '';
  let PROFILE = null;
  let PRICE = null;
  let MINT = '';
  let CONFIG = {};
  let _jupLoaded = false;
  let _pollTimer = null;

  // ---- helpers ---------------------------------------------------------- //
  const $ = (id) => document.getElementById(id);
  let _toastT;
  function toast(m) {
    const t = $('toast');
    t.textContent = m;
    t.classList.add('show');
    clearTimeout(_toastT);
    _toastT = setTimeout(() => t.classList.remove('show'), 2400);
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
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  // Like fmt() but keeps fractional motion visible for the live ticker.
  function fmtLive(n) {
    n = Number(n || 0);
    if (n >= 1e6) return fmt(n);
    if (n >= 1e3) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (n >= 1) return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
    return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 });
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
      el.disabled = true;
    }
    try {
      return await fn();
    } finally {
      if (el) {
        delete el.dataset.busy;
        el.disabled = false;
      }
    }
  }
  function openExt(u) {
    if (tg && tg.openLink) tg.openLink(u);
    else window.open(u, '_blank');
  }
  function copy(text, okMsg) {
    const done = () => toast(okMsg);
    if (navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(text).then(done, done);
    else done();
    haptic('light');
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
    const note = $('rateNote');
    const dot = $('liveDot');
    if (accrual.rate > 0) {
      note.textContent = '⚡ Earning ~' + fmtLive(accrual.rate * 86400) + ' $CLEAN/day, live';
      note.classList.remove('hide');
      dot.classList.remove('hide');
      startTicker();
    } else {
      note.classList.add('hide');
      dot.classList.add('hide');
      stopTicker(true);
    }
  }
  function paintPending(v) {
    const s = fmtLive(v);
    $('stPending').textContent = s;
    $('claimAmt').textContent = s;
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
        paintPending(accrual.base + accrual.rate * ((performance.now() - accrual.t0) / 1000));
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
  try {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.setHeaderColor && tg.setHeaderColor(BRAND_BG);
      tg.setBackgroundColor && tg.setBackgroundColor(BRAND_BG);
      tg.BackButton && tg.BackButton.onClick(() => showTab('stake'));
    }
  } catch (e) {}
  function setClosingConfirm(on) {
    try {
      if (!tg) return;
      if (on) tg.enableClosingConfirmation && tg.enableClosingConfirmation();
      else tg.disableClosingConfirmation && tg.disableClosingConfirmation();
    } catch (e) {}
  }
  function confirmNative(message) {
    return new Promise((resolve) => {
      if (tg && tg.showConfirm) tg.showConfirm(message, resolve);
      else resolve(global.confirm(message));
    });
  }

  // ---- wallet picker sheet ---------------------------------------------- //
  function openSheet() {
    const el = $('walletList');
    el.innerHTML = '';
    CleanWallet.listWallets().forEach((w) => {
      const b = document.createElement('button');
      b.className = 'btn btn-solid';
      b.textContent = 'Connect ' + w.name;
      b.onclick = () => {
        haptic('light');
        $('sheetSpin').classList.remove('hide');
        try {
          CleanWallet.connect(w.id);
        } catch (e) {
          toast(String(e.message || e));
          $('sheetSpin').classList.add('hide');
        }
      };
      el.appendChild(b);
    });
    $('sheetBg').classList.add('show');
    $('sheet').classList.add('show');
  }
  function closeSheet() {
    $('sheetBg').classList.remove('show');
    $('sheet').classList.remove('show');
    $('sheetSpin').classList.add('hide');
  }
  $('sheetBg').onclick = closeSheet;
  $('sheetCancel').onclick = closeSheet;

  function requireLogin() {
    if (TOKEN && PROFILE) return true;
    openSheet();
    return false;
  }

  // ---- render ------------------------------------------------------------ //
  function setBoost(id, frac) {
    const el = $(id);
    const pctV = Math.round(Number(frac || 0) * 100);
    el.textContent = '+' + pctV + '%';
    el.classList.toggle('zero', pctV === 0);
  }
  function paint(p) {
    PROFILE = p;
    const a = p.apr || {};
    $('aprNum').textContent =
      a.effective_apr_pct != null ? a.effective_apr_pct : Math.round((a.effective_apr || 0) * 100);
    $('stStaked').textContent = fmt(p.staked);
    $('stRank').textContent = p.rank != null ? fmt(p.rank) : '—';
    $('balTxt').textContent = fmt(p.balance) + ' $CLEAN';
    setBoost('brBase', a.base);
    $('brBase').classList.remove('zero');
    setBoost('brTier', a.amount_boost);
    setBoost('brLoyal', a.loyalty_boost);
    setBoost('brRef', a.referral_boost);
    setBoost('brBurn', a.burn_bonus_apr);
    // boost tab
    setBoost('boostNow', a.burn_bonus_apr);
    const cap = Number(CONFIG.burn_cap_apr || 0);
    $('boostBar').style.width =
      cap > 0 ? Math.min(100, (Number(a.burn_bonus_apr || 0) / cap) * 100) + '%' : '0%';
    $('burnedTxt').textContent = fmt(p.total_burned) + ' $CLEAN';
    // invite tab
    $('invCount').textContent = fmt(p.active_referrals);
    setBoost('invBonus', a.referral_boost);
    $('invBonus').classList.remove('zero');
    const pk = CleanWallet.currentPubkey() || p.wallet || '';
    $('wallet').textContent = pk ? pk.slice(0, 4) + '…' + pk.slice(-4) : 'Connect';
    if (pk && CONFIG.botUsername)
      $('refText').textContent =
        't.me/' + CONFIG.botUsername + '/' + (CONFIG.appShortName || 'app') + '?startapp=' + pk;
    seedAccrual(p);
    paintPending(accrual.base);
    updatePortfolio();
  }
  function resetToLoggedOut(note) {
    TOKEN = '';
    PROFILE = null;
    SS.removeItem('clw_token');
    stopTicker();
    stopPolling();
    setClosingConfirm(false);
    $('wallet').textContent = 'Connect';
    if (note) toast(note);
  }

  async function refresh() {
    try {
      paint(await api('/api/profile', authedBody()));
    } catch (e) {
      if (String(e.message).indexOf('session') >= 0 || e.message === 401) {
        resetToLoggedOut('Session expired — reconnect your wallet.');
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
    if (!TOKEN) return;
    try {
      const { leaderboard } = await api('/api/leaderboard', authedBody());
      $('lbList').innerHTML = leaderboard
        .map(
          (r) =>
            `<div class="lb${r.me ? ' me' : ''}"><div class="rk${r.rank === 1 ? ' top' : ''}">${r.rank}</div>` +
            `<div class="ad">${r.me ? 'You' : esc(r.name)}</div><div class="amt">${fmt(r.staked)}</div></div>`,
        )
        .join('');
      if (!leaderboard.length) $('lbList').innerHTML = '<p class="muted">No stakers yet — be first.</p>';
    } catch (e) {
      toast(String(e.message));
    }
  }

  // ---- market / trade ----------------------------------------------------- //
  function usd(n) {
    const v = (Number(n) || 0) * (PRICE ? PRICE.price_usd : 0);
    return '$' + (v >= 1 ? fmt(v) : v.toPrecision(2));
  }
  async function loadPrice() {
    try {
      PRICE = await (await fetch('/api/price')).json();
      MINT = PRICE.mint || MINT;
      if (MINT) $('caText').textContent = MINT;
      if (!PRICE.available) {
        $('mPrice').textContent = 'no data';
        return;
      }
      const p = PRICE.price_usd;
      $('mPrice').textContent = '$' + (p < 1 ? p.toPrecision(3) : fmt(p));
      const ch = PRICE.change_24h;
      $('mChange').textContent = ch != null ? (ch >= 0 ? '+' : '') + Number(ch).toFixed(1) + '%' : '—';
      $('mChange').style.color = ch != null && ch < 0 ? 'var(--fire)' : 'var(--pos)';
      $('mMc').textContent = PRICE.market_cap ? '$' + fmt(PRICE.market_cap) : '—';
      $('mLiq').textContent = PRICE.liquidity_usd ? '$' + fmt(PRICE.liquidity_usd) : '—';
      $('mVol').textContent = PRICE.volume_24h ? '$' + fmt(PRICE.volume_24h) : '—';
      updatePortfolio();
    } catch (e) {}
  }
  function updatePortfolio() {
    if (!PRICE || !PRICE.available || !PROFILE) return;
    $('mPortfolio').textContent =
      `Your stake ${usd(PROFILE.staked)} · pending ${usd(PROFILE.pending_rewards)} · wallet ${usd(PROFILE.balance)}`;
  }

  // ---- tabs --------------------------------------------------------------- //
  function showTab(t) {
    document.querySelectorAll('.tabbtn').forEach((x) => x.classList.toggle('on', x.dataset.tab === t));
    document.querySelectorAll('.tab').forEach((s) => (s.hidden = true));
    const sec = $('tab-' + t);
    sec.hidden = false;
    sec.classList.remove('fade');
    void sec.offsetWidth;
    sec.classList.add('fade');
    $('scroll').scrollTo({ top: 0, behavior: 'smooth' });
    try {
      if (tg && tg.BackButton) {
        if (t !== 'stake') tg.BackButton.show();
        else tg.BackButton.hide();
      }
    } catch (e) {}
    haptic('light');
    if (t === 'board') loadBoard();
    if (t === 'trade') loadPrice();
  }
  document.querySelectorAll('.tabbtn').forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

  // ---- actions ------------------------------------------------------------ //
  $('wallet').onclick = async () => {
    if (!TOKEN || !PROFILE) return openSheet();
    if (await confirmNative('Disconnect this wallet?')) {
      CleanWallet.disconnect();
      resetToLoggedOut('Wallet disconnected.');
    }
  };
  $('stakeBtn').onclick = function () {
    if (!requireLogin()) return;
    withBusy(this, async () => {
      try {
        paint(await api('/api/stake', authedBody()));
        haptic('success');
        toast('Soft-staked ' + fmt(PROFILE.staked) + ' $CLEAN ✦');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  $('unstakeBtn').onclick = function () {
    if (!requireLogin()) return;
    withBusy(this, async () => {
      try {
        paint(await api('/api/unstake', authedBody()));
        haptic('warning');
        toast('Unstaked — tokens were never locked anyway');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  $('claimBtn').onclick = function () {
    if (!requireLogin()) return;
    withBusy(this, async () => {
      try {
        const r = await api('/api/claim', authedBody());
        paint(r.profile);
        haptic('success');
        toast('Claimed ' + fmt(r.claimed) + ' $CLEAN ✦');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  $('burnVerifyBtn').onclick = function () {
    if (!requireLogin()) return;
    const sig = $('burnSig').value.trim();
    if (!sig) return toast('Paste your burn tx signature');
    withBusy(this, async () => {
      try {
        const r = await api('/api/burn', authedBody({ signature: sig }));
        paint(r.profile);
        $('burnSig').value = '';
        haptic('success');
        toast('Burned ' + fmt(r.burned) + ' 🔥 — bonus updated');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  document.querySelectorAll('.chip').forEach(
    (b) =>
      (b.onclick = () => {
        $('burnAmount').value = b.dataset.burn;
        haptic('light');
      }),
  );
  $('burnInAppBtn').onclick = async () => {
    if (!requireLogin()) return;
    const amt = parseFloat($('burnAmount').value);
    if (!amt || amt <= 0) return toast('Enter an amount to burn');
    if (!MINT) return toast('Token not loaded yet');
    const pk = CleanWallet.currentPubkey();
    if (!pk) return toast('Connect your wallet first');
    // Burning is irreversible — confirm through the native dialog when available.
    if (!(await confirmNative('Burn ' + amt + ' $CLEAN? This is permanent and cannot be undone.')))
      return;
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
      CleanWallet.signAndSendTransaction(
        CleanWallet.b58encode(new Uint8Array(ser)),
        { amount: amt },
        tx, // extension path signs the live Transaction object directly
      );
    } catch (e) {
      toast('Burn build failed: ' + (e.message || e));
    }
  };
  $('caChip').onclick = () => {
    if (MINT) copy(MINT, 'Contract copied ✦');
  };
  $('buyBtn').onclick = () => {
    if (MINT) openExt('https://pump.fun/coin/' + MINT);
  };
  $('chartBtn').onclick = () => {
    if (MINT) openExt(PRICE && PRICE.url ? PRICE.url : 'https://dexscreener.com/solana/' + MINT);
  };
  $('jupBtn').onclick = () => {
    if (MINT) openExt('https://jup.ag/swap/SOL-' + MINT);
  };
  $('swapWidgetBtn').onclick = () => {
    if (!MINT) return toast('Price still loading…');
    if (_jupLoaded) return toast('Swap widget already loaded below.');
    const s = document.createElement('script');
    s.src = 'https://plugin.jup.ag/plugin-v1.js';
    s.async = true;
    s.onload = () => {
      try {
        window.Jupiter.init({
          displayMode: 'integrated',
          integratedTargetId: 'jupTerminal',
          endpoint: (CONFIG && CONFIG.swapRpc) || 'https://api.mainnet-beta.solana.com',
          formProps: {
            initialInputMint: 'So11111111111111111111111111111111111111112',
            initialOutputMint: MINT,
          },
        });
        _jupLoaded = true;
      } catch (e) {
        toast('Swap widget unavailable — use Jupiter swap.');
      }
    };
    s.onerror = () => toast('Swap widget failed to load — use Jupiter swap.');
    document.body.appendChild(s);
  };
  function refLink() {
    const pk = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    return (
      'https://t.me/' +
      ((CONFIG && CONFIG.botUsername) || 'YOUR_BOT') +
      '/' +
      ((CONFIG && CONFIG.appShortName) || 'app') +
      '?startapp=' +
      pk
    );
  }
  $('refChip').onclick = () => {
    if (!requireLogin()) return;
    copy(refLink(), 'Invite link copied ✦');
  };
  $('shareBtn').onclick = () => {
    if (!requireLogin()) return;
    const url = refLink();
    const text = 'Wash your bags with $CLEAN 🧤✨ — stake with me';
    haptic('light');
    if (tg && tg.openTelegramLink)
      tg.openTelegramLink(
        'https://t.me/share/url?url=' + encodeURIComponent(url) + '&text=' + encodeURIComponent(text),
      );
    else copy(url, 'Invite link copied ✦');
  };

  // ---- login round-trip --------------------------------------------------- //
  async function afterConnect(pubkey) {
    try {
      toast('Wallet connected — confirm the signature 🧤');
      const { nonce, message } = await fetch(
        '/api/nonce?wallet=' + encodeURIComponent(pubkey),
      ).then((r) => r.json());
      CleanWallet.signMessage(message, { nonce: nonce, wallet: pubkey });
    } catch (e) {
      toast('Login failed: ' + e.message);
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
      closeSheet();
      paint(r.profile);
      setClosingConfirm(true);
      startPolling();
      haptic('success');
      toast('Gloves on — you are in 🧤✦');
    } catch (e) {
      toast('Login failed: ' + e.message);
    }
  }
  async function afterBurnTx(sig, ctx) {
    try {
      const r = await api('/api/burn', authedBody({ signature: sig }));
      paint(r.profile);
      haptic('success');
      toast('Burned ' + fmt(r.burned) + ' 🔥 — bonus updated');
    } catch (e) {
      haptic('error');
      toast('Burn submit failed: ' + String(e.message));
    }
  };

  // ---- boot ----------------------------------------------------------------- //
  function applyEconomicsText() {
    setBoost('invMax', CONFIG.referral_cap);
    $('invMax').classList.remove('zero');
    if (CONFIG.referral_per != null)
      $('invDesc').innerHTML =
        'Each friend who connects &amp; stakes from your link adds <b>+' +
        Math.round(CONFIG.referral_per * 100) +
        '%</b> to your APR (up to <b>+' +
        Math.round(CONFIG.referral_cap * 100) +
        '%</b>). <span class="scr">gloves multiply ✦</span>';
    if (CONFIG.burn_unit != null)
      $('burnDesc').innerHTML =
        'Burn $CLEAN to <b>permanently</b> raise your APR — <b>+' +
        Math.round(CONFIG.burn_apr_per_unit * 100) +
        '%</b> per <b>' +
        fmt(CONFIG.burn_unit) +
        '</b> burned, up to <b>+' +
        Math.round(CONFIG.burn_cap_apr * 100) +
        '%</b>. Burning shrinks supply for everyone.';
  }
  async function boot() {
    try {
      CONFIG = await fetch('/api/economics').then((r) => r.json());
    } catch (e) {
      CONFIG = {};
    }
    MINT = CONFIG.mint || '';
    if (MINT) $('caText').textContent = MINT;
    applyEconomicsText();
    if (CONFIG.inAppBurn) $('burnInApp').classList.remove('hide');
    loadPrice();

    // Resolve any wallet callback first (we just came back from a wallet app).
    const step = CleanWallet.init({
      onConnect: afterConnect,
      onSign: afterSign,
      onTx: afterBurnTx,
      onError: (e) => {
        toast('Wallet: ' + (e.message || e));
        closeSheet();
      },
    });
    if (step) return; // a callback handler is driving the flow

    // Returning user with a live token?
    if (TOKEN) {
      try {
        paint(await api('/api/profile', authedBody()));
        setClosingConfirm(true);
        startPolling();
        return;
      } catch (e) {
        TOKEN = '';
        SS.removeItem('clw_token');
      }
    }
  }
  boot();
})(window);
