#!/usr/bin/env node
// PvZ 直播循环自玩看门狗 v3（CDP 真实鼠标点击版）
// 全流程：世界地图→点关卡→Play→本地策略开打→通关点结算→回图→无限循环
// 用法: node loop_watchdog.mjs [页面关键词，默认 Cocos]
const PORT = 9222;
const KEYWORD = process.argv[2] || 'Cocos';

// ── 页面内工具 JS ─────────────────────────────────────────────
const CFG_JS = `(()=>{const K='pvzai.agent.mod.cfg.v1';let c={};try{c=JSON.parse(localStorage.getItem(K)||'{}')}catch(e){};c.localBrain=true;c.autoSun=true;c.autoDeck=true;c.autoShovel=true;localStorage.setItem(K,JSON.stringify(c));return 'cfg-ok';})()`;

// 通用：按名字正则找可见节点，返回 CSS 像素坐标（CDP 鼠标坐标原点在左上角）
// 关键：camera.worldToScreen 原点在左下角 → y 必须翻转；相机优先取节点所在 Canvas 的 UI 相机
const FIND_CLICK = (re) => `(()=>{
  const sc=cc.director.getScene(); const cams=[];
  const wc=n=>{for(const c of n.components||[])if(c instanceof cc.Camera)cams.push(c);for(const k of n.children||[])wc(k)};
  wc(sc); if(!cams.length) return null;
  const re=${re}; let best=null;
  const pick=(node)=>{
    // 优先：节点祖先里最近的 Canvas 的相机；否则依次试所有相机，取换算后落在窗口内的
    let cam=null,p=node,depth=0;
    while(p&&depth<8){ if(p.getComponent){const cv=p.getComponent(cc.Canvas); if(cv&&cv.camera){cam=cv.camera;break}} p=p.parent;depth++ }
    const order=cam?[cam,...cams]:cams;
    for(const c of order){
      try{
        const sp=c.worldToScreen(new cc.Vec3(node.worldPosition.x,node.worldPosition.y,0));
        const dpr=devicePixelRatio||1;
        const x=sp.x/dpr, y=innerHeight-sp.y/dpr;   // 左下原点 → 左上原点
        if(x>30&&x<innerWidth-30&&y>30&&y<innerHeight-30) return {x:Math.round(x),y:Math.round(y)};
      }catch(e){}
    }
    return null;
  };
  const w=n=>{ if(n.activeInHierarchy!==false && re.test(n.name||'')){
    const pos=pick(n);
    if(pos){
      const score=(re.test('playbutton|continuebutton|retrybutton|nextlevel')?1000:0)-Math.abs(pos.x-innerWidth/2)/100;
      if(!best||score>best.score) best={x:pos.x,y:pos.y,score,name:n.name};
    }
  } for(const k of n.children||[])w(k)};
  w(sc); return best?JSON.stringify({x:best.x,y:best.y,name:best.name}):null;
})()`;

const PHASE_JS = `(()=>{try{const s=window.PvzAI.snapshot();return JSON.stringify({ok:s.ok,phase:s.phase})}catch(e){return JSON.stringify({ok:false})}})()`;

const CALL = (fn) => `(function(){try{${fn}}catch(e){return 'ERR '+e.message}})()`;

// ── 主流程 ────────────────────────────────────────────────────
// 用浏览器级 ws + Target.attachToTarget 会话：页面级 ws 只允许一个客户端，
// 调试页面/截图工具一连就会把看门狗踢下线；会话方式可多方共存。
async function connect() {
  const pages = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
  const page = pages.find(p => p.type === 'page' && (p.title + p.url).toLowerCase().includes(KEYWORD.toLowerCase()))
            || pages.find(p => p.type === 'page');
  if (!page) throw new Error('no game page');
  const ver = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const send = (method, params, sessionId) => new Promise((res, rej) => {
    const mid = ++id; pending.set(mid, { res, rej });
    ws.send(JSON.stringify({ id: mid, method, params, sessionId }));
  });
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id); pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    }
  };
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
  return { ws, send: (m, p) => send(m, p, attached.sessionId) };
}

const log = (...m) => console.log(`[${new Date().toLocaleTimeString()}]`, ...m);

async function sessionLoop() {
  const { ws, send } = await connect();
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
    return r && r.result ? r.result.value : undefined;
  };
  const click = async (x, y) => {
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
    await new Promise(r => setTimeout(r, 100));
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await new Promise(r => setTimeout(r, 60));
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
  };
  const findClick = async (reSrc) => {
    const raw = await ev(FIND_CLICK(reSrc));
    return raw ? JSON.parse(raw) : null;
  };

  log('已连接游戏页面');
  log('cfg:', await ev(CFG_JS));

  const SETTLE_RE = "/coinbagprize|moneybag|continuebutton|nextlevel|playagain|retrybutton|replay|continue|confirm|reward|claimbutton|again|backhome/i";
  const PLAY_RE = "/^playbutton$|^retrybutton$|letsrock|fightbutton|startlevel/i";
  const LEVEL_RE = "/^normalLEVELNODE$|^minigameLEVELNODE$/";
  const WORLD_RE = "/^playbutton$|^startgame|start_button|mainstart/i";

  let lastAction = 0;
  let lastPhase = '';
  while (true) {
    let acted = false;
    try {
      const now = Date.now();
      const ph = JSON.parse(await ev(PHASE_JS) || '{"ok":false}');
      if (ph.phase !== lastPhase) { log('阶段:', lastPhase || '-', '→', ph.phase); lastPhase = ph.phase; }
      if (ph.ok && now - lastAction >= 2500) {
        const act = async (fn) => { acted = true; lastAction = Date.now(); await fn(); };
        switch (ph.phase) {
          case 'objective':
            await act(async () => log(await ev(CALL(`window.PvzAI.dismissObjective(); window.PvzAI.start();`))));
            break;
          case 'preparing':
            await act(async () => { log(await ev(CALL(`window.PvzAI.startLevel(); window.PvzAI.start();`))); log('本地策略已启动，开打'); });
            break;
          case 'running':
            await act(async () => {});
            break; // 战斗中交给本地策略
          case 'won': case 'lost': {
            const t = await findClick(SETTLE_RE);
            if (t) await act(async () => { log((ph.phase === 'won' ? '胜利' : '失败') + '→点击[' + t.name + ']'); await click(t.x, t.y); });
            break;
          }
          case 'worldmap': case 'menu': {
            // 有弹窗先点 Play
            const play = await findClick(PLAY_RE);
            if (play) { await act(async () => { log('弹窗→点击[' + play.name + ']'); await click(play.x, play.y); }); break; }
            // 主菜单点开始
            const world = ph.phase === 'menu' ? await findClick(WORLD_RE) : null;
            if (world) { await act(async () => { log('主菜单→点击[' + world.name + ']'); await click(world.x, world.y); }); break; }
            // 世界地图点关卡
            const lv = await findClick(LEVEL_RE);
            if (lv) await act(async () => { log('地图→点击[' + lv.name + '] @', lv.x, lv.y); await click(lv.x, lv.y); });
            break;
          }
        }
      }
      await new Promise(r => setTimeout(r, acted ? 1200 : 1500));
    } catch (e) {
      // 连接断了（游戏重启/页面刷新）：抛出去触发重连
      log('连接断开，5 秒后重连:', e.message);
      try { ws.close(); } catch (_) {}
      throw e;
    }
  }
}

async function run() {
  while (true) {
    try {
      await sessionLoop();
    } catch (_) { /* sessionLoop 已记日志，稍后重连 */ }
    await new Promise(r => setTimeout(r, 5000));
  }
}

run().catch(e => { console.error('启动失败:', e.message); process.exit(1); });
