import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
import atexit
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, unquote
import edge_tts
from codex_resident import ResidentCodex
from providers import ProviderManager, ProviderError
from speech import synthesize
from music import MusicService, atomic_json
from studio import Studio
from douyin import DouyinSource
from music_tools import MusicTools
from visuals import VisualDirector
from rules_config import RuleConfig
from model_policy import ModelPolicy
from expense import ExpenseEstimate
from gift_replies import plan_gift_reply
from live_tools import LiveTools
from live_clock import beijing_time
ROOT = Path(__file__).parent.resolve()
RUNTIME = ROOT / '.runtime'
RUNTIME.mkdir(exist_ok=True)
AUDIO = ROOT / 'audio'
AUDIO.mkdir(exist_ok=True)
LOCK = threading.Lock()
CODEX = shutil.which('codex') or (str(p) if (p := Path(r'D:\Tools\node22\codex.cmd')).exists() else None)
HISTORY = []
CONFIG = json.loads((ROOT / 'local-config.json').read_text(encoding='utf-8'))

def codex_environment():
    env = os.environ.copy()
    proxy = CONFIG.get('codex_proxy')
    if proxy:
        env['HTTPS_PROXY'] = proxy
        env['HTTP_PROXY'] = proxy
        env['https_proxy'] = proxy
        env['http_proxy'] = proxy
        env['NO_PROXY'] = '127.0.0.1,localhost'
        env['no_proxy'] = env['NO_PROXY']
        return env
    if os.name == 'nt' and (not any((env.get(k) for k in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy')))):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings') as key:
                enabled = winreg.QueryValueEx(key, 'ProxyEnable')[0]
                address = winreg.QueryValueEx(key, 'ProxyServer')[0]
            if enabled and address:
                if '=' in address:
                    entries = dict((part.split('=', 1) for part in address.split(';') if '=' in part))
                    address = entries.get('https') or entries.get('http')
                if address:
                    proxy = address if '://' in address else 'http://' + address
                    env['HTTPS_PROXY'] = proxy
                    env['HTTP_PROXY'] = proxy
        except OSError:
            pass
    env.setdefault('NO_PROXY', '127.0.0.1,localhost')
    return env
RESIDENT = ResidentCodex(CODEX, RUNTIME, codex_environment, CONFIG.get('model', 'gpt-6.1-sol'), CONFIG.get('codex_service_tier', 'default'))
RULES = RuleConfig(ROOT)
MODELS = ProviderManager(RESIDENT, ROOT, CONFIG, rules=RULES)
AUTO_MODELS = ModelPolicy(ROOT, RULES, RESIDENT, MODELS, LOCK)
EXPENSES = ExpenseEstimate(ROOT, RULES)
VISUALS = VisualDirector(ROOT, rules=RULES)
MODELS.visual_context = VISUALS.status

def fixed_response(text, event):
    return {'reply': text, 'usage': None, 'first_text_ms': 0, 'codex_pid': None, 'resident_starts': None, 'session_turns': None, 'provider': 'rules', 'model': '礼物规则' if event == 'gift' else '进场欢迎' if event == 'welcome' else '点歌提示'}

def model_response(text, event, user, room_id, event_id, gift_context):
    AUTO_MODELS.apply_pending(already_locked=True)
    if MODELS.selected == 'codex' and (not CODEX):
        raise ProviderError('未找到 Codex CLI', 503)
    if MODELS.selected == 'codex' and CONFIG.get('codex_proxy'):
        target = urlparse(CONFIG['codex_proxy'])
        try:
            with socket.create_connection((target.hostname, target.port or 80), timeout=2):
                pass
        except OSError:
            raise ProviderError('Codex 专用代理未启动：请打开 Clash，并确认代理端口为 ' + str(target.port or 80), 503) from None
    options = {}
    result = MODELS.generate(text, event, user, room_id, event_id, gift_context=gift_context, **options)
    EXPENSES.record(result, text)
    return result

def generate_response(text, event='chat', voice=True, fixed_reply=None, special=None, user=None, room_id='local', event_id=None, gift_context=None, gift_fallback=None):
    """One model/voice pipeline shared by manual tests and the live dispatcher."""
    if not LOCK.acquire(blocking=False):
        raise ProviderError('正在处理上一条回复，请稍后重试', 429)
    start = time.perf_counter()
    try:
        if fixed_reply is None:
            try:
                result = model_response(text, event, user, room_id, event_id, gift_context)
            except (ProviderError, TimeoutError) as exc:
                if event != 'gift' or not gift_fallback:
                    raise
                result = {**fixed_response(gift_fallback, event), 'model': '礼物兜底', 'gift_reply_fallback': True, 'model_error': str(exc)[:250]}
        else:
            result = fixed_response(fixed_reply, event)
        if not result.get('debug_id'):
            trace = MODELS.debug.start(provider='rules', model=result['model'], event=event, viewer=user, room_id=room_id, event_id=event_id, trigger=text)
            trace.input('', {'event': event, 'message': text, 'fixed_reply': result['reply']}, [])
            trace.finish({**result, 'debug_raw_output': result['reply']})
            result['debug_id'] = trace.id
        llm_ms = round((time.perf_counter() - start) * 1000)
        debug_id = result.get('debug_id')
        if debug_id:
            MODELS.debug.update(debug_id, playback='静默，不合成语音' if result.get('silent') else '准备语音' if voice else '仅文字', llm_ms=llm_ms, voice_enabled=bool(voice))
        try:
            speech = {'audio': None, 'audio_parts': []} if result.get('silent') else synthesize(ROOT, result['reply'], voice, special)
        except Exception as exc:
            if debug_id:
                MODELS.debug.update(debug_id, playback='语音准备失败', audio_error=str(exc)[:250])
            raise
        total_ms = round((time.perf_counter() - start) * 1000)
        if debug_id:
            MODELS.debug.update(debug_id, playback='静默，不合成语音' if result.get('silent') else '语音准备失败' if speech.get('audio_error') else '语音已准备，等待播放' if speech.get('audio') or speech.get('audio_parts') else '仅文字', audio_error=speech.get('audio_error'), tts_ms=total_ms - llm_ms, total_ms=total_ms)
        return {**result, **speech, 'llm_ms': llm_ms, 'tts_ms': total_ms - llm_ms, 'total_ms': total_ms}
    finally:
        LOCK.release()

def render_event(event, voice):
    data = event['data']
    plan = plan_gift_reply(event, RULES.snapshot()) if event['type'] == 'gift' else None
    options = {'gift_context': plan['context'], 'gift_fallback': plan['fallback']} if plan and plan['use_model'] else {}
    fixed = None if options else plan['fallback'] if plan else data.get('reply')
    result = generate_response(data.get('content', ''), event['type'], voice, fixed_reply=fixed, special=data.get('special'), user=event.get('user'), room_id=event.get('roomId', 'local'), event_id=event.get('id'), **options)
    if not result.get('silent'):
        result['visual'] = VISUALS.respond(event, result.pop('visual_request', {}))
    MODELS.debug.update(result.get('debug_id'), visual=result.get('visual'))
    return result
MUSIC = MusicService(ROOT, rules=RULES)
MUSIC_TOOLS = MusicTools(MUSIC)
STUDIO = Studio(ROOT, MUSIC, render_event, rulebook=RULES, command_tools=MUSIC_TOOLS)

def read_live_statistics():
    with STUDIO.rules.lock, EXPENSES.lock:
        income, expenses = (STUDIO.rules.income.summary(), EXPENSES.status())
        live, simulated = (income['today']['live'], income['today']['simulation'])
        rate = RULES.snapshot()['income']['yuan_per_diamond']
        preview = not DOUYIN.room_id and (not live['gift_count'])
        displayed = simulated if preview else live
        amount, spent = (round(displayed['coins'] * rate, 2), expenses['estimated_yuan'])
        return {'day': income['day'], 'display_mode': 'simulation' if preview else 'live', 'income_yuan': amount, 'real_gift_income_yuan': round(live['coins'] * rate, 2), 'simulated_gift_income_yuan': round(simulated['coins'] * rate, 2), 'expense_yuan': round(spent, 3), 'net_yuan': round(amount - spent, 2), 'tokens': expenses['tokens'], 'model_requests': expenses['requests'], 'gift_count': displayed['gift_count'], 'unknown_price_gifts': displayed['unknown_price_count'], 'is_estimate': True, 'official_settlement_available': False, 'note': '收入按礼物折算、支出为展示估算，不是官方结算或API余额；模拟收入不能说成真实收入。'}
STUDIO.debug = MODELS.debug
MODELS.tool_host = LiveTools(MUSIC_TOOLS, read_live_statistics, STUDIO.search_recent_chat, RULES)
RESIDENT.tool_specs_provider = lambda: MODELS.tool_host.specs

def ingest_live_event(event):
    return STUDIO.ingest({**event, 'simulation': False})
DOUYIN = DouyinSource(ROOT, ingest_live_event)

def close_integrations():
    AUTO_MODELS.close()
    STUDIO.close()
    DOUYIN.close()
    MUSIC.close()
    RESIDENT.close()
atexit.register(close_integrations)

def reset_statistics():
    if not LOCK.acquire(blocking=False):
        raise ProviderError('请等当前回复生成结束后再清空统计', 409)
    try:
        with STUDIO.rules.lock, EXPENSES.lock:
            backup = 'stats-' + str(time.time_ns()) + '.json'
            atomic_json(RUNTIME / 'stats-backups' / backup, {'income': STUDIO.rules.income.state, 'expenses': EXPENSES.days})
            income, expenses = (STUDIO.reset_income(), EXPENSES.reset())
            return {'message': '收支、礼物数量和token统计已清零；聊天与点歌保留', 'income': income, 'expenses': expenses, 'backup': backup}
    finally:
        LOCK.release()

def clear_session_queues():
    if not LOCK.acquire(blocking=False):
        raise ProviderError('请等当前回复生成完成后再清空队列', 409)
    try:
        with MUSIC_TOOLS.lock, MUSIC.lock, STUDIO.rules.lock:
            backup = None
            state = STUDIO.rules.state
            if MUSIC.queue or MUSIC.current or any((state[k] for k in ('jobs', 'combos', 'voice_retries'))):
                backup = 'queues-' + str(time.time_ns()) + '.json'
                atomic_json(RUNTIME / 'session-backups' / backup, {'music': {'queue': MUSIC.queue, 'current': MUSIC.current}, 'studio': {k: state[k] for k in ('jobs', 'combos', 'voice_retries')}, 'income': STUDIO.rules.income.state})
            songs = MUSIC.clear_pending()
            counts = STUDIO.clear_pending()
            return {'message': '本场待播歌、待回复消息和语音重试已清空，收入记录保留', 'songs_cleared': songs, **counts, 'backup': backup, 'playback_epoch': state['playback_epoch']}
    finally:
        LOCK.release()

def pvz_target():
    """Locate the running PvZ2 Gardendless page via its WebView2 CDP port (bypass system proxy).
    Returns the BROWSER-level ws + page targetId: page-level ws allows only one client,
    so the watchdog and the preview page would keep kicking each other off."""
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        pages = json.loads(opener.open('http://127.0.0.1:9222/json', timeout=1.5).read())
        page = None
        for item in pages:
            if item.get('type') == 'page' and item.get('url', '').rstrip('/') == 'http://tauri.localhost':
                page = item
                break
        if not page:
            return {'ok': False, 'pages': [{'title': p.get('title', ''), 'url': p.get('url', '')} for p in pages if p.get('type') == 'page']}
        ver = json.loads(opener.open('http://127.0.0.1:9222/json/version', timeout=1.5).read())
        return {'ok': True, 'ws': ver.get('webSocketDebuggerUrl', ''), 'targetId': page.get('id', ''), 'title': page.get('title', '')}
    except Exception as error:
        return {'ok': False, 'error': str(error)}


def _pvz_tool_path(key, fallback):
    """PvZ 相关路径优先读 local-config.json，缺省回落到项目内脚本，换电脑不必改代码。"""
    value = CONFIG.get(key)
    path = Path(value) if value else Path(fallback)
    if key == 'node_path' and not path.exists():
        found = shutil.which('node')
        return Path(found) if found else path
    return path

PVZ_TOOLS = {
    'node': _pvz_tool_path('node_path', r'D:\Tools\node22\node.exe'),
    'watchdog': _pvz_tool_path('pvz_watchdog', str(ROOT / 'tools' / 'pvz' / 'loop_watchdog.mjs')),
    'launcher': _pvz_tool_path('pvz_launcher', r'D:\pvz2-garden\ai-bundle\PvZAI-发布包\PvZAI_Launcher.exe'),
}
PVZ = {'proc': None, 'state': 'idle', 'message': '', 'since': 0.0, 'gen': 0}
PVZ_LOCK = threading.Lock()

def pvz_clean_env():
    """清掉大小写重复的代理变量（会导致官方启动器崩溃），并绕过系统代理访问本机。"""
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in ('https_proxy', 'http_proxy', 'all_proxy'):
            env.pop(key, None)
    return env

def pvz_port_open():
    try:
        with socket.create_connection(('127.0.0.1', 9222), timeout=1.5):
            return True
    except OSError:
        return False

def pvz_watchdog_log(limit=6):
    try:
        return (RUNTIME / 'pvz-watchdog.log').read_text(encoding='utf-8', errors='replace').splitlines()[-limit:]
    except OSError:
        return []

def _pvz_launch_flow(gen):
    try:
        if not pvz_port_open():
            if not PVZ_TOOLS['launcher'].exists():
                with PVZ_LOCK:
                    PVZ['state'], PVZ['message'] = 'error', '找不到游戏启动器 ' + str(PVZ_TOOLS['launcher'])
                return
            with PVZ_LOCK:
                PVZ['message'] = '正在启动游戏（PvZAI_Launcher）…'
            subprocess.Popen([str(PVZ_TOOLS['launcher'])], cwd=str(PVZ_TOOLS['launcher'].parent), env=pvz_clean_env(),
                             stdout=open(RUNTIME / 'pvz-launcher.log', 'a', encoding='utf-8'), stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            deadline = time.time() + 180
            while not pvz_port_open():
                if time.time() > deadline:
                    with PVZ_LOCK:
                        if PVZ['gen'] == gen:
                            PVZ['state'], PVZ['message'] = 'error', '180 秒内游戏调试端口(9222)未就绪，请确认游戏窗口已弹出'
                    return
                time.sleep(2)
                if PVZ['gen'] != gen:
                    return
        if not (PVZ_TOOLS['node'].exists() and PVZ_TOOLS['watchdog'].exists()):
            with PVZ_LOCK:
                PVZ['state'], PVZ['message'] = 'error', '缺少 node.exe 或 loop_watchdog.mjs'
            return
        with PVZ_LOCK:
            if PVZ['gen'] != gen:
                return
            PVZ['message'] = '游戏就绪，挂载循环看门狗…'
            logf = open(RUNTIME / 'pvz-watchdog.log', 'a', encoding='utf-8')
            PVZ['proc'] = subprocess.Popen([str(PVZ_TOOLS['node']), str(PVZ_TOOLS['watchdog']), 'Cocos'],
                                           cwd=str(PVZ_TOOLS['watchdog'].parent), env=pvz_clean_env(),
                                           stdout=logf, stderr=subprocess.STDOUT)
        time.sleep(3)
        with PVZ_LOCK:
            if PVZ['gen'] != gen:
                return
            if PVZ['proc'] and PVZ['proc'].poll() is not None:
                PVZ['state'], PVZ['message'] = 'error', '看门狗启动后立即退出，请查看日志'
                return
            PVZ['state'], PVZ['message'], PVZ['since'] = 'running', '循环运行中：自动进关，打完自动续关', time.time()
    except Exception as exc:
        with PVZ_LOCK:
            PVZ['state'], PVZ['message'] = 'error', str(exc)[:300]

def pvz_loop_start():
    with PVZ_LOCK:
        proc = PVZ['proc']
        if proc and proc.poll() is None:
            return {'message': '循环自玩已在运行', 'state': PVZ['state']}
        PVZ['gen'] += 1
        gen = PVZ['gen']
        PVZ['state'], PVZ['message'], PVZ['since'] = 'launching', '准备启动…', time.time()
        threading.Thread(target=_pvz_launch_flow, args=(gen,), name='pvz-loop', daemon=True).start()
    return {'message': '启动中：游戏未开会先开游戏，然后自动挂循环看门狗', 'state': 'launching'}

def pvz_loop_stop(close_game=False):
    with PVZ_LOCK:
        PVZ['gen'] += 1
        stopped = []
        proc = PVZ['proc']
        if proc and proc.poll() is None:
            try:
                proc.kill()
                stopped.append('循环看门狗')
            except OSError:
                pass
        PVZ['proc'] = None
        PVZ['state'], PVZ['message'] = 'idle', '已停止' if stopped else '本来就没在运行'
    if close_game:
        game = subprocess.run(['taskkill', '/F', '/IM', 'pvzge-0.16.1.exe'], capture_output=True)
        subprocess.run(['taskkill', '/F', '/IM', 'PvZAI_Launcher.exe'], capture_output=True)
        if game.returncode == 0:
            stopped.append('游戏')
            with PVZ_LOCK:
                PVZ['message'] = '已停止：' + '、'.join(stopped)
    return {'message': PVZ['message'], 'stopped': stopped, 'state': PVZ['state']}

def pvz_loop_status():
    with PVZ_LOCK:
        proc = PVZ['proc']
        running = bool(proc and proc.poll() is None)
        if not running and PVZ['state'] == 'running':
            PVZ['state'], PVZ['message'] = 'idle', '看门狗已退出'
        return {'state': PVZ['state'], 'message': PVZ['message'], 'watchdog_running': running,
                'game_ready': pvz_port_open(), 'log': pvz_watchdog_log(), 'since': PVZ['since']}


class Handler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if self.headers.get('Host') not in ('127.0.0.1:18763', 'localhost:18763'):
            return self.reply(403, {'error': '仅允许本机访问'})
        if path == '/api/time':
            return self.reply(200, beijing_time())
        if path == '/api/pvz/target':
            return self.reply(200, pvz_target())
        if path == '/api/pvz/loop/status':
            return self.reply(200, pvz_loop_status())
        if path == '/api/debug/turns':
            result = MODELS.debug.index()
            return self.reply(200, result)
        if path.startswith('/api/debug/turns/'):
            value = MODELS.debug.detail(path.rsplit('/', 1)[-1])
            return self.reply(200, value) if value else self.reply(404, {'error': '记录已过期或不存在'})
        if path == '/api/studio/status':
            return self.reply(200, {'studio': STUDIO.status(), 'douyin': DOUYIN.status(), 'music': MUSIC.status(), 'visual': VISUALS.status(), 'models': MODELS.status(), 'policy': AUTO_MODELS.status(), 'expenses': EXPENSES.status()})
        if path == '/api/studio/presentation':
            return self.reply(200, {'presentation': STUDIO.presentation(), 'visual': VISUALS.status()})
        if path == '/api/status':
            return self.reply(200, {**MODELS.status(), 'features': {'lifecycle': True}, 'service': {'pid': os.getpid(), 'root': str(ROOT)}, 'policy': AUTO_MODELS.status(), 'codex': bool(CODEX), 'voice': 'zh-CN-XiaoyiNeural', 'busy': LOCK.locked(), 'timeout': 60, 'resident_pid': RESIDENT.process.pid if RESIDENT.process and RESIDENT.process.poll() is None else None, 'resident_starts': RESIDENT.starts})
        if any((part.startswith('.') for part in path.replace('\\', '/').split('/') if part)):
            return self.send_error(404)
        if path.startswith('/assets/live2d/'):
            target = (ROOT / path.lstrip('/')).resolve()
            if target.is_relative_to((ROOT / 'assets/live2d').resolve()) and target.suffix in ('.json', '.png', '.moc3', '.js') and target.is_file():
                return super().do_GET()
            return self.send_error(404)
        if path.startswith('/assets/gift_icons/'):
            target = (ROOT / path.lstrip('/')).resolve()
            if target.is_relative_to((ROOT / 'assets/gift_icons').resolve()) and target.suffix == '.png' and target.is_file():
                return super().do_GET()
            return self.send_error(404)
        if path not in ('/', '/index.html', '/debug.html', '/debug.js', '/debug.css', '/live.js', '/studio.js', '/music_queue.js', '/live_view.css', '/live_view.js', '/avatar.css', '/presentation.js', '/voice_player.js', '/character.png', '/favicon.ico', '/assets/gift_audio/meow_paw.mp3', '/assets/gift_audio/meow_paw.wav', '/assets/gift_audio/meow_paw.ogg') and (not (path.startswith('/audio/') and path.endswith('.mp3') and (path.count('/') == 2))):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        if self.headers.get('Host') not in ('127.0.0.1:18763', 'localhost:18763'):
            return self.reply(403, {'error': '仅允许本机访问'})
        if self.headers.get('Origin') not in (None, 'http://127.0.0.1:18763', 'http://localhost:18763'):
            return self.reply(403, {'error': '不允许跨站请求'})
        if self.headers.get_content_type() != 'application/json':
            return self.reply(415, {'error': '需要 application/json'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                return self.reply(413, {'error': '请求过大或为空'})
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            return self.reply(400, {'error': '无效 JSON'})
        path = urlparse(self.path).path
        if path == '/api/debug/clear':
            return self.reply(200, MODELS.debug.clear())
        if path == '/api/studio/lifecycle/stop':
            STUDIO.stop_event.set()
            self.reply(200, {'message': '后台正在停止；聊天、统计和配置保留'})
            threading.Thread(target=self.server.shutdown, name='local-shutdown', daemon=True).start()
            return
        if path.startswith(('/api/music/', '/api/studio/', '/api/douyin/', '/api/pvz/')):
            try:
                if path == '/api/music/connect':
                    result = MUSIC.connect()
                elif path == '/api/music/order':
                    text = data.get('text')
                    if not isinstance(text, str):
                        raise ValueError('请输入歌名')
                    result = MUSIC.order(text, automatic=True)
                elif path == '/api/music/control':
                    result = MUSIC.control(data.get('action'), data.get('id'))
                elif path == '/api/studio/event':
                    if 'voice' in data and (not isinstance(data['voice'], bool)):
                        raise ValueError('voice 必须为布尔值')
                    data['simulation'] = True
                    data['roomId'] = 'local'
                    result = STUDIO.ingest(data)
                    if result['status'] == 'queued':
                        pass
                    elif result['status'] in ('gift_queued', 'gift_pending'):
                        result['message'] = '礼物已收到，正在排队感谢'
                    elif result['status'] == 'music':
                        result['message'] = '点歌请求已收到，正在处理'
                elif path == '/api/studio/playback':
                    with STUDIO.rules.lock:
                        result = {**STUDIO.claim(data.get('client_id'), data.get('ack'), data.get('failed', False), data.get('voice'), data.get('role', 'preview'), data.get('audio_ready', True), data.get('failure_reason', '')), 'playback_epoch': STUDIO.rules.state['playback_epoch']}
                elif path == '/api/studio/presentation/clear':
                    result = STUDIO.clear_presentation()
                elif path == '/api/studio/stats/reset':
                    result = reset_statistics()
                elif path == '/api/studio/queues/clear':
                    result = clear_session_queues()
                elif path == '/api/studio/retry':
                    result = STUDIO.retry()
                elif path == '/api/studio/visual-reset':
                    result = VISUALS.idle()
                elif path == '/api/studio/theme':
                    result = VISUALS.set_theme(data.get('mode'))
                elif path == '/api/pvz/loop/start':
                    result = pvz_loop_start()
                elif path == '/api/pvz/loop/stop':
                    result = pvz_loop_stop(bool(data.get('close_game')))
                elif path == '/api/douyin/connect':
                    result = DOUYIN.connect(data.get('room_id'), data.get('cookie', ''))
                    if not RULES.yaml_mode:
                        with STUDIO.rules.lock:
                            STUDIO.rules.reload_config()
                            if STUDIO.rules.config_error:
                                raise ValueError(STUDIO.rules.config_error)
                            STUDIO.rules.config['room_id'] = result['room_id']
                            from music import atomic_json
                            atomic_json(STUDIO.rules.config_path, STUDIO.rules.config)
                elif path == '/api/douyin/disconnect':
                    result = DOUYIN.disconnect()
                else:
                    return self.reply(404, {'error': '接口不存在'})
                return self.reply(200, result)
            except ProviderError as exc:
                return self.reply(exc.status, {'error': str(exc)[:350]})
            except (ValueError, TypeError, KeyError) as exc:
                return self.reply(400, {'error': str(exc)[:350]})
            except Exception as exc:
                return self.reply(502, {'error': str(exc)[:350]})
        if urlparse(self.path).path in ('/api/reset', '/api/provider'):
            if not LOCK.acquire(blocking=False):
                return self.reply(409, {'error': '请等当前回复完成再清空上下文'})
            try:
                if urlparse(self.path).path == '/api/provider':
                    return self.reply(200, AUTO_MODELS.manual_select(data.get('provider')))
                MODELS.reset()
                return self.reply(200, {'ok': True})
            except ProviderError as exc:
                return self.reply(exc.status, {'error': str(exc)})
            finally:
                LOCK.release()
        if urlparse(self.path).path != '/api/chat':
            return self.reply(404, {'error': '接口不存在'})
        text = data.get('text')
        if not isinstance(text, str) or not text.strip():
            return self.reply(400, {'error': '请输入弹幕'})
        text = text.strip()
        if len(text) > 500:
            return self.reply(400, {'error': '弹幕最多 500 字'})
        try:
            return self.reply(200, generate_response(text, data.get('event', 'chat'), data.get('voice', True)))
        except ProviderError as exc:
            return self.reply(exc.status, {'error': str(exc)})
        except TimeoutError as exc:
            return self.reply(504, {'error': str(exc)})
        except Exception as exc:
            return self.reply(502, {'error': str(exc)[:400]})
if __name__ == '__main__':
    if RULES.snapshot()['studio']['clear_queues_on_start'] and os.environ.get('WHALE_PRESERVE_SESSION') != '1':
        clear_session_queues()
    MUSIC.run()
    STUDIO.run()
    if not os.environ.get('WHALE_NO_WARMUP'):
        try:
            RESIDENT.start()
            print('Resident Codex ready, PID:', RESIDENT.process.pid, flush=True)
        except Exception as exc:
            print('Codex warm-up failed:', str(exc), flush=True)
    print('Local: http://127.0.0.1:18763/', flush=True)
    AUTO_MODELS.run()
    ThreadingHTTPServer(('127.0.0.1', 18763), Handler).serve_forever()
