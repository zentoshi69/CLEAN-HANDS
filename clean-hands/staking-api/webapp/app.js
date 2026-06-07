/*
 * app.js — CLEAN soft-staking Mini App logic.
 * Ties the wallet deeplink flow (wallet.js) to the staking API (same origin).
 *
 * Login is a two-hop wallet round-trip:
 *   connect  -> (reload) onConnect -> GET /api/nonce -> signMessage
 *   sign     -> (reload) onSign    -> POST /api/login -> token -> app
 */
(function (global) {
  'use strict';
  const tg = global.Telegram && global.Telegram.WebApp;
  tg && tg.ready();
  tg && tg.expand();
  const initData = (tg && tg.initData) || '';
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
  const SS = global.sessionStorage;

  let TOKEN = SS.getItem('clw_token') || '';
  let PROFILE = null;
  let PRICE = null;
  let MINT = '';
  let _jupLoaded = false;

  // ---- helpers ---------------------------------------------------------- //
  const $ = (id) => document.getElementById(id);
  function toast(m) {
    const t = $('toast');
    t.textContent = m;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2200);
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
    ['stake', 'trade', 'boost', 'board', 'invite'].forEach((n) =>
      $('view-' + n).classList.toggle('hide', n !== v),
    );
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('sel', t.dataset.v === v));
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
    $('pending').textContent = fmt(p.pending_rewards);
    $('rank').textContent = '#' + fmt(p.rank);
    $('balance').textContent = fmt(p.balance) + ' $CLEAN';
    $('claimable').textContent = fmt(p.pending_rewards);
    $('burned').textContent = fmt(p.total_burned) + ' $CLEAN';
    $('ref-count').textContent = fmt(p.active_referrals);
    $('b-base').textContent = pct(a.base);
    $('b-amount').textContent = pct(a.amount_boost);
    $('b-loyalty').textContent = pct(a.loyalty_boost);
    $('b-ref').textContent = pct(a.referral_boost);
    $('b-burn').textContent = pct(a.burn_bonus_apr);
    const pk = CleanWallet.currentPubkey() || p.wallet || '';
    $('wallet-chip').textContent = pk ? pk.slice(0, 4) + '…' + pk.slice(-4) : '—';
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
  async function stake() {
    try {
      paint(await api('/api/stake', authedBody()));
      toast('Staked ✅');
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
      toast('Claimed ' + fmt(r.claimed) + ' $CLEAN');
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
    const pk = CleanWallet.currentPubkey();
    const bot = (CONFIG && CONFIG.botUsername) || 'YOUR_BOT';
    const short = (CONFIG && CONFIG.appShortName) || 'app';
    const url = `https://t.me/${bot}/${short}?startapp=${pk}`;
    const text = 'Stake $CLEAN with me — clean hands, dirty money 🧤';
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
    toast('Link copied');
  }
  function logout() {
    CleanWallet.disconnect();
    TOKEN = '';
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
    try {
      CONFIG = await fetch('/api/economics').then((r) => r.json());
    } catch (e) {}
    try {
      CONFIG.botUsername = CONFIG.botUsername || '';
    } catch (e) {}

    // Resolve any wallet callback first (we just came back from a wallet app).
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
