#!/usr/bin/env node
// CDP 工具：在游戏页面执行 JS
// 用法: node cdp_eval.mjs <page关键词> "<js表达式>"
// 用浏览器级 ws + Target.attachToTarget 会话，不与看门狗/预览页互踢。
const PORT = 9222;
const [,, keyword = 'gp', expr = 'document.title'] = process.argv;

const pages = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
const page = pages.find(p => p.type === 'page' && (p.title + p.url).toLowerCase().includes(keyword.toLowerCase()));
if (!page) { console.error('pages:', pages.map(p => p.title + ' | ' + p.url).join('\n')); process.exit(1); }
const ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();

const ws = new WebSocket(ver.webSocketDebuggerUrl);
let id = 0, sessionId = null;
const pending = new Map();
function send(method, params) {
  return new Promise((res, rej) => {
    const mid = ++id;
    pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params, sessionId }));
  });
}
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && msg.result?.sessionId && !sessionId) { sessionId = msg.result.sessionId; return; }
  if (msg.id && pending.has(msg.id)) {
    const { res, rej } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
  }
};
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
await send('Target.attachToTarget', { targetId: page.id, flatten: true });
const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
console.log(JSON.stringify(r.result?.value ?? r.result, null, 2).slice(0, 8000));
ws.close();
process.exit(0);
