"""Shared, viewer-scoped music tools. Both model adapters execute this registry."""
import copy
import hashlib
import json
import threading
import time
import uuid
from music import atomic_json, DuplicateSongError


TOOL_SPECS = [
    {'name': 'request_song', 'description': '观众想听歌、点歌、播放某首歌时调用。本工具会自动连接或启动网易云、搜歌并安排播放，不要求观众点击开关。提取歌名和明确提出的歌手，不需要固定指令。直接采用搜索结果第一条并安排播放，不列候选、不向观众询问序号。返回 queued 才能说已入队，失败如实告知。',
     'parameters': {'type': 'object', 'properties': {
         'title': {'type': 'string', 'description': '纯歌名，去掉“我想听”“帮我放”等请求话语；指定歌曲ID时可留空'},
         'artist': {'type': 'string', 'description': '观众明确指定的歌手，没指定则空字符串'},
         'song_id': {'type': 'string', 'description': '仅限观众明确给出的网易云歌曲ID，否则空字符串，不得编造'}},
         'required': ['title', 'artist', 'song_id'], 'additionalProperties': False}},
    {'name': 'withdraw_song', 'description': '观众明确要撤回自己最近一首尚未交给播放器的点歌时调用，不能撤回其他人的点歌。',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
]


def public_track(track):
    return {k: str(track.get(k, ''))[:120] for k in ('id', 'title', 'artist', 'album')}


class MusicTools:
    def __init__(self, music):
        self.music = music
        self.lock = threading.RLock()
        self.path = music.root / '.runtime/music-tool-receipts.json'
        self.receipts = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def bind(self, user=None, room_id='local', event_id=None):
        user = user or {'id': 'local', 'nickname': '测试观众'}
        user_id = 'local' if room_id == 'local' else 'douyin:' + str(room_id) + ':' + str(user['id'])
        return MusicToolSession(self, user_id, str(user.get('nickname') or '朋友')[:40],
                                str(event_id or uuid.uuid4().hex))


class MusicToolSession:
    MAX_CALLS = 3

    def __init__(self, host, user_id, nickname, event_id):
        self.host, self.user_id, self.nickname, self.event_id = host, user_id, nickname, event_id
        self.records = []
    def context(self):
        music = self.host.music
        with music.lock:
            limits = music.rulebook.snapshot()['music']
            player = music.bridge.snapshot
            current = player.get('current') if player.get('connected') else None
            stall_after = float(limits.get('staged_resume_after_seconds') or 0)
            handoff_waiting_seconds = 0.0
            head = music.queue[0] if music.queue else None
            if head is not None and head.get('state') == 'staged':
                started = head.get('staged_wait_started_at') or head.get('created')
                if started:
                    handoff_waiting_seconds = max(0.0, music.clock()-float(started))
            # A staged request that has been waiting past the nudge threshold never
            # reached the player, so the room is effectively silent even though the
            # native player still reports a current track.
            playback_stalled = bool(stall_after and handoff_waiting_seconds >= stall_after)
            return {'viewer': {'nickname': self.nickname},
                    'my_queued_songs': [public_track(v['track']) for v in music.queue if v['user_id'] == self.user_id],
                    'queue_count': len(music.queue), 'queue_limit': limits['max_pending'],
                    'current_track': public_track(current) if current else None,
                    'playback_stalled': playback_stalled,
                    'handoff_waiting_seconds': round(handoff_waiting_seconds),
                    'player_connected': bool(player.get('connected')), 'observed_at': player.get('observedAt'),
                    'queue_count_excludes_current': True,
                    'queued_songs': [{'position': i, 'track': public_track(v['track']), 'nickname': v['nickname']} for i,v in enumerate(music.queue,1)],
                    'my_queue_limit': limits['max_pending_per_user'],
                    'music_automatically_prepared_on_request': True}

    def execute(self, name, arguments, call_id=''):
        try:
            if len(self.records) >= self.MAX_CALLS:
                raise ValueError('本条弹幕工具调用次数已达上限，请向观众说明结果')
            if isinstance(arguments, str):
                if len(arguments) > 4096:
                    raise ValueError('工具参数过长')
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError('工具参数必须为对象')
            spec = next((s for s in TOOL_SPECS if s['name'] == name), None)
            if not spec:
                raise ValueError('没有这个工具，只能使用点歌相关工具')
            if set(arguments)-set(spec['parameters']['properties']):
                raise ValueError('工具包含不允许的参数')
            if set(spec['parameters'].get('required', []))-set(arguments):
                raise ValueError('工具缺少必要参数')
            key = hashlib.sha256(json.dumps([self.user_id, self.event_id, name, arguments], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            with self.host.lock:
                if key in self.host.receipts:
                    result = copy.deepcopy(self.host.receipts[key]['result'])
                else:
                    result = self._execute(name, arguments)
                    self.host.receipts[key] = {'time': time.time(), 'result': result}
                    self.host.receipts = dict(sorted(self.host.receipts.items(), key=lambda kv: kv[1]['time'])[-1000:])
                    atomic_json(self.host.path, self.host.receipts)
        except DuplicateSongError as exc:
            with self.host.music.lock:
                result = {'status': exc.status, 'message': str(exc), 'queued': False,
                          'track': public_track(exc.track), 'position': exc.position,
                          'queue_count': len(self.host.music.queue)}
        except Exception as exc:
            result = {'status': 'error', 'message': str(exc)[:300], 'queued': False}
        self.records.append({'tool': name, 'call_id': str(call_id)[:120], 'result': copy.deepcopy(result)})
        return result

    def _execute(self, name, arguments):
        music = self.host.music
        if name == 'request_song':
            for key in ('title', 'artist', 'song_id'):
                if not isinstance(arguments[key], str) or len(arguments[key]) > 120:
                    raise ValueError('歌名、歌手与ID必须为120字以内的字符串')
            title, artist, song_id = (arguments[k].strip() for k in ('title', 'artist', 'song_id'))
            if song_id:
                if not song_id.isascii() or not song_id.isdigit() or not 1 <= len(song_id) <= 19:
                    raise ValueError('歌曲ID格式无效')
                query = 'id=' + song_id
                request = {'query': query, 'song_id': song_id, 'title': '', 'artist': ''}
            else:
                if not title:
                    raise ValueError('没有明确歌名，请询问观众想听哪一首')
                query = title + (' / ' + artist if artist else '')
                request = {'query': (title+' '+artist).strip(), 'title': title, 'artist': artist}
            result = music.order(query, self.user_id, self.nickname, request=request, automatic=True)
            return self._public_result(result)
        with music.lock:
            item = next((v for v in reversed(music.queue) if v['user_id'] == self.user_id and v['state'] == 'waiting'), None)
            if not item:
                raise ValueError('你没有可以撤回的待播歌曲；已经交给播放器的歌曲不能在此撤回')
            track = public_track(item['track'])
            music.control('remove', item['id'])
            return {'status': 'withdrawn', 'track': track}

    def _public_result(self, result):
        return {'status': 'queued', 'track': public_track(result['item']['track']),
                'position': result['position'], 'queue_count': result['queue_count'], 'queue_limit': result['queue_limit'],
                'message': result['message'], 'playback_enabled': self.host.music.enabled,
                'selection': 'first_search_result',
                'note': '已自动选择搜索结果第一条并安排播放，不再询问序号；已排队不等于已确认播出'}
