"""Publication contract: this package is music/chat only, with complete model assets."""
import ast
import json
import unittest
from pathlib import Path
from live_tools import TOOL_SPECS
from rules_config import RuleConfig

ROOT=Path(__file__).parents[1]

class ReleaseContentsTests(unittest.TestCase):
    def test_no_watch_or_game_modules_or_tool_permissions(self):
        for name in ['watch_video.py','watch_bridge.mjs','watch_ui.js','agnes_vision.py','spire_server.py','game.html']:
            self.assertFalse((ROOT/name).exists(),name)
        self.assertFalse(any(s['name'].startswith(('watch_','spire_')) for s in TOOL_SPECS))
        tree=ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
        self.assertFalse(any(isinstance(n,ast.Constant) and n.value=='/api/studio/watch' for n in ast.walk(tree)))

    def test_live2d_model_references_exist_with_author_notice(self):
        model=ROOT/'assets/live2d/model'
        data=json.loads((model/'c_0120.model3.json').read_text(encoding='utf-8'))
        refs=data['FileReferences']
        files=[refs['Moc']]+refs.get('Textures',[])
        files += [refs[k] for k in ('Physics','Pose','DisplayInfo') if refs.get(k)]
        files += [e['File'] for e in refs.get('Expressions',[])]
        files += [motion['File'] for group in refs.get('Motions',{}).values() for motion in group]
        for file in files:self.assertTrue((model/file).is_file(),file)
        notice=(ROOT/'assets/live2d/模型使用须知.txt').read_text(encoding='utf-8-sig')
        self.assertIn('11272072',notice)

    def test_published_rules_keep_music_window_and_no_watch_section(self):
        rules=RuleConfig(ROOT).snapshot()
        self.assertNotIn('watch',rules)
        self.assertEqual(rules['studio']['chat_max_pending'],20)
        self.assertEqual(rules['music']['max_pending'],12)
        self.assertEqual(rules['music']['max_pending_per_user'],2)

if __name__=='__main__':unittest.main()
