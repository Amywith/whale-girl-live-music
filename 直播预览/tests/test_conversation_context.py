"""Shared context regressions: no network, CLI subprocess, model, or TTS calls."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conversation_context import ConversationContext
from codex_resident import ResidentCodex
from providers import ProviderManager


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ConversationContext(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_window_identity_room_and_restart(self):
        for i in range(25):
            self.store.append('room-a', {'id':str(i), 'nickname':'观众'+str(i)},
                              'chat', str(i), '回复'+str(i), 'codex')
        self.store.append('room-b', {'id':'other'}, 'chat', '其他房间', '其他回复', 'deepseek')
        restored = ConversationContext(self.root)
        recent = restored.recent('room-a')
        self.assertEqual([t['user'] for t in recent], [str(i) for i in range(13,25)])
        self.assertEqual(recent[-1]['viewer'], {'id':'24','nickname':'观众24'})
        self.assertEqual(len(restored.recent('room-b')), 1)
        self.assertEqual(restored.recent('unknown'), [])

    def test_room_scoped_dedup_and_concurrent_writes(self):
        def write(i):
            self.store.append('a', {}, 'chat', str(i), 'ok', 'codex', str(i))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, list(range(10))*2))
        self.assertEqual(len(self.store.recent('a')), 10)
        self.store.append('b', {}, 'chat', 'same-id', 'ok', 'deepseek', '1')
        self.assertEqual(len(self.store.recent('b')), 1)

    def test_legacy_import_once_and_never_attributed_to_live_room(self):
        legacy = self.root/'.runtime/model-history.json'
        legacy.write_text(json.dumps([{'event':'chat','user':'old','assistant':'reply'}]), encoding='utf-8')
        # Use a separate fresh root: the migration marker was already set in setUp.
        other = self.root/'fresh'; (other/'.runtime').mkdir(parents=True)
        (other/'.runtime/model-history.json').write_bytes(legacy.read_bytes())
        store = ConversationContext(other)
        self.assertEqual(store.recent('live-room'), [])
        self.assertEqual(len(store.recent('__legacy__')), 1)
        self.assertEqual(len(ConversationContext(other).recent('__legacy__')), 1)
        store.clear()
        self.assertEqual(ConversationContext(other).recent('__legacy__'), [])
        self.assertTrue((other/'.runtime/model-history.json').exists())


class OfflineResident(ResidentCodex):
    def __init__(self, root):
        super().__init__('offline-unused', root, lambda:{}, 'gpt-6-luna')
        self.native = {}
        self.inputs = []
        self.sequence = 0
        self.fail_turn = False
        self.support_revert = True

    def start(self):
        if self.process is None:
            self.process = SimpleNamespace(pid=42, poll=lambda:None)
            self.starts += 1
            self.reset()

    def close(self):
        self.process = None
        self.thread_id = self.context_turn_id = None

    def rpc(self, method, params, timeout=30):
        if method == 'thread/start':
            self.sequence += 1
            thread = 'thread-'+str(self.sequence)
            self.native[thread] = []
            return {'thread':{'id':thread}}
        if method == 'thread/archive':
            self.native.pop(params['threadId'], None)
            return {}
        if method == 'thread/revert':
            if not self.support_revert:
                raise RuntimeError('unsupported')
            turns = self.native[params['threadId']]
            self.native[params['threadId']] = turns[:next(i for i,t in enumerate(turns) if t[0]==params['beforeTurnId'])]
            return {}
        if method != 'turn/start':
            raise AssertionError(method)
        if self.fail_turn:
            self.fail_turn = False
            raise RuntimeError('offline disconnect')
        turn = 'turn-'+str(len(self.inputs))
        envelope = json.loads(json.loads(params['input'][0]['text'])['message'])
        self.inputs.append(envelope)
        self.native[params['threadId']].append((turn, envelope))
        for event in (
            {'method':'item/completed', 'params':{'item':{'type':'agentMessage','phase':'final_answer','text':'回复'}}},
            {'method':'turn/completed','params':{'turn':{'status':'completed'}}}):
            self.events.put(event)
        return {'turn':{'id':turn}}


class ProviderContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.resident = OfflineResident(self.root)
        self.manager = ProviderManager(self.resident, self.root, {'provider':'codex'})

    def tearDown(self):
        self.resident.close(); self.temp.cleanup()

    def test_native_history_is_bounded_while_process_stays_warm(self):
        for i in range(30):
            self.manager.generate(str(i), 'chat', room_id='a', user={'id':str(i),'nickname':'观众'+str(i)})
        self.assertEqual(self.resident.starts, 1)
        self.assertEqual(len(self.resident.native[self.resident.thread_id]), 1)
        payload = self.resident.inputs[-1]
        self.assertEqual([x['user'] for x in payload['recent_conversation']], [str(i) for i in range(17,29)])
        self.assertEqual(payload['viewer']['id'], '29')
        self.assertEqual(payload['recent_conversation'][0]['viewer']['id'], '17')

    def test_failure_recovery_restores_history_without_committing_failed_turn(self):
        self.manager.generate('first', 'chat', room_id='a')
        self.resident.fail_turn = True
        with self.assertRaises(RuntimeError):
            self.manager.generate('failed', 'chat', room_id='a')
        self.manager.generate('recovered', 'chat', room_id='a')
        self.assertEqual(self.resident.starts, 2)
        self.assertEqual([t['user'] for t in self.resident.inputs[-1]['recent_conversation']], ['first'])
        self.assertEqual([t['user'] for t in self.manager.history], ['first','recovered'])

    def test_unsupported_revert_rotates_thread_in_same_process(self):
        self.resident.support_revert = False
        for i in range(5): self.manager.generate(str(i), 'chat')
        self.assertEqual(self.resident.starts, 1)
        self.assertEqual(len(self.resident.native), 1)
        self.assertEqual(len(self.resident.inputs[-1]['recent_conversation']), 4)

    def test_both_adapters_receive_identical_context_and_room_changes_are_isolated(self):
        self.manager.generate('first', 'chat', room_id='a', user={'id':'a1','nickname':'甲'})
        captured = []
        deepseek = self.manager.adapters['deepseek']
        def stream(body, *args):
            self.assertEqual(len(body['messages']), 2)  # No second adapter-owned history.
            captured.append(json.loads(body['messages'][-1]['content']))
            return {'role':'assistant','content':'回复'}, 0, None
        with patch.object(deepseek, 'key', return_value='offline'), patch.object(deepseek, '_stream', side_effect=stream):
            self.manager.selected = 'deepseek'
            self.manager.generate('second', 'chat', room_id='a', user={'id':'a2','nickname':'乙'})
        self.assertEqual(captured[-1]['recent_conversation'][0]['viewer']['nickname'], '甲')
        self.manager.selected = 'codex'
        self.manager.generate('third', 'chat', room_id='a')
        self.assertEqual([t['user'] for t in self.resident.inputs[-1]['recent_conversation']], ['first','second'])
        self.manager.generate('other-room', 'chat', room_id='b')
        self.assertEqual(self.resident.inputs[-1]['recent_conversation'], [])
        self.assertEqual(self.resident.starts, 1)
        restored = ProviderManager(self.resident, self.root, {'provider':'codex'})
        restored.generate('after-app-restart', 'chat', room_id='a')
        self.assertEqual([t['user'] for t in self.resident.inputs[-1]['recent_conversation']], ['first','second','third'])

    def test_identical_snapshot_is_sent_to_both_models(self):
        self.manager.generate('previous', 'chat', room_id='a', user={'id':'a1','nickname':'甲'})
        captured = []
        deepseek = self.manager.adapters['deepseek']
        def stream(body, *args):
            captured.append(json.loads(body['messages'][-1]['content']))
            return {'role':'assistant','content':'回复'}, 0, None
        with patch('providers.beijing_time', return_value={'time':'12:00:00'}), \
             patch.object(self.manager.context, 'append'), \
             patch.object(deepseek, 'key', return_value='offline'), \
             patch.object(deepseek, '_stream', side_effect=stream):
            for name in ('codex','deepseek'):
                self.manager.selected = name
                self.manager.generate('current', 'chat', room_id='a', user={'id':'a2','nickname':'乙'})
        self.assertEqual(self.resident.inputs[-1], captured[-1])

    def test_failed_empty_reply_is_not_added_and_clear_survives_restart(self):
        self.manager.generate('valid', 'chat', room_id='a')
        with patch.object(self.manager.adapters['codex'], 'generate', return_value={'reply':'<visual>{}</visual>'}):
            with self.assertRaises(RuntimeError):
                self.manager.generate('empty', 'chat', room_id='a')
        self.assertEqual([t['user'] for t in self.manager.history], ['valid'])
        self.manager.reset()
        self.assertEqual(ConversationContext(self.root).recent('a'), [])


if __name__ == '__main__':
    unittest.main()
