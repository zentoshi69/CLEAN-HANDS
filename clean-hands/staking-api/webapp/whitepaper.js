(function(){
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* populate every stage: shared base (orbs + stars) plus a THEMED recipe per
     section, so the magic changes as you scroll — never the same stage twice */
  var GLYPHS = ['✦','✧','✶','·','✷'];
  var HUES = ['rgba(134,191,248,.55)','rgba(169,211,251,.5)','rgba(214,234,253,.65)','rgba(46,116,192,.28)'];
  var R = Math.random;
  function el(sec, cls, css, txt){
    var n = document.createElement('span'); n.className = cls; n.style.cssText = css;
    if (txt) n.textContent = txt; sec.appendChild(n);
  }
  /* one recipe per section, cycling: dust → aurora → rings → comets → rain → bloom */
  var RECIPES = [
    function dust(s){ for (var i=0;i<22;i++) el(s,'mote','left:'+(R()*96)+'%;--sz:'+(3+R()*6)+'px;--d:'+(7+R()*7)+'s;--w:-'+(R()*9)+'s;--o:'+(0.5+R()*0.5)+';--sway:'+(10+R()*40)+'px'); },
    function aurora(s){ for (var i=0;i<3;i++) el(s,'aur','top:'+(8+i*26+R()*8)+'%;--d:'+(9+R()*6)+'s;--w:-'+(R()*8)+'s'); },
    function rings(s){ for (var i=0;i<5;i++){ var sz=90+R()*220; el(s,'ring','width:'+sz+'px;height:'+sz+'px;left:'+(R()*80)+'%;top:'+(R()*70)+'%;--d:'+(4+R()*4)+'s;--w:-'+(R()*5)+'s'); } },
    function comets(s){ for (var i=0;i<6;i++) el(s,'comet','left:'+(R()*55)+'%;top:'+(R()*80)+'%;--d:'+(5+R()*5)+'s;--w:-'+(R()*7)+'s;--rot:'+(6+R()*14)+'deg'); },
    function rain(s){ for (var i=0;i<18;i++) el(s,'fleck','left:'+(R()*97)+'%;--d:'+(6+R()*6)+'s;--w:-'+(R()*8)+'s;--o:'+(0.4+R()*0.5)); },
    function blooms(s){ for (var i=0;i<4;i++) el(s,'bloom','left:'+(8+R()*78)+'%;top:'+(10+R()*70)+'%;--sz:'+(26+R()*40)+'px;--d:'+(5+R()*4)+'s;--w:-'+(R()*6)+'s', GLYPHS[i%GLYPHS.length]); }
  ];
  document.querySelectorAll('.s').forEach(function(sec, si){
    for (var o = 0; o < 3; o++){
      var orb = document.createElement('i'); orb.className = 'orb';
      var sz = 140 + R()*220;
      orb.style.cssText = 'width:'+sz+'px;height:'+sz+'px;left:'+(R()*85)+'%;top:'+(R()*70)+'%;'+
        'background:'+HUES[(si+o)%HUES.length]+';animation-duration:'+(12+R()*10)+'s;animation-delay:-'+(R()*12)+'s';
      sec.appendChild(orb);
    }
    var n = 18 + Math.floor(R()*12);
    for (var i = 0; i < n; i++){
      el(sec,'star','left:'+(R()*96)+'%;top:'+(R()*92)+'%;font-size:'+(7+R()*20)+'px;'+
        '--d:'+(2.2+R()*4)+'s;--w:'+(R()*5)+'s;--o:'+(0.45+R()*0.55), GLYPHS[Math.floor(R()*GLYPHS.length)]);
    }
    /* this stage's signature effect + a hint of the next one for depth */
    RECIPES[si % RECIPES.length](sec);
    if (si % 2 === 1) RECIPES[(si + 3) % RECIPES.length](sec);
  });

  /* reveal stages (starts twinkle + fires the shine sweep) */
  var io = new IntersectionObserver(function(es){
    es.forEach(function(e){ if (e.isIntersecting){ e.target.classList.add('in'); } });
  }, { threshold: 0.25 });
  document.querySelectorAll('.s').forEach(function(s){ io.observe(s); });

  /* scroll-spy TOC */
  var links = Array.prototype.slice.call(document.querySelectorAll('.toc a'));
  var secs = links.map(function(a){ return document.querySelector(a.getAttribute('href')); });
  var spy = new IntersectionObserver(function(es){
    es.forEach(function(e){
      if (e.isIntersecting){
        var i = secs.indexOf(e.target);
        links.forEach(function(l){ l.classList.remove('on'); });
        if (links[i]) links[i].classList.add('on');
      }
    });
  }, { rootMargin: '-40% 0px -55% 0px' });
  secs.forEach(function(s){ if (s) spy.observe(s); });

  /* progress bar + nav condense (same as landing) */
  var prog = document.getElementById('prog');
  var navEl = document.getElementById('nav');
  addEventListener('scroll', function(){
    var h = document.documentElement;
    prog.style.width = (h.scrollTop / (h.scrollHeight - h.clientHeight) * 100) + '%';
    if (navEl) navEl.classList.toggle('scrolled', h.scrollTop > 12);
  }, { passive: true });

  /* occasional shooting star */
  if (!reduce){
    var shoot = document.createElement('div'); shoot.className = 'shoot'; document.body.appendChild(shoot);
    (function fire(){
      setTimeout(function(){
        shoot.style.setProperty('--sx', (5 + Math.random()*40) + 'vw');
        shoot.style.setProperty('--sy', (5 + Math.random()*40) + 'vh');
        shoot.classList.remove('go'); void shoot.offsetWidth; shoot.classList.add('go');
        fire();
      }, 3500 + Math.random()*6000);
    })();
  }
})();
