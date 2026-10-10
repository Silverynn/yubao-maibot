"""安装演练全部在临时模拟MaiBot中，拒绝覆盖并保持其他插件。"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='storyroom-install-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/'plugins/other').mkdir(parents=True)
        (self.root/'src/plugin_runtime').mkdir(parents=True)
        (self.root/'plugins/other/keep.txt').write_text('伙伴已有语音包', encoding='utf-8')
        self.script = Path(__file__).resolve().parents[1]/'scripts/install_pc.py'

    def run_install(self, *args):
        return subprocess.run([sys.executable, '-X', 'utf8', '-B', str(self.script), str(self.root), *args],
                              text=True, encoding='utf-8', capture_output=True)

    def test_preflight_does_not_install(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.assertFalse((self.root/'plugins/storyroom_pc').exists())

    def test_install_is_disabled_preserves_other_plugin_and_has_git(self):
        self.assertEqual(self.run_install('--install').returncode, 0)
        target = self.root/'plugins/storyroom_pc'
        config = tomllib.loads((target/'config.toml').read_text(encoding='utf-8'))
        self.assertFalse(config['connection']['enabled'])
        self.assertEqual(config['connection']['player_token'], '')
        self.assertEqual((self.root/'plugins/other/keep.txt').read_text(encoding='utf-8'), '伙伴已有语音包')
        self.assertTrue((target/'.git').is_dir())
        self.assertEqual(json.loads((target/'_manifest.json').read_text(encoding='utf-8'))['capabilities'], ['llm.generate'])

    def test_existing_target_is_never_overwritten(self):
        target = self.root/'plugins/storyroom_pc'
        target.mkdir()
        marker = target/'plugin.py'
        marker.write_text('伙伴修改过的版本', encoding='utf-8')
        self.assertNotEqual(self.run_install('--install').returncode, 0)
        self.assertEqual(marker.read_text(encoding='utf-8'), '伙伴修改过的版本')
