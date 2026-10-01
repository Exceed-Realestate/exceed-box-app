// Print the visible form fields, buttons and headings of the tab whose URL
// contains argv[3], on CDP port argv[2]. Never prints input values.
const WS = require('/Users/a44/code/exceed-box-ios/node_modules/ws');
(async () => {
  const [port, match] = process.argv.slice(2);
  const tabs = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const t = tabs.find((x) => x.type === 'page' && x.url.includes(match));
  if (!t) return console.log('no tab matching', match);
  const ws = new WS(t.webSocketDebuggerUrl, { perMessageDeflate: false });
  await new Promise((r) => ws.on('open', r));
  ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { returnByValue: true, expression: `(() => {
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const out = { url: location.href, title: document.title, headings: [], fields: [], buttons: [], frames: [] };
    document.querySelectorAll('h1,h2,h3,label').forEach((e) => vis(e) && out.headings.push(e.innerText.trim().slice(0, 80)));
    document.querySelectorAll('input,select,textarea').forEach((e) => vis(e) && out.fields.push({ tag: e.tagName, type: e.type, name: e.name, id: e.id, placeholder: e.placeholder, autocomplete: e.autocomplete, filled: !!e.value, checked: e.checked }));
    document.querySelectorAll('button,a[role=button],[type=submit]').forEach((e) => vis(e) && out.buttons.push(e.innerText.trim().slice(0, 60)));
    document.querySelectorAll('iframe').forEach((e) => out.frames.push((e.src || '').slice(0, 80)));
    return out; })()` } }));
  ws.on('message', (m) => { const j = JSON.parse(m); if (j.id === 1) { console.log(JSON.stringify(j.result.result.value, null, 1)); ws.close(); } });
})();
