/*
 * app.js — CLEAN soft-staking Mini App ("final clean" sky design).
 * Ties the wallet flows (wallet.js: extension / deeplink / Telegram relay)
 * to the staking API (same origin). Everything renders from server truth:
 * /api/economics for the rules, /api/profile for the wallet's numbers,
 * /api/price for the market, /api/stats for protocol-wide aggregates.
 *
 * Login (deeplink mode) is a two-hop wallet round-trip:
 *   connect -> onConnect -> GET /api/nonce -> signMessage
 *   sign    -> onSign    -> POST /api/login -> token -> app
 * Inside Telegram the hops resolve via the /api/relay poller (no reload).
 */
(function (global) {
  'use strict';
  const tg = global.Telegram && global.Telegram.WebApp;
  const initData = (tg && tg.initData) || '';
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
  const LS = global.localStorage; // shared across tabs — survives wallet round-trips
  const BRAND_BG = '#F4FAFF';
  const SECONDS_PER_YEAR = 365 * 24 * 3600;
  const reduce =
    global.matchMedia && matchMedia('(prefers-reduced-motion:reduce)').matches;

  let TOKEN = LS.getItem('clw_token') || '';
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
  function confirmNative(message) {
    return new Promise((resolve) => {
      if (tg && tg.showConfirm) tg.showConfirm(message, resolve);
      else resolve(global.confirm(message));
    });
  }

  // ---- motion: count-up, sparkle bursts, button spinners ----------------- //
  function countUp(el, to, opts) {
    const { dec = 0, suffix = '', prefix = '' } = opts || {};
    const fin = () => {
      el.textContent =
        prefix + Number(to).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) + suffix;
    };
    if (reduce) return fin();
    const from = parseFloat((el.textContent || '0').replace(/[^0-9.\-]/g, '')) || 0;
    const t0 = performance.now();
    (function f(t) {
      const k = Math.min(1, (t - t0) / 700);
      const e = 1 - Math.pow(1 - k, 3);
      const v = from + (to - from) * e;
      el.textContent =
        prefix + v.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) + suffix;
      if (k < 1) requestAnimationFrame(f);
      else fin();
    })(t0);
  }
  function burst(el, color) {
    if (reduce || !el) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    for (let i = 0; i < 9; i++) {
      const s = document.createElement('div');
      s.className = 'spark-burst';
      s.textContent = '✦';
      s.style.left = cx + 'px';
      s.style.top = cy + 'px';
      s.style.color = color || 'var(--sky)';
      s.style.fontSize = 8 + Math.random() * 12 + 'px';
      document.body.appendChild(s);
      const ang = Math.random() * 6.28;
      const dist = 40 + Math.random() * 55;
      const dx = Math.cos(ang) * dist;
      const dy = Math.sin(ang) * dist - 20;
      s.animate(
        [
          { transform: 'translate(-50%,-50%) translate(0,0) scale(.4)', opacity: 1 },
          { transform: `translate(-50%,-50%) translate(${dx}px,${dy}px) scale(1.1)`, opacity: 0 },
        ],
        { duration: 700 + Math.random() * 250, easing: 'cubic-bezier(.2,.7,.2,1)' },
      ).onfinish = () => s.remove();
    }
  }
  // Busy-guard + inline spinner; restores the button's original markup.
  async function withBusy(el, fn) {
    if (el && el.dataset.busy) return;
    let saved = null;
    if (el) {
      el.dataset.busy = '1';
      el.disabled = true;
      saved = el.innerHTML;
      el.innerHTML = '<span class="spin"></span>';
    }
    try {
      return await fn();
    } finally {
      if (el) {
        delete el.dataset.busy;
        el.disabled = false;
        if (saved !== null) el.innerHTML = saved;
      }
    }
  }

  // ---- ambient sparkles --------------------------------------------------- //
  if (!reduce) {
    [[8, 20], [90, 16], [14, 82], [88, 86], [50, 9], [72, 46]].forEach((p, i) => {
      const s = document.createElement('div');
      s.className = 'spark';
      s.textContent = '✦';
      s.style.left = p[0] + '%';
      s.style.top = p[1] + '%';
      s.style.fontSize = 0.8 + Math.random() * 1.4 + 'rem';
      s.style.animationDelay = i * 0.55 + 's';
      document.querySelector('.aura').appendChild(s);
    });
  }

  // ---- recent activity (client-side log, persisted per device) ----------- //
  function acts() {
    try {
      return JSON.parse(LS.getItem('clw_acts') || '[]');
    } catch (e) {
      return [];
    }
  }
  function relTime(ts) {
    const d = Math.max(0, Date.now() - ts) / 1000;
    if (d < 90) return 'just now';
    if (d < 3600) return Math.round(d / 60) + 'm ago';
    if (d < 86400) return Math.round(d / 3600) + 'h ago';
    return Math.round(d / 86400) + 'd ago';
  }
  function logAct(icon, text) {
    const a = acts();
    a.unshift({ icon, text, t: Date.now() });
    LS.setItem('clw_acts', JSON.stringify(a.slice(0, 6)));
    renderActs();
  }
  function renderActs() {
    const w = $('actList');
    const a = acts();
    if (!a.length) {
      w.innerHTML = '<div class="empty">No activity yet — stake to begin. 🧤</div>';
      return;
    }
    w.innerHTML = a
      .map(
        (x) =>
          `<div class="act"><div class="ic">${esc(x.icon)}</div><span>${esc(x.text)}</span><span class="tm">${relTime(x.t)}</span></div>`,
      )
      .join('');
  }

  // ---- live rewards ticker ------------------------------------------------ //
  // Server returns pending_rewards at sync time; we interpolate forward using
  // the effective APR and reseed on every refresh, so it never drifts.
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
    if (accrual.rate > 0) {
      note.innerHTML =
        '<span class="live-dot"></span>Earning ~' + fmtLive(accrual.rate * 86400) + ' $CLEAN/day, live';
      note.classList.remove('hide');
      startTicker();
    } else {
      note.classList.add('hide');
      stopTicker(true);
    }
  }
  function paintPending(v) {
    const s = fmtLive(v);
    $('stPending').textContent = s;
    const c = $('claimAmt');
    if (c) c.textContent = s;
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

  // ---- Telegram-native chrome --------------------------------------------- //
  try {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.setHeaderColor && tg.setHeaderColor(BRAND_BG);
      tg.setBackgroundColor && tg.setBackgroundColor(BRAND_BG);
      tg.BackButton &&
        tg.BackButton.onClick(() => {
          const b = document.querySelector('.tabbtn[data-tab="stake"]');
          b && b.click();
        });
    }
  } catch (e) {}
  function setClosingConfirm(on) {
    try {
      if (!tg) return;
      if (on) tg.enableClosingConfirmation && tg.enableClosingConfirmation();
      else tg.disableClosingConfirmation && tg.disableClosingConfirmation();
    } catch (e) {}
  }

  // ---- splash / welcome gate ---------------------------------------------- //
  function hideSplash() {
    const s = $('splash');
    s && s.classList.add('hide');
  }
  setTimeout(hideSplash, 2500); // fallback — boot() hides it sooner
  function showWelcome() {
    $('welcome').classList.remove('hide');
  }
  function closeWelcome() {
    $('welcome').classList.add('hide');
  }
  $('wcExplore').onclick = () => {
    closeWelcome();
    haptic('light');
  };
  $('wcConnect').onclick = () => {
    closeWelcome();
    openSheet();
  };

  // ---- wallet picker sheet -------------------------------------------------- //
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

  // ---- render ---------------------------------------------------------------- //
  function setBoost(id, frac) {
    const el = $(id);
    const pctV = Math.round(Number(frac || 0) * 100);
    el.textContent = '+' + pctV + '%';
    el.classList.toggle('zero', pctV === 0);
  }
  function paint(p) {
    PROFILE = p;
    const a = p.apr || {};
    const aprPct =
      a.effective_apr_pct != null ? a.effective_apr_pct : Math.round((a.effective_apr || 0) * 100);
    countUp($('aprNum'), aprPct);
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
    setBoost('refPct2', a.referral_boost);
    const rcap = Number(CONFIG.referral_cap || 0);
    $('refBar').style.width =
      rcap > 0 ? Math.min(100, (Number(a.referral_boost || 0) / rcap) * 100) + '%' : '0%';
    // header chip
    const pk = CleanWallet.currentPubkey() || p.wallet || '';
    $('walletTxt').textContent = pk ? pk.slice(0, 4) + '…' + pk.slice(-4) : 'Connect';
    $('wallet').classList.toggle('live', !!pk && !!TOKEN);
    if (pk)
      $('refText').textContent = p.ref_code
        ? location.host + '/g/' + p.ref_code
        : 't.me/' + (CONFIG.botUsername || '') + '/' + (CONFIG.appShortName || 'app') + '?startapp=' + pk;
    // claim vesting + payout + fee state — disclosed, never silent
    const lockNote = $('lockNote');
    const needsPayout = p.payout_setup_open && !p.payout_confirmed;
    $('payoutSetup').classList.toggle('hide', !needsPayout);
    if (p.claim_locked && p.claim_lock_days > 0) {
      $('claimBtn').disabled = true;
      lockNote.textContent =
        '🔒 Rewards unlock after ' +
        p.claim_lock_days +
        ' days of staking — ' +
        p.claim_unlock_in_days +
        'd to go. Unstaking resets pending rewards.';
      lockNote.classList.remove('hide');
    } else if (needsPayout) {
      $('claimBtn').disabled = true;
      lockNote.textContent = '💳 Confirm your payout wallet below to enable claiming.';
      lockNote.classList.remove('hide');
    } else if (p.claim_fee_usd > 0) {
      $('claimBtn').disabled = false;
      lockNote.textContent =
        '💳 A $' +
        p.claim_fee_usd +
        ' processing fee (in $CLEAN) is deducted from each claim.' +
        (p.payout_wallet && p.payout_wallet !== p.wallet
          ? ' Payout → ' + p.payout_wallet.slice(0, 4) + '…' + p.payout_wallet.slice(-4)
          : '');
      lockNote.classList.remove('hide');
    } else {
      $('claimBtn').disabled = false;
      lockNote.classList.add('hide');
    }
    seedAccrual(p);
    paintPending(accrual.base);
    updatePortfolio();
  }
  function resetToLoggedOut(note) {
    TOKEN = '';
    PROFILE = null;
    LS.removeItem('clw_token');
    stopTicker();
    stopPolling();
    setClosingConfirm(false);
    $('walletTxt').textContent = 'Connect';
    $('wallet').classList.remove('live');
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
      const medal = { 1: 'g', 2: 's', 3: 'b' };
      $('lbList').innerHTML = leaderboard
        .map(
          (r) =>
            `<div class="lb${r.me ? ' you' : ''}"><div class="rk ${medal[r.rank] || ''}">${r.rank}</div>` +
            `<div class="ad">${r.me ? 'You' : esc(r.name)}</div><div class="amt">${fmt(r.staked)}</div></div>`,
        )
        .join('');
      if (!leaderboard.length)
        $('lbList').innerHTML = '<div class="empty">No stakers yet — be first. 🧤</div>';
    } catch (e) {
      toast(String(e.message));
    }
  }

  // ---- protocol stats (Supply washed) -------------------------------------- //
  async function loadStats() {
    try {
      const s = await (await fetch('/api/stats')).json();
      countUp($('totBurn'), s.total_burned >= 1e6 ? +(s.total_burned / 1e6).toFixed(1) : Math.round(s.total_burned), {
        suffix: s.total_burned >= 1e6 ? 'M' : '',
        dec: s.total_burned >= 1e6 ? 1 : 0,
      });
      countUp($('totBurners'), s.burners || 0);
      if (s.burned_pct != null) countUp($('totPct'), s.burned_pct, { dec: 1, suffix: '%' });
      else $('totPctWrap').classList.add('hide');
      // season campaign card (server-driven; hidden when no season is active)
      if (s.season) {
        const se = s.season;
        $('seasonName').textContent = se.name;
        $('seasonDesc').innerHTML =
          'Wash <b>' +
          se.goal_pct +
          '%</b> of the supply together before the season ends. ' +
          'Every burn boosts <b>your</b> APR forever — every invite stacks more. <span class="scr">gloves on ✦</span>';
        $('seasonPct').textContent = se.progress_pct + '%';
        $('seasonBar').style.width = Math.min(100, se.progress_pct) + '%';
        $('seasonDays').textContent = se.days_left;
        $('seasonGoal').textContent = fmt(se.goal_tokens);
        $('seasonWashed').textContent = fmt(s.total_burned);
        $('seasonPanel').classList.remove('hide');
      } else {
        $('seasonPanel').classList.add('hide');
      }
    } catch (e) {
      $('supplyPanel').classList.add('hide');
    }
  }

  // ---- market / trade --------------------------------------------------------- //
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

  // ---- tabs ---------------------------------------------------------------------- //
  const TAB_ORDER = ['stake', 'trade', 'boost', 'board', 'invite'];
  function showTab(t) {
    document.querySelectorAll('.tabbtn').forEach((x) => x.classList.toggle('on', x.dataset.tab === t));
    $('tabbar').style.setProperty('--i', TAB_ORDER.indexOf(t));
    document.querySelectorAll('.tab').forEach((s) => {
      if (s.id === 'tab-' + t) {
        s.hidden = false;
        if (!reduce)
          [...s.children].forEach((c) => {
            c.style.animation = 'none';
            void c.offsetWidth;
            c.style.animation = '';
          });
      } else s.hidden = true;
    });
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
    if (t === 'boost') loadStats();
  }
  document.querySelectorAll('.tabbtn').forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

  // ---- actions ----------------------------------------------------------------------- //
  $('wallet').onclick = async () => {
    if (!TOKEN || !PROFILE) return openSheet();
    if (await confirmNative('Disconnect this wallet?')) {
      CleanWallet.disconnect();
      resetToLoggedOut('Wallet disconnected.');
    }
  };
  $('stakeBtn').onclick = function () {
    if (!requireLogin()) return;
    const self = this;
    withBusy(self, async () => {
      try {
        paint(await api('/api/stake', authedBody()));
        haptic('success');
        burst(self);
        toast('Soft-staked ' + fmt(PROFILE.staked) + ' $CLEAN ✦');
        logAct('🧤', 'Staked ' + fmt(PROFILE.staked) + ' $CLEAN');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  $('unstakeBtn').onclick = async function () {
    if (!requireLogin()) return;
    // Unstaking forfeits pending rewards — never let that happen silently.
    const pend = PROFILE ? Number(PROFILE.pending_rewards || 0) : 0;
    const warn =
      pend > 0
        ? 'Unstaking resets your pending rewards to 0 (' +
          fmtLive(pend) +
          ' $CLEAN forfeited) and restarts the unlock clock. Tokens stay in your wallet. Continue?'
        : 'Unstake? Your tokens stay in your wallet; the unlock clock resets.';
    if (!(await confirmNative(warn))) return;
    withBusy(this, async () => {
      try {
        const was = PROFILE ? PROFILE.staked : 0;
        paint(await api('/api/unstake', authedBody()));
        haptic('warning');
        toast(pend > 0 ? 'Unstaked — pending rewards forfeited' : 'Unstaked');
        logAct('↩️', 'Unstaked ' + fmt(was) + ' $CLEAN');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  $('claimBtn').onclick = function () {
    if (!requireLogin()) return;
    const self = this;
    withBusy(self, async () => {
      try {
        const r = await api('/api/claim', authedBody());
        paint(r.profile);
        haptic('success');
        burst(self);
        toast(
          'Claimed ' +
            fmt(r.claimed) +
            ' $CLEAN ✦' +
            (r.fee > 0 ? ' ($' + r.fee_usd + ' fee deducted)' : ''),
        );
        logAct('💧', 'Claimed ' + fmt(r.claimed) + ' $CLEAN');
      } catch (e) {
        haptic('error');
        toast(String(e.message));
      }
    });
  };
  $('payoutBtn').onclick = async function () {
    if (!requireLogin()) return;
    const addr = $('payoutAddr').value.trim();
    const dest = addr || CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    const short = dest ? dest.slice(0, 4) + '…' + dest.slice(-4) : 'this wallet';
    if (!(await confirmNative('Send all claim payouts to ' + short + '?'))) return;
    const self = this;
    withBusy(self, async () => {
      try {
        paint(await api('/api/payout', authedBody({ address: addr || null })));
        haptic('success');
        burst(self);
        toast('Payout wallet confirmed ✦');
        logAct('💳', 'Payout wallet set');
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
    const self = this;
    withBusy(self, async () => {
      try {
        const r = await api('/api/burn', authedBody({ signature: sig }));
        paint(r.profile);
        $('burnSig').value = '';
        haptic('success');
        burst(self, 'var(--fire)');
        toast('Burned ' + fmt(r.burned) + ' 🔥 — bonus updated');
        logAct('🔥', 'Burned ' + fmt(r.burned) + ' $CLEAN');
        loadStats();
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
    const code = PROFILE && PROFILE.ref_code;
    if (code) return location.origin + '/g/' + code; // unfurls with the banner
    const id = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    return (
      'https://t.me/' +
      ((CONFIG && CONFIG.botUsername) || 'YOUR_BOT') +
      '/' +
      ((CONFIG && CONFIG.appShortName) || 'app') +
      '?startapp=' +
      id
    );
  }
  $('refChip').onclick = () => {
    if (!requireLogin()) return;
    copy(refLink(), 'Invite link copied ✦');
  };
  $('shareBtn').onclick = () => {
    if (!requireLogin()) return;
    const url = refLink();
    const code = (PROFILE && PROFILE.ref_code) || '';
    const text =
      '🧤 Wash your bags with $CLEAN\n' +
      'Soft staking — tokens never leave your wallet. Burn to boost, invite to multiply.' +
      (code ? '\nGlove code: ' + code : '');
    haptic('light');
    if (tg && tg.openTelegramLink)
      tg.openTelegramLink(
        'https://t.me/share/url?url=' + encodeURIComponent(url) + '&text=' + encodeURIComponent(text),
      );
    else copy(url, 'Invite link copied ✦');
  };

  // ---- login round-trip ------------------------------------------------------------ //
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
      LS.setItem('clw_token', TOKEN);
      closeSheet();
      closeWelcome();
      paint(r.profile);
      setClosingConfirm(true);
      startPolling();
      haptic('success');
      burst($('wallet'));
      toast('Gloves on — you are in 🧤✦');
      logAct('🧤', 'Wallet connected');
    } catch (e) {
      toast('Login failed: ' + e.message);
    }
  }
  // Telegram server-side handshake delivered a ready session + profile.
  function onSession(token, profile) {
    TOKEN = token;
    LS.setItem('clw_token', TOKEN);
    closeSheet();
    closeWelcome();
    paint(profile);
    setClosingConfirm(true);
    startPolling();
    haptic('success');
    burst($('wallet'));
    toast('Gloves on — you are in 🧤✦');
    logAct('🧤', 'Wallet connected');
  }

  async function afterBurnTx(sig, ctx) {
    try {
      const r = await api('/api/burn', authedBody({ signature: sig }));
      paint(r.profile);
      haptic('success');
      toast('Burned ' + fmt(r.burned) + ' 🔥 — bonus updated');
      logAct('🔥', 'Burned ' + fmt(r.burned) + ' $CLEAN');
      loadStats();
    } catch (e) {
      haptic('error');
      toast('Burn submit failed: ' + String(e.message));
    }
  }

  // ---- boot ----------------------------------------------------------------------------- //
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
    if (CONFIG.burn_unit != null) {
      $('burnDesc').innerHTML =
        'Burn $CLEAN to <b>permanently</b> raise your APR — <b>+' +
        Math.round(CONFIG.burn_apr_per_unit * 100) +
        '%</b> per <b>' +
        fmt(CONFIG.burn_unit) +
        '</b> burned, up to <b>+' +
        Math.round(CONFIG.burn_cap_apr * 100) +
        '%</b>. Burning shrinks supply for everyone.';
      $('featBurn').textContent =
        'Burn $CLEAN to lift your APR — up to +' + Math.round(CONFIG.burn_cap_apr * 100) + '%, forever.';
    }
  }
  async function boot() {
    renderActs();
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
    loadStats();

    // Resolve any wallet callback / pending handshake first.
    const step = CleanWallet.init({
      onConnect: afterConnect,
      onSign: afterSign,
      onTx: afterBurnTx,
      onSession: onSession, // Telegram server-side handshake completed
      onError: (e) => {
        toast('Wallet: ' + (e.message || e));
        closeSheet();
        if (!TOKEN) showWelcome();
      },
    });
    if (step) {
      hideSplash();
      // A poller is finishing a sign-in started before a webview relaunch.
      if (step === 'resume' && !TOKEN) toast('Finishing sign-in… 🧤');
      return;
    }

    // Returning user with a live token?
    if (TOKEN) {
      try {
        paint(await api('/api/profile', authedBody()));
        setClosingConfirm(true);
        startPolling();
        hideSplash();
        return;
      } catch (e) {
        TOKEN = '';
        LS.removeItem('clw_token');
      }
    }
    // Cold relaunch mid-handshake (storage wiped)? Recover from the server.
    if (CleanWallet.checkTgSession && (await CleanWallet.checkTgSession())) {
      hideSplash();
      return;
    }
    hideSplash();
    showWelcome();
  }
  boot();
})(window);
