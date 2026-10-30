"""User playlist, viewer requests and free play share one native order; no live calls."""
import copy
import tempfile
import unittest
from pathlib import Path
from music import MusicService
from rules_config import RuleConfig
from test_integrations import FakeBridge, Clock


def track(identifier):
    return {'id': str(identifier), 'title': '歌曲'+str(identifier), 'artist': '歌手'}


class PlaylistBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.playlist = [track('bg1'), track('bg2'), track('bg3')]
        self.index = 0
        self.observe()

    def observe(self):
        self.snapshot['current'] = copy.deepcopy(self.playlist[self.index])
        self.snapshot['next'] = copy.deepcopy(self.playlist[self.index+1]) if self.index+1 < len(self.playlist) else None

    def advance(self):
        self.index += 1
        self.observe()

    def request(self, action, **kwargs):
        if action == 'execute' and kwargs.get('command') == 'InsertNext':
            self.calls.append((action, copy.deepcopy(kwargs)))
            self.playlist.insert(self.index+1, copy.deepcopy(kwargs['track']))
            self.observe()
            return {'outcome': 'verified', 'snapshot': copy.deepcopy(self.snapshot)}
        if action == 'execute' and kwargs.get('command') == 'PlaySelected':
            self.calls.append((action, copy.deepcopy(kwargs)))
            # Switching now: the requested song plays at once and the user's own
            # list stays intact behind it, exactly like the native player does.
            self.playlist.insert(self.index+1, copy.deepcopy(kwargs['track']))
            self.index += 1
            self.observe()
            return {'outcome': 'verified', 'snapshot': copy.deepcopy(self.snapshot)}
        return super().request(action, **kwargs)


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bridge = PlaylistBridge()
        self.clock = Clock()
        self.music = MusicService(self.root, self.bridge, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def rules(self, **overrides):
        book = RuleConfig(self.root)
        book.data['music'].update(overrides)
        return book

    def service(self, **overrides):
        return MusicService(self.root, self.bridge, self.rules(**overrides), clock=self.clock)

    def add(self, identifier, music=None):
        self.bridge.tracks = [track(identifier)]
        return (music or self.music).order('歌', user_id=identifier, automatic=True)

    def commands(self):
        return [p['command'] for a, p in self.bridge.calls if a == 'execute']

    def test_viewer_request_switches_to_it_immediately(self):
        self.add('r1'); self.music.tick()
        self.assertEqual(self.commands(), ['PlaySelected'])
        self.assertEqual(self.bridge.snapshot['current']['id'], 'r1')
        self.music.tick()
        self.assertEqual(self.music.current['track']['id'], 'r1')

    def test_second_request_keeps_its_place_behind_the_first(self):
        self.add('r1'); self.add('r2')
        self.music.tick(); self.music.tick()
        self.assertEqual(self.music.current['track']['id'], 'r1')
        self.music.tick()
        # The song on air is a viewer request, so the next one waits its turn.
        self.assertEqual(self.bridge.snapshot['current']['id'], 'r1')
        self.assertEqual(self.commands(), ['PlaySelected', 'InsertNext'])
        self.assertEqual(self.bridge.snapshot['next']['id'], 'r2')

    def test_free_play_fills_the_room_when_the_player_runs_out(self):
        music = self.service(autoplay_playlist=['自由歌'])
        self.bridge.tracks = [track('free1')]
        self.bridge.snapshot['current'] = None
        music.enabled = True
        music.tick()
        self.assertEqual([t['track']['id'] for t in music.queue], ['free1'])
        self.assertEqual(music.queue[0]['source'], 'auto')
        music.tick()
        self.assertEqual(self.bridge.snapshot['current']['id'], 'free1')
        # It never turns itself off just because nobody is requesting.
        music.tick()
        self.assertTrue(music.enabled)

    def test_free_play_never_interrupts_a_running_song(self):
        music = self.service(autoplay_playlist=['自由歌'])
        self.bridge.tracks = [track('free1')]
        music.enabled = True
        music.tick()
        self.assertEqual(music.queue, [])
        self.assertEqual(self.bridge.snapshot['current']['id'], 'bg1')

    def test_viewer_request_drops_a_pending_free_play_entry(self):
        music = self.service(autoplay_playlist=['自由歌'])
        self.bridge.tracks = [track('free1')]
        self.bridge.snapshot['current'] = None
        music.enabled = True
        music.tick()
        self.assertEqual(len(music.queue), 1)
        self.bridge.tracks = [track('r1')]
        music.order('歌', 'u1', '观众', automatic=True)
        self.assertEqual([t['track']['id'] for t in music.queue], ['r1'])

    def test_free_play_keeps_the_player_alive(self):
        music = self.service(autoplay_keep_playing_seconds=30)
        self.bridge.snapshot['capabilities']['resume'] = True
        music.enabled = True
        music.tick()
        self.assertEqual([p['command'] for a, p in self.bridge.calls if a == 'execute'], [])
        self.clock.advance(31)
        music.tick()
        self.assertEqual([p['command'] for a, p in self.bridge.calls if a == 'execute'], ['Resume'])
        self.clock.advance(31)
        music.tick()
        self.assertEqual([p['command'] for a, p in self.bridge.calls if a == 'execute'], ['Resume', 'Resume'])

    def test_free_play_resumes_after_the_player_comes_back(self):
        music = self.service()
        music.enabled = False
        music.tick()
        self.assertTrue(music.enabled)

    def test_free_play_waits_for_the_operator_while_an_error_is_showing(self):
        music = self.service()
        music.enabled, music.error = False, '网易云已断开，点歌队列保留'
        music.tick()
        self.assertFalse(music.enabled)

    def test_request_waits_behind_current_song_when_switching_disabled(self):
        music = self.service(request_switches_now=False)
        self.add('r1', music); self.add('r2', music)
        music.tick(); music.tick()
        self.assertEqual(self.bridge.snapshot['current']['id'], 'bg1')
        self.assertEqual(self.bridge.snapshot['next']['id'], 'r1')
        self.assertEqual([t['id'] for t in self.bridge.playlist], ['bg1','r1','bg2','bg3'])
        self.bridge.advance(); music.tick()
        self.assertEqual(music.current['track']['id'], 'r1')
        self.assertEqual(self.bridge.snapshot['next']['id'], 'r2')
        self.bridge.advance(); music.tick()
        self.assertEqual(music.current['track']['id'], 'r2')
        self.assertEqual(self.bridge.snapshot['next']['id'], 'bg2')
        self.bridge.advance(); music.tick()
        self.assertIsNone(music.current)
        self.assertEqual([t['id'] for t in self.bridge.playlist], ['bg1','r1','r2','bg2','bg3'])
        self.assertEqual(self.commands(), ['InsertNext','InsertNext'])

    def test_long_background_song_does_not_time_out_or_reinsert(self):
        music = self.service(request_switches_now=False)
        self.add('r1', music); music.tick()
        self.clock.advance(3600); music.tick()
        self.assertEqual(self.bridge.snapshot['current']['id'], 'bg1')
        self.assertEqual(len([a for a,_ in self.bridge.calls if a=='execute']), 1)

    def test_empty_player_can_start_without_waiting_for_an_impossible_song_end(self):
        self.bridge.snapshot['current'] = None
        self.add('r1'); self.music.tick()
        self.assertEqual([p['command'] for a,p in self.bridge.calls if a=='execute'], ['PlaySelected'])

    def test_guard_is_allowed_to_complete_transition_before_pausing(self):
        music = self.service(request_switches_now=False)
        self.add('r1', music); music.tick()
        self.bridge.snapshot.update(current=track('unexpected'), nextGuardState='waitingLateTarget')
        music.tick()
        self.clock.advance(3)
        self.bridge.snapshot['current'] = track('r1')
        music.tick()
        self.assertEqual(music.current['track']['id'], 'r1')

    def test_failed_handoff_preserves_requests_and_never_overwrites_background(self):
        music = self.service(request_switches_now=False)
        self.add('r1', music); music.tick()
        self.bridge.snapshot['current'] = track('unexpected')
        music.tick(); self.clock.advance(11)
        with self.assertRaisesRegex(RuntimeError, '未切到'):
            music.tick()
        self.assertEqual(len(music.queue), 1)
        self.assertEqual([p['command'] for a,p in self.bridge.calls if a=='execute'], ['InsertNext'])

    def test_staged_request_survives_restart_without_duplicate_native_insertion(self):
        music = self.service(request_switches_now=False)
        self.add('r1', music); music.tick()
        restored = MusicService(self.root, self.bridge, self.rules(request_switches_now=False), clock=self.clock)
        restored.start_requested_queue(); restored.tick()
        self.assertEqual([p['command'] for a,p in self.bridge.calls if a=='execute'], ['InsertNext'])
        self.assertEqual(self.bridge.snapshot['current']['id'], 'bg1')

    def test_verified_native_next_can_resume_after_restart_without_reinsertion(self):
        music = self.service(request_switches_now=False)
        self.add('r1', music); music.tick()
        restored = MusicService(self.root, self.bridge, self.rules(request_switches_now=False), clock=self.clock)
        restored.control('start'); restored.tick()
        self.assertTrue(restored.enabled)
        self.assertEqual([p['command'] for a,p in self.bridge.calls if a=='execute'], ['InsertNext'])

    def test_no_insert_capability_never_falls_back_to_interrupting_current_song(self):
        music = self.service(request_switches_now=False)
        self.bridge.snapshot['capabilities']['insertNext'] = False
        self.add('r1', music)
        with self.assertRaisesRegex(RuntimeError, '下一首插入能力'):
            music.tick()
        self.assertEqual(self.bridge.snapshot['current']['id'], 'bg1')
        self.assertFalse(any(action == 'execute' for action, _ in self.bridge.calls))
        self.assertEqual(music.queue[0]['state'], 'waiting')


if __name__ == '__main__':
    unittest.main()
