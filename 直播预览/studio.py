"""Douyin-only event rules, gift settlement and durable playback outbox.

No Super Chat branch. All prices are raw diamondCount units, never estimated RMB.
"""
import copy
import json
import queue
import random
import re
import threading
import time
import uuid
from pathlib import Path
from music import atomic_json
from income import GiftIncome
from presentation import Presentation
from music_commands import SongCommands, is_song_command
from rules_config import RuleConfig, STUDIO_DEFAULTS as DEFAULTS

def integer(value, default=0, maximum=10 ** 12):
    try:
        if isinstance(value, bool):
            return default
        result = int(value)
        return min(max(0, result), maximum)
    except (TypeError, ValueError, OverflowError):
        return default

class EventRules:
    """Pure rules + atomic state; tests can advance a supplied clock without APIs."""

    def __init__(self, root, clock=time.time, rulebook=None):
        self.root, self.clock = (Path(root), clock)
        self.path = self.root / '.runtime/studio-state.json'
        self.rulebook = rulebook or RuleConfig(self.root)
        self.config_path = self.rulebook.path
        self.config = copy.deepcopy(DEFAULTS)
        self.lock = threading.RLock()
        self.config_error = ''
        self.music_enabled = lambda: True
        self._config_stamp = None
        if not self.config_path.exists() and (not self.rulebook.yaml_mode):
            atomic_json(self.config_path, self.config)
        self.reload_config()
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        for key, default in {'seen': {}, 'combos': {}, 'settled': {}, 'catalog': {}, 'jobs': [], 'history': [], 'user_last': {}, 'chat_last': 0, 'music_inbox': [], 'recent_chat_text': {}}.items():
            self.state.setdefault(key, default)
        self.income = GiftIncome(self.state.setdefault('income', {}), self.clock)
        self.presentation = Presentation(self.state.setdefault('presentation', {}), self.clock, archive_limit=lambda: self.rulebook.snapshot()['live_tools']['history_retention_messages'])
        self.state.setdefault('last_activity_at', self.clock())
        self.state.setdefault('welcome_last_at', 0)
        self.state.setdefault('welcome_users', {})
        self.state.setdefault('welcome_index', 0)
        for job in self.state['jobs']:
            if job['state'] == 'generating':
                job['state'] = 'queued'
        self.state['jobs'][:] = [j for j in self.state['jobs'] if True]
        for legacy in self.state.pop('music_inbox', []):
            self._job(legacy, 10)
        self.save()

    def reload_config(self):
        with self.lock:
            self.rulebook.refresh()
            self.config_error = self.rulebook.error
            if not self.config_error and self._config_stamp != self.rulebook.stamp:
                self.config = self.rulebook.snapshot()['studio']
                self._config_stamp = self.rulebook.stamp

    def save(self):
        atomic_json(self.path, self.state)

    def log(self, kind, text, **extra):
        self.state['history'].append({'kind': kind, 'text': str(text)[:500], 'time': self.clock(), **extra})
        del self.state['history'][:-80]

    def _job(self, event, priority):
        job = {'id': uuid.uuid4().hex, 'state': 'queued', 'created': self.clock(), 'event': event, 'priority': priority, 'attempts': 0}
        self.state['jobs'].append(job)
        return job

    def sampled_chat(self, event):
        return not event.get('simulation') or False

    def ingest(self, event):
        if not isinstance(event, dict) or event.get('platform', 'douyin') != 'douyin':
            raise ValueError('仅接受抖音事件')
        kind = event.get('type')
        if kind not in ('chat', 'gift', 'member', 'roomStatus'):
            return {'status': 'ignored', 'reason': '此版本仅处理弹幕与礼物'}
        event_id = str(event.get('id') or '')[:180]
        if not event_id:
            raise ValueError('事件缺少稳定 ID，不能安全去重')
        user = event.get('user') or {}
        data = event.get('data') or {}
        if not isinstance(user, dict) or not isinstance(data, dict):
            raise ValueError('事件 user/data 格式无效')
        if kind != 'roomStatus' and (not user.get('id')):
            raise ValueError('事件缺少用户 ID')
        now = self.clock()
        with self.lock:
            self.reload_config()
            seen = self.state['seen']
            dedup = str(event.get('roomId', 'local')) + ':' + event_id
            if dedup in seen:
                return {'status': 'duplicate'}
            seen[dedup] = now
            if len(seen) > 20000:
                self.state['seen'] = dict(sorted(seen.items(), key=lambda x: x[1])[-15000:])
            event = copy.deepcopy(event)
            event['user'] = {'id': str(user.get('id', '')), 'nickname': str(user.get('nickname') or '朋友')[:40]}
            if kind in ('chat', 'gift'):
                self.state['last_activity_at'] = now
                self.state['jobs'][:] = [j for j in self.state['jobs'] if not (j['state'] == 'queued' and j['event']['type'] == 'welcome')]
            if kind == 'roomStatus':
                self.log('room', data.get('message') or data.get('status', ''))
                result = {'status': 'room'}
            elif kind == 'member':
                result = self._welcome(event)
            elif kind == 'gift':
                result = self._gift(event)
            else:
                text = str(data.get('content') or '').strip()[:500]
                if not text:
                    result = {'status': 'ignored', 'reason': '空弹幕'}
                else:
                    event['data'] = {'content': text}
                    self.presentation.incoming(event)
                    if re.fullmatch('(\\d)\\1{3,}', re.sub('\\s+', '', text)):
                        self.log('selection', '纯数字重复刷屏已略过，不调用模型')
                        self.save()
                        return {'status': 'ignored', 'message': '重复数字已显示在弹幕区，不生成回复'}
                    policy = self.rulebook.snapshot()['music']
                    if self.music_enabled() and policy['direct_commands'] and is_song_command(text):
                        commands = [j for j in self.state['jobs'] if j['event']['type'] == 'song_command']
                        mine = [j for j in commands if j['event'].get('roomId') == event.get('roomId') and j['event']['user']['id'] == event['user']['id']]
                        if len(commands) >= policy['command_max_pending'] or len(mine) >= policy['command_max_pending_per_user']:
                            message = '点歌正在处理，请稍等再点'
                            self.state['music_command'] = {'time': now, 'status': 'error', 'message': message}
                            result = {'status': 'rejected', 'message': message}
                        else:
                            self._job({**event, 'type': 'song_command'}, 20)
                            result = {'status': 'song_command_queued', 'message': '点歌处理中，结果会显示在歌单，不播报语音'}
                        self.save()
                        return result
                    jobs = self.state['jobs']
                    # 同文去重：同一观众短窗口内重发一模一样的弹幕（误双击/以为没发出）只回一次。
                    # 只拦普通弹幕；点歌命令有自己的去重，上面已提前返回。
                    text_key = str(event.get('roomId', 'local')) + ':' + str(event['user']['id']) + ':' + re.sub('\\s+', '', text)
                    text_seen = self.state['recent_chat_text'].get(text_key)
                    if text_seen is not None and now - text_seen < self.config['chat_duplicate_window']:
                        self.log('chat', event['user']['nickname'] + ' 短时间内重发同一条弹幕，已忽略：' + text)
                        self.save()
                        return {'status': 'duplicate', 'message': '同一条弹幕刚发过，已忽略重复发送'}
                    self.state['recent_chat_text'][text_key] = now
                    if len(self.state['recent_chat_text']) > 2000:
                        self.state['recent_chat_text'] = dict(sorted(self.state['recent_chat_text'].items(), key=lambda kv: kv[1])[-1000:])
                    if self.sampled_chat(event):
                        jobs[:] = [j for j in jobs if not (j['state'] == 'queued' and j['event']['type'] == 'chat' and self.sampled_chat(j['event']) and (j['event']['user']['id'] == event['user']['id']))]
                    chats = [j for j in jobs if j['state'] == 'queued' and j['event']['type'] == 'chat']
                    while len(chats) >= self.config['chat_max_pending']:
                        oldest = min(chats, key=lambda j: j['created'])
                        jobs.remove(oldest)
                        chats.remove(oldest)
                    event['data'] = {'content': text}
                    self._job(event, 10)
                    self.log('chat', event['user']['nickname'] + '：' + text)
                    result = {'status': 'queued'}
            self.save()
            return result

    def _welcome(self, event):
        now, policy = (self.clock(), self.rulebook.snapshot()['welcome'])
        key = str(event.get('roomId', 'local')) + ':' + event['user']['id']
        if not policy['enabled'] or now - self.state['last_activity_at'] < policy['idle_seconds'] or now - self.state['welcome_last_at'] < policy['gap_seconds'] or (now - self.state['welcome_users'].get(key, -10 ** 12) < policy['same_user_seconds']):
            return {'status': 'ignored', 'reason': '进场欢迎限频或当前并非冷场'}
        if any((j['state'] != 'failed' for j in self.state['jobs'])):
            return {'status': 'ignored', 'reason': '优先处理弹幕与礼物'}
        messages = policy['messages']
        reply = messages[self.state['welcome_index'] % len(messages)].format(nickname=event['user']['nickname'])
        self.state['welcome_index'] += 1
        self.state['welcome_last_at'] = now
        self.state['welcome_users'][key] = now
        self.state['welcome_users'] = {k: v for k, v in self.state['welcome_users'].items() if now - v < max(policy['same_user_seconds'], 60)}
        event['data'] = {'content': '进入直播间'}
        self.presentation.incoming(event)
        self._job({**event, 'type': 'welcome', 'data': {'content': '进入直播间', 'reply': reply}}, 1)
        return {'status': 'welcome_queued'}

    def _gift(self, event):
        data, user = (event['data'], event['user'])
        now = self.clock()
        gift_id = str(data.get('giftId') or data.get('giftName') or 'unknown')[:100]
        catalog_key = ('simulation:' if event.get('simulation') else 'live:') + gift_id
        cached = self.state['catalog'].get(catalog_key, {})
        name = str(data.get('giftName') or cached.get('name') or '礼物')[:50]
        reference = self.config['simulation_gift_prices'].get(name) if event.get('simulation') else None
        price = integer(data.get('diamondCount')) or (reference if reference is not None else cached.get('price'))
        self.state['catalog'][catalog_key] = {'name': name, 'price': price, 'seen': now}
        count = max(1, integer(data.get('repeatCount')), integer(data.get('comboCount'))) * max(1, integer(data.get('groupCount')))
        group = str(data.get('groupId') or '')
        if group in ('0', 'None'):
            group = ''
        base = ':'.join([str(event.get('roomId', 'local')), user['id'], gift_id])
        combo = data.get('combo') is True
        if group:
            key = base + ':' + group
        elif data.get('traceId'):
            key = base + ':trace:' + str(data['traceId'])[:150]
        elif combo:
            key = next((k for k, v in self.state['combos'].items() if v['base'] == base and v['updated'] > now - 5), base + ':fallback:' + event['id'])
        else:
            key = base + ':event:' + event['id']
        old = self.state['combos'].get(key)
        settled = self.state['settled'].get(key, {}).get('count', 0)
        if count <= settled:
            simulation = bool(event.get('simulation'))
            if price is not None and self.income.has_receipt(key, simulation):
                self.income.record(key, count, price, simulation=simulation)
            return {'status': 'duplicate_combo'}
        value = {'base': base, 'key': key, 'room_id': str(event.get('roomId', 'local')), 'user': user, 'gift_id': gift_id, 'gift': name, 'unit_diamonds': price, 'count': max(count, old['count'] if old else 0), 'simulation': event.get('simulation', False), 'voice': event.get('voice'), 'income_day': old.get('income_day', self.income.day(old['updated'])) if old else self.income.day(), 'updated': now, 'ended': data.get('repeatEnd') is True or not combo}
        self.state['combos'][key] = value
        self.presentation.incoming(event, gift_key=key, gift_name=name, count=value['count'])
        if value['ended']:
            self._settle(key)
        return {'status': 'gift_pending' if not value['ended'] else 'gift_queued'}

    def _settle(self, key):
        gift = self.state['combos'].pop(key)
        prior = self.state['settled'].get(key, {}).get('count', 0)
        self.state['settled'][key] = {'count': gift['count'], 'time': self.clock()}
        delta = gift['count'] - prior
        self.income.record(key, gift['count'], gift['unit_diamonds'], simulation=gift.get('simulation', False), day=gift.get('income_day'), baseline_count=prior)
        if delta <= 0:
            return
        total = gift['unit_diamonds'] * gift['count'] if gift['unit_diamonds'] is not None else None
        board = self.state.setdefault('feeders', {})
        fid = str(gift['user'].get('id') or gift['user'].get('nickname') or 'anon')[:60]
        entry = board.setdefault(fid, {'name': str(gift['user'].get('nickname') or fid)[:20], 'coins': 0, 'gifts': 0, 'updated': self.clock()})
        entry['name'] = str(gift['user'].get('nickname') or entry['name'])[:20]
        entry['coins'] += total or 0
        entry['gifts'] += delta
        entry['updated'] = self.clock()
        tier = self.config['gift_tiers'][0]
        if total is not None:
            tier = max((t for t in self.config['gift_tiers'] if total >= t['min_diamonds']), key=lambda t: t['min_diamonds'])
        gift_rule = self.rulebook.snapshot()['gift_rules'].get(gift['gift'], {})
        special = gift_rule.get('audio_key')
        if special == 'none':
            special = None
        chosen_template = gift_rule.get('thank_template', tier['template'])
        reply = chosen_template.format(nickname=gift['user']['nickname'], gift=gift['gift'], count=delta)
        event = {'id': 'gift:' + key + ':' + str(gift['count']), 'type': 'gift', 'user': gift['user'], 'roomId': gift.get('room_id', gift['base'].split(':', 1)[0]), 'data': {**gift, 'count': delta, 'total_diamonds': total, 'tier': tier['name'] if total is not None else '价格未知', 'combo_total_count': gift['count'], 'tier_guidance': tier.get('guidance', '') if total is not None else '', 'reply': reply, 'special': special}, 'simulation': gift.get('simulation', False)}
        if gift.get('voice') is not None:
            event['voice'] = gift['voice']
        self._job(event, 100 + min(total or 0, 1000000) / 1000000)
        self.log('gift', reply, count=delta, total_diamonds=total, tier=event['data']['tier'])

    def due(self):
        with self.lock:
            self.reload_config()
            now = self.clock()
            for key, gift in list(self.state['combos'].items()):
                if now - gift['updated'] >= self.config['gift_combo_wait_seconds']:
                    self._settle(key)
            self.state['settled'] = {k: v for k, v in self.state['settled'].items() if v['time'] > now - 86400}
            max_age = self.config['chat_max_age_seconds']
            self.state['jobs'] = [j for j in self.state['jobs'] if not (max_age > 0 and j['event']['type'] == 'chat' and self.sampled_chat(j['event']) and (j['state'] == 'queued') and (now - j['created'] > max_age))]
            welcome = self.rulebook.snapshot()['welcome']
            self.state['jobs'] = [j for j in self.state['jobs'] if not (j['state'] == 'queued' and j['event']['type'] == 'welcome' and (now - j['created'] > welcome['max_age_seconds']))]
            self.save()

    def next_job(self):
        with self.lock:
            self.reload_config()
            now = self.clock()
            jobs = sorted((j for j in self.state['jobs'] if j['state'] == 'queued' and j['event']['type'] != 'song_command'), key=lambda j: (-j['priority'], j['created']))
            for job in jobs:
                if job.get('retry_at', 0) > now:
                    continue
                if job['event']['type'] == 'chat' and self.sampled_chat(job['event']):
                    if now - self.state['chat_last'] < self.config['chat_global_gap_seconds']:
                        continue
                    if self.config['chat_selection'] == 'window_random':
                        batch = [j for j in self.state['jobs'] if j['state'] == 'queued' and j['event']['type'] == 'chat' and self.sampled_chat(j['event'])]
                        expired_window = {j['id'] for j in batch[:-self.config['chat_max_pending']]}
                        if expired_window:
                            self.state['jobs'][:] = [j for j in self.state['jobs'] if j['id'] not in expired_window]
                            batch = batch[-self.config['chat_max_pending']:]
                        max_age = self.config['chat_max_age_seconds']
                        eligible = [j for j in batch if j.get('retry_at', 0) <= now and (max_age <= 0 or now - j['created'] <= max_age) and (now - self.state['user_last'].get(j['event']['user']['id'], 0) >= self.config['chat_user_gap_seconds'])]
                        if not eligible:
                            continue
                        if len(batch) < self.config['chat_max_pending'] and now - min((j['created'] for j in batch)) < self.config['chat_window_wait_seconds']:
                            continue
                        job = random.choice(eligible)
                        discarded = set()
                        if self.config['chat_discard_before_selected']:
                            discarded = {j['id'] for j in batch[:batch.index(job)]}
                            self.state['jobs'][:] = [j for j in self.state['jobs'] if j['id'] not in discarded]
                        self.log('selection', '滑动窗口随机选取 1 条，跳过较早候选 ' + str(len(discarded)) + ' 条，保留候选 ' + str(len(batch) - len(discarded) - 1) + ' 条')
                    if now - self.state['user_last'].get(job['event']['user']['id'], 0) < self.config['chat_user_gap_seconds']:
                        continue
                job['state'] = 'generating'
                self.save()
                return copy.deepcopy(job)
            return None

class Studio:

    def __init__(self, root, music, render, rulebook=None, command_tools=None):
        self.rules = EventRules(root, rulebook=rulebook)
        self.music, self.render = (music, render)
        self.song_commands = SongCommands(music, command_tools) if music is not None else None
        self.playback_blocked = False
        self.voice = True
        self.error = ''
        self.stop_event = threading.Event()
        self.owner = None
        self.owner_role = 'preview'
        self.live_waiting_until = 0
        self.clients = {}
        self.owner_audio_ready = True
        self.rules.state.setdefault('voice_retries', [])
        self.rules.state.setdefault('playback_epoch', 0)
        self.audio_warning = ''
        self.owner_until = 0
        self.lease = None

    def ingest(self, event):
        with self.rules.lock:
            result = self.rules.ingest(event)
            return {k: v for k, v in result.items() if k != 'event'}

    def status(self):
        with self.rules.lock:
            self.rules.reload_config()
            clip = any(((self.rules.root / 'assets/gift_audio' / ('meow_paw' + e)).exists() for e in ('.mp3', '.wav', '.ogg')))
            return {'playback_blocked': self.playback_blocked, 'voice': self.voice, 'audio_warning': self.audio_warning, 'voice_retries': len(self.rules.state['voice_retries']), 'playback_epoch': self.rules.state['playback_epoch'], 'error': self.rules.config_error or self.error, 'processing': any((j['state'] in ('generating', 'ready') for j in self.rules.state['jobs'])), 'pending': len(self.rules.state['jobs']), 'combos': len(self.rules.state['combos']), 'chat_selection': self.rules.config['chat_selection'], 'chat_window_size': self.rules.config['chat_max_pending'], 'chat_discard_before_selected': self.rules.config['chat_discard_before_selected'], 'gift_reply_mode': self.rules.config['gift_reply_mode'], 'history_retained_messages': len(self.rules.presentation.state['archive']), 'music_command': copy.deepcopy(self.rules.state.get('music_command')), 'history': copy.deepcopy(self.rules.state['history'][-30:]), 'tiers': self.rules.config['gift_tiers'], 'meow_clip': clip, 'config_file': self.rules.config_path.name, 'simulation_gift_prices': copy.deepcopy(self.rules.config['simulation_gift_prices']), 'income': self.rules.income.summary(), 'feeders': sorted(self.rules.state.get('feeders', {}).values(), key=lambda e: -e.get('coins', 0))[:8]}

    def presentation(self):
        with self.rules.lock:
            return self.rules.presentation.snapshot()

    def search_recent_chat(self, room_id, viewer_id, query, limit, only_me=False):
        with self.rules.lock:
            policy = self.rules.rulebook.snapshot()['live_tools']
            scoped = [row for row in self.rules.presentation.state['archive'] if row.get('room_id') == str(room_id) and (not only_me or row.get('user_id') == str(viewer_id))]
            query = query.strip().casefold()
            matches = [row for row in scoped if not query or query in (row.get('name', '') + ' ' + row['content']).casefold()]
            recent = matches[-min(limit, policy['history_max_results']):]
            return {'status': 'ok', 'message': '已查询本直播间近期已回应对话', 'count': len(recent), 'records': [{k: row[k] for k in ('role', 'name', 'content', 'time', 'type')} for row in recent], 'scope': '当前房间的已回应对话', 'history_is_complete': False, 'retention_messages': policy['history_retention_messages'], 'note': '只覆盖保留记录；清空画面不删除归档。旧版本未标房间的记录不返回。没有记录不能假装记得。'}

    def clear_presentation(self):
        with self.rules.lock:
            self.rules.presentation.clear()
            self.rules.save()
            return self.rules.presentation.snapshot()

    def clear_pending(self):
        with self.rules.lock:
            counts = {key: len(self.rules.state[key]) for key in ('jobs', 'combos', 'voice_retries')}
            for key in list(self.rules.state['combos']):
                self.rules._settle(key)
            self.rules.state.update(jobs=[], combos={}, voice_retries=[], music_command=None, user_last={}, chat_last=0, playback_epoch=self.rules.state['playback_epoch'] + 1)
            self.rules.presentation.state['current'] = None
            self.rules.presentation.changed()
            self.lease = None
            self.error = self.audio_warning = ''
            self.playback_blocked = False
            self.rules.log('session', '已清空待播回复、合并礼物与语音重试队列')
            self.rules.save()
            return counts

    def reset_income(self):
        with self.rules.lock:
            for key, gift in self.rules.state['combos'].items():
                self.rules.income.record(key, gift['count'], gift['unit_diamonds'], simulation=gift.get('simulation', False), day=gift.get('income_day'), baseline_count=self.rules.state['settled'].get(key, {}).get('count', 0))
            result = self.rules.income.reset()
            self.rules.save()
            return result

    def claim(self, client_id, ack=None, failed=False, voice=None, role='preview', audio_ready=True, failure_reason=''):
        if not isinstance(client_id, str) or not 5 <= len(client_id) <= 100:
            raise ValueError('播放器标识无效')
        if role not in ('preview', 'live'):
            raise ValueError('播放器角色无效')
        if type(audio_ready) is not bool:
            raise ValueError('audio_ready 必须是布尔值')
        with self.rules.lock:
            if voice is not None:
                if not isinstance(voice, bool):
                    raise ValueError('voice 必须为布尔值')
                self.voice = voice
            now = time.time()
            self.clients[client_id] = {'time': now, 'audio_ready': audio_ready}
            self.clients = {k: v for k, v in self.clients.items() if now - v['time'] < 15}
            other_audio = any((k != client_id and v['audio_ready'] for k, v in self.clients.items()))
            acknowledging = self.lease and ack == self.lease['id'] and (client_id == self.lease['client'])
            if self.lease and (not acknowledging) and (self.lease['until'] <= now or self.lease['client'] not in self.clients):
                self.lease = None
            if self.lease and self.lease['client'] != client_id:
                if role == 'live' and self.owner_role != 'live':
                    self.live_waiting_until = now + 2
                return {'response': None, 'another_player': True}
            if self.owner and self.owner != client_id and (self.owner_until > now) and (not self.lease):
                if role == 'live' and self.owner_role != 'live':
                    self.live_waiting_until = now + 2
                if self.lease or (self.owner_audio_ready and (not audio_ready or role != 'live' or self.owner_role == 'live')):
                    return {'response': None, 'another_player': True}
            self.owner, self.owner_until = (client_id, now + 15)
            self.owner_role = role
            self.owner_audio_ready = audio_ready
            if ack and self.lease and (self.lease['id'] == ack) and (self.lease['client'] == client_id):
                job = next((j for j in self.rules.state['jobs'] if j['id'] == ack), None)
                if job:
                    self.rules.state['jobs'].remove(job)
                    debug = getattr(self, 'debug', None)
                    if debug:
                        debug.update((job.get('result') or {}).get('debug_id'), playback='播放失败' if failed else '文字已展示' if not (job.get('result') or {}).get('audio_parts') and (not (job.get('result') or {}).get('audio')) else '已播完', playback_error=failure_reason if failed else None)
                    if job['event']['type'] == 'chat' and self.rules.sampled_chat(job['event']):
                        self.rules.state['chat_last'] = now
                        self.rules.state['user_last'][job['event']['user']['id']] = now
                if failed and job:
                    self._remember_voice(job, failure_reason or '浏览器未能播放语音')
                self.lease = None
                self.rules.save()
                if failed:
                    self.owner, self.owner_until = (None, 0)
                    self.live_waiting_until = 0
                    return {'response': None, 'audio_warning': self.audio_warning}
            if self.playback_blocked:
                return {'response': None}
            if role == 'preview' and self.live_waiting_until > now and (not self.lease):
                return {'response': None, 'another_player': True}
            if self.lease and self.lease['until'] > now:
                self.lease['until'] = now + 20
                return {'response': None, 'playing': True}
            if not audio_ready and other_audio:
                self.owner, self.owner_until = (None, 0)
                return {'response': None, 'another_player': True}
            ready = sorted((j for j in self.rules.state['jobs'] if j['state'] == 'ready'), key=lambda j: (-j['priority'], j['created']))
            if not ready:
                return {'response': None}
            job = ready[0]
            debug = getattr(self, 'debug', None)
            if debug:
                debug.update((job.get('result') or {}).get('debug_id'), playback='正在播放' if audio_ready else '文字已展示，音频未就绪')
            self.lease = {'id': job['id'], 'client': client_id, 'until': now + 20, 'started_at': now}
            self.rules.presentation.present(job)
            response = {'id': job['id'], 'event': job['event'], **job['result']}
            if not audio_ready:
                self._remember_voice(job, '此页面声音尚未恢复，先继续文字回复')
                response.update(audio_parts=[], audio=None, text_only=True)
            self.rules.save()
            return {'response': response}

    def _remember_voice(self, job, reason):
        result = job.get('result') or {}
        if not result.get('audio_parts') and (not result.get('audio')):
            return
        retries = self.rules.state['voice_retries']
        retries[:] = [j for j in retries if j['id'] != job['id']]
        retries.append(copy.deepcopy(job))
        del retries[:-3]
        self.audio_warning = '语音未播出，文字继续：' + str(reason)[:180]
        self.rules.log('audio', self.audio_warning)

    def retry(self):
        with self.rules.lock:
            self.playback_blocked = False
            self.error = ''
            self.audio_warning = ''
            if self.rules.state['voice_retries']:
                replay = self.rules.state['voice_retries'].pop()
                if not any((j['id'] == replay['id'] for j in self.rules.state['jobs'])):
                    replay.update(state='ready', priority=200)
                    self.rules.state['jobs'].append(replay)
            for job in self.rules.state['jobs']:
                if job['state'] == 'failed':
                    job.update(state='queued', attempts=0, retry_at=0)
            self.rules.save()
            return self.status()

    def _responses(self):
        last_housekeeping = 0
        while not self.stop_event.wait(0.25):
            if time.time() - last_housekeeping > 1:
                self.rules.due()
                last_housekeeping = time.time()
            with self.rules.lock:
                if self.playback_blocked or self.owner_until < time.time() or any((j['state'] == 'ready' for j in self.rules.state['jobs'])) or None:
                    continue
                job = self.rules.next_job()
            if not job:
                continue
            try:
                result = self.render(job['event'], job['event'].get('voice', self.voice))
                with self.rules.lock:
                    stored = next((j for j in self.rules.state['jobs'] if j['id'] == job['id']), None)
                    if stored:
                        stored.update(state='ready', result=result)
                        self.error = ''
                        for call in result.get('tool_calls', []):
                            outcome = call.get('result') or {}
                            track = outcome.get('track') or {}
                            read_tool = str(call.get('tool', '')).startswith(('get_', 'search_recent_'))
                            self.rules.log('tool' if read_tool or False else 'music', ('查询工具：' if read_tool else '点歌工具：') + str(outcome.get('status')) + ' ' + str(track.get('title') or outcome.get('message') or ''))
                        self.rules.save()
            except Exception as exc:
                with self.rules.lock:
                    stored = next((j for j in self.rules.state['jobs'] if j['id'] == job['id']), None)
                    if stored:
                        stored['attempts'] += 1
                        stored['state'] = 'queued'
                        stored['retry_at'] = time.time() + 5
                        if stored['attempts'] >= 3:
                            stored['state'] = 'failed'
                    self.error = str(exc)[:300]
                    self.rules.save()

    def run(self):
        threading.Thread(target=self._responses, name='live-responses', daemon=True).start()
        threading.Thread(target=self._commands, name='song-commands', daemon=True).start()

    def _commands(self):
        while not self.stop_event.wait(0.25):
            with self.rules.lock:
                job = next((j for j in self.rules.state['jobs'] if j['state'] == 'queued' and j['event']['type'] == 'song_command'), None)
                if job is None:
                    continue
                job['state'] = 'generating'
                self.rules.save()
            self.process_song_command(job)

    def process_song_command(self, job):
        try:
            result = self.song_commands.execute(job['event'])
        except Exception as exc:
            result = {'status': 'error', 'message': str(exc)[:250], 'queued': False}
        with self.rules.lock:
            if not any((j['id'] == job['id'] for j in self.rules.state['jobs'])):
                return {'status': 'cancelled', 'queued': False, 'message': '本场任务已清空'}
            self.rules.state['music_command'] = {**result, 'time': self.rules.clock(), 'nickname': job['event']['user']['nickname']}
            self.rules.log('music', '直接点歌：' + str(result.get('message', result['status'])))
            self.rules.state['jobs'][:] = [j for j in self.rules.state['jobs'] if j['id'] != job['id']]
            self.rules.save()
        return result

    def close(self):
        self.stop_event.set()
