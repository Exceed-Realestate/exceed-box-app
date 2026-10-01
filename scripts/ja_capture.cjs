// Screenshot the live app in Japanese from an already signed-in tab.
const fs = require('fs'), path = require('path');
const WS = require('/Users/a44/code/exceed-box-ios/node_modules/ws');
const OUT = path.join(__dirname, '..', 'qa-shots', 'japanese');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const port = process.argv[2];
  const t = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json())
    .find((x) => x.type === 'page' && x.url.includes('exceedbox.app'));
  if (!t) throw new Error('no exceedbox tab open');
  const ws = new WS(t.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((r) => ws.on('open', r));
  let id = 0; const p = new Map();
  ws.on('message', (m) => { const j = JSON.parse(m); p.get(j.id)?.(j); p.delete(j.id); });
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; p.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (e) => (await send('Runtime.evaluate', { expression: e, returnByValue: true })).result?.result?.value;
  const shot = async (n) => { const { data } = (await send('Page.captureScreenshot', { format: 'png' })).result; fs.writeFileSync(path.join(OUT, n), Buffer.from(data, 'base64')); };
  await send('Page.bringToFront');
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(1500);
  const point = (label) => ev(`(()=>{const el=[...document.querySelectorAll("[aria-label]")].find(e=>e.getAttribute("aria-label")===${JSON.stringify(label)}&&e.getBoundingClientRect().x<280);if(!el)return null;const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()`);
  const clickAt = async (pt) => { for (const type of ['mouseMoved','mousePressed','mouseReleased']) await send('Input.dispatchMouseEvent', { type, x: pt.x, y: pt.y, button: 'left', clickCount: 1 }); };
  console.log('sidebar:', JSON.stringify(await ev(`[...document.querySelectorAll("[aria-label]")].map(e=>({l:e.getAttribute("aria-label"),r:e.getBoundingClientRect()})).filter(x=>x.r.x<280&&x.r.width>120&&x.r.height>20&&x.r.height<80).map(x=>x.l)`)));
  let n = 1;
  for (const label of ['本日', 'リード', 'パイプライン', 'ダッシュボード', 'ナーチャー', 'SNS', '連携']) {
    const pt = await point(label);
    if (!pt) { console.log('skip (not in this role\'s menu):', label); continue; }
    await clickAt(pt); await sleep(9000);
    const file = `${String(n++).padStart(2, '0')}-${label}.png`;
    await shot(file);
    const head = await ev(`document.body.innerText.replace(/\\s+/g," ").slice(0,90)`);
    console.log(`${label} -> ${file} | ${head}`);
  }
  ws.close();
})().catch((e) => { console.error('aborted:', e.message); process.exit(1); });
