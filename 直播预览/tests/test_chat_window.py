"""Offline burst selection and passive music observation; no model/native calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml
from rules_config import DEFAULT_RULES
from studio import EventRules
from music import MusicService
from test_integrations import Clock, chat, gift, FakeBridge


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        config = copy.deepcopy(DEFAULT_RULES)
        config['studio'].update(chat_selection='window_random', chat_max_pending=5,
                                chat_max_age_seconds=15, chat_window_wait_seconds=2)
        (self.root/'直播规则.yaml').write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
        self.rules = EventRules(self.root, self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fifo_eviction_random_middle_selection_discards_only_earlier(self):
        for i in range(8):
            self.rules.ingest(chat(str(i), '消息'+str(i), uid=str(i)))
        self.assertEqual([j['event']['id'] for j in self.rules.state['jobs']], ['3','4','5','6','7'])
        with patch('studio.random.choice', side_effect=lambda items: items[2]):
            chosen = self.rules.next_job()
        self.assertEqual(chosen['event']['id'], '5')
        self.assertEqual([j['event']['id'] for j in self.rules.state['jobs']], ['5','6','7'])
        self.assertEqual(sum(j['state']=='queued' for j in self.rules.state['jobs']), 2)
        # New arrivals during generation belong to the next window.
        self.rules.ingest(chat('new', '新一轮', uid='new'))
        self.assertEqual(len(self.rules.state['jobs']), 4)
        # Equal arrival timestamps must not erase newer candidates.
        restored = EventRules(self.root, self.clock)
        self.assertEqual([j['event']['id'] for j in restored.state['jobs']], ['5','6','7','new'])

    def test_low_traffic_does_not_wait_for_five_messages(self):
        self.rules.ingest(chat('one', '你好'))
        self.assertIsNone(self.rules.next_job())
        self.clock.advance(2)
        self.assertEqual(self.rules.next_job()['event']['id'], 'one')

    def test_spam_is_one_candidate_and_cooldown_viewer_is_excluded(self):
        for i in range(8):
            self.rules.ingest(chat(str(i), '刷屏'+str(i)))
        self.rules.ingest(chat('other', '另一个人', uid='u2'))
        self.rules.state['user_last']['u1'] = self.clock()
        self.clock.advance(2)
        self.assertEqual(self.rules.next_job()['event']['id'], 'other')
        self.assertEqual(len(self.rules.state['jobs']), 1)

    def test_newest_selection_keeps_gifts_commands_manual_and_inflight_jobs(self):
        self.rules.ingest(chat('older', '普通弹幕'))
        self.rules.ingest(gift('gift', end=True))
        self.rules.ingest(chat('command', '点歌 晴天'))
        manual = chat('manual', '本地模拟'); manual['simulation'] = True
        self.rules.ingest(manual)
        self.rules._job(chat('inflight', '正在生成', uid='busy'), 10)['state'] = 'generating'
        self.rules._job(chat('ready', '正在播音', uid='ready'), 10)['state'] = 'ready'
        self.rules.ingest(chat('newest', '最新弹幕', uid='new'))
        # Isolate the chat selection; the gift is still protected from eviction.
        next(j for j in self.rules.state['jobs'] if j['event']['type']=='gift')['state']='ready'
        self.clock.advance(2)
        with patch('studio.random.choice', side_effect=lambda items: items[-1]):
            selected = self.rules.next_job()
        self.assertEqual(selected['event']['id'], 'newest')
        remaining = {j['event']['id'] for j in self.rules.state['jobs']}
        self.assertEqual(remaining, {'gift:room:u1:gift1:g1:1','command','manual','inflight','ready','newest'})

    def test_first_selection_keeps_newer_and_toggle_can_preserve_older(self):
        for i in range(5): self.rules.ingest(chat(str(i), '消息', uid=str(i)))
        with patch('studio.random.choice', side_effect=lambda items: items[0]):
            self.assertEqual(self.rules.next_job()['event']['id'], '0')
        self.assertEqual(sum(j['state']=='queued' for j in self.rules.state['jobs']),4)
        config = self.rules.rulebook.snapshot()
        config['studio']['chat_discard_before_selected']=False
        (self.root/'直播规则.yaml').write_text(yaml.safe_dump(config,allow_unicode=True),encoding='utf-8')
        self.clock.advance(2)
        with patch('studio.random.choice', side_effect=lambda items: items[-1]):
            self.assertEqual(self.rules.next_job()['event']['id'],'4')
        self.assertEqual({j['event']['id'] for j in self.rules.state['jobs']},{'0','1','2','3','4'})

    def test_shrinking_window_applies_before_next_pick_without_new_arrivals(self):
        for i in range(5): self.rules.ingest(chat(str(i), '消息', uid=str(i)))
        config=self.rules.rulebook.snapshot();config['studio']['chat_max_pending']=3
        (self.root/'直播规则.yaml').write_text(yaml.safe_dump(config,allow_unicode=True),encoding='utf-8')
        with patch('studio.random.choice', side_effect=lambda items: items[0]):
            self.assertEqual(self.rules.next_job()['event']['id'],'2')
        self.assertEqual([j['event']['id'] for j in self.rules.state['jobs']],['2','3','4'])

    def test_gift_is_not_sampled_or_dropped_with_chat_window(self):
        for i in range(5):
            self.rules.ingest(chat(str(i), '弹幕', uid=str(i)))
        self.rules.ingest(gift('realgift', end=True))
        self.assertEqual(self.rules.next_job()['event']['type'], 'gift')
        self.assertEqual(sum(j['event']['type']=='chat' for j in self.rules.state['jobs']), 5)

    def test_old_message_expires_and_manual_test_remains_individual(self):
        self.rules.ingest(chat('stale', '旧消息'))
        self.clock.advance(16)
        self.assertIsNone(self.rules.next_job())
        self.rules.due()
        self.assertEqual(self.rules.state['jobs'], [])
        for i in range(2):
            event = chat('manual'+str(i), '本地调试' + str(i))
            event['simulation'] = True
            self.rules.ingest(event)
        self.assertIsNotNone(self.rules.next_job())
        self.assertIsNotNone(self.rules.next_job())

    def test_repeated_digits_stay_visible_but_never_become_model_jobs(self):
        for i,simulated in enumerate((False,True)):
            event=chat(str(i),'1 111111111')
            event['simulation']=simulated
            self.assertEqual(self.rules.ingest(event)['status'],'ignored')
        self.assertEqual(self.rules.state['jobs'],[])
        self.assertEqual(len(self.rules.presentation.snapshot()['incoming']),2)
        self.assertEqual(self.rules.ingest(chat('number','20261029'))['status'],'queued')



class MusicObserveTests(unittest.TestCase):
    def test_stopped_scheduler_reconciles_current_without_native_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = FakeBridge()
            service = MusicService(Path(directory), bridge)
            service.current = {'track': {'id': 'old'}, 'state': 'playing'}
            service.queue = [{'id': 'pending', 'state': 'waiting', 'track': {'id': 'later'}}]
            bridge.snapshot['current'] = {'id': 'actual', 'title': '网易云正在放'}
            service.error = '接管暂停'
            service.tick()
            self.assertIsNone(service.current)
            self.assertEqual(service.status()['player']['current']['id'], 'actual')
            self.assertEqual(len(service.queue), 1)
            self.assertFalse(service.enabled)
            self.assertEqual(bridge.calls, [])


if __name__ == '__main__':
    unittest.main()
