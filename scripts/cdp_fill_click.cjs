// node cdp_fill_click.cjs <port> <urlMatch> <selector> <value> <buttonText>
// Sets one input React-style, clicks the button with that text, waits, prints
// the resulting page text (first 600 chars) and saves a screenshot.
const fs = require('fs');
const WS = require('/Users/a44/code/exceed-box-ios/node_modules/ws');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const [port, match, sel, val, btn] = process.argv.slice(2);
  const t = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((x) => x.type === 'page' && x.url.includes(match));
  if (!t) throw new Error('tab not found');
  const ws = new WS(t.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((r) => ws.on('open', r));
  let id = 0; const p = new Map();
  ws.on('message', (m) => { const j = JSON.parse(m); p.get(j.id)?.(j); p.delete(j.id); });
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; p.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (e) => (await send('Runtime.evaluate', { expression: e, returnByValue: true })).result?.result?.value;
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  console.log('filled chars:', await ev(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return 'missing'; el.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, ${JSON.stringify(val)});
    el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); return el.value.length; })()`));
  console.log('clicked:', await ev(`(() => { const b = [...document.querySelectorAll('button')].find(b => b.innerText.trim() === ${JSON.stringify(btn)}); if (!b) return false; b.click(); return true; })()`));
  await sleep(9000);
  console.log(JSON.stringify(await ev(`({ url: location.href.slice(0, 100), text: document.body.innerText.replace(/\\s+/g,' ').slice(0, 600) })`), null, 1));
  const { data } = (await send('Page.captureScreenshot', { format: 'png' })).result;
  fs.writeFileSync('/Users/a44/code/exceed-box-app/qa-shots/sendgrid-step.png', Buffer.from(data, 'base64'));
  ws.close();
})().catch((e) => { console.error('aborted:', e.message); process.exit(1); });
