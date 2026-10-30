"""Model tool protocol tests with fake streams. Never call an online model."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from codex_resident import ResidentCodex
from providers import DeepSeekProvider, ProviderManager
from music import MusicService, NeteaseBridge
from music_tools import MusicTools, TOOL_SPECS
from persona import PERSONA, MUSIC_PERSONA, LIVE_PERSONA
from test_integrations import FakeBridge, chat
from studio import EventRules

ARGS = {'title': '晴天', 'artist': '周杰伦', 'song_id': ''}


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bridge = FakeBridge()
        self.music = MusicService(self.root, self.bridge)
        self.host = MusicTools(self.music)
    def tearDown(self): self.tmp.cleanup()

    def session(self, uid='u1', event='e1'):
        return self.host.bind({'id': uid, 'nickname': '观众'+uid}, '123456', event)

    def test_structured_title_artist_search_and_queue(self):
        result = self.session().execute('request_song', ARGS)
        self.assertEqual(result['status'], 'queued')
        search = next(v for k, v in self.bridge.calls if k == 'search')
        self.assertEqual(search['query'], '晴天 周杰伦')
        self.assertEqual(self.music.queue[0]['user_id'], 'douyin:123456:u1')
        self.assertNotIn('nativeData', result['track'])

    def test_song_tool_also_starts_playback_without_control_buttons(self):
        result = self.session().execute('request_song', ARGS)
        self.assertTrue(result['playback_enabled'])
        self.assertTrue(self.music.wake_event.is_set())
        self.music.tick()
        commands = [d['command'] for a, d in self.bridge.calls if a == 'execute']
        # A viewer asked for it, so it plays now instead of waiting its turn.
        self.assertEqual(commands, ['PlaySelected'])
        self.assertEqual(self.bridge.snapshot['current']['id'], '1000001')

    def test_unavailable_player_returns_error_without_queueing(self):
        self.bridge.snapshot['connected'] = False
        result = self.session().execute('request_song', ARGS)
        self.assertEqual(result['status'], 'error')
        self.assertFalse(self.music.queue)
        self.assertFalse(self.music.enabled)
        self.assertTrue(self.music.error)

    def test_multiple_results_start_first_song_without_confirmation(self):
        self.bridge.tracks.reverse()
        first = self.bridge.tracks[0]['id']
        result = self.session().execute('request_song', {**ARGS, 'artist':'不存在'})
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['track']['id'], first)
        self.assertTrue(result['playback_enabled'])
        self.assertNotIn('choices', result)

    def test_native_bridge_opens_installed_player_when_needed(self):
        bridge = NeteaseBridge(self.root)
        with patch.object(bridge,'request',side_effect=[{'connected':False},{'connected':True}]), \
             patch.object(bridge,'player_executable',return_value=Path('C:/Fake/cloudmusic.exe')), \
             patch('music.subprocess.Popen') as launch, patch('music.time.sleep'):
            result = bridge.ensure_player()
        self.assertTrue(result['connected'])
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.args[0], ['C:\\Fake\\cloudmusic.exe'])

    def test_native_bridge_reuses_running_player(self):
        bridge = NeteaseBridge(self.root)
        with patch.object(bridge,'request',return_value={'connected':True}), patch('music.subprocess.Popen') as launch:
            self.assertTrue(bridge.ensure_player()['connected'])
        launch.assert_not_called()

    def test_retry_same_viewer_event_uses_receipt_once(self):
        self.session().execute('request_song', ARGS)
        result = MusicTools(self.music).bind({'id':'u1'}, '123456', 'e1').execute('request_song', ARGS)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(len(self.music.queue), 1)
        self.assertEqual(sum(a == 'search' for a, _ in self.bridge.calls), 1)

    def test_no_arbitrary_tools_or_identity_override(self):
        self.assertEqual(self.session().execute('exec_command', {})['status'], 'error')
        self.assertEqual(self.session().execute('request_song', {**ARGS, 'user_id':'admin'})['status'], 'error')
        self.assertFalse(self.music.queue)

    def test_select_tool_is_not_exposed_or_required(self):
        self.assertNotIn('choose_song', [spec['name'] for spec in TOOL_SPECS])
        result = self.session().execute('request_song', ARGS)
        self.assertEqual(result['selection'], 'first_search_result')
        self.assertEqual(self.session(event='later').execute('choose_song', {})['status'], 'error')

    def test_withdrawal_only_own_song(self):
        self.session().execute('request_song', ARGS)
        self.assertEqual(self.session('u2', 'e2').execute('withdraw_song', {})['status'], 'error')
        self.assertEqual(len(self.music.queue), 1)
        self.assertEqual(self.session('u1', 'e3').execute('withdraw_song', {})['status'], 'withdrawn')

    def test_natural_language_is_a_model_job(self):
        rules = EventRules(self.root)
        rules.ingest(chat('natural', '能放一首周杰伦的晴天吗'))
        self.assertEqual(rules.state['jobs'][0]['event']['data']['content'], '能放一首周杰伦的晴天吗')
        self.assertFalse(self.music.queue)

    def test_only_tool_permission_changes_original_character(self):
        original_prefix = PERSONA.split('你不是DeepSeek官方')[0]
        self.assertTrue(MUSIC_PERSONA.startswith(original_prefix))
        self.assertIn('不得调用工具', PERSONA)
        self.assertNotIn('不得调用工具', MUSIC_PERSONA)
        self.assertIn('不得读取文件、执行命令或自行联网', MUSIC_PERSONA)

    def test_deepseek_real_tool_loop_feeds_result_back_to_model(self):
        provider = DeepSeekProvider(self.root, {'deepseek_model':'deepseek-flash'})
        session = self.session()
        bodies = []
        def fake_stream(body, key, began, timeout):
            bodies.append(copy.deepcopy(body))
            if len(bodies) == 1:
                return ({'role':'assistant', 'content':None, 'tool_calls':[
                    {'id':'call-song','type':'function','function':{
                        'name':'request_song','arguments':json.dumps(ARGS,ensure_ascii=False)}}]}, None, {'prompt_tokens':10,'completion_tokens':5})
            self.assertEqual(body['messages'][-1]['role'], 'tool')
            result = json.loads(body['messages'][-1]['content'])
            self.assertEqual(result['status'], 'queued')
            return ({'role':'assistant','content':'本鲸帮你把晴天排好啦～'}, 123, {'prompt_tokens':20,'completion_tokens':8})
        with patch.object(provider, 'key', return_value='fake-local-test'), patch.object(provider, '_stream', side_effect=fake_stream):
            result = provider.generate([], '能放周杰伦的晴天吗', 'chat', session)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(len(bodies[0]['tools']), 2)
        self.assertEqual(result['tool_calls'][0]['result']['status'], 'queued')
        self.assertEqual(result['usage']['prompt_tokens'], 30)

    def test_deepseek_no_tool_chat_needs_only_one_completion(self):
        provider = DeepSeekProvider(self.root, {})
        with patch.object(provider, 'key', return_value='fake'), patch.object(provider, '_stream', return_value=({'role':'assistant','content':'晚上好～'},1,None)) as stream:
            result = provider.generate([], '晚上好', 'chat', self.session())
        self.assertEqual(stream.call_count, 1)
        self.assertFalse(result['tool_calls'])
        self.assertFalse(self.music.queue)

    def test_deepseek_stream_joins_fragmented_function_arguments(self):
        def line(delta):
            return ('data: '+json.dumps({'choices':[{'delta':delta}]},ensure_ascii=False)+'\n').encode()
        content = b''.join([
            line({'tool_calls':[{'index':0,'id':'c1','function':{'name':'request_song','arguments':'{"title":"晴'}}]}),
            line({'tool_calls':[{'index':0,'function':{'arguments':'天","artist":"周杰伦","song_id":""}'}}]}),
            b'data: [DONE]\n'])
        provider = DeepSeekProvider(self.root, {})
        opener = SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(content))
        import time
        with patch('providers.urllib.request.build_opener', return_value=opener):
            message, _, _ = provider._stream({'messages':[]}, 'fake', time.perf_counter(), 10)
        function = message['tool_calls'][0]['function']
        self.assertEqual(function['name'], 'request_song')
        self.assertEqual(json.loads(function['arguments']), ARGS)

    def test_codex_rpc_request_runs_same_scoped_tool_and_returns_content(self):
        session = self.session()
        class FakeResident(ResidentCodex):
            def __init__(self):
                super().__init__('unused', self_root, lambda:{}, 'gpt-6-luna')
                self.thread_id = 'thread-test'
                self.process = SimpleNamespace(pid=42)
                self.sent = []
            def start(self): pass
            def close(self): pass
            def _send(self, item): self.sent.append(item)
            def rpc(self, method, params, timeout=30):
                self.events.put({'id':99,'method':'item/tool/call','params':{
                    'threadId':'thread-test','turnId':'turn-test','tool':'request_song',
                    'callId':'song1','arguments':ARGS}})
                self.events.put({'method':'item/completed','params':{'threadId':'thread-test','turnId':'turn-test',
                    'item':{'type':'agentMessage','phase':'final_answer','text':'晴天已经排好啦～'}}})
                self.events.put({'method':'turn/completed','params':{'threadId':'thread-test','turnId':'turn-test','turn':{'status':'completed'}}})
                return {'turn':{'id':'turn-test'}}
        self_root = self.root
        resident = FakeResident()
        result = resident.chat('想听晴天', tool_session=session)
        self.assertEqual(result['tool_calls'][0]['result']['status'], 'queued')
        reply = resident.sent[0]
        self.assertEqual(reply['id'], 99)
        self.assertTrue(reply['result']['success'])
        self.assertEqual(json.loads(reply['result']['contentItems'][0]['text'])['status'], 'queued')

    def test_codex_reset_registers_only_current_music_tools(self):
        resident = ResidentCodex('unused', self.root, lambda:{}, 'gpt-6-luna')
        resident.process = SimpleNamespace(poll=lambda:None)
        try:
            with patch.object(resident, 'rpc', return_value={'thread':{'id':'test'}}) as rpc:
                resident.reset()
            params = rpc.call_args.args[1]
            from live_tools import TOOL_SPECS as LIVE_SPECS
            self.assertEqual([t['name'] for t in params['dynamicTools']], [t['name'] for t in LIVE_SPECS])
            self.assertEqual(params['baseInstructions'], LIVE_PERSONA)
            self.assertEqual(params['developerInstructions'], '')
        finally:
            resident.process = None


if __name__ == '__main__': unittest.main()
