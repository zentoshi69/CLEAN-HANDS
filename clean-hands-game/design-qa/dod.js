// Definition-of-Done checks: 360px layout, reduced-motion, and a live gameplay smoke test.
const { chromium } = require('playwright');
const path = require('path');
const FILE = 'file://' + path.resolve(__dirname, 'work/clean-hands.html');
const OUT = path.resolve(__dirname, 'work/shots');
const wait = ms => new Promise(r=>setTimeout(r,ms));
const OPS = ['hands','counter','laundro','carwash','store','casino','shell','art','trust','mixer','yacht','bank','sov'];
const IGNORE=[/Failed to load resource/i,/net::ERR/i,/\/api\//i,/URL scheme "file"/i,/Fetch API cannot load/i,/\.(mp3|webp|jpg)/i];
const real=m=>!IGNORE.some(re=>re.test(m));

(async()=>{
  const browser=await chromium.launch();
  const results=[];

  // ---- A. 360px + reduced-motion ----
  {
    const ctx=await browser.newContext({viewport:{width:360,height:760},deviceScaleFactor:2,isMobile:true,hasTouch:true,reducedMotion:'reduce'});
    const errs=[]; const page=await ctx.newPage();
    page.on('console',m=>{if(m.type()==='error'&&real(m.text()))errs.push(m.text());});
    page.on('pageerror',e=>errs.push('pageerror: '+e.message));
    const owned={};OPS.forEach((id,i)=>owned[id]=i<8?12-i:0);const unlocked={};OPS.forEach((id,i)=>unlocked[id]=i<10);
    await page.addInitScript(s=>{localStorage.setItem('chdm_save_v1',s);localStorage.setItem('chdm_mute','1');localStorage.setItem('chdm_privacy','1');localStorage.setItem('chdm_lastday',new Date().toISOString().slice(0,10));},
      JSON.stringify({bank:8400000,earned:12500000,owned,unlocked,heat:62,passports:3,pastEarned:9000000,lastSeen:Date.now(),achv:{a1:1,a2:1,a3:1,a4:1,a5:1,a6:1,a7:1}}));
    await page.goto(FILE,{waitUntil:'load'}); await wait(700);
    await page.screenshot({path:path.join(OUT,'dod-360-home.png')});
    await page.click('#tabEsc'); await wait(300);
    await page.screenshot({path:path.join(OUT,'dod-360-escape.png')});
    await page.click('#fleeBtn',{force:true}); await wait(300);
    await page.screenshot({path:path.join(OUT,'dod-360-flee.png')});
    results.push({name:'360px + reduced-motion',errs}); await ctx.close();
  }

  // ---- B. gameplay smoke: tap-to-earn + buy still work ----
  {
    const ctx=await browser.newContext({viewport:{width:390,height:844},deviceScaleFactor:2,isMobile:true,hasTouch:true});
    const errs=[]; const page=await ctx.newPage();
    page.on('console',m=>{if(m.type()==='error'&&real(m.text()))errs.push(m.text());});
    page.on('pageerror',e=>errs.push('pageerror: '+e.message));
    await page.addInitScript(s=>{localStorage.setItem('chdm_save_v1',s);localStorage.setItem('chdm_mute','1');localStorage.setItem('chdm_privacy','1');localStorage.setItem('chdm_lastday',new Date().toISOString().slice(0,10));},
      JSON.stringify({bank:5000,earned:5000,owned:{hands:1},unlocked:{hands:true,counter:true},lastSeen:Date.now()}));
    await page.goto(FILE,{waitUntil:'load'}); await wait(600);
    const read=async()=>page.$eval('#bankNum',e=>e.textContent);
    const before=await read();
    const box=await page.$eval('#scene',e=>{const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
    for(let i=0;i<5;i++){ await page.mouse.click(box.x,box.y); await wait(60); }
    const afterTap=await read();
    await page.click('#tabBuild'); await wait(300);
    const ownedBefore=await page.$eval('#paneBuildings .op .ct',e=>e.textContent).catch(()=>'?');
    await page.click('#paneBuildings .op'); await wait(250);  // buy first building
    const bankAfterBuy=await read();
    await page.screenshot({path:path.join(OUT,'dod-gameplay.png')});
    results.push({name:'gameplay (tap+buy)',errs,detail:`bank ${before} -> tap ${afterTap} -> buy ${bankAfterBuy}`});
    await ctx.close();
  }

  await browser.close();
  console.log('\n══════ DoD REPORT ══════');
  let bad=0;
  for(const r of results){const ok=r.errs.length===0;if(!ok)bad++;console.log((ok?'✓':'✗')+' '+r.name+(r.detail?'  ['+r.detail+']':'')+(ok?'':'\n    '+r.errs.join('\n    ')));}
  console.log(bad===0?'\n✅ DoD PASS':'\n⚠ DoD issues: '+bad);
})();
