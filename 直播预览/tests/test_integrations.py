"""Offline behavioral tests. No model, TTS, music playback or live-room calls."""
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from music import MusicService, parse_request
from rules_config import RuleConfig
from studio import EventRules, Studio
from douyin import DouyinSource
from speech import synthesize
from unittest.mock import patch


class Clock:
    def __init__(self): self.value = 100000
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


def gift(eid='one', count=1, end=False, price=10, name='测试礼物', group='g1', uid='u1', room='room'):
    return {'id': eid, 'platform': 'douyin', 'roomId': room, 'type': 'gift',
            'user': {'id': uid, 'nickname': '小水'},
            'data': {'giftId': 'gift1', 'giftName': name, 'diamondCount': price,
                'repeatCount': count, 'comboCount': count, 'groupCount': 1,
                'groupId': group, 'repeatEnd': end, 'combo': True}}


def chat(eid, text, uid='u1'):
    return {'id': eid, 'platform': 'douyin', 'roomId': 'room', 'type': 'chat',
            'user': {'id': uid, 'nickname': '小水'}, 'data': {'content': text}}


class GiftRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.rules = EventRules(self.root, self.clock)
    def tearDown(self): self.tmp.cleanup()
    def jobs(self): return self.rules.state['jobs']

    def test_saved_config_applies_to_next_gift_without_restart(self):
        config = copy.deepcopy(self.rules.config)
        config['gift_tiers'][1]['min_diamonds'] = 1
        config['gift_tiers'][1]['template'] = '新文案：{nickname} / {gift} / {count}'
        self.rules.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        self.rules.ingest(gift(end=True, price=10))
        data = self.jobs()[0]['event']['data']
        self.assertEqual(data['tier'], '用心')
        self.assertEqual(data['reply'], '新文案：小水 / 测试礼物 / 1')

    def test_incomplete_config_keeps_last_valid_rules_then_recovers(self):
        self.rules.config_path.write_text('{', encoding='utf-8')
        self.rules.ingest(gift(end=True, price=1000))
        self.assertEqual(self.jobs()[0]['event']['data']['tier'], '惊喜')
        self.assertIn('上一份有效配置', self.rules.config_error)
        self.rules.config_path.write_text('{"chat_global_gap_seconds": 7}', encoding='utf-8-sig')
        self.rules.reload_config()
        self.assertEqual(self.rules.config['chat_global_gap_seconds'], 7)
        self.assertEqual(self.rules.config_error, '')

    def test_bad_tier_or_template_does_not_replace_valid_config(self):
        original = copy.deepcopy(self.rules.config)
        for change in ({'min_diamonds': -1}, {'template': '{nickname.invalid}'}, {'template': '{missing}'}):
            config = copy.deepcopy(original)
            config['gift_tiers'][0].update(change)
            self.rules.config_path.write_text(json.dumps(config), encoding='utf-8')
            self.rules.reload_config()
            self.assertTrue(self.rules.config_error)
            self.assertEqual(self.rules.config, original)

    def test_combo_timeout_uses_saved_config(self):
        self.rules.ingest(gift())
        self.rules.config_path.write_text('{"gift_combo_wait_seconds": 10}', encoding='utf-8')
        self.clock.advance(4)
        self.rules.due()
        self.assertFalse(self.jobs())
        self.clock.advance(7)
        self.rules.due()
        self.assertEqual(len(self.jobs()), 1)

    def test_status_reloads_tiers_and_exposes_config_error(self):
        studio = Studio(self.root, None, None)
        studio.rules.config_path.write_text('{"gift_tiers": []}', encoding='utf-8')
        self.assertIn('gift_tiers', studio.status()['error'])
        config = copy.deepcopy(self.rules.config)
        config['gift_tiers'][1]['min_diamonds'] = 2
        studio.rules.config_path.write_text(json.dumps(config), encoding='utf-8')
        status = studio.status()
        self.assertEqual(status['error'], '')
        self.assertEqual(status['tiers'][1]['min_diamonds'], 2)

    def test_combo_final_is_cumulative_not_sum(self):
        self.rules.ingest(gift('1', 1))
        self.rules.ingest(gift('2', 2))
        self.rules.ingest(gift('3', 5, True))
        self.assertEqual(len(self.jobs()), 1)
        d = self.jobs()[0]['event']['data']
        self.assertEqual((d['count'], d['total_diamonds']), (5, 50))

    def test_duplicate_message_and_duplicate_final_suppressed(self):
        self.rules.ingest(gift('1', 4, True))
        self.assertEqual(self.rules.ingest(gift('1', 4, True))['status'], 'duplicate')
        self.rules.ingest(gift('2', 4, True))
        self.assertEqual(len(self.jobs()), 1)

    def test_out_of_order_frames_take_max(self):
        self.rules.ingest(gift('1', 5))
        self.rules.ingest(gift('2', 2))
        self.rules.ingest(gift('3', 4, True))
        self.assertEqual(self.jobs()[0]['event']['data']['count'], 5)

    def test_timeout_settles_missing_final(self):
        self.rules.ingest(gift('1', 3))
        self.clock.advance(3.1); self.rules.due()
        self.assertEqual(self.jobs()[0]['event']['data']['count'], 3)
        self.rules.ingest(gift('2', 3, True))
        self.assertEqual(len(self.jobs()), 1)

    def test_late_higher_final_only_thanks_delta(self):
        self.rules.ingest(gift('1', 3))
        self.clock.advance(3.1); self.rules.due()
        self.rules.ingest(gift('2', 5, True))
        self.assertEqual([j['event']['data']['count'] for j in self.jobs()], [3, 2])

    def test_price_tier_uses_total(self):
        self.rules.ingest(gift(count=10, end=True, price=100))
        self.assertEqual(self.jobs()[0]['event']['data']['tier'], '惊喜')

    def test_unknown_price_not_assumed_cheap(self):
        self.rules.ingest(gift(end=True, price=None))
        d = self.jobs()[0]['event']['data']
        self.assertIsNone(d['total_diamonds'])
        self.assertEqual(d['tier'], '价格未知')

    def test_new_gift_no_catalog_needed(self):
        self.rules.ingest(gift(end=True, name='新出的特别礼物', price=10000))
        self.assertIn('新出的特别礼物', self.jobs()[0]['event']['data']['reply'])

    def test_simulated_sunglasses_uses_confirmed_reference_when_price_missing(self):
        event = gift(end=True, name='墨镜', price=None)
        event['simulation'] = True
        self.rules.ingest(event)
        self.assertEqual(self.jobs()[0]['event']['data']['unit_diamonds'], 99)
        self.assertEqual(self.rules.income.summary()['unit'], '钻石')

    def test_simulation_reference_does_not_supply_unknown_live_price(self):
        self.rules.ingest(gift(end=True, name='墨镜', price=None))
        self.assertIsNone(self.jobs()[0]['event']['data']['unit_diamonds'])

    def test_reported_live_price_wins_over_simulation_reference(self):
        self.rules.ingest(gift(end=True, name='墨镜', price=123))
        self.assertEqual(self.jobs()[0]['event']['data']['unit_diamonds'], 123)

    def test_simulated_price_can_be_overridden_to_test_other_tiers(self):
        event = gift(end=True, name='墨镜', price=1)
        event['simulation'] = True
        self.rules.ingest(event)
        self.assertEqual(self.jobs()[0]['event']['data']['unit_diamonds'], 1)

    def test_paw_has_exact_template_and_audio_key(self):
        self.rules.ingest(gift(end=True, name='喵喵爪'))
        d = self.jobs()[0]['event']['data']
        self.assertEqual(d['reply'], '感谢小水的喵喵爪，喵喵喵喵喵！')
        self.assertEqual(d['special'], 'meow_paw')

    def test_room_and_user_isolate_same_group(self):
        for ev in (gift('1', end=True), gift('1', end=True, uid='u2'), gift('1', end=True, room='other')):
            self.rules.ingest(ev)
        # Same event ID within one room is globally unique, even across senders.
        self.assertEqual(len(self.jobs()), 2)

    def test_noncombo_same_user_separate_transactions(self):
        for i in range(2):
            e = gift(str(i), count=2, end=False, group='')
            e['data']['combo'] = False
            self.rules.ingest(e)
        self.assertEqual(len(self.jobs()), 2)

    def test_restart_preserves_pending_combo_and_dedup(self):
        self.rules.ingest(gift('1', 4))
        reloaded = EventRules(self.root, self.clock)
        self.assertEqual(reloaded.ingest(gift('1', 4))['status'], 'duplicate')
        self.clock.advance(4); reloaded.due()
        self.assertEqual(reloaded.state['jobs'][0]['event']['data']['count'], 4)

    def test_chat_merged_by_user_and_gift_prioritized(self):
        self.rules.ingest(chat('1', '旧弹幕'))
        self.rules.ingest(chat('2', '新弹幕'))
        self.rules.ingest(gift('3', end=True))
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.rules.next_job()['event']['type'], 'gift')

    def test_chat_stale_but_gift_retained(self):
        self.rules.ingest(chat('1', '你好'))
        self.rules.ingest(gift('2', end=True))
        self.clock.advance(31); self.rules.due()
        self.assertEqual([j['event']['type'] for j in self.jobs()], ['gift'])

    def test_superchat_unsupported(self):
        self.assertEqual(self.rules.ingest({'id':'1','type':'superchat'})['status'], 'ignored')

    def test_explicit_song_command_is_durable_without_a_model_job(self):
        result = self.rules.ingest(chat('1', '点歌 晴天 / 周杰伦'))
        self.assertEqual(result['status'], 'song_command_queued')
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['event']['type'], 'song_command')
        restored = EventRules(self.root, self.clock)
        self.assertEqual(restored.state['jobs'][0]['event']['data']['content'], '点歌 晴天 / 周杰伦')

    def test_invalid_input(self):
        with self.assertRaises(ValueError): self.rules.ingest({'type': 'gift', 'id': 'x'})
        with self.assertRaises(ValueError): self.rules.ingest({'type': 'gift'})

    def test_manual_chat_is_not_merged_or_rate_limited(self):
        for i in range(2):
            event = chat(str(i), '手动测试'+str(i))
            event['simulation'] = True
            self.rules.ingest(event)
        self.rules.state['chat_last'] = self.clock()
        self.rules.state['user_last']['u1'] = self.clock()
        self.assertEqual(len(self.jobs()), 2)
        self.assertIsNotNone(self.rules.next_job())
        self.assertIsNotNone(self.rules.next_job())

    def test_manual_chat_remains_pending_after_live_chat_expiry(self):
        event = chat('manual', '手动测试')
        event['simulation'] = True
        self.rules.ingest(event)
        self.clock.advance(31); self.rules.due()
        self.assertEqual(len(self.jobs()), 1)


TRACKS = [
    {'id':'1000001', 'title':'晴天', 'artist':'周杰伦', 'album':'叶惠美'},
    {'id':'1000002', 'title':'晴天（翻唱）', 'artist':'另一个人', 'album':'翻唱'},
    {'id':'1000003', 'title':'稻香', 'artist':'周杰伦', 'album':'魔杰座'},
]


class FakeBridge:
    def __init__(self):
        self.lock = threading.RLock()
        self.error = ''
        self.calls = []
        self.tracks = copy.deepcopy(TRACKS)
        self.snapshot = {'connected': True, 'current': {'id':'original'}, 'capabilities': {'insertNext': True}}
        self.fail = False
    def request(self, action, **kwargs):
        self.calls.append((action, copy.deepcopy(kwargs)))
        if action == 'probe': return copy.deepcopy(self.snapshot)
        if action == 'search': return copy.deepcopy(self.tracks)
        if self.fail: raise RuntimeError('超时，结果不明')
        if kwargs.get('command') == 'PlaySelected': self.snapshot['current'] = copy.deepcopy(kwargs['track'])
        if kwargs.get('command') == 'InsertNext': self.snapshot['next'] = copy.deepcopy(kwargs['track'])
        return {'outcome': 'verified', 'snapshot': copy.deepcopy(self.snapshot)}
    def close(self): pass


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.bridge = Path(self.tmp.name), FakeBridge()
        self.music = MusicService(self.root, self.bridge)
    def tearDown(self): self.tmp.cleanup()

    def queue_behind_current(self):
        """The legacy policy: a request waits behind whatever is playing."""
        book = RuleConfig(self.root)
        book.data['music']['request_switches_now'] = False
        self.music.rulebook = book

    def test_explicit_singer_exact_version(self):
        result = self.music.order('点歌 晴天 / 周杰伦')
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['item']['track']['id'], '1000001')
        self.assertFalse(any(a == 'execute' for a, _ in self.bridge.calls))

    def test_first_search_result_wins_even_with_different_artist(self):
        self.bridge.tracks = [self.bridge.tracks[1], self.bridge.tracks[0]]
        result = self.music.order('晴天 / 周杰伦')
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['item']['track']['id'], '1000002')

    def test_duplicate_titles_still_use_search_ranking(self):
        self.bridge.tracks.insert(0, {'id':'1000004','title':'晴天','artist':'翻唱歌手','album':'翻唱'})
        self.assertEqual(self.music.order('晴天')['item']['track']['id'], '1000004')

    def test_numeric_id(self):
        self.assertEqual(self.music.order('id=1000001')['item']['track']['id'], '1000001')

    def test_duplicate_queue_rejected(self):
        self.music.order('晴天 / 周杰伦')
        with self.assertRaises(ValueError): self.music.order('晴天 / 周杰伦')

    def test_preview_search_does_not_start_music(self):
        self.music.order('晴天 / 周杰伦')
        self.music.tick()
        self.assertFalse(any(a == 'execute' for a, _ in self.bridge.calls))

    def test_request_switches_to_the_song_on_air_at_once(self):
        self.music.order('晴天 / 周杰伦')
        self.music.control('start')
        self.music.tick()
        commands = [d['command'] for a, d in self.bridge.calls if a == 'execute']
        self.assertEqual(commands, ['PlaySelected'])
        self.assertEqual(self.bridge.snapshot['current']['id'], '1000001')
        self.music.tick()
        self.assertEqual(self.music.current['track']['id'], '1000001')
        self.assertEqual(self.music.queue, [])

    def test_next_insert_is_once_and_acknowledged_by_song_id(self):
        self.queue_behind_current()
        self.music.order('晴天 / 周杰伦')
        self.bridge.tracks = [copy.deepcopy(TRACKS[2])]; self.music.order('稻香 / 周杰伦')
        self.music.control('start')
        self.music.tick(); self.music.tick(); self.music.tick()
        commands = [d['command'] for a, d in self.bridge.calls if a == 'execute']
        self.assertEqual(commands, ['InsertNext'])
        self.assertEqual(self.bridge.snapshot['current']['id'], 'original')
        self.assertIsNone(self.music.current)
        self.bridge.snapshot['current'] = copy.deepcopy(TRACKS[0])
        self.music.tick()
        commands = [d['command'] for a, d in self.bridge.calls if a == 'execute']
        self.assertEqual(commands, ['InsertNext', 'InsertNext'])
        self.assertEqual(self.music.current['track']['id'], '1000001')
        self.bridge.snapshot['current'] = copy.deepcopy(TRACKS[2])
        self.music.tick()
        self.assertEqual(self.music.current['track']['id'], '1000003')
        self.assertEqual(self.music.queue, [])

    def test_uncertain_native_operation_not_retried_after_restart(self):
        self.music.order('晴天 / 周杰伦')
        self.music.control('start'); self.bridge.fail = True
        with self.assertRaises(RuntimeError): self.music.tick()
        restored = MusicService(self.root, FakeBridge())
        self.assertFalse(restored.enabled)
        with self.assertRaises(ValueError): restored.control('start')
        restored.control('reconcile')
        self.assertEqual(restored.queue, [])

    def test_staged_song_not_falsely_withdrawn(self):
        self.music.order('晴天 / 周杰伦'); self.bridge.tracks = [copy.deepcopy(TRACKS[2])]; self.music.order('稻香 / 周杰伦')
        self.music.control('start'); self.music.tick(); self.music.tick()
        with self.assertRaises(ValueError): self.music.control('remove', self.music.queue[0]['id'])

    def test_empty_search_fails_without_confirmation(self):
        self.bridge.tracks = []
        with self.assertRaises(ValueError): self.music.order('晴天')
        self.assertEqual(self.music.queue, [])

    def test_request_has_no_candidate_confirmation_step(self):
        result = self.music.order('晴天 / 不存在')
        self.assertEqual(result['status'], 'queued')
        self.assertNotIn('candidate', result)

    def test_room_validation(self):
        self.assertEqual(DouyinSource.normalize_room('https://live.douyin.com/123456?x=1'), '123456')
        with self.assertRaises(ValueError): DouyinSource.normalize_room('https://evil.test/123456')


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = Studio(Path(self.tmp.name), None, None)
        r = self.studio.rules
        r.ingest(gift(end=True))
        job = r.state['jobs'][0]
        job.update(state='ready', result={'reply': '谢谢', 'audio_parts': ['/audio/offline.mp3']})
        self.job_id = job['id']
    def tearDown(self): self.tmp.cleanup()

    def test_only_one_browser_claims_voice(self):
        first = self.studio.claim('client-one')
        self.assertEqual(first['response']['id'], self.job_id)
        self.assertIsNone(self.studio.claim('client-two')['response'])
        self.assertIsNone(self.studio.claim('client-one')['response'])

    def test_ack_removes_durable_response(self):
        self.studio.claim('client-one')
        self.studio.claim('client-one', ack=self.job_id)
        self.assertEqual(self.studio.rules.state['jobs'], [])

    def test_playback_failure_preserves_voice_retry_without_blocking_new_messages(self):
        self.studio.claim('client-one')
        self.studio.claim('client-one', ack=self.job_id, failed=True)
        self.assertFalse(self.studio.playback_blocked)
        self.assertEqual(len(self.studio.rules.state['jobs']), 0)
        self.assertEqual(len(self.studio.rules.state['voice_retries']), 1)

    def test_wrong_browser_cannot_ack(self):
        self.studio.claim('client-one')
        self.studio.claim('client-two', ack=self.job_id)
        self.assertEqual(len(self.studio.rules.state['jobs']), 1)

    def test_playback_block_prevents_repeated_delivery(self):
        self.studio.playback_blocked = True
        self.assertIsNone(self.studio.claim('client-one')['response'])

    def test_gift_delivery_needs_no_enable_switch(self):
        self.assertFalse(self.studio.playback_blocked)
        self.assertEqual(self.studio.claim('client-one')['response']['id'], self.job_id)

    def test_retry_recovers_playback_without_mode_switch(self):
        self.studio.claim('client-one')
        self.studio.claim('client-one', ack=self.job_id, failed=True)
        self.studio.retry()
        self.assertFalse(self.studio.playback_blocked)
        self.assertEqual(self.studio.claim('client-one')['response']['id'], self.job_id)

    def test_fresh_gift_renders_without_enabling(self):
        rendered = []
        self.studio.rules.state['jobs'].clear()
        def fake_render(event, voice):
            rendered.append((event['type'], voice))
            return {'reply': event['data']['reply'], 'audio_parts': []}
        self.studio.render = fake_render
        event = gift('fresh', end=True, group='new-group')
        event.update(simulation=True, voice=False)
        self.studio.ingest(event)
        self.studio.claim('client-one')
        self.studio.run()
        try:
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                if self.studio.rules.state['jobs'][0]['state'] == 'ready':
                    break
                time.sleep(.02)
            self.assertEqual(rendered, [('gift', False)])
            self.assertIsNotNone(self.studio.claim('client-one')['response'])
        finally:
            self.studio.close()


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'audio').mkdir()
        self.spoken = []
        outer = self
        class FakeTTS:
            def __init__(self, text, voice): outer.spoken.append(text)
            async def save(self, path): Path(path).write_bytes(b'fake-audio')
        self.patch = patch('speech.edge_tts.Communicate', FakeTTS)
        self.patch.start()
    def tearDown(self): self.patch.stop(); self.tmp.cleanup()

    def test_no_clip_uses_full_xiaoyi_line(self):
        line = '感谢小水的喵喵爪，喵喵喵喵喵！'
        result = synthesize(self.root, line, special='meow_paw')
        self.assertEqual(self.spoken, [line])
        self.assertEqual(len(result['audio_parts']), 1)

    def test_clip_is_sequenced_after_personalized_thanks(self):
        directory = self.root/'assets/gift_audio'; directory.mkdir(parents=True)
        (directory/'meow_paw.mp3').write_bytes(b'fake-clip')
        result = synthesize(self.root, '感谢小水的喵喵爪，喵喵喵喵喵！', special='meow_paw')
        self.assertEqual(self.spoken, ['感谢小水的喵喵爪！'])
        self.assertEqual(result['audio_parts'][1], '/assets/gift_audio/meow_paw.mp3')

    def test_voice_disabled_does_not_synthesize(self):
        result = synthesize(self.root, '谢谢', enabled=False)
        self.assertFalse(self.spoken)
        self.assertEqual(result['audio_parts'], [])


if __name__ == '__main__': unittest.main()
