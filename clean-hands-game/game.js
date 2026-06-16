/* ======================================================================
   Clean Hands, Dirty Money — game engine (vanilla JS, CSP-safe)
   A tap + idle launderer: launder cash, buy fronts, dodge HEAT, bribe the
   law, build REP, and ESCAPE to a richer city for a permanent multiplier.
   Progress saves to localStorage and (if a backend is reachable) syncs.
   ====================================================================== */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);

  // -------------------------------------------------------------- content
  const UPGRADES = [
    { id: 'hands', name: 'Extra Hands', ico: '🧤', base: 50, inc: 1 },
    { id: 'mat', name: 'Laundromat', ico: '🧺', base: 600, inc: 9 },
    { id: 'wash', name: 'Car Wash', ico: '🚿', base: 5200, inc: 55 },
    { id: 'casino', name: 'Casino Floor', ico: '🎰', base: 42000, inc: 320 },
    { id: 'shell', name: 'Shell Company', ico: '🏢', base: 300000, inc: 1800 },
    { id: 'mixer', name: 'Crypto Mixer', ico: '🌀', base: 2.2e6, inc: 10500 },
    { id: 'bank', name: 'Private Bank', ico: '🏦', base: 1.5e7, inc: 62000 },
    { id: 'cartel', name: 'Cartel Route', ico: '🛥️', base: 1.1e8, inc: 380000 },
    { id: 'state', name: 'Captured State', ico: '🏛️', base: 9e8, inc: 2.4e6 },
  ];
  const COST_GROWTH = 1.15;

  const PERKS = [
    { id: 'nitrile', name: 'Nitrile Pro', ico: '💎', cost: 8000, desc: '×2 per-tap launder' },
    { id: 'spin', name: 'Spin Cycle', ico: '⚡', cost: 90000, desc: '×2 idle income' },
    { id: 'cold', name: 'Cold Wash', ico: '❄️', cost: 600000, desc: 'Heat builds 50% slower' },
  ];

  const BRIBES = [
    { id: 'cop', name: 'Local Cop', ico: '👮', base: 1500, cut: 25, desc: 'Look the other way' },
    { id: 'mayor', name: 'The Mayor', ico: '🎩', base: 18000, cut: 55, desc: 'A friend in city hall' },
    { id: 'judge', name: 'A Judge', ico: '⚖️', base: 140000, cut: 100, desc: 'Make the case vanish' },
  ];

  const REP = [
    { id: 'first', name: 'First Wash', ico: '🫧', desc: 'Launder your first $1,000', test: (s) => s.lifetime >= 1000 },
    { id: 'busy', name: 'Busy Hands', ico: '👏', desc: 'Tap 500 times', test: (s) => s.taps >= 500 },
    { id: 'team', name: 'Full Crew', ico: '🧤', desc: 'Own 10× Extra Hands', test: (s) => (s.ups.hands || 0) >= 10 },
    { id: 'mil', name: 'Millionaire', ico: '💰', desc: 'Reach $1,000,000', test: (s) => s.lifetime >= 1e6 },
    { id: 'ice', name: 'Ice Cold', ico: '🧊', desc: 'Buy the Cold Wash perk', test: (s) => !!s.perks.cold },
    { id: 'fixer', name: 'The Fixer', ico: '🤝', desc: 'Pay 5 bribes', test: (s) => s.bribes >= 5 },
    { id: 'jet', name: 'Jet Set', ico: '✈️', desc: 'Escape to a new city', test: (s) => s.prestige >= 1 },
    { id: 'whale', name: 'Dirty Whale', ico: '🐋', desc: 'Reach $1,000,000,000', test: (s) => s.lifetime >= 1e9 },
  ];

  const CITIES = ['Dubai', 'Monaco', 'Singapore', 'Zurich', 'Cayman Is.', 'Macau', 'Geneva', 'Hong Kong'];

  // tunables
  const TAP_HEAT = 2.2;
  const HEAT_DECAY = 3.0; // per second
  const BASE_TAP = 5;
  const TAP_FRACTION = 0.1; // taps also pay a slice of your $/s
  const BUST_LOSS = 0.18;
  const OFFLINE_RATE = 0.5;
  const OFFLINE_CAP = 8 * 3600; // 8h

  // -------------------------------------------------------------- state
  const DEFAULT = () => ({
    cash: 0,
    lifetime: 0,
    taps: 0,
    cityIdx: 0,
    heat: 0,
    ups: {},
    perks: {},
    rep: {},
    bribes: 0,
    prestige: 0,
    lastSeen: Date.now(),
  });
  let state = DEFAULT();
  let bustedUntil = 0;
  let player = '';

  const SAVE_KEY = 'chdm_save_v1';
  function loadLocal() {
    try {
      const raw = localStorage.getItem(SAVE_KEY);
      if (raw) state = Object.assign(DEFAULT(), JSON.parse(raw));
    } catch (_) {}
    try {
      player = localStorage.getItem('chdm_player') || '';
      if (!player) {
        player = (crypto.randomUUID && crypto.randomUUID()) || 'p' + Date.now() + Math.random().toString(36).slice(2);
        localStorage.setItem('chdm_player', player);
      }
    } catch (_) {}
  }
  let _saveT = 0;
  function saveLocal() {
    state.lastSeen = Date.now();
    try {
      localStorage.setItem(SAVE_KEY, JSON.stringify(state));
    } catch (_) {}
  }
  function saveSoon() {
    const now = Date.now();
    if (now - _saveT < 1500) return;
    _saveT = now;
    saveLocal();
    syncUp();
  }

  // -------------------------------------------------------------- backend (optional)
  const API = ''; // same origin; standalone server.py serves it
  async function syncDown() {
    try {
      const r = await fetch(API + '/api/load', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ player }),
      });
      if (!r.ok) return;
      const d = await r.json();
      if (d && d.state) {
        const remote = JSON.parse(d.state);
        // take whichever was touched most recently
        if ((remote.lastSeen || 0) > (state.lastSeen || 0)) state = Object.assign(DEFAULT(), remote);
      }
    } catch (_) {}
  }
  async function syncUp() {
    try {
      await fetch(API + '/api/save', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ player, state: JSON.stringify(state), score: Math.floor(state.lifetime) }),
      });
    } catch (_) {}
  }
  async function loadBoard() {
    try {
      const r = await fetch(API + '/api/leaderboard');
      if (!r.ok) return [];
      return (await r.json()).top || [];
    } catch (_) {
      return [];
    }
  }

  // -------------------------------------------------------------- math/derived
  const upCost = (u) => Math.ceil(u.base * Math.pow(COST_GROWTH, state.ups[u.id] || 0));
  const bribeCost = (b) => Math.ceil(b.base * Math.pow(1.22, state.bribes));
  const prestigeMult = () => 1 + 0.75 * state.prestige;
  const idleMult = () => (state.perks.spin ? 2 : 1) * prestigeMult();
  const tapMult = () => (state.perks.nitrile ? 2 : 1) * prestigeMult();
  function perSec() {
    let s = 0;
    for (const u of UPGRADES) s += (state.ups[u.id] || 0) * u.inc;
    return s * idleMult();
  }
  const perTap = () => (BASE_TAP + perSec() * TAP_FRACTION) * tapMult();
  const coolFactor = () => (state.perks.cold ? 0.5 : 1);
  const level = () => Math.max(1, Math.floor(Math.log10(Math.max(10, state.lifetime))) - 1);
  const escapeGoal = () => 250000 * Math.pow(15, state.cityIdx);
  const perksOwned = () => PERKS.filter((p) => state.perks[p.id]).length;
  const repOwned = () => REP.filter((r) => state.rep[r.id]).length;

  // pick the upgrade with the best value you can afford, else the cheapest target
  function featured() {
    const aff = UPGRADES.filter((u) => upCost(u) <= state.cash);
    if (aff.length) return aff.reduce((a, b) => (b.inc / upCost(b) > a.inc / upCost(a) ? b : a));
    return UPGRADES.slice().sort((a, b) => upCost(a) - upCost(b))[0];
  }

  // -------------------------------------------------------------- format
  function abbr(n) {
    n = Math.floor(n);
    if (n < 1000) return '' + n;
    const u = ['', 'K', 'M', 'B', 'T', 'Qa', 'Qi', 'Sx'];
    let i = 0,
      x = n;
    while (x >= 1000 && i < u.length - 1) {
      x /= 1000;
      i++;
    }
    return (x < 10 ? x.toFixed(2) : x < 100 ? x.toFixed(1) : Math.floor(x)) + u[i];
  }
  const money = (n) => (n < 1e9 ? '$' + Math.floor(n).toLocaleString('en-US') : '$' + abbr(n));
  const moneyShort = (n) => '$' + abbr(n);

  // -------------------------------------------------------------- UI helpers
  let toastT;
  function toast(msg) {
    const t = $('ui-toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastT);
    toastT = setTimeout(() => t.classList.remove('show'), 1900);
  }
  function haptic(ms) {
    try {
      navigator.vibrate && navigator.vibrate(ms || 8);
    } catch (_) {}
  }

  // -------------------------------------------------------------- render
  let curTab = 'launder';
  function render() {
    const ps = perSec();
    $('ui-level').textContent = level();
    $('ui-loc').textContent = (CITIES[state.cityIdx] || 'Offshore').toUpperCase();
    $('ui-goal').textContent = abbr(escapeGoal());
    $('ui-cash').textContent = money(state.cash);
    $('ui-rate').textContent = moneyShort(ps) + '/s';

    const hp = Math.round(state.heat);
    $('ui-heatfill').style.width = Math.min(100, state.heat) + '%';
    $('ui-heatpct').textContent = hp + '%';
    $('ui-heatsub').textContent =
      state.heat >= 95
        ? 'RAID INCOMING — bribe or cool down!'
        : state.heat >= 75
          ? 'the feds opened a case. allegedly.'
          : state.heat >= 40
            ? 'local authorities are watching.'
            : 'all quiet for now.';
    $('app').querySelector('.heat').classList.toggle('hot', state.heat >= 90);

    // dock
    const f = featured();
    const fc = upCost(f);
    $('ui-feat-ico').textContent = f.ico;
    $('ui-feat-name').textContent = f.name + (state.ups[f.id] ? ' ·' + (state.ups[f.id]) : '');
    $('ui-feat-cost').textContent = moneyShort(fc) + '  (+' + moneyShort(f.inc * idleMult()) + '/s)';
    const canBuy = state.cash >= fc;
    $('ui-feat-cta').textContent = canBuy ? 'BUY' : 'SOON';
    $('ui-feat').classList.toggle('afford', canBuy);
    $('ui-feat').classList.toggle('poor', !canBuy);

    $('ui-s1').textContent = moneyShort(ps) + '/s';
    $('ui-s2').textContent = '×' + (prestigeMult() < 10 ? prestigeMult().toFixed(2) : Math.floor(prestigeMult()));
    $('ui-s3').textContent = perksOwned() + '/' + PERKS.length;
    $('ui-s4').textContent = abbr(state.taps);
    $('ui-repbadge').textContent = repOwned() + '/' + REP.length;
    $('ui-price').textContent = cleanPrice;

    if (curTab !== 'launder') renderPanel(curTab);
  }

  function renderPanel(tab) {
    if (tab === 'perks') {
      $('list-perks').innerHTML =
        head('Perks', 'Permanent boosts. Buy once.') +
        PERKS.map((p) => {
          const owned = !!state.perks[p.id];
          const can = state.cash >= p.cost;
          const cls = owned ? 'done' : can ? 'afford' : 'locked';
          const cta = owned ? 'OWNED' : moneyShort(p.cost);
          return item('perk', p.id, p.ico, p.name, p.desc, cta, cls);
        }).join('');
    } else if (tab === 'bribes') {
      $('list-bribes').innerHTML =
        head('Bribes', 'Spend dirty money to cool the HEAT — instantly.') +
        BRIBES.map((b) => {
          const c = bribeCost(b);
          const can = state.cash >= c;
          return item('bribe', b.id, b.ico, b.name, b.desc + ' · −' + b.cut + '% heat', moneyShort(c), can ? 'afford' : 'locked');
        }).join('');
    } else if (tab === 'rep') {
      $('list-rep').innerHTML =
        head('Reputation', repOwned() + ' of ' + REP.length + ' badges earned') +
        REP.map((r) => {
          const done = !!state.rep[r.id];
          return item('rep', r.id, done ? r.ico : '🔒', r.name, r.desc, done ? 'DONE' : '—', done ? 'done' : 'locked');
        }).join('');
    } else if (tab === 'escape') {
      const goal = escapeGoal();
      const ready = state.lifetime >= goal;
      const next = CITIES[state.cityIdx + 1] || 'a private island';
      let html =
        head('Escape', 'Burn it all down. Relocate to ' + next + ' for a permanent ×1.75 on everything.') +
        '<div class="item ' +
        (ready ? 'afford' : 'locked') +
        '" data-act="escape"><span class="item-ico">✈️</span><span class="item-meta"><b>Escape to ' +
        next +
        '</b><span>Needs ' +
        money(goal) +
        ' laundered (lifetime). You have ' +
        money(state.lifetime) +
        '.</span></span><span class="item-cta">' +
        (ready ? 'GO' : abbr(state.lifetime) + '/' + abbr(goal)) +
        '</span></div>' +
        '<div class="panel-head" style="margin-top:14px"><h2>Most Wanted</h2><p>top launderers worldwide</p></div>' +
        '<div id="board"></div>';
      $('list-escape').innerHTML = html;
      loadBoard().then((top) => {
        const el = $('board');
        if (!el) return;
        el.innerHTML = top.length
          ? top
              .map(
                (r, i) =>
                  '<div class="item"><span class="item-ico">' +
                  (['🥇', '🥈', '🥉'][i] || i + 1) +
                  '</span><span class="item-meta"><b>' +
                  (r.wallet || r.name || 'anon') +
                  '</b><span>' +
                  money(r.score) +
                  ' laundered</span></span></div>',
              )
              .join('')
          : '<div class="item locked"><span class="item-meta"><b>No rankings yet</b><span>be the first dirty whale.</span></span></div>';
      });
    }
  }
  const head = (t, p) => '<div class="panel-head"><h2>' + t + '</h2><p>' + p + '</p></div>';
  function item(kind, id, ico, name, desc, cta, cls) {
    return (
      '<button class="item ' +
      cls +
      '" data-act="' +
      kind +
      '" data-id="' +
      id +
      '"><span class="item-ico">' +
      ico +
      '</span><span class="item-meta"><b>' +
      name +
      '</b><span>' +
      desc +
      '</span></span><span class="item-cta">' +
      cta +
      '</span></button>'
    );
  }

  // -------------------------------------------------------------- actions
  function launder(e) {
    if (Date.now() < bustedUntil) return;
    const gain = perTap();
    state.cash += gain;
    state.lifetime += gain;
    state.taps++;
    state.heat = Math.min(100, state.heat + TAP_HEAT * coolFactor());
    // drop the +$ and the ripple right where they tapped (over the hands)
    let xp = 50,
      yp = 50;
    const tap = $('ui-tap');
    if (e && tap && e.clientX != null) {
      const r = tap.getBoundingClientRect();
      const lx = e.clientX - r.left,
        ly = e.clientY - r.top;
      xp = (lx / r.width) * 100;
      yp = (ly / r.height) * 100;
      ring(lx, ly);
    }
    floater('+' + moneyShort(gain), xp, yp);
    $('ui-cash').classList.remove('pop');
    void $('ui-cash').offsetWidth;
    $('ui-cash').classList.add('pop');
    haptic(6);
    if (state.heat >= 100) bust();
    checkRep();
    render();
    saveSoon();
  }
  function ring(lx, ly) {
    const r = $('ui-tapring');
    if (!r) return;
    r.style.left = lx + 'px';
    r.style.top = ly + 'px';
    r.classList.remove('go');
    void r.offsetWidth;
    r.classList.add('go');
  }
  let _fl = 0;
  function floater(txt, xp, yp) {
    const now = Date.now();
    if (now - _fl < 50) return;
    _fl = now;
    const f = document.createElement('span');
    f.className = 'float';
    f.textContent = txt;
    f.style.left = (xp != null ? xp : 50) + '%';
    f.style.top = (yp != null ? yp : 50) + '%';
    $('ui-floaters').appendChild(f);
    setTimeout(() => f.remove(), 900);
  }
  function bust() {
    const loss = state.cash * BUST_LOSS;
    state.cash -= loss;
    state.heat = 35;
    bustedUntil = Date.now() + 1500;
    const b = $('ui-bust');
    $('ui-bust-sub').textContent = 'the feds took ' + money(loss) + '.';
    b.classList.add('show');
    haptic([30, 40, 30]);
    setTimeout(() => b.classList.remove('show'), 900);
  }
  function buyFeatured() {
    const f = featured();
    const c = upCost(f);
    if (state.cash < c) return toast('Keep laundering — ' + moneyShort(c) + ' needed.');
    state.cash -= c;
    state.ups[f.id] = (state.ups[f.id] || 0) + 1;
    haptic(10);
    checkRep();
    render();
    saveSoon();
  }
  function buyPerk(id) {
    const p = PERKS.find((x) => x.id === id);
    if (!p || state.perks[id]) return;
    if (state.cash < p.cost) return toast('Need ' + moneyShort(p.cost) + ' for ' + p.name + '.');
    state.cash -= p.cost;
    state.perks[id] = true;
    toast(p.name + ' unlocked ✦');
    haptic(14);
    checkRep();
    render();
    saveLocal();
    syncUp();
  }
  function payBribe(id) {
    const b = BRIBES.find((x) => x.id === id);
    if (!b) return;
    const c = bribeCost(b);
    if (state.cash < c) return toast('Need ' + moneyShort(c) + ' for ' + b.name + '.');
    state.cash -= c;
    state.heat = Math.max(0, state.heat - b.cut);
    state.bribes++;
    toast(b.name + ' paid — heat down.');
    haptic(12);
    checkRep();
    render();
    saveLocal();
    syncUp();
  }
  function doEscape() {
    if (state.lifetime < escapeGoal()) return toast('Not enough laundered to escape yet.');
    state.prestige++;
    state.cityIdx = Math.min(CITIES.length - 1, state.cityIdx + 1);
    state.cash = 0;
    state.ups = {};
    state.bribes = 0;
    state.heat = 0;
    setScene(CITIES[state.cityIdx]);
    toast('Escaped to ' + CITIES[state.cityIdx] + ' ✈️  ×' + prestigeMult().toFixed(2));
    confetti();
    checkRep();
    render();
    saveLocal();
    syncUp();
  }
  function checkRep() {
    let any = false;
    for (const r of REP) {
      if (!state.rep[r.id] && r.test(state)) {
        state.rep[r.id] = true;
        any = true;
        toast('Badge earned: ' + r.name + ' ' + r.ico);
      }
    }
    if (any) confetti();
  }
  function confetti() {
    const h = $('ui-hype');
    const cols = ['#bfe0fb', '#2f74c0', '#e8b53a', '#2f9e63', '#ffffff'];
    for (let i = 0; i < 26; i++) {
      const c = document.createElement('span');
      c.className = 'confetti';
      c.style.left = Math.random() * 100 + '%';
      c.style.background = cols[i % cols.length];
      c.style.animationDuration = 1 + Math.random() * 1.2 + 's';
      c.style.animationDelay = Math.random() * 0.2 + 's';
      h.appendChild(c);
      setTimeout(() => c.remove(), 2400);
    }
  }

  // -------------------------------------------------------------- tabs
  function showTab(tab) {
    curTab = tab;
    ['launder', 'perks', 'bribes', 'rep', 'escape'].forEach((t) => {
      const v = $('view-' + t);
      if (v) v.hidden = t !== tab;
    });
    $('ui-dock').classList.toggle('hidden', tab !== 'launder');
    document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
    if (tab !== 'launder') renderPanel(tab);
  }

  // -------------------------------------------------------------- scene image
  // Full-bleed background photo per city. Drop art at assets/scene-<city>.<ext>
  // (jpg/png/webp). Until then, a soft gradient + a "drop it here" hint show.
  function setScene(city) {
    const slug = (city || 'dubai').toLowerCase().replace(/[^a-z0-9]/g, '');
    const exts = ['jpg', 'jpeg', 'png', 'webp'];
    const img = $('scene-img');
    const hint = $('scene-hint');
    if (!img) return;
    let i = 0;
    (function next() {
      if (i >= exts.length) {
        img.classList.remove('on');
        if (hint) hint.hidden = false;
        return;
      }
      const url = 'assets/scene-' + slug + '.' + exts[i++];
      const probe = new Image();
      probe.onload = () => {
        img.style.backgroundImage = 'url("' + url + '")';
        img.classList.add('on');
        if (hint) hint.hidden = true;
      };
      probe.onerror = next;
      probe.src = url;
    })();
  }

  // -------------------------------------------------------------- $CLEAN buy
  let cleanPrice = '$0.000023';
  function buyClean() {
    // Embedded in the mini app later: ask the parent to open the real swap.
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'clean:buy' }, '*');
        toast('Opening $CLEAN swap…');
        return;
      }
    } catch (_) {}
    window.open('https://jup.ag/', '_blank', 'noopener');
  }
  // accept price/auth from a parent host (mini app), when integrated
  window.addEventListener('message', (e) => {
    const d = e.data || {};
    if (d.type === 'clean:price' && d.price) {
      cleanPrice = d.price;
      $('ui-price').textContent = cleanPrice;
    }
  });

  // -------------------------------------------------------------- engine loop
  function tick(dt) {
    const ps = perSec();
    if (ps > 0) {
      state.cash += ps * dt;
      state.lifetime += ps * dt;
    }
    if (state.heat > 0 && Date.now() >= bustedUntil) state.heat = Math.max(0, state.heat - HEAT_DECAY * dt);
    checkRep();
  }
  function offline() {
    const dt = Math.min(OFFLINE_CAP, (Date.now() - (state.lastSeen || Date.now())) / 1000);
    const earned = perSec() * dt * OFFLINE_RATE;
    if (earned > 1) {
      state.cash += earned;
      state.lifetime += earned;
      setTimeout(() => toast('Welcome back — +' + moneyShort(earned) + ' laundered while away.'), 600);
    }
  }

  // -------------------------------------------------------------- wire up
  function bind() {
    $('ui-tap').addEventListener('pointerdown', (e) => {
      e.preventDefault();
      launder(e);
    });
    $('ui-feat').addEventListener('click', buyFeatured);
    $('ui-buy').addEventListener('click', buyClean);
    $('ui-loc-btn').addEventListener('click', () => showTab('escape'));
    document.querySelectorAll('.nav-btn').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
    // event-delegated list actions
    document.querySelector('.stage').addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      if (!el) return;
      const act = el.dataset.act;
      if (act === 'perk') buyPerk(el.dataset.id);
      else if (act === 'bribe') payBribe(el.dataset.id);
      else if (act === 'escape') doEscape();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        saveLocal();
        syncUp();
      }
    });
    window.addEventListener('pagehide', saveLocal);
  }

  // -------------------------------------------------------------- boot
  function boot() {
    loadLocal();
    offline();
    bind();
    showTab('launder');
    setScene(CITIES[state.cityIdx]);
    render();
    let last = performance.now();
    setInterval(() => {
      const now = performance.now();
      const dt = Math.min(0.5, (now - last) / 1000);
      last = now;
      tick(dt);
      render();
    }, 100);
    setInterval(saveSoon, 5000);
    // try to pull a cloud save, then re-render
    syncDown().then(render);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
