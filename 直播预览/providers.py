"""Model adapters and shared conversation state; no HTTP UI or speech logic here."""
import json
import base64
import os
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from pathlib import Path
from persona import LIVE_PERSONA, build_live_persona
from rules_config import RuleConfig
from visuals import split_visual_reply
from game_context import game_context
from music_tools import TOOL_SPECS
from live_clock import beijing_time
from conversation_context import ConversationContext
from reply_protocol import strip_watch_tags
from model_debug import ModelDebug, CURRENT_TRACE

class ProviderError(RuntimeError):

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status

class ModelProvider(ABC):

    @abstractmethod
    def generate(self, history, text, event, tool_session=None):
        pass

    @abstractmethod
    def reset(self):
        pass

class CodexProvider(ModelProvider):

    def __init__(self, resident, config, persona_provider=None):
        self.resident, self.config = (resident, config)
        self.persona_provider = persona_provider or (lambda: LIVE_PERSONA)
        self.current_persona = self.persona_provider()
        self.resident.persona_provider = self.persona_provider
        self.needs_sync = False

    def reset(self):
        self.needs_sync = True

    def generate(self, history, text, event, tool_session=None):
        desired = self.persona_provider()
        if desired != self.current_persona:
            self.current_persona = desired
            self.needs_sync = True
        if self.needs_sync:
            self.resident.reset()
            self.needs_sync = False
        images = getattr(tool_session, 'images', [])
        if images:
            return self.resident.chat(text, event, tool_session=tool_session, images=images)
        return self.resident.chat(text, event, tool_session=tool_session)

class DeepSeekProvider(ModelProvider):

    def __init__(self, root, config, persona_provider=None):
        self.root, self.config = (root, config)
        self.persona_provider = persona_provider or (lambda: LIVE_PERSONA)

    def reset(self):
        pass

    def key(self):
        key = os.environ.get('DEEPSEEK_API_KEY')
        secret_file = self.root / '.runtime' / 'secrets.json'
        if not key and secret_file.exists():
            key = json.loads(secret_file.read_text(encoding='utf-8')).get('deepseek_api_key')
        return key

    def generate(self, history, text, event, tool_session=None):
        key = self.key()
        if not key:
            raise ProviderError('DeepSeek 密钥未配置', 503)
        messages = [{'role': 'system', 'content': self.persona_provider()}]
        # model_input 已经是完整的信封 JSON（含 game_context / recent_replies 等），直接作为用户消息下发，
        # 不要再包一层 {'event':..,'message':..}——双层嵌套会让模型忽略里面的游戏观赛数据。
        messages.append({'role': 'user', 'content': text})
        images = getattr(tool_session, 'images', [])
        if images:
            from pathlib import Path
            messages[-1]['content'] = [{'type': 'text', 'text': messages[-1]['content']}] + [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(Path(p).read_bytes()).decode()}} for p in images]
        began = time.perf_counter()
        usage = {}
        try:
            for round_number in range(4):
                body = {'model': self.config.get('deepseek_model', 'deepseek-flash'), 'messages': messages, 'thinking': {'type': 'disabled'}, 'max_tokens': 512, 'stream': True, 'stream_options': {'include_usage': True}}
                if tool_session:
                    body['tools'] = [{'type': 'function', 'function': spec} for spec in getattr(tool_session, 'specs', TOOL_SPECS)]
                    body['tool_choice'] = 'none' if round_number == 3 else 'auto'
                remaining = 60 - (time.perf_counter() - began)
                if remaining <= 0:
                    raise TimeoutError('DeepSeek 工具与回复流程超过60秒，请重试')
                message, first_ms, round_usage = self._stream(body, key, began, remaining)
                for k, v in (round_usage or {}).items():
                    if type(v) in (int, float):
                        usage[k] = usage.get(k, 0) + v
                calls = message.get('tool_calls', [])
                if not calls:
                    reply = (message.get('content') or '').strip()
                    if not reply:
                        raise ProviderError('DeepSeek 没有返回正文')
                    return {'reply': reply[:1200], 'usage': usage or None, 'first_text_ms': first_ms, 'codex_pid': None, 'resident_starts': None, 'session_turns': len(history) + 1, 'tool_calls': tool_session.records if tool_session else []}
                if not tool_session or round_number == 3:
                    raise ProviderError('模型在本轮没有完成工具后的回复，请重试')
                messages.append(message)
                for call in calls:
                    function = call.get('function') or {}
                    result = tool_session.execute(function.get('name', ''), function.get('arguments', ''), call['id'])
                    trace = CURRENT_TRACE.get()
                    if trace:
                        trace.tool(function.get('name', ''), function.get('arguments', ''), result)
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(result, ensure_ascii=False)})
        except urllib.error.HTTPError as exc:
            descriptions = {401: 'DeepSeek 密钥无效', 402: 'DeepSeek 余额不足', 429: 'DeepSeek 请求过于频繁', 400: 'DeepSeek 请求参数不被接受'}
            raise ProviderError(descriptions.get(exc.code, 'DeepSeek 服务错误（HTTP ' + str(exc.code) + '）'), 502) from None
        except urllib.error.URLError:
            raise ProviderError('DeepSeek 网络连接失败，请检查网络') from None

    def _stream(self, body, key, began, timeout):
        trace = CURRENT_TRACE.get()
        if trace:
            trace.request('deepseek.chat.completions', body)
        request = urllib.request.Request('https://api.deepseek.com/chat/completions', data=json.dumps(body, ensure_ascii=False).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        chunks, calls, first_ms, usage, finished = ([], {}, None, None, False)
        with opener.open(request, timeout=timeout) as response:
            for line in response:
                if time.perf_counter() - began > 60:
                    raise TimeoutError('DeepSeek 超过60秒，请重试')
                if not line.startswith(b'data:'):
                    continue
                raw = line[5:].strip()
                if raw == b'[DONE]':
                    finished = True
                    break
                item = json.loads(raw)
                if item.get('error'):
                    raise ProviderError('DeepSeek 返回生成错误，请稍后重试')
                usage = item.get('usage') or usage
                for choice in item.get('choices', []):
                    delta = choice.get('delta') or {}
                    content = delta.get('content')
                    if content:
                        if first_ms is None:
                            first_ms = round((time.perf_counter() - began) * 1000)
                        chunks.append(content)
                    for piece in delta.get('tool_calls') or []:
                        index = piece.get('index', 0)
                        call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        if piece.get('id'):
                            call['id'] = piece['id']
                        function = piece.get('function') or {}
                        call['function']['name'] += function.get('name') or ''
                        call['function']['arguments'] += function.get('arguments') or ''
                        if len(call['function']['arguments']) > 4096 or len(calls) > 8:
                            raise ProviderError('模型工具参数过大')
                    if choice.get('finish_reason') == 'length':
                        raise ProviderError('DeepSeek 输出达到长度上限，请缩短输入后重试')
        if not finished:
            raise ProviderError('DeepSeek 响应不完整，请重试')
        message = {'role': 'assistant', 'content': ''.join(chunks) or None}
        if calls:
            if any((not c['id'] or not c['function']['name'] for c in calls.values())):
                raise ProviderError('DeepSeek 工具调用不完整')
            message['tool_calls'] = [calls[i] for i in sorted(calls)]
        if trace:
            trace.response({**message, 'usage': usage})
        return (message, first_ms, usage)

class WorkBuddyProvider(ModelProvider):
    """用本机 WorkBuddy（CodeBuddy CLI）的账号登录出话，不需要另外买 API Key。

    走 codebuddy -p 无交互模式：每次弹幕起一个子进程，人设用 --system-prompt 传入，
    关闭全部内置工具，避免它去读文件或执行命令。
    """

    DEFAULT_CLI = Path(r'D:\Tools\node22\node_modules\@tencent-ai\codebuddy-code\bin\codebuddy')

    DEFAULT_NODE = Path(r'D:\Tools\node22\node.exe')
    # 只放行这些系统变量：父进程若来自 WorkBuddy 桌面，会注入一堆 CODEBUDDY_*（含
    # FORCE_LITE_WB_BUNDLE / FORCE_HEADLESS_BUNDLE），会让 CLI 切到读不到登录态的产物。
    ENV_KEEP = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC',
                'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP', 'USERNAME',
                'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
                'NUMBER_OF_PROCESSORS', 'OS', 'LANG'}

    def __init__(self, config, persona_provider=None):
        self.config = config
        self.persona_provider = persona_provider or (lambda: LIVE_PERSONA)

    def cli_path(self):
        configured = self.config.get('workbuddy_cli')
        if configured and Path(configured).exists():
            return Path(configured)
        if self.DEFAULT_CLI.exists():
            return self.DEFAULT_CLI
        found = shutil.which('codebuddy')
        return Path(found) if found else None

    def node_path(self):
        configured = self.config.get('workbuddy_node')
        if configured and Path(configured).exists():
            return str(configured)
        if self.DEFAULT_NODE.exists():
            return str(self.DEFAULT_NODE)
        return shutil.which('node') or 'node'

    def env(self):
        env = {k: v for k, v in os.environ.items() if k.upper() in self.ENV_KEEP}
        # 抑制无关提示，并关掉 CLI 自己的自动记忆，避免污染人设提示词。
        env['CODEBUDDY_SKIP_GIT_BASH_CHECK'] = '1'
        env['CODEBUDDY_DISABLE_AUTO_MEMORY'] = '1'
        env['CODEBUDDY_DISABLE_IDE'] = '1'
        return env

    @staticmethod
    def parse_reply(stdout):
        """CLI 的 --output-format json 会先打几行告警，再输出消息数组，最后一环是 result。"""
        raw = (stdout or '').strip()
        start = min([i for i in (raw.find('['), raw.find('{')) if i >= 0], default=-1)
        payload = None
        if start >= 0:
            try:
                payload = json.loads(raw[start:])
            except ValueError:
                for line in reversed(raw.splitlines()):
                    line = line.strip()
                    if line.startswith('{'):
                        try:
                            payload = json.loads(line)
                            break
                        except ValueError:
                            continue
        if isinstance(payload, dict):
            reply = payload.get('result') or payload.get('response') or payload.get('text') or ''
        elif isinstance(payload, list):
            reply = ''
            for item in reversed(payload):
                if isinstance(item, dict) and item.get('type') == 'result':
                    reply = item.get('result') or ''
                    break
            if not reply:
                parts = []
                for item in payload:
                    if isinstance(item, dict) and item.get('role') == 'assistant':
                        content = item.get('content')
                        if isinstance(content, str):
                            parts.append(content)
                        elif isinstance(content, list):
                            parts.extend(b.get('text', '') for b in content
                                         if isinstance(b, dict) and b.get('type') in ('text', 'output_text'))
                reply = ''.join(parts)
        else:
            reply = raw
        if isinstance(reply, list):
            reply = ''.join(p.get('text', '') if isinstance(p, dict) else str(p) for p in reply)
        return str(reply or '').strip()

    def reset(self):
        pass

    # 本通道没有原生 function calling，模型按人设用文本标签发起工具调用，由这里解析执行。
    TOOL_CALL_RE = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.S)
    TOOL_PROTOCOL = (

        '\n\n# 工具调用协议（本通道为文本协议，务必严格遵守）\n'
        '可用工具及参数 schema 如下（参数名必须一字不差，required 字段全部必填，'
        '禁止添加 schema 之外的字段）：\n{tools}\n'
        '需要调用工具时，在回复的最末尾单独一行输出：\n'
        '<tool_call>{"name":"工具名","arguments":{...按schema...}}</tool_call>\n'
        'arguments 必须是合法 JSON 对象；一次只输出一个标签。'
        '系统会把工具执行结果回传给你，你再给观众最终回复。'
        '观众没有说出明确歌名时，禁止调用 request_song，可以自然地问想听什么歌。'
        '查询类工具（get_music_queue、get_today_stats、search_recent_chat）只在观众真的问起时调用。'
        '除标签外正常说话，绝不向观众提到标签、协议或工具执行细节。')

    def _system(self, tool_session=None):
        specs = getattr(tool_session, 'specs', None) or TOOL_SPECS
        rendered = '\n'.join('- ' + json.dumps(s, ensure_ascii=False) for s in specs)
        return self.persona_provider() + self.TOOL_PROTOCOL.replace('{tools}', rendered)

    def _run_cli(self, args, prompt):
        try:
            done = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=90, env=self.env(),
                                  cwd=str(Path(self.config.get('workbuddy_cwd') or Path.home())))
        except FileNotFoundError:
            raise ProviderError('无法启动 WorkBuddy CLI，请确认 node 与 codebuddy 路径', 503) from None
        except subprocess.TimeoutExpired:
            raise ProviderError('WorkBuddy 超过90秒未返回，请重试', 504) from None
        stdout, stderr = done.stdout or '', done.stderr or ''
        combined = stdout + stderr
        if 'Authentication required' in combined or 'not logged in' in combined:
            raise ProviderError('WorkBuddy CLI 未登录：在终端运行 codebuddy 后输入 /login 登录一次', 503)
        reply = self.parse_reply(stdout)
        if not reply:
            raise ProviderError('WorkBuddy 没有返回正文：' + (stderr.strip()[-200:] or stdout.strip()[-200:] or '无输出'), 502)
        return reply

    def _extract_calls(self, reply):
        calls = []
        for match in self.TOOL_CALL_RE.finditer(reply):
            try:
                payload = json.loads(match.group(1))
            except ValueError:
                continue
            name = payload.get('name') or ''
            arguments = payload.get('arguments')
            if isinstance(name, str) and name and isinstance(arguments, dict):
                calls.append((name, arguments))
        return calls

    def generate(self, history, text, event, tool_session=None):
        cli = self.cli_path()
        if not cli:
            raise ProviderError('未找到 WorkBuddy CLI（codebuddy），请在 local-config.json 用 workbuddy_cli 指定完整路径', 503)
        began = time.perf_counter()
        args = [self.node_path(), str(cli), '-p', '--output-format', 'json',
                '--tools', '',
                # 直播场景每次弹幕都要起进程，这里关掉 MCP 与插件加载，省掉约 4 秒启动开销。
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--settings', '{"enabledPlugins":{}}',
                '--system-prompt', self._system(tool_session),
                '--model', self.config.get('workbuddy_model', 'fast-model'),
                '--port', str(self.config.get('workbuddy_port', 41777))]
        trace = CURRENT_TRACE.get()
        reply = self._run_cli(args, text)
        # 文本工具循环：解析标签 -> 真执行 -> 结果回传 -> 让模型给最终回复（最多3轮）。
        for round_number in range(3):
            if not tool_session:
                break
            calls = self._extract_calls(reply)
            if not calls:
                break
            results = []
            for name, arguments in calls:
                result = tool_session.execute(name, json.dumps(arguments, ensure_ascii=False), 'wb-%d' % round_number)
                if trace:
                    trace.tool(name, json.dumps(arguments, ensure_ascii=False), result)
                results.append({'tool': name, 'arguments': arguments, 'result': result})
            follow = json.dumps({'你自己上一轮的回复': reply, '工具执行结果': results,
                                 '要求': '根据上面的工具执行结果给观众最终回复；不要再输出<tool_call>标签；'
                                         '工具失败或参数缺失时，就自然地告诉观众，不要编造结果'},
                                ensure_ascii=False)
            reply = self._run_cli(args, follow)
        reply = self.TOOL_CALL_RE.sub('', reply).replace('</tool_call>', '').strip()
        if not reply:
            raise ProviderError('WorkBuddy 没有返回正文，请重试', 502)
        return {'reply': reply[:1200], 'usage': None,
                'first_text_ms': round((time.perf_counter() - began) * 1000),
                'codex_pid': None, 'resident_starts': None,
                'session_turns': len(history) + 1, 'tool_calls': tool_session.records if tool_session else []}

def _lcs_len(a, b):
    """两段短文本的最长公共子串长度。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def _repeat_matches(reply, recent_replies, message=''):
    """返回与本次回复撞车的最近回复列表：全等、包含、相同开头或大段共用措辞。

    message 是本轮观众消息；开头与消息本身重复（复述问题）不计入撞车。
    """
    r = ''.join(str(reply or '').split())
    if not r:
        return []
    msg = ''.join(str(message or '').split())
    matched = []
    for prev in recent_replies or []:
        p = ''.join(str(prev or '').split())
        if not p:
            continue
        if r == p:
            matched.append(prev)
            continue
        if len(r) >= 8 and len(p) >= 8 and (r in p or p in r):
            matched.append(prev)
            continue
        # 相同的开头：前 10 字全同，或前 4 字同且不是在复述观众的问题
        if len(r) >= 10 and len(p) >= 10 and r[:10] == p[:10]:
            matched.append(prev)
            continue
        if len(r) >= 4 and len(p) >= 4 and r[:4] == p[:4] and r[:4] not in msg:
            matched.append(prev)
            continue
        # 换皮复读：哪怕措辞不同，只要共用 6 字以上的连续片段（同一个梗反复用）也算
        if len(r) >= 6 and _lcs_len(r, p) >= 6:
            matched.append(prev)
    return matched


def _is_repeat(reply, recent_replies, message=''):
    """判断回复是否与最近几条回复重复或高度相似。"""
    return bool(_repeat_matches(reply, recent_replies, message))


class ProviderManager:

    def __init__(self, resident, root, config, rules=None, context=None):
        self.config, self.path = (config, root / 'local-config.json')
        self.rules = rules or RuleConfig(root)
        self.debug = ModelDebug()

        def persona():
            settings = self.rules.snapshot()['persona']
            return build_live_persona(settings)
        self.adapters = {'codex': CodexProvider(resident, config, persona), 'deepseek': DeepSeekProvider(root, config, persona), 'workbuddy': WorkBuddyProvider(config, persona)}
        self.selected = config.get('provider', 'codex')
        self.context = context if context is not None else ConversationContext(root)
        self.context_room = 'local'
        self.last_provider = None
        self.tool_host = None
        self.visual_context = None

    @property
    def history(self):
        return self.context.recent(self.context_room)

    def model(self, name):
        if name == 'codex':
            return self.config.get('model', 'gpt-6.1-sol')
        if name == 'workbuddy':
            return self.config.get('workbuddy_model', 'fast-model')
        return self.config.get('deepseek_model', 'deepseek-flash')

    def status(self):
        providers = []
        for name, adapter in self.adapters.items():
            if name == 'deepseek':
                configured = bool(adapter.key())
            elif name == 'workbuddy':
                configured = adapter.cli_path() is not None
            else:
                configured = bool(getattr(getattr(adapter, 'resident', None), 'executable', None))
            providers.append({'id': name, 'model': self.model(name), 'configured': configured})
        return {'provider': self.selected, 'model': self.model(self.selected), 'history_turns': len(self.history), 'context_window': self.context.window, 'context_storage': 'sqlite', 'tool_names': [spec['name'] for spec in getattr(self.tool_host, 'specs', TOOL_SPECS)] if self.tool_host else [], 'providers': providers}

    def select(self, name):
        if name not in self.adapters:
            raise ProviderError('未知模型供应商', 400)
        if name == 'deepseek' and (not self.adapters[name].key()):
            raise ProviderError('DeepSeek 密钥未配置', 503)
        updated = {**self.config, 'provider': name}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
        self.config.update(updated)
        self.selected = name
        return self.status()

    def reset(self):
        self.context.clear()
        for adapter in self.adapters.values():
            adapter.reset()
        self.last_provider = None

    def generate(self, text, event, user=None, room_id='local', event_id=None, gift_context=None):
        if not hasattr(self, 'debug'):
            self.debug = ModelDebug()
        trace = self.debug.start(provider=self.selected, model=self.model(self.selected), event=event, viewer=user, room_id=room_id, event_id=event_id, trigger=text)
        token = CURRENT_TRACE.set(trace)
        try:
            result = self._generate(text, event, user, room_id, event_id, gift_context)
            trace.finish(result)
            return {**{k: v for k, v in result.items() if not k.startswith('debug_')}, 'debug_id': trace.id}
        except Exception as exc:
            trace.finish(error=exc)
            raise
        finally:
            CURRENT_TRACE.reset(token)

    def _generate(self, text, event, user=None, room_id='local', event_id=None, gift_context=None):
        name = self.selected
        adapter = self.adapters[name]
        self.context_room = str(room_id)
        history = self.context.recent(self.context_room)
        session = None
        if self.tool_host and event in ('chat',):
            session = self.tool_host.bind(user, room_id, event_id)
        envelope = {'message': text, 'trusted_event': {'type': event, 'gift_received': event == 'gift'}}
        envelope['room_id'] = self.context_room
        envelope['viewer'] = {'id': str((user or {}).get('id') or ''), 'nickname': str((user or {}).get('nickname') or '朋友')}
        envelope['recent_conversation'] = history
        recent_replies = [t['assistant'] for t in history if (t.get('assistant') or '').strip()][-6:]
        envelope['reply_rules'] = {
            'recent_replies': recent_replies,
            'note': ('recent_replies 是你最近说过的话。新回复不能与其中任何一条相同，'
                     '也不能是同一说法换皮（同样的开头、同样的比喻、同样的句式都算）；'
                     '开头几个字必须和里面每一条都不同，之前用过的梗（哪怕换个词）也不再拿出来。'
                     '必须换新的角度或细节，宁可短一点，也不要复读自己。'),
        }
        envelope['current_time'] = {**beijing_time(), 'note': '本轮回复开始时的北京时间；以此为准，不沿用旧轮次的时间。自然结合时段聊天，无需每次报时。'}
        if event == 'gift' and gift_context:
            envelope['trusted_event']['gift'] = gift_context['facts']
            envelope['gift_thanks'] = {k: v for k, v in gift_context.items() if k != 'facts'}
            envelope['gift_thanks']['recent_replies'] = [t['assistant'] for t in history if t['event'] == 'gift'][-6:]
        if session:
            context = session.context()
            envelope['music_context'] = context
        game = game_context()
        if game:
            envelope['game_context'] = game
        if self.visual_context:
            envelope['appearance_context'] = self.visual_context()
        model_input = json.dumps(envelope, ensure_ascii=False)
        trace = CURRENT_TRACE.get()
        if trace:
            persona = getattr(adapter, 'persona_provider', None)
            trace.input(persona() if callable(persona) else '', envelope, getattr(session, 'specs', []) if session else [])
            trace.store.update(trace.id, model_called=True)
        result = adapter.generate(history, model_input, event, session)
        # 复读检测：与最近几条回复撞车时退回重说一次。
        # 只补说一次——多补会明显拖慢回复（每次模型调用约 5~7 秒）。
        # 注意用剥掉 visual 标签后的正文比对，避免标签字段造成误判。
        rejected = []
        for _attempt in range(2):
            dups = _repeat_matches(strip_watch_tags(result.get('reply', '')), recent_replies, text)
            if not dups:
                break
            rejected.append(strip_watch_tags(result.get('reply', '')))
            trace2 = CURRENT_TRACE.get()
            # 具体列出被禁的开头，比抽象说“换个说法”更有效。
            # 用前 3 个字做黑名单，连“吃过”这类惯性起手词一起封掉。
            banned = []
            for src in list(recent_replies) + rejected:
                head = ''.join(str(src or '').split())[:3]
                if head and head not in banned:
                    banned.append(head)
            envelope['retry_note'] = ('你这次的回复和最近说过的话撞车了：『'
                                      + '』『'.join(str(d) for d in dups[:3]) + '』。'
                                      '下面这些开头字样都已经被你用过，这次开头的头几个字绝对不许再是它们：『'
                                      + '』『'.join(banned[:10]) + '』。'
                                      + ('另外这几条也是你刚才说过、被我退回的，同样不许再撞：『'
                                         + '』『'.join(rejected[-2:]) + '』。' if rejected else '')
                                      + '先想一个和这些完全不同的起手方式，'
                                        '再用新的角度和措辞回应这条消息，宁可聊点别的也不要重复。')
            model_input = json.dumps(envelope, ensure_ascii=False)
            if trace2:
                trace2.store.update(trace2.id, note=f'检测到复读（第{_attempt + 1}次），重说')
            result = adapter.generate(history, model_input, event, session)
        original_output = result['reply']
        if trace:
            trace.store.update(trace.id, raw_output=original_output)
        raw_reply = strip_watch_tags(result['reply'])
        reply, visual = split_visual_reply(raw_reply)
        silent = False
        if not reply and (not silent):
            raise ProviderError('模型未返回可播放的回复正文，请重试')
        result = {**result, 'reply': '' if silent else reply, 'visual_request': visual}
        result['debug_raw_output'] = original_output
        history_text = json.dumps(gift_context['facts'], ensure_ascii=False) if event == 'gift' and gift_context else text
        if not silent:
            self.context.append(self.context_room, user, event, history_text, result['reply'], name, event_id)
        self.last_provider = name
        return {**result, 'provider': name, 'model': self.model(name)}
