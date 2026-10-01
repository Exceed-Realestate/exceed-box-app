// Fill the Twilio/SendGrid signup form in the warm `sendgrid` profile and
// press Continue once. Password is read from the vault inside this process and
// never printed. Prints only what the page shows afterwards.
const fs = require('fs');
const WS = require('/Users/a44/code/exceed-box-ios/node_modules/ws');
const VAULT = '/Users/a44/Documents/dojo/dojo/Master vault/Exceed Real Estate/Exceed Box/🔐 Exceed-Box-Secrets.md';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const port = process.argv[2];
  const pw = fs.readFileSync(VAULT, 'utf8').split('SendGrid (Twilio) account')[1].match(/Password: `([^`]+)`/)[1];
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const t = tabs.find((x) => x.type === 'page' && x.url.includes('login.twilio.com'));
  if (!t) throw new Error('signup tab not found');
  const ws = new WS(t.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((r) => ws.on('open', r));
  let id = 0; const pending = new Map();
  ws.on('message', (m) => { const j = JSON.parse(m); if (pending.has(j.id)) { pending.get(j.id)(j); pending.delete(j.id); } });
  const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result?.result?.value;
  await send('Page.bringToFront');
  await send('Emulation.setFocusEmulationEnabled', { enabled: true });
  const setVal = (sel, v) => ev(`(() => { const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return 'missing';
      el.focus(); const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; s.call(el, ${JSON.stringify(v)});
      el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur();
      return el.value.length; })()`);
  console.log('first name:', await setVal('#first-name', 'Balraj'));
  console.log('last name:', await setVal('#last-name', 'Kalra'));
  console.log('email:', await setVal('#email', 'balraj@exceed-re.ae'));
  console.log('password length set:', await setVal('#password', pw));
  console.log('terms checked:', await ev(`(() => { const c = document.querySelector('#terms-of-service'); if (!c.checked) c.click(); return c.checked; })()`));
  await sleep(500);
  const errs = await ev(`[...document.querySelectorAll('[class*=error],[role=alert]')].map(e=>e.innerText.trim()).filter(Boolean).slice(0,5)`);
  console.log('errors before submit:', JSON.stringify(errs));
  console.log('continue clicked:', await ev(`(() => { const b = [...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'Continue'); if (!b) return false; b.click(); return true; })()`));
  await sleep(8000);
  const after = await ev(`({ url: location.href.slice(0, 90), title: document.title, text: document.body.innerText.replace(/\\s+/g,' ').slice(0, 500) })`);
  console.log('after:', JSON.stringify(after, null, 1));
  const { data } = (await send('Page.captureScreenshot', { format: 'png' })).result;
  fs.writeFileSync('/Users/a44/code/exceed-box-app/qa-shots/sendgrid-after-signup.png', Buffer.from(data, 'base64'));
  ws.close();
})().catch((e) => { console.error('aborted:', e.message); process.exit(1); });
