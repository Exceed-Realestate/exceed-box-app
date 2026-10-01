// Sign in on the live site, switch the app to Japanese, and screenshot a few
// screens — proof that Japanese mode really renders, not just that keys exist.
const fs = require('fs'), path = require('path');
const WS = require('/Users/a44/code/exceed-box-ios/node_modules/ws');
const PORT = process.argv[2], BASE = 'https://exceedbox.app';
const OUT = path.join(__dirname, '..', 'qa-shots', 'japanese');
const VAULT = '/Users/a44/Documents/dojo/dojo/Master vault/Exceed Real Estate/Exceed Box/🔐 Exceed-Box-Secrets.md';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const pw = fs.readFileSync(VAULT, 'utf8').match(/qa-\*@exceed-re\.ae` account: `([^`]+)`/)[1];
  const target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })).json();
  const ws = new WS(target.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((r) => ws.on('open', r));
  let id = 0; const p = new Map();
  ws.on('message', (m) => { const j = JSON.parse(m); p.get(j.id)?.(j); p.delete(j.id); });
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; p.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (e) => (await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true })).result?.result?.value;
  const shot = async (n) => { const { data } = (await send('Page.captureScreenshot', { format: 'png' })).result; fs.writeFileSync(path.join(OUT, n), Buffer.from(data, 'base64')); };
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.bringToFront');
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: BASE });
  await sleep(12000);
  // force Japanese the way the app stores it, then reload
  await ev(`(()=>{try{localStorage.setItem("exceedbox.language","ja")}catch(e){}return true})()`);
  await send('Page.reload');
  await sleep(14000);
  await shot('01-login-ja.png');
  console.log('login page text:', (await ev(`document.body.innerText.replace(/\\s+/g," ").slice(0,120)`)) || '(empty)');
  const click = (label, last) => ev(`(()=>{const hits=[...document.querySelectorAll("body *")].filter(e=>e.childElementCount===0&&e.textContent.trim()===${JSON.stringify(label)}).map(e=>e.getBoundingClientRect()).filter(r=>r.width&&r.height).sort((a,b)=>a.y-b.y);const r=${last ? 'hits[hits.length-1]' : 'hits[0]'};if(!r)return false;return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  const clickAt = async (pt) => { for (const type of ['mouseMoved','mousePressed','mouseReleased']) await send('Input.dispatchMouseEvent', { type, x: pt.x, y: pt.y, button: 'left', clickCount: 1 }); };
  const emailBtn = await click('メールでログイン') || await click('Sign in with email');
  if (!emailBtn) { console.log('no sign-in button; login screen text:', await ev(`document.body.innerText.replace(/\\s+/g," ").slice(0,200)`)); process.exit(1); }
  await clickAt(emailBtn); await sleep(2500);
  const fill = (sel, v) => ev(`(()=>{const el=document.querySelector(${JSON.stringify(sel)});if(!el)return "missing";el.focus();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(el,${JSON.stringify(v)});el.dispatchEvent(new Event("input",{bubbles:true}));return el.value.length})()`);
  console.log('email:', await fill('input[placeholder^="name@"]', 'qa-admin@exceed-re.ae'));
  console.log('password chars:', await fill('input[type=password]', pw));
  const submit = await click('ログイン', true) || await click('Sign in', true);
  await clickAt(submit); await sleep(15000);
  await shot('02-after-login-ja.png');
  console.log('sidebar:', JSON.stringify(await ev(`[...document.querySelectorAll("[aria-label]")].map(e=>({l:e.getAttribute("aria-label"),r:e.getBoundingClientRect()})).filter(x=>x.r.x<280&&x.r.width>120&&x.r.height>20&&x.r.height<80).map(x=>x.l)`)));
  for (const [label, file] of [['リード', '03-leads-ja.png'], ['パイプライン', '04-pipeline-ja.png'], ['ダッシュボード', '05-dashboard-ja.png'], ['ナーチャリング', '06-nurture-ja.png']]) {
    const pt = await click(label);
    if (!pt) { console.log('not found in sidebar:', label); continue; }
    await clickAt(pt); await sleep(9000); await shot(file);
    console.log('captured', label);
  }
  ws.close();
  await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`).catch(() => {});
})().catch((e) => { console.error('aborted:', e.message); process.exit(1); });
