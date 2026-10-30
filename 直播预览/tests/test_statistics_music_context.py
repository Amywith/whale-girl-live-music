"""Reset display totals and provide factual song context; all state is temporary."""
import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from music import MusicService, atomic_json
from music_tools import MusicTools
from studio import Studio
from rules_config import RuleConfig
from expense import ExpenseEstimate
from providers import ProviderError
from test_integrations import FakeBridge, gift


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.studio = Studio(self.root, None, lambda *args: None)
        self.expenses = ExpenseEstimate(self.root, RuleConfig(self.root))

    def tearDown(self): self.tmp.cleanup()

    def coins(self): return self.studio.rules.income.summary()['lifetime']['live']['coins']

    def test_reset_keeps_receipts_and_counts_only_new_combo_quantity(self):
        self.studio.ingest(gift('first', count=3, price=10, end=True))
        self.assertEqual(self.coins(), 30)
        jobs = len(self.studio.rules.state['jobs'])
        self.studio.reset_income()
        self.studio.ingest(gift('replay', count=3, price=10, end=True))
        self.assertEqual(self.coins(), 0)
        self.assertEqual(len(self.studio.rules.state['jobs']), jobs)
        self.studio.ingest(gift('continued', count=4, price=10, end=True))
        self.assertEqual(self.coins(), 10)
        self.assertEqual(self.studio.rules.income.summary()['today']['live']['gift_count'], 1)

    def test_pending_combo_does_not_refill_reset_totals_or_lose_thanks(self):
        self.studio.ingest(gift('pending', count=3, price=10, end=False))
        self.studio.reset_income()
        self.studio.ingest(gift('final', count=3, price=10, end=True))
        self.assertEqual(self.coins(), 0)
        self.assertEqual(len(self.studio.rules.state['jobs']), 1)
        self.assertEqual(self.studio.rules.state['jobs'][0]['event']['data']['count'], 3)

    def test_reset_api_backs_up_and_preserves_pending_jobs(self):
        self.studio.ingest(gift('gift', price=99, end=True))
        self.expenses.record({'provider':'codex','reply':'好','usage':{'total_tokens':100}}, '你好')
        source = ast.parse((Path(__file__).parents[1]/'server.py').read_text(encoding='utf-8'))
        functions = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'reset_statistics']
        namespace = {'LOCK':threading.Lock(), 'STUDIO':self.studio, 'EXPENSES':self.expenses,
                     'RUNTIME':self.root/'.runtime', 'atomic_json':atomic_json,
                     'ProviderError':ProviderError, 'time':__import__('time')}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<stats-reset-offline>', 'exec'), namespace)
        result = namespace['reset_statistics']()
        backup = json.loads((self.root/'.runtime/stats-backups'/result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(backup['income']['lifetime']['live']['coins'], 99)
        self.assertEqual(self.coins(), 0)
        self.assertEqual(self.expenses.status()['tokens'], 0)
        self.assertEqual(len(self.studio.rules.state['jobs']), 1)
        self.expenses.record({'provider':'codex','reply':'好','usage':{'total_tokens':10}}, '新消息')
        self.assertEqual(self.expenses.status()['tokens'], 10)
        namespace['LOCK'].acquire()
        with self.assertRaises(ProviderError) as error: namespace['reset_statistics']()
        self.assertEqual(error.exception.status, 409)
        namespace['LOCK'].release()


class QueueContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.bridge = FakeBridge(); self.music = MusicService(self.root, self.bridge)
        self.host = MusicTools(self.music)

    def tearDown(self): self.tmp.cleanup()

    def session(self, identifier): return self.host.bind({'id':identifier,'nickname':'观众'}, '123', identifier)

    def test_empty_waiting_queue_still_exposes_actual_playing_song(self):
        self.bridge.snapshot['current'] = {'id':'1000001','title':'晴天','artist':'周杰伦'}
        context = self.session('context').context()
        self.assertEqual(context['queue_count'], 0)
        self.assertEqual(context['current_track']['title'], '晴天')
        self.assertTrue(context['queue_count_excludes_current'])
        self.assertEqual(context['queued_songs'], [])

    def test_duplicate_tool_separates_playing_from_queued_and_keeps_position(self):
        args = {'title':'晴天','artist':'周杰伦','song_id':''}
        self.bridge.snapshot['current'] = self.bridge.tracks[0]
        playing = self.session('playing').execute('request_song', args)
        self.assertEqual(playing['status'], 'already_playing')
        self.assertFalse(playing['queued'])
        self.bridge.snapshot['current'] = {'id':'other'}
        self.session('first').execute('request_song', args)
        queued = self.session('second').execute('request_song', args)
        self.assertEqual(queued['status'], 'already_queued')
        self.assertEqual(queued['position'], 1)
        self.assertEqual(len(self.music.queue), 1)
        self.assertEqual(self.session('third').context()['queued_songs'][0]['track']['title'], '晴天')

    def test_observer_removes_actually_started_song_even_when_scheduler_is_paused(self):
        book = RuleConfig(self.root); book.data['music']['autoplay_enabled'] = False
        self.music.rulebook = book
        self.music.order('晴天')
        self.bridge.snapshot['current'] = self.bridge.tracks[0]
        self.music.tick()
        self.assertFalse(self.music.enabled)
        self.assertEqual(self.music.queue, [])
        self.assertEqual(self.music.current['track']['id'], self.bridge.tracks[0]['id'])
        self.assertFalse(any(action=='execute' for action,_ in self.bridge.calls))


if __name__ == '__main__': unittest.main()
