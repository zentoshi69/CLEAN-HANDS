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
  // Fetch a login nonce, surfacing real errors (429/400) instead of letting an
  // error body flow through and make us sign the literal string "undefined".
  async function getNonce(pubkey) {
    const r = await fetch('/api/nonce?wallet=' + encodeURIComponent(pubkey));
    if (!r.ok) {
      let d = r.status;
      try {
        d = (await r.json()).detail || d;
      } catch (e) {}
      throw new Error(d);
    }
    return r.json();
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
  function inviteLink() {
    const pk = CleanWallet.currentPubkey() || (PROFILE && PROFILE.wallet) || '';
    const bot = (CONFIG && CONFIG.botUsername) || 'YOUR_BOT';
    const short = (CONFIG && CONFIG.appShortName) || 'app';
    return `https://t.me/${bot}/${short}?startapp=${pk}`;
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
    ['stake', 'trade', 'boost', 'meme', 'board', 'invite', 'folio'].forEach((n) => {
      const el = $('view-' + n);
      if (el) el.hidden = n !== v;
    });
    document
      .querySelectorAll('.tabbtn')
      .forEach((b) => b.classList.toggle('on', b.dataset.tab === v));
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
    haptic();
    if (v === 'board') loadBoard();
    if (v === 'trade') loadPrice();
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

    // CASE 3 — Telegram mobile / mobile browser with no injected wallet. Re-open
    // the app INSIDE the wallet's browser, where its provider is injected and
    // connect works without a deeplink round-trip. WalletConnect covers every
    // other wallet app.
    CleanWallet.listWallets().forEach((w) => {
      addBtn(el, 'Open in ' + w.name, () => CleanWallet.openInWallet(w.id), true);
    });
    addWalletConnect(el);
    setNote('Tap your wallet — the app reopens inside it, then tap Connect Wallet.');
  }

  // ---- render profile --------------------------------------------------- //
  function paint(p) {
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
    $('burned').textContent = fmt(p.total_burned) + ' $CLEAN';
    $('ref-count').textContent = fmt(p.active_referrals);
    $('inv-earned').textContent = fmt(p.active_referrals);
    $('b-base').textContent = pct(a.base);
    $('b-amount').textContent = pct(a.amount_boost);
    $('b-loyalty').textContent = pct(a.loyalty_boost);
    $('b-ref').textContent = pct(a.referral_boost);
    $('b-burn').textContent = pct(a.burn_bonus_apr);
    $('inv-bonus').textContent = pct(a.referral_boost);
    // grey out boosters that are still zero (matches the design's .zero style)
    [
      ['b-amount', a.amount_boost],
      ['b-loyalty', a.loyalty_boost],
      ['b-ref', a.referral_boost],
      ['b-burn', a.burn_bonus_apr],
    ].forEach(([id, v]) => {
      const el = $(id);
      if (el) el.classList.toggle('zero', !Number(v));
    });
    // burn gauge
    $('boost-now').textContent = pct(a.burn_bonus_apr);
    const cap = Number((CONFIG && CONFIG.burn_cap_apr) || 2.0) || 2.0;
    $('boost-bar').style.width = Math.min(100, ((Number(a.burn_bonus_apr) || 0) / cap) * 100) + '%';
    const pk = CleanWallet.currentPubkey() || p.wallet || '';
    const chipAddr = $('wallet-chip-addr');
    if (chipAddr) chipAddr.textContent = pk ? pk.slice(0, 4) + '…' + pk.slice(-4) : '—';
    const chip = $('wallet-chip');
    if (chip) chip.classList.toggle('connected', !!pk);
    if (MINT) $('ca-text').textContent = MINT;
    $('ref-text').textContent = inviteLink().replace(/^https?:\/\//, '');
    updatePortfolio();
    startTicker();
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
      paint(await api('/api/profile', authedBody()));
    } catch (e) {
      if (String(e.message).indexOf('session') >= 0 || e.message === 401) {
        TOKEN = '';
        SS.removeItem('clw_token');
        showConnect('Session expired — reconnect your wallet.');
      } else toast(String(e.message));
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
      paint(await api('/api/stake', authedBody({ percent: STAKE_PCT })));
      haptic('medium');
      toast(STAKE_PCT === 100 ? 'Soft-staked ✦' : 'Soft-staked ' + STAKE_PCT + '% of your bag ✦');
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
      toast('Claimed ' + fmt(r.claimed) + ' $CLEAN ✦');
    } catch (e) {
      toast(String(e.message));
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
    const text = 'Wash your bags with $CLEAN — clean hands, dirty money 🧤';
    if (tg && tg.openTelegramLink)
      tg.openTelegramLink(
        `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`,
      );
    else copyLink();
  }
  function copyLink() {
    const url = inviteLink();
    navigator.clipboard && navigator.clipboard.writeText(url);
    haptic();
    toast('Invite link copied ✦');
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
      MEME_DATA = r.image;
      $('meme-img').src = r.image;
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
    openExt(location.origin + '/whitepaper');
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
    try {
      CONNECTING = true;
      $('connect-spin').classList.remove('hide');
      const pk = await CleanWallet.connectInjected(id);
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
    } catch (e) {
      CONNECTING = false;
      toast('Connect failed: ' + (e.message || e));
      showConnect();
      diag('connect err: ' + (e.message || e));
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
    if (_switching) return;
    _switching = true;
    // tear down the previous session right away
    TOKEN = '';
    SS.removeItem('clw_token');
    PROFILE = null;
    try {
      toast('Wallet switched — signing you in…');
      const { nonce, message } = await getNonce(newAddr);
      const sig = await CleanWallet.signInjected(message); // signs with the NEW account
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
      // signature declined / failed — fall back to the connect screen for the new wallet
      showConnect('Wallet changed — reconnect to continue.');
    } finally {
      _switching = false;
    }
  }

  // ---- WalletConnect login: QR/relay -> nonce -> sign -> login ----------- //
  async function loginWalletConnect() {
    try {
      CONNECTING = true;
      $('connect-spin').classList.remove('hide');
      toast('Opening WalletConnect…');
      const pk = await CleanWallet.wcConnect(CONFIG.wcProjectId);
      const { nonce, message } = await getNonce(pk);
      const sig = await CleanWallet.wcSign(message);
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
    toast('Building burn…');
    try {
      const web3 = await import('https://esm.sh/@solana/web3.js@1');
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

    // Live wallet detection: extensions inject asynchronously on desktop, so
    // re-render the connect screen whenever a new wallet announces itself.
    CleanWallet.onDetect &&
      CleanWallet.onDetect(() => {
        if (!CONNECTING && $('connect') && !$('connect').classList.contains('hide'))
          renderWallets();
      });

    // Re-auth automatically when the user switches accounts inside their wallet.
    CleanWallet.onAccountChange && CleanWallet.onAccountChange(onWalletSwitch);

    // Resolve any wallet callback first (we just came back from a wallet app).
    const step = CleanWallet.init({
      onConnect: afterConnect,
      onSign: afterSign,
      onTx: afterBurnTx,
      onError: (e) => {
        toast('Wallet: ' + (e.message || e));
        showConnect();
        diag('wallet err: ' + (e.message || e));
      },
    });

    if (step) return; // a callback handler is driving the flow

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
        try {
          paint(p);
        } catch (_) {}
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
    $('f-holdings').textContent = fmt(t.holdings || 0);
    $('f-usd').textContent = PRICE && PRICE.available ? usd(t.holdings || 0) : '';
    $('f-bal').textContent = fmt(t.balance || 0);
    $('f-staked').textContent = fmt(t.staked || 0);
    $('f-pend').textContent = fmt(t.pending_rewards || 0);
    const box = $('folio-rows');
    box.innerHTML = (r.wallets || [])
      .map(
        (w) => `<div class="card" style="display:flex;align-items:center;gap:10px;justify-content:space-between;flex-wrap:wrap">
        <div style="min-width:0">
          <div style="font-weight:700;color:var(--ink-deep,#0F3E73)">${esc(w.wallet.slice(0, 4) + '…' + w.wallet.slice(-4))}
            ${w.anchor ? '<span class="lbl"> · anchor</span>' : ''}${w.me ? '<span class="lbl"> · this session</span>' : ''}</div>
          <div class="lbl">bal ${fmt(w.balance)} · staked ${fmt(w.staked)} · pending ${fmt(w.pending_rewards)} · ${w.apr_pct}% APR</div>
        </div>
        ${w.anchor ? '' : `<button class="btn btn-ghost" data-unlink="${esc(w.wallet)}" style="padding:8px 12px">Unlink</button>`}
      </div>`,
      )
      .join('');
    box.querySelectorAll('[data-unlink]').forEach((b) => {
      b.onclick = () => unlinkWallet(b.dataset.unlink);
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
    if (PROFILE && pk === PROFILE.wallet) throw new Error('that is already your signed-in wallet');
    const { nonce, message } = await getNonce(pk);
    const sig = await sign(message);
    const r = await api('/api/link', authedBody({ wallet: pk, signature: sig, nonce: nonce }));
    renderFolio(r);
    toast('🧤 Wallet linked');
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

  global.App = {
    show,
    stake,
    unstake,
    claim,
    submitBurn,
    invite,
    copyLink,
    copyCA,
    logout,
    walletMenu,
    portfolio,
    linkWallet,
    linkWalletWC,
    copyAddr,
    changeWallet,
    refresh,
    buy,
    chart,
    birdeye,
    whitepaper,
    loadSwap,
    burnInApp,
    genMeme,
    shareMeme,
    downloadMeme,
  };
  boot();
})(window);
