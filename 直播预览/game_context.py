"""游戏战况采集：给大肥鱼的围观解说提供真实对局数据。

通过游戏的 WebView2 调试端口（9222）读取 PvzAI 模组的 snapshot()，
压缩成小字典塞进聊天上下文。带短缓存，避免拖慢回复。
"""
import json
import threading
import time
import urllib.request

_CDP = 'http://127.0.0.1:9222/json'
_LOCK = threading.Lock()
_CACHE = {'at': 0.0, 'data': None}
TTL_SECONDS = 4.0


def _opener():
    # 直连回环，绕过系统代理（Oppa 等会拦 127.0.0.1）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _find_ws_url():
    # 游戏忙时 CDP HTTP 端点可能 2 秒才应答，1.2 秒超时经常抢空导致解说“看不到战况”。
    # 放宽超时并重试一次；失败只影响本轮解说素材，不值得拖死回复。
    last_error = None
    for _attempt in range(2):
        try:
            raw = _opener().open(_CDP, timeout=3.0).read()
            for page in json.loads(raw):
                if page.get('type') == 'page' and page.get('url', '').rstrip('/') == 'http://tauri.localhost':
                    return page.get('webSocketDebuggerUrl')
            return None
        except Exception as exc:  # noqa: BLE001 - 超时/连接失败都只是拿不到素材
            last_error = exc
            time.sleep(0.3)
    if last_error:
        print('[game_context] CDP 探测失败:', last_error)
    return None


def _fetch_snapshot(ws_url):
    import websocket
    ws = websocket.create_connection(ws_url, timeout=1.5)
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
            'expression': "JSON.stringify(window.PvzAI ? window.PvzAI.snapshot() : null)",
            'returnByValue': True}}))
        deadline = time.time() + 2.0
        while time.time() < deadline:
            frame = json.loads(ws.recv())
            if frame.get('id') == 1:
                value = frame.get('result', {}).get('result', {}).get('value')
                return json.loads(value) if value else None
        return None
    finally:
        ws.close()


def _summarize(snapshot):
    if not isinstance(snapshot, dict):
        return None
    level = snapshot.get('level') or {}
    phase = snapshot.get('phase')
    in_battle = phase in ('seed_selection', 'playing') or bool(level.get('name'))
    if not in_battle:
        # 主菜单 / 世界地图 / 结算画面：不算观赛中，让解说自然闭麦
        return None
    return {
        'in_game': True,
        'phase': snapshot.get('phase'),
        'level': level.get('name'),
        'wave': level.get('wave'),
        'waves': level.get('waves'),
        'sun': snapshot.get('sun'),
        'plants': len(snapshot.get('plants') or []),
        'zombies': len(snapshot.get('zombies') or []),
    }


def game_context():
    """返回 {in_game, phase, level, wave, waves, sun, plants, zombies} 或 None（带 4 秒缓存）。"""
    now = time.monotonic()
    if now - _CACHE['at'] < TTL_SECONDS:
        return _CACHE['data']
    with _LOCK:
        if time.monotonic() - _CACHE['at'] < TTL_SECONDS:
            return _CACHE['data']
        data = None
        try:
            ws_url = _find_ws_url()
            if ws_url:
                data = _summarize(_fetch_snapshot(ws_url))
        except Exception:
            data = None
        _CACHE['at'] = time.monotonic()
        _CACHE['data'] = data
        return data
