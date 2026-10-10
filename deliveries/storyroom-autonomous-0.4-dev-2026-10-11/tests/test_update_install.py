from pathlib import Path
import importlib.util
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pc_update_script', ROOT/'scripts/update_pc.py')
updater = importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);(self.root/'src/plugin_runtime').mkdir(parents=True)
        self.target = self.root/'plugins/storyroom_pc'
        shutil.copytree(ROOT/'plugins/storyroom_pc',self.target,ignore=shutil.ignore_patterns('__pycache__','config.toml'))
        (self.target/'config.toml').write_text('[connection]\nenabled=false\nplayer_token="isolated-private-player-key"\n',encoding='utf-8')
        (self.target/'.git').mkdir();(self.target/'.git/keep').write_text('own git')
        (self.root/'data/keep').parent.mkdir();(self.root/'data/keep').write_text('persisted memory')

    def test_update_preserves_configuration_git_data_and_backs_up(self):
        before=(self.target/'config.toml').read_bytes()
        backup=updater.update(self.root,True)
        self.assertEqual((self.target/'config.toml').read_bytes(),before)
        self.assertEqual((backup/'config.toml').read_bytes(),before)
        self.assertEqual((self.target/'.git/keep').read_text(),'own git')
        self.assertEqual((self.root/'data/keep').read_text(),'persisted memory')

    def test_modified_source_not_overwritten(self):
        (self.target/'worker.py').write_text('partner custom code')
        with self.assertRaises(ValueError):updater.update(self.root,True)
        self.assertEqual((self.target/'worker.py').read_text(),'partner custom code')

    def test_enabled_plugin_is_not_hot_overwritten(self):
        (self.target/'config.toml').write_text('[connection]\nenabled=true\n')
        with self.assertRaises(ValueError):updater.update(self.root,True)

    def test_published_v03_manifest_bug_can_be_fixed_without_losing_config(self):
        original = ROOT/'tests/fixtures/storyroom-v03-manifest-before-fix.json'
        shutil.copy2(original,self.target/'_manifest.json')
        shutil.copy2(ROOT/'tests/fixtures/worker-v03.py',self.target/'worker.py')
        before=(self.target/'config.toml').read_bytes()
        backup=updater.update(self.root,True)
        self.assertEqual((backup/'_manifest.json').read_bytes(),original.read_bytes())
        self.assertEqual((self.target/'_manifest.json').read_bytes(),(ROOT/'plugins/storyroom_pc/_manifest.json').read_bytes())
        self.assertEqual((self.target/'config.toml').read_bytes(),before)

    def test_unknown_manifest_changes_are_not_overwritten(self):
        import json
        path=self.target/'_manifest.json'
        custom=json.loads(path.read_text(encoding='utf-8'));custom['description']='伙伴自定义描述'
        path.write_text(json.dumps(custom,ensure_ascii=False),encoding='utf-8')
        before=path.read_bytes()
        with self.assertRaises(ValueError):updater.update(self.root,True)
        self.assertEqual(path.read_bytes(),before)
