"""NetEase request queue over Awoo's published JSONL connector (protocol v1).

The native player/search implementation is reused unchanged. This host owns only
requests, exact-version selection and queue scheduling; it never reads cookies.
"""
import json
import copy
import queue
import re
import subprocess
import threading
import time
import uuid
import os
from pathlib import Path
from rules_config import RuleConfig


class DuplicateSongError(ValueError):
    def __init__(self, status, message, track, position=None):
        super().__init__(message)
        self.status, self.track, self.position = status, track, position


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def parse_request(text):
    """Use explicit delimiters for unambiguous singer matching; preserve versions."""
    value = re.sub(r'^点歌\s*', '', text.strip())
    if not value or len(value) > 180:
        raise ValueError('请输入歌名；可用：点歌 晴天 / 周杰伦，或 点歌 id=186016')
    match = re.fullmatch(r'(?:id\s*=\s*)?([0-9]{6,19})', value, re.I)
    if match:
        return {'query': 'id=' + match[1], 'song_id': match[1], 'title': '', 'artist': ''}
    for separator in (' / ', '/', ' - ', '—', '｜', '|'):
        if separator in value:
            title, artist = value.split(separator, 1)
            if not title.strip() or not artist.strip():
                raise ValueError('歌名和歌手不能为空')
            return {'query': title.strip() + ' ' + artist.strip(), 'title': title.strip(), 'artist': artist.strip()}
    return {'query': value, 'title': value, 'artist': ''}


class NeteaseBridge:
    def __init__(self, root):
        self.root = Path(root)
        self.process = None
        self.snapshot = {}
        self.capabilities = {}
        self.error = ''
        self.requests = {}
        self.lock = threading.RLock()
        self.serial = threading.RLock()
        self.started = 0

    def _read(self, child):
        try:
            for line in child.stdout:
                if len(line) > 2_000_000:
                    continue
                try:
                    item = json.loads(line.lstrip('\ufeff'))
                except ValueError:
                    continue
                if item.get('type') == 'event':
                    if item.get('snapshot'):
                        with self.lock:
                            self.snapshot = {**item['snapshot'], 'capabilities': item['snapshot'].get('capabilities') or self.capabilities}
                    continue
                with self.lock:
                    waiting = self.requests.pop(str(item.get('id')), None)
                if waiting:
                    waiting.put(item)
        finally:
            with self.lock:
                if self.process is child:
                    self.snapshot = {**self.snapshot, 'connected': False}
                    self.error = '网易云连接器已退出，请重新连接'
                    for waiting in self.requests.values():
                        waiting.put({'ok': False, 'error': self.error})
                    self.requests.clear()

    def start(self):
        with self.serial:
            if self.process and self.process.poll() is None:
                return
            installed = self.root / 'vendor/netease/installed.json'
            if not installed.exists():
                raise RuntimeError('网易云连接器尚未安装，请运行 setup_integrations.py')
            info = json.loads(installed.read_text(encoding='utf-8'))
            executable = (self.root / info['executable']).resolve()
            if not executable.is_relative_to((self.root / 'vendor/netease').resolve()):
                raise RuntimeError('连接器路径无效')
            log = open(self.root / '.runtime/netease-connector.log', 'a', encoding='utf-8')
            try:
                self.process = subprocess.Popen([str(executable)], cwd=executable.parent,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, encoding='utf-8', errors='replace', bufsize=1,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            finally:
                log.close()
            self.started += 1
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
            hello = self.request('ping', timeout=18, start=False)
            self.capabilities = hello.get('capabilities') or {}
            if 'snapshot-events-v1' in hello.get('features', []):
                self.request('subscribe', eventProtocolVersion=1, timeout=8, start=False)

    def request(self, action, timeout=25, start=True, **payload):
        with self.serial:
            if start:
                self.start()
            request_id = uuid.uuid4().hex
            waiting = queue.Queue(1)
            with self.lock:
                self.requests[request_id] = waiting
            try:
                self.process.stdin.write(json.dumps({'id': request_id, 'action': action, 'player': 'netease', **payload}, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
                response = waiting.get(timeout=timeout)
                if not response.get('ok'):
                    raise RuntimeError(str(response.get('error') or '连接器拒绝了操作')[:350])
                result = response.get('result')
                if action == 'probe' and isinstance(result, dict):
                    with self.lock:
                        self.snapshot = {**result, 'capabilities': result.get('capabilities') or self.capabilities}
                elif isinstance(result, dict) and result.get('snapshot'):
                    with self.lock:
                        self.snapshot = {**result['snapshot'], 'capabilities': result['snapshot'].get('capabilities') or self.capabilities}
                        result['snapshot'] = self.snapshot
                self.error = ''
                return result
            except queue.Empty:
                self.error = '网易云操作超时，结果未确认；自动播放已停止，请检查客户端'
                raise RuntimeError(self.error) from None
            finally:
                with self.lock:
                    self.requests.pop(request_id, None)

    def close(self):
        with self.serial:
            if self.process and self.process.poll() is None:
                try:
                    self.request('shutdown', timeout=25, start=False)
                    self.process.wait(timeout=5)
                except Exception:
                    self.process.terminate()

    @staticmethod
    def player_executable():
        """Resolve the installed app; never inspect browser/account credentials."""
        candidates = []
        if os.name == 'nt':
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for key_path in (r'Software\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe',
                                 r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\cloudmusic.exe'):
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            candidates.append(winreg.QueryValueEx(key, '')[0])
                    except OSError:
                        pass
                for key_path in (r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                                 r'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'):
                    try:
                        with winreg.OpenKey(hive, key_path) as parent:
                            for i in range(winreg.QueryInfoKey(parent)[0]):
                                try:
                                    with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as key:
                                        if '网易云音乐' in str(winreg.QueryValueEx(key, 'DisplayName')[0]):
                                            candidates.append(winreg.QueryValueEx(key, 'DisplayIcon')[0])
                                except OSError:
                                    continue
                    except OSError:
                        pass
        for value in candidates:
            value = str(value).split(',')[0].strip().strip('"')
            path = Path(value)
            if path.name.lower() == 'cloudmusic.exe' and path.is_file():
                return path
        raise RuntimeError('没有找到网易云电脑版，请先安装并登录网易云，再直接点歌')

    def ensure_player(self):
        with self.serial:
            snapshot = self.request('probe')
            if snapshot.get('connected'):
                return snapshot
            executable = self.player_executable()
            options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
            if os.name == 'nt':
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                options['startupinfo'] = startup
            subprocess.Popen([str(executable)], cwd=executable.parent,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                time.sleep(.5)
                snapshot = self.request('probe', timeout=5)
                if snapshot.get('connected'):
                    return snapshot
            raise RuntimeError('已尝试打开网易云，但播放器仍未就绪；请在网易云完成登录后再点歌')


class MusicService:
    def __init__(self, root, bridge=None, rules=None, clock=time.time):
        self.root = Path(root)
        self.rulebook = rules or RuleConfig(root)
        self.clock = clock
        self.path = self.root / '.runtime/music-state.json'
        self.bridge = bridge or NeteaseBridge(root)
        self.lock = threading.RLock()
        saved = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        self.queue = saved.get('queue', [])
        self.current = saved.get('current')
        self.recent = saved.get('recent', [])
        # Reopening the app never resumes playback or re-inserts an uncertain song,
        # but a request that is still waiting must keep being served: viewers are
        # watching it and the queue would otherwise sit still until the next request.
        # 'activating' means the native outcome is unknown, so it stays paused; so
        # does a 'staged' item that was never confirmed by the player.
        self.enabled = any(t.get('state') == 'waiting' or
                           (t.get('state') == 'staged' and str(t.get('native_outcome', '')).lower() == 'verified')
                           for t in self.queue)
        self.error = ''
        self.deadline = 0
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.worker = None

    def save(self):
        atomic_json(self.path, {'queue': self.queue, 'current': self.current,
                               'recent': self.recent[-30:]})

    def clear_pending(self):
        """Discard host requests; do not mutate the user's native music playlist."""
        with self.lock:
            count = len(self.queue)
            self.enabled, self.error, self.deadline = False, '', 0
            self.queue, self.current = [], None
            self.save()
            return count

    def status(self):
        with self.lock, self.bridge.lock:
            limits = self.rulebook.snapshot()['music']
            return copy.deepcopy({'enabled': self.enabled, 'error': self.error or self.bridge.error,
                'player': self.bridge.snapshot, 'queue': self.queue, 'current_request': self.current,
                'recent': self.recent[-8:], 'queue_count': len(self.queue), 'limits': limits})

    def _finish_current(self):
        if self.current:
            self.recent.append({**self.current, 'finished_at': self.clock()})
            self.current = None

    def _check_capacity(self, user_id, limits):
        if len(self.queue) >= limits['max_pending']:
            raise ValueError('待播队列已满（'+str(limits['max_pending'])+'首），等播出一些再点吧')
        if sum(str(t['user_id']) == str(user_id) for t in self.queue) >= limits['max_pending_per_user']:
            raise ValueError('每位观众最多同时排队'+str(limits['max_pending_per_user'])+'首歌')

    def connect(self):
        with self.lock:
            snapshot = self.bridge.request('probe')
            if not snapshot.get('connected'):
                raise RuntimeError(snapshot.get('status') or '请打开网易云电脑版、登录并播放一首歌，再点击连接')
            self.error = ''
            return self.status()

    def prepare_playback(self):
        """Called inside the music tool: connect/launch without another UI step."""
        try:
            snapshot = self.bridge.ensure_player() if hasattr(self.bridge, 'ensure_player') else self.bridge.request('probe')
            if not snapshot.get('connected'):
                raise RuntimeError(snapshot.get('status') or '网易云未就绪，请先完成网易云登录后再点歌')
        except Exception as exc:
            self.error = str(exc)[:350]
            raise
        playing_id = str((snapshot.get('current') or {}).get('id', ''))
        if self.current and playing_id != str(self.current['track']['id']) and not (
                self.queue and playing_id == str(self.queue[0]['track']['id'])):
            self._finish_current()
        self.error = ''

    def start_requested_queue(self):
        # Only a successful request starts playback; opening the page does not.
        self.enabled = True
        if self.queue and self.queue[0]['state'] == 'activating' and not self.deadline:
            self.deadline = time.monotonic()+25  # observe an old operation, never reinsert it
        self.error = ''
        self.wake_event.set()

    def order(self, text, user_id='local', nickname='测试观众', request=None, automatic=False):
        request = request or parse_request(text)
        with self.lock:
            limits = self.rulebook.snapshot()['music']
            self._check_capacity(user_id, limits)
            if automatic:
                self.prepare_playback()
            tracks = self.bridge.request('search', query=request['query']) or []
            tracks = [t for t in tracks if isinstance(t, dict) and t.get('id')][:15]
            if not tracks:
                raise ValueError('网易云没有搜到这首歌')
            # User policy: preserve search ranking and play its first result.
            result = self._enqueue(tracks[0], user_id, nickname)
            if automatic:
                self.start_requested_queue()
            return result

    def _enqueue(self, track, user_id, nickname):
        limits = self.rulebook.snapshot()['music']
        self._check_capacity(user_id, limits)
        track_id = str(track['id'])
        native_current = (self.bridge.snapshot.get('current') or {}).get('id') if self.bridge.snapshot.get('connected') else None
        if native_current is not None and str(native_current) == track_id:
            raise DuplicateSongError('already_playing', '这首歌已经在播放，本次没有重复加入', track)
        queued = next((i for i,t in enumerate(self.queue,1) if str(t['track']['id']) == track_id), None)
        if queued is not None:
            raise DuplicateSongError('already_queued', '这首歌已经在排队，待播第'+str(queued)+'首，本次没有重复加入', track, queued)
        if self.current and str(self.current['track']['id']) == track_id:
            raise DuplicateSongError('already_requested', '这首歌已经点过，播放器当前状态待确认，本次没有重复加入', track)
        cooldown = limits['replay_cooldown_seconds']
        if cooldown and any(str(item['track']['id']) == track_id and item.get('finished_at') is not None and self.clock()-item['finished_at'] < cooldown for item in self.recent):
            raise ValueError('这首歌刚放过，'+str(cooldown)+'秒内不重复点播，换一首吧')
        item = {'id': uuid.uuid4().hex, 'track': track, 'user_id': str(user_id),
                'nickname': str(nickname)[:40], 'state': 'waiting', 'created': self.clock()}
        self.queue.append(item)
        self.save()
        return {'status': 'queued', 'item': item, 'position': len(self.queue), 'queue_count': len(self.queue),
                'queue_limit': limits['max_pending'], 'message': '已加入点歌队列：' + track['title'] + ' · ' + track['artist']}

    def control(self, action, item_id=None):
        with self.lock:
            if action == 'start':
                self.connect()
                snapshot = self.bridge.snapshot
                actual = str((snapshot.get('current') or {}).get('id', ''))
                native_next = str((snapshot.get('next') or {}).get('id', ''))
                if any(t['state'] != 'waiting' and not (
                        actual == str(t['track']['id']) or
                        (t['state'] == 'staged' and t.get('native_outcome') == 'verified' and native_next == str(t['track']['id']))) for t in self.queue):
                    raise ValueError('存在已交给网易云的歌曲，请核对当前播放后移除该项，再开始')
                self.enabled, self.error = True, ''
            elif action == 'stop':
                self.enabled = False
                # A staged native song remains in the player's queue; no false cancellation.
            elif action in ('pause', 'resume'):
                result = self.bridge.request('execute', command='Pause' if action == 'pause' else 'Resume')
                if str(result.get('outcome', '')).lower() not in ('accepted', 'applied', 'verified'):
                    raise RuntimeError(result.get('message') or '播放器未确认控制操作')
            elif action == 'skip':
                result = self.bridge.request('execute', command='Next')
                if str(result.get('outcome', '')).lower() not in ('accepted', 'applied', 'verified'):
                    raise RuntimeError(result.get('message') or '播放器未确认切歌')
            elif action == 'remove':
                item = next((t for t in self.queue if t['id'] == item_id), None)
                if not item:
                    raise ValueError('排队歌曲不存在')
                if item['state'] != 'waiting':
                    raise ValueError('歌曲已交给网易云，请在网易云播放列表中处理；后台不假装撤回')
                self.queue.remove(item)
            elif action == 'reconcile':
                if self.enabled:
                    raise ValueError('请先停止自动播放，再核对队列')
                # Only the operator can acknowledge an uncertain native mutation.
                self.queue = [t for t in self.queue if t['state'] == 'waiting']
                self.current = None
                self.error = ''
            else:
                raise ValueError('未知音乐操作')
            self.save()
            return self.status()

    def _execute_once(self, command, item):
        # Persist before a native side effect: no blind retry after timeout/restart.
        item['state'] = 'activating' if command == 'PlaySelected' else 'staged'
        self.save()
        result = self.bridge.request('execute', command=command, track=item['track'])
        if str(result.get('outcome', '')).lower() not in ('accepted', 'applied', 'verified'):
            raise RuntimeError(result.get('message') or '网易云没有确认操作，自动播放已暂停')
        item['native_outcome'] = str(result['outcome']).lower()
        self.save()

    def tick(self):
        with self.lock:
            if not self.enabled:
                # Observe the native player even when scheduling has stopped. This
                # never executes playback commands or resumes an uncertain queue.
                installed = self.root / 'vendor/netease/installed.json'
                if installed.exists() or (getattr(self.bridge, 'process', None) and self.bridge.process.poll() is None):
                    snapshot = self.bridge.request('probe', timeout=10)
                else:
                    snapshot = self.bridge.snapshot
                playing_id = str((snapshot.get('current') or {}).get('id', ''))
                if snapshot.get('connected') and self.queue and playing_id == str(self.queue[0]['track']['id']):
                    self._finish_current()
                    self.current = self.queue.pop(0)
                    self.current.update(state='playing', started_at=self.clock())
                    self.deadline = 0
                    self.save()
                if snapshot.get('connected') and playing_id and self.current and playing_id != str(self.current['track']['id']):
                    self._finish_current()
                    self.save()
                return
            snapshot = self.bridge.request('probe', timeout=10)
            if not snapshot.get('connected'):
                raise RuntimeError('网易云已断开，点歌队列保留')
            playing_id = str((snapshot.get('current') or {}).get('id', ''))
            if self.queue and playing_id == str(self.queue[0]['track']['id']):
                if self.current:
                    self._finish_current()
                self.current = self.queue.pop(0)
                self.current['state'] = 'playing'
                self.current['started_at'] = self.clock()
                self.deadline = 0
                self.save()
            if not self.queue:
                if self.current and playing_id != str(self.current['track']['id']):
                    self._finish_current()
                    self.enabled = False
                    self.save()
                return
            head = self.queue[0]
            if head['state'] == 'activating':
                if time.monotonic() > self.deadline:
                    raise RuntimeError('点歌未在25秒内成为当前曲目；可能无法播放，需人工核对')
                return
            if head['state'] == 'staged':
                if snapshot.get('nextGuardState') in ('terminalFailure', 'expired'):
                    raise RuntimeError('网易云下一首接管失败，请核对曲库权限或歌曲版本')
                anchor = head.get('anchor_id') or (str(self.current['track']['id']) if self.current else '')
                # A full-length song may still be playing. Only time the handoff
                # once the observed current track has actually left that anchor.
                if anchor and playing_id and playing_id != anchor:
                    changed_at = head.setdefault('transition_observed_at', self.clock())
                    if self.clock()-changed_at >= self.rulebook.snapshot()['music']['handoff_grace_seconds']:
                        raise RuntimeError('网易云未切到已安排的点歌，接管暂停，待播保留；未重复插入或覆盖原歌单')
                else:
                    # Still parked on the anchor track. If the native player is
                    # paused (or finished and stopped advancing), the handoff never
                    # arrives and the request would stall forever, which looks like
                    # "点歌没反应 / 一直在放同一首". Nudge it: wake the player first,
                    # then play the request directly once we have waited long enough.
                    limits = self.rulebook.snapshot()['music']
                    # Fall back to the queued time so requests that were already
                    # stalled before this logic existed recover on the next tick
                    # instead of starting a fresh wait.
                    started = head.get('staged_wait_started_at') or head.get('created') or self.clock()
                    head['staged_wait_started_at'] = started
                    waited = self.clock()-started
                    resume_after = float(limits.get('staged_resume_after_seconds') or 0)
                    timeout = float(limits.get('staged_wait_timeout_seconds') or 0)
                    capabilities = snapshot.get('capabilities') or getattr(self.bridge, 'capabilities', {}) or {}
                    if timeout and waited >= timeout:
                        # Stop waiting for a handoff that is not coming: play it now.
                        head.pop('staged_wait_started_at', None)
                        head.pop('resume_attempted_at', None)
                        self.deadline = time.monotonic()+25
                        self._execute_once('PlaySelected', head)
                        self.save()
                        return
                    if (resume_after and waited >= resume_after and capabilities.get('resume')
                            and self.clock()-float(head.get('resume_attempted_at') or 0) >= resume_after):
                        # Resume is idempotent while a track is playing, so nudging a
                        # paused player is safe and costs nothing. Repeat every
                        # resume_after seconds until the handoff actually happens.
                        try:
                            self.bridge.request('execute', command='Resume')
                        except Exception:
                            pass
                        head['resume_attempted_at'] = self.clock()
                        self.save()
                        return
                    if head.pop('transition_observed_at', None) is not None:
                        self.save()
                return
            if self.current and playing_id and playing_id != str(self.current['track']['id']):
                # Respect a manual switch; never immediately replace the user's song.
                raise RuntimeError('网易云切到了队列以外的歌曲，已暂停自动接管，请核对队列')
            if not snapshot.get('current'):
                self.deadline = time.monotonic()+25
                self._execute_once('PlaySelected', head)
            else:
                if not playing_id:
                    raise RuntimeError('当前歌曲尚未识别，点歌保留，暂不打断网易云播放')
                capabilities = snapshot.get('capabilities') or getattr(self.bridge, 'capabilities', {}) or {}
                if not capabilities.get('insertNext'):
                    raise RuntimeError('播放器当前未提供下一首插入能力，点歌接管暂停，队列已保留')
                head['anchor_id'] = playing_id
                # Retain what the native playlist would have played, for audit.
                # InsertNext preserves this list; no queue rebuild or PlaySelected.
                head['background_next'] = copy.deepcopy(snapshot.get('next'))
                self._execute_once('InsertNext', head)
                # NetEase InsertNext already arms its native next-song guard.

    def run(self):
        def loop():
            while not self.stop_event.is_set():
                self.wake_event.wait(2)
                self.wake_event.clear()
                if self.stop_event.is_set():
                    break
                try:
                    self.tick()
                except Exception as exc:
                    with self.lock:
                        self.error, self.enabled = str(exc)[:350], False
                        self.save()
        self.worker = threading.Thread(target=loop, name='music-queue', daemon=True)
        self.worker.start()

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        self.bridge.close()
