// Headless verification + screenshot harness for Clean Hands.
// Usage: NODE_PATH=$(npm root -g) PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node verify.js [tag]
const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

const FILE = 'file://' + path.resolve(__dirname, 'work/clean-hands.html');
const OUT = path.resolve(__dirname, 'work/shots');
const TAG = process.argv[2] || 'run';
fs.mkdirSync(OUT, { recursive: true });

const OPS = ['hands','counter','laundro','carwash','store','casino','shell','art','trust','mixer','yacht','bank','sov'];
const UPS = ['pen','gloves','machine','lawyer','banker','senator','immunity','parachute'];
const ACH = Array.from({length:13},(_,i)=>'a'+(i+1));

function richSave(over={}){
  const owned={}; OPS.forEach((id,i)=>owned[id]= i<8 ? (12-i) : 0);
  const unlocked={}; OPS.forEach((id,i)=>unlocked[id]= i<10);
  const ups={}; UPS.forEach((id,i)=>ups[id]= i<3);
  const achv={}; ACH.forEach((id,i)=>achv[id]= i<7);
  return Object.assign({
    bank: 8_400_000, earned: 12_500_000, taps: 5200, deals: 6,
    owned, ups, achv, unlocked, heat: 62, passports: 3, pastEarned: 9_000_000,
    lastSeen: Date.now(), busted:false, bribeOwned:{cop:2,inspector:1},
    refBonusUntil:0, fedHeat:0, fbiSeen:true, fbiEvaded:0, everReferred:false, fbiIdx:1, fbiSkips:0
  }, over);
}

const wait = ms => new Promise(r=>setTimeout(r,ms));

const scenarios = [
  { name:'01-home',   save:richSave() },
  { name:'02-launder',save:richSave(), act:async p=>{ await p.click('#tabBuild'); } },
  { name:'03-perks',  save:richSave(), act:async p=>{ await p.click('#tabUp'); } },
  { name:'04-bribes', save:richSave(), act:async p=>{ await p.click('#tabBri'); } },
  { name:'05-rep',    save:richSave(), act:async p=>{ await p.click('#tabAch'); } },
  { name:'06-escape', save:richSave(), act:async p=>{ await p.click('#tabEsc'); } },
  { name:'07-reset',  save:richSave(), act:async p=>{ await p.click('#tabEsc'); await wait(350); await p.click('#resetBtn'); } },
  { name:'08-raid',   save:richSave({busted:true, heat:100}), freeze:true },
  { name:'09-flee',   save:richSave(), act:async p=>{ await p.click('#tabEsc'); await wait(350); await p.click('#fleeBtn',{force:true}); } },
  { name:'11-fednear', save:richSave({earned:44_000_000, fbiIdx:1, heat:62}) },
  { name:'12-invite', save:richSave(), act:async p=>{ await p.click('#invite'); } },
  { name:'13-offline', save:richSave({lastSeen: Date.now()-7200000}) },
  { name:'10-frenzy', save:richSave(), act:async p=>{ await p.evaluate(()=>{ const s=document.getElementById('scene'); s&&s.classList.add('frenzy'); const m=document.getElementById('multBadge'); if(m){m.style.display='block';m.textContent='FRENZY ×7';} }); } },
];

const IGNORE = [/Failed to load resource/i, /net::ERR/i, /favicon/i, /\.mp3/i, /\.webp/i, /\.jpg/i, /the server responded with a status/i, /ERR_FILE_NOT_FOUND/i, /\/api\//i, /URL scheme "file"/i, /Fetch API cannot load/i];
const isReal = m => !IGNORE.some(re=>re.test(m));

(async () => {
  const browser = await chromium.launch();
  const report = [];
  for (const sc of scenarios) {
    const ctx = await browser.newContext({ viewport:{width:390,height:844}, deviceScaleFactor:2, isMobile:true, hasTouch:true });
    const errs = [];
    ctx.on('weberror', e=>errs.push('weberror: '+e.error().message));
    const page = await ctx.newPage();
    page.on('console', m=>{ if(m.type()==='error' && isReal(m.text())) errs.push('console: '+m.text()); });
    page.on('pageerror', e=>errs.push('pageerror: '+e.message));
    page.on('dialog', d=>{ errs.push('DIALOG(native!): '+d.message().slice(0,60)); d.dismiss().catch(()=>{}); });
    if (sc.freeze) await page.addInitScript(()=>{ window.requestAnimationFrame=function(){return 0;}; });
    if (sc.save) await page.addInitScript(s=>{ try{ localStorage.setItem('chdm_save_v1', s); localStorage.setItem('chdm_mute','1'); localStorage.setItem('chdm_privacy','1'); localStorage.setItem('chdm_lastday', new Date().toISOString().slice(0,10)); }catch(e){} }, JSON.stringify(sc.save));
    await page.goto(FILE, { waitUntil:'load' });
    await wait(700);
    if (sc.act) { try { await sc.act(page); } catch(e){ errs.push('ACT ERR: '+e.message); } }
    await wait(500);
    await page.screenshot({ path: path.join(OUT, sc.name+'.png') });
    report.push({ name:sc.name, errs });
    await ctx.close();
  }
  await browser.close();
  console.log('\n══════ VERIFY REPORT ['+TAG+'] ══════');
  let bad=0;
  for (const r of report){ const ok=r.errs.length===0; if(!ok)bad++; console.log((ok?'✓':'✗')+' '+r.name+(ok?'':'\n    '+r.errs.join('\n    '))); }
  console.log(bad===0 ? '\n✅ ALL CLEAN — no JS/console/dialog errors across '+report.length+' surfaces.' : '\n⚠ '+bad+' surface(s) with issues.');
})();
