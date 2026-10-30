"""Clock rollover and fresh per-turn context, without online model calls."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_clock import beijing_time
from providers import ProviderManager


class BeijingClockTests(unittest.TestCase):
    def test_utc_day_rollover_uses_beijing_date_and_weekday(self):
        stamp = datetime(2026, 10, 28, 16, 0, 1, tzinfo=timezone.utc).timestamp()
        value = beijing_time(stamp)
        self.assertEqual(value['date'], '2026-10-29')
        self.assertEqual(value['time'], '00:00:01')
        self.assertEqual(value['weekday'], '星期四')
        self.assertEqual(value['iso'], '2026-10-29T00:00:01+08:00')
        self.assertEqual(value['unix_ms'], int(stamp * 1000))

    def test_both_adapters_get_fresh_time_each_turn_without_persona_changes(self):
        stamp = datetime(2026, 10, 28, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        for name in ('codex', 'deepseek'):
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as folder:
                calls = []
                def chat_side_effect(*args, **kwargs):
                    calls.append(1)
                    return {'reply': '好呀' if len(calls) == 1 else '好呀好呀'}
                resident = SimpleNamespace(executable='unused', reset=Mock(),
                    chat=Mock(side_effect=chat_side_effect))
                manager = ProviderManager(resident, Path(folder), {'provider': name})
                persona = manager.adapters[name].persona_provider()
                captured = []

                def fake_stream(body, *args):
                    captured.append(json.loads(body['messages'][-1]['content']))
                    return {'role': 'assistant', 'content': '好呀' if len(captured) == 1 else '好呀好呀'}, 1, None

                deepseek = manager.adapters['deepseek']
                with patch.object(deepseek, 'key', return_value='offline'), \
                     patch.object(deepseek, '_stream', side_effect=fake_stream):
                    for offset in (0, 2):
                        with patch('live_clock.time.time', return_value=stamp + offset):
                            manager.generate('现在几点？', 'chat')
                if name == 'codex':
                    captured = [json.loads(c.args[0]) for c in resident.chat.call_args_list]
                self.assertEqual([x['current_time']['time'] for x in captured], ['07:59:59', '08:00:01'])
                self.assertTrue(all(x['current_time']['timezone'] == 'Asia/Shanghai' for x in captured))
                self.assertTrue(all(x['message'] == '现在几点？' for x in captured))
                self.assertEqual(manager.adapters[name].persona_provider(), persona)
                self.assertEqual([x['user'] for x in manager.history], ['现在几点？'] * 2)


if __name__ == '__main__':
    unittest.main()
