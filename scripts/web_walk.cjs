// Walk the LIVE staff web app in the warm `exceedbox` Chrome profile, as each
// QA role: sign in, click every sidebar destination, screenshot it, and report
// load time, console errors, failed API calls and any error text on screen.
//
//   node scripts/web_walk.cjs [qa-admin qa-sales-a ...]
//
// Drives Chrome over CDP on the profile's fixed port (9407) — the persistent
// profile, never a headless or throwaway browser. The QA password is read from
// the vault inside this process and typed with Input.insertText; it is never
// printed. Screenshots land in qa-shots/<role>/.
'use strict';
const fs = require('fs');
const path = require('path');
const WS = require('/Users/a44/code/exceed-box-ios/node_modules/ws');

const PORT = 9407;
const BASE = 'https://exceedbox.app';
const OUT = path.join(__dirname, '..', 'qa-shots');
const SECRETS = '/Users/a44/Documents/dojo/dojo/Master vault/Exceed Real Estate/Exceed Box/🔐 Exceed-Box-Secrets.md';
const ROLES = process.argv.slice(2).length ? process.argv.slice(2) : ['qa-admin', 'qa-sales-a', 'qa-marketing'];
// 失敗 ("failure") is deliberately absent: it appears in real nurture email
// subjects and flagged every screen that showed one.
const ERROR_TEXT = /something went wrong|couldn['’]t|could not|failed to|error|forbidden|not found|エラー|読み込めません/i;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdpTarget() {
  const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' });
  return r.json();
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WS(wsUrl, { perMessageDeflate: false });
    let id = 0;
    const pending = new Map();
    const listeners = [];
    ws.on('message', (raw) => {
      const m = JSON.parse(raw);
      if (m.id && pending.has(m.id)) {
        const { res, rej } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? rej(new Error(m.error.message)) : res(m.result);
      } else if (m.method) listeners.forEach((fn) => fn(m));
    });
    ws.on('open', () =>
      resolve({
        send: (method, params = {}) =>
          new Promise((res, rej) => {
            const i = ++id;
            pending.set(i, { res, rej });
            ws.send(JSON.stringify({ id: i, method, params }));
          }),
        on: (fn) => listeners.push(fn),
        close: () => ws.close(),
      })
    );
    ws.on('error', reject);
  });
}

async function evaluate(c, fn, ...args) {
  const expr = `(${fn})(...${JSON.stringify(args)})`;
  const r = await c.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return r.result.value;
}

async function waitFor(c, fn, args = [], timeout = 40000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try {
      const v = await evaluate(c, fn, ...args);
      if (v) return v;
    } catch (_) {}
    await sleep(300);
  }
  return null;
}

async function clickAt(c, x, y) {
  for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased'])
    await c.send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
}

// centre of a visible leaf element whose own text is exactly `label`.
// `last` picks the lowest one on screen: the sign-in sheet's TITLE and its
// BUTTON both read "Sign in", and the button is the one below.
const findText = (label, last) => {
  const hits = [...document.querySelectorAll('body *')]
    .filter((el) => el.childElementCount === 0 && el.textContent.trim() === label)
    .map((el) => el.getBoundingClientRect())
    .filter((r) => r.width && r.height)
    .sort((a, b) => a.y - b.y);
  const r = last ? hits[hits.length - 1] : hits[0];
  return r ? { x: r.x + r.width / 2, y: r.y + r.height / 2 } : null;
};

// Clicks only once the target has stopped moving: the login page lays out in
// stages (fonts, the hero block), and a click aimed at where a button WAS lands
// on empty space — seen live as a sign-in sheet that never opened.
async function clickText(c, label, timeout = 20000, last = false) {
  const t0 = Date.now();
  let prev = null;
  let p = null;
  while (Date.now() - t0 < timeout) {
    p = await evaluate(c, findText, label, last).catch(() => null);
    if (p && prev && Math.abs(p.x - prev.x) < 1 && Math.abs(p.y - prev.y) < 1) break;
    prev = p;
    await sleep(300);
  }
  if (!p) throw new Error(`no visible "${label}"`);
  await clickAt(c, p.x, p.y);
}

// Types into an input once it has stopped moving (the sign-in sheet slides
// up; clicks during the animation land on empty space), then confirms the
// value really arrived — returns only a length, never the text itself.
async function typeInto(c, selector, text) {
  const rectOf = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return r.width ? { x: r.x + r.width / 2, y: r.y + r.height / 2 } : null;
  };
  let prev = null;
  let p = null;
  const t0 = Date.now();
  while (Date.now() - t0 < 20000) {
    p = await evaluate(c, rectOf, selector).catch(() => null);
    if (p && prev && Math.abs(p.x - prev.x) < 1 && Math.abs(p.y - prev.y) < 1) break;
    prev = p;
    await sleep(250);
  }
  if (!p) throw new Error(`no input ${selector}`);
  await clickAt(c, p.x, p.y);
  await sleep(150);
  await c.send('Input.insertText', { text });
  const lengthNow = () => evaluate(c, (sel) => (document.querySelector(sel)?.value || '').length, selector);
  let len = await lengthNow();
  if (len !== text.length) {
    // Input.insertText only reaches a page whose window has OS focus; when the
    // Chrome window sits behind other apps nothing lands (seen live: 0/21).
    // Fall back to what React itself listens for: the native value setter plus
    // a bubbling input event. The value travels inside the CDP message only.
    await evaluate(c, (sel, value) => {
      const el = document.querySelector(sel);
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(el, value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    }, selector, text);
    await sleep(100);
    len = await lengthNow();
  }
  if (len !== text.length) throw new Error(`typing into ${selector} did not land (${len}/${text.length} chars)`);
}

// Every exit — including a failed login — closes the tab it opened. The first
// version returned early on failure and left stale Login tabs behind.
async function walkRole(role, password) {
  const target = await cdpTarget();
  const c = await connect(target.webSocketDebuggerUrl);
  try {
    return await walkRoleIn(c, role, password);
  } finally {
    c.close();
    await fetch(`http://127.0.0.1:${PORT}/json/close/${target.id}`).catch(() => {});
  }
}

async function walkRoleIn(c, role, password) {
  const dir = path.join(OUT, role);
  fs.mkdirSync(dir, { recursive: true });
  const consoleErrors = [];
  const apiFailures = [];
  const open = new Map();   // requestId -> {url, at}, so a hang can be named
  let stuck = [];
  c.on((m) => {
    if (m.method === 'Runtime.exceptionThrown')
      consoleErrors.push(m.params.exceptionDetails.exception?.description?.split('\n')[0] || m.params.exceptionDetails.text);
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      consoleErrors.push(m.params.args.map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 160));
    if (m.method === 'Network.requestWillBeSent') open.set(m.params.requestId, { url: m.params.request.url, at: Date.now() });
    if (m.method === 'Network.loadingFinished' || m.method === 'Network.loadingFailed') open.delete(m.params.requestId);
    if (m.method === 'Network.responseReceived') {
      const { url, status } = m.params.response;
      if (url.includes('/api/') && status >= 400) apiFailures.push(`${status} ${new URL(url).pathname}`);
    }
  });
  // A freshly created tab can sit behind the window's current one, and typed
  // text (Input.insertText) goes to whichever tab has focus — the first role
  // failed twice with "0/21 chars" while later roles, whose tabs came to the
  // front, typed fine.
  await c.send('Page.bringToFront');
  // makes the page behave as focused even when the Chrome window is behind
  // other apps — clicks then focus inputs the way they would for a person
  await c.send('Emulation.setFocusEmulationEnabled', { enabled: true }).catch(() => {});
  await c.send('Page.enable');
  await c.send('Runtime.enable');
  await c.send('Network.enable');
  await c.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  const settle = async (max = 30000) => {
    const t0 = Date.now();
    let quietSince = Date.now();
    stuck = [];
    // A request Chrome never reports finished (seen once: the server logged
    // 200 and the screen rendered, but no loadingFinished arrived) would
    // otherwise pin every later screen at the 30 s cap. Anything open longer
    // than 15 s is reported as stuck and no longer counted as "still loading".
    const STUCK_MS = 15000;
    const live = () => [...open.values()].filter((r) => Date.now() - r.at < STUCK_MS);
    while (Date.now() - t0 < max) {
      if (live().length > 0) quietSince = Date.now();
      else if (Date.now() - quietSince > 1200) break;
      await sleep(150);
    }
    const old = [...open.entries()].filter(([, r]) => Date.now() - r.at >= STUCK_MS);
    stuck = [...new Set(old.map(([, r]) => r.url.slice(0, 100)))].slice(0, 4);
    old.forEach(([id]) => open.delete(id));   // report each hang once, not on every screen
    return Date.now() - t0;
  };
  const shot = async (name) => {
    const { data } = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(path.join(dir, name), Buffer.from(data, 'base64'));
  };

  // signed-out start
  await c.send('Page.navigate', { url: BASE });
  await sleep(1500);
  await evaluate(c, () => { try { localStorage.clear(); sessionStorage.clear(); } catch (_) {} return true; });
  await c.send('Page.reload', { ignoreCache: true });
  await settle();

  const report = { role, login: null, screens: [] };
  try {
    // Opening the sheet submits nothing, so retrying the click is safe.
    let sheetOpen = null;
    for (let attempt = 0; attempt < 3 && !sheetOpen; attempt++) {
      await clickText(c, 'Sign in with email', 40000);
      sheetOpen = await waitFor(c, () => !!document.querySelector('input[placeholder^="name@"]'), [], 6000);
    }
    if (!sheetOpen) throw new Error('the sign-in sheet did not open after 3 clicks');
    await typeInto(c, 'input[placeholder^="name@"]', `${role}@exceed-re.ae`);
    await typeInto(c, 'input[type="password"]', password);
    const t0 = Date.now();
    await clickText(c, 'Sign in', 20000, true);   // the button, not the sheet title
    report.submitted = true;
    const ok = await waitFor(c, () => !document.body.innerText.includes('Sign in with email') && document.querySelectorAll('[aria-label]').length > 5, [], 60000);
    await settle();
    report.login = ok ? `ok ${((Date.now() - t0) / 1000).toFixed(1)}s` : 'FAILED';
    await shot('00-after-login.png');
    if (!ok) {
      report.login += ' | screen: ' + (await evaluate(c, () => document.body.innerText.slice(0, 200))).replace(/\s+/g, ' ');
      return report;
    }
  } catch (e) {
    report.login = 'FAILED ' + e.message;
    await shot('00-login-failed.png');
    return report;
  }

  // sidebar rows: aria-labelled, inside the left column
  const labels = await evaluate(c, () =>
    [...document.querySelectorAll('[aria-label]')]
      .map((el) => ({ label: el.getAttribute('aria-label'), r: el.getBoundingClientRect() }))
      .filter((x) => x.r.x < 280 && x.r.width > 120 && x.r.height > 20 && x.r.height < 80)
      .map((x) => x.label)
  );
  report.sidebar = labels;

  let n = 1;
  for (const label of labels) {
    consoleErrors.length = 0;
    apiFailures.length = 0;
    const p = await evaluate(c, (l) => {
      const el = [...document.querySelectorAll('[aria-label]')].find((e) => e.getAttribute('aria-label') === l && e.getBoundingClientRect().x < 280);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }, label);
    if (!p) { report.screens.push({ label, result: 'sidebar row vanished' }); continue; }
    await clickAt(c, p.x, p.y);
    await sleep(300);
    const ms = await settle();
    const errText = await evaluate(c, (src) => {
      const re = new RegExp(src, 'i');
      // only text a person can actually see: web keeps earlier tabs mounted
      // behind the current one, and their leftovers are not this screen's errors
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || r.x < 280 || r.x > innerWidth || r.y > innerHeight || r.bottom < 0) return false;
        for (let n = el; n; n = n.parentElement) {
          const st = getComputedStyle(n);
          if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
        }
        return document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)?.closest('*') === el ||
               el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
      };
      const main = [...document.querySelectorAll('body *')].filter((el) => el.childElementCount === 0 && visible(el));
      const hits = main.map((el) => el.textContent.trim()).filter((t) => t && t.length < 200 && re.test(t));
      return [...new Set(hits)].slice(0, 3);
    }, ERROR_TEXT.source);
    const file = `${String(n++).padStart(2, '0')}-${label.replace(/[^\w]+/g, '_').slice(0, 30)}.png`;
    await shot(file);
    report.screens.push({
      label, loadSeconds: +(ms / 1000).toFixed(1), apiFailures: [...apiFailures],
      consoleErrors: [...new Set(consoleErrors)].slice(0, 3), errorText: errText, file,
      stuck: [...stuck],
    });
  }
  return report;
}

(async () => {
  const text = fs.readFileSync(SECRETS, 'utf8');
  const m = text.match(/qa-\*@exceed-re\.ae` account: `([^`]+)`/);
  if (!m) throw new Error('QA password not found in the vault note');
  for (const role of ROLES) {
    const r = await walkRole(role, m[1]);
    console.log(`\n=== ${r.role}: login ${r.login}`);
    // A password that was really submitted and still failed must not be
    // retried on the next account in a loop — stop and let a human look.
    if (r.submitted && String(r.login).startsWith('FAILED')) {
      console.log('stopping: a submitted sign-in failed; not trying further accounts');
      break;
    }
    if (r.sidebar) console.log(`sidebar (${r.sidebar.length}): ${r.sidebar.join(' · ')}`);
    for (const s of r.screens) {
      const flags = [
        s.apiFailures?.length ? `API ${s.apiFailures.join(', ')}` : '',
        s.consoleErrors?.length ? `CONSOLE ${s.consoleErrors.join(' | ')}` : '',
        s.errorText?.length ? `TEXT "${s.errorText.join('" "')}"` : '',
        s.stuck?.length ? `STILL LOADING ${s.stuck.join(', ')}` : '',
      ].filter(Boolean).join('  ');
      console.log(`  ${s.label.padEnd(26)} ${String(s.loadSeconds ?? '').padStart(5)}s  ${flags || 'ok'}`);
    }
  }
})().catch((e) => { console.error('walk aborted:', e.message); process.exit(1); });
