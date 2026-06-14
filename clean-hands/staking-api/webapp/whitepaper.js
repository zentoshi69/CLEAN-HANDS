/* $CLEAN white paper — scroll-reactive FX engine.
   Layered bubble parallax (with bursts), inward-curved twinkle stars, per-act
   motion-gradient shines, and scroll-driven brightness/parallax. Pure
   transform/opacity so it stays buttery; everything degrades to readable text
   under prefers-reduced-motion or if this script never loads. */
(function () {
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var R = Math.random;
  var GLYPHS = ['✦', '✧', '✶', '✷'];

  /* ---- generate bubbles into each parallax layer (small → medium) ---- */
  document.querySelectorAll('.bubbles').forEach(function (layer) {
    var back = layer.classList.contains('l-back');
    var front = layer.classList.contains('l-front');
    var n = back ? 11 : front ? 7 : 9;
    var min = back ? 5 : front ? 14 : 9;
    var span = back ? 9 : front ? 16 : 12;
    for (var i = 0; i < n; i++) {
      var b = document.createElement('i');
      b.className = 'bub';
      var sz = (min + R() * span).toFixed(1);
      b.style.cssText =
        'width:' + sz + 'px;height:' + sz + 'px;left:' + (R() * 98).toFixed(2) + '%;top:' +
        (R() * 100).toFixed(2) + '%;--d:' + (9 + R() * 10).toFixed(1) + 's;--w:-' +
        (R() * 14).toFixed(1) + 's;--o:' + (0.4 + R() * 0.45).toFixed(2);
      layer.appendChild(b);
    }
  });

  /* ---- inward-curved twinkle stars scattered around each headline ---- */
  document.querySelectorAll('.act .stars').forEach(function (box) {
    var n = 10 + Math.floor(R() * 6);
    for (var i = 0; i < n; i++) {
      var s = document.createElement('span');
      s.className = 'cstar';
      s.textContent = GLYPHS[Math.floor(R() * GLYPHS.length)];
      var ang = R() * 6.283, dist = 28 + R() * 70;
      s.style.cssText =
        'left:' + (8 + R() * 84).toFixed(2) + '%;top:' + (6 + R() * 84).toFixed(2) +
        '%;font-size:' + (9 + R() * 22).toFixed(1) + 'px;--fx:' +
        (Math.cos(ang) * dist).toFixed(0) + 'px;--fy:' + (Math.sin(ang) * dist).toFixed(0) +
        'px;--d:' + (2.6 + R() * 3.4).toFixed(1) + 's;--w:-' + (R() * 5).toFixed(1) +
        's;--o:' + (0.55 + R() * 0.45).toFixed(2);
      box.appendChild(s);
    }
  });

  /* ---- sparkle puff (used on bubble burst) ---- */
  function sparkle(x, y) {
    for (var i = 0; i < 6; i++) {
      var s = document.createElement('div');
      s.className = 'spark';
      s.textContent = '✦';
      var ang = R() * 6.283, d = 16 + R() * 34;
      s.style.cssText =
        'left:' + x + 'px;top:' + y + 'px;font-size:' + (8 + R() * 10).toFixed(0) +
        'px;--dx:' + (Math.cos(ang) * d).toFixed(0) + 'px;--dy:' + (Math.sin(ang) * d).toFixed(0) + 'px';
      document.body.appendChild(s);
      (function (el) { setTimeout(function () { el.parentNode && el.parentNode.removeChild(el); }, 640); })(s);
    }
  }

  /* ---- bubbles occasionally burst (only when on-screen) ---- */
  if (!reduce) {
    var bubs = Array.prototype.slice.call(document.querySelectorAll('.bub'));
    (function pop() {
      setTimeout(function () {
        if (bubs.length && !document.hidden) {
          var b = bubs[Math.floor(R() * bubs.length)];
          var r = b.getBoundingClientRect();
          if (r.width && r.top > -40 && r.top < innerHeight + 40 && !b.classList.contains('pop')) {
            b.classList.add('pop');
            sparkle(r.left + r.width / 2, r.top + r.height / 2);
            (function (bb) {
              setTimeout(function () {
                bb.classList.remove('pop');
                bb.style.left = (R() * 98).toFixed(2) + '%';
                bb.style.top = (72 + R() * 36).toFixed(2) + '%';
                bb.style.setProperty('--w', '-' + (R() * 6).toFixed(1) + 's');
              }, 560);
            })(b);
          }
        }
        pop();
      }, 650 + R() * 1300);
    })();
  }

  /* ---- scroll-driven parallax + brightness (rAF) ---- */
  var acts = Array.prototype.slice.call(document.querySelectorAll('.act'));
  var layered = acts.map(function (a) {
    return {
      act: a,
      inner: a.querySelector('.act-inner'),
      depthEls: Array.prototype.slice.call(a.querySelectorAll('[data-depth]')),
    };
  });
  var vh = innerHeight, ticking = false;
  addEventListener('resize', function () { vh = innerHeight; schedule(); }, { passive: true });

  function frame() {
    ticking = false;
    for (var i = 0; i < layered.length; i++) {
      var L = layered[i];
      var r = L.act.getBoundingClientRect();
      var p = (r.top + r.height / 2 - vh / 2) / vh; // ~ -1..1, 0 at center
      for (var j = 0; j < L.depthEls.length; j++) {
        var el = L.depthEls[j];
        var d = parseFloat(el.getAttribute('data-depth')) || 0;
        el.style.transform = 'translate3d(0,' + (p * d * vh).toFixed(1) + 'px,0)';
      }
      if (L.inner) L.inner.style.opacity = Math.max(0, 1 - Math.abs(p)).toFixed(3);
    }
  }
  function schedule() { if (!ticking) { ticking = true; requestAnimationFrame(frame); } }

  if (!reduce) {
    addEventListener('scroll', schedule, { passive: true });
    frame();
  }

  /* ---- act activation: re-fire the shine sweep each time it enters ---- */
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { e.target.classList.toggle('lit', e.isIntersecting); });
    }, { threshold: 0.4 });
    acts.forEach(function (a) { io.observe(a); });

    /* dots scroll-spy */
    var spy = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) {
          document.querySelectorAll('.dots a').forEach(function (d) { d.classList.remove('on'); });
          var t = document.querySelector('.dots a[href="#' + e.target.id + '"]');
          if (t) t.classList.add('on');
        }
      });
    }, { rootMargin: '-45% 0px -45% 0px' });
    acts.forEach(function (a) { spy.observe(a); });
  }

  /* ---- progress bar + nav condense ---- */
  var prog = document.getElementById('prog'), navEl = document.getElementById('nav');
  addEventListener('scroll', function () {
    var h = document.documentElement;
    if (prog) prog.style.width = (h.scrollTop / (h.scrollHeight - h.clientHeight) * 100) + '%';
    if (navEl) navEl.classList.toggle('scrolled', h.scrollTop > 12);
  }, { passive: true });
})();
