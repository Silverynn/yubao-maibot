from pathlib import Path
from unittest.mock import patch
import importlib.util
import json
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('increment_installer',ROOT/'tools/install_delivery.py')
install=importlib.util.module_from_spec(spec);spec.loader.exec_module(install)

class IncrementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.roots={'maibot':base/'伙伴自选MaiBot','vt':base/'自己的VT'}
        for root in self.roots.values():(root/'src').mkdir(parents=True)
        self.manifest=json.loads((ROOT/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))
        for row in self.manifest['entries']:
            if row.get('baseline'):
                target=self.roots[row['project']]/row['target'];target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((ROOT/row['baseline']).read_bytes())
        plugin=self.roots['maibot']/'plugins/heart-realtime';plugin.mkdir(parents=True,exist_ok=True)
        (plugin/'_manifest.json').write_text('{"id":"heart.realtime"}')
        self.voice=self.roots['vt']/'conf.yaml';self.voice.write_text('tts: existing-partner-voice')
        self.state=self.roots['maibot']/'data/storyroom-keep.json';self.state.parent.mkdir();self.state.write_text('{"keep":true}')
    def plan(self, components=None):return install.plan(self.roots,components or install.DEFAULT_COMPONENTS)
    def apply(self, **kwargs):return install.apply(self.roots,install.DEFAULT_COMPONENTS,stopped=True,reviewed=True,**kwargs)
    def test_plan_recognizes_eleven_actual_entries_without_writing(self):
        rows=self.plan();self.assertEqual(len(rows),11)
        self.assertEqual({r['state'] for r in rows},{'new','update'})
        self.assertFalse((self.roots['vt']/'.runtime').exists())
    def test_both_sides_backup_idempotency_and_user_data(self):
        result=self.apply();self.assertEqual(result['changed'],11)
        self.assertEqual(self.apply()['changed'],0)
        self.assertEqual(set(result['backups']),{'maibot','vt'})
        self.assertEqual(self.voice.read_text(),'tts: existing-partner-voice')
        self.assertEqual(self.state.read_text(),'{"keep":true}')
    def test_unknown_modified_file_blocks_all_writes(self):
        (self.roots['vt']/'heart_realtime_client.py').write_text('partner custom code')
        with self.assertRaises(ValueError):self.apply()
        self.assertFalse((self.roots['maibot']/'heart_shared/console_view.py').exists())
    def test_half_bundle_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'半套'):
            install.apply(self.roots,{'shared','realtime'},stopped=True,reviewed=True)
    def test_stopped_review_gate(self):
        with self.assertRaises(ValueError):install.apply(self.roots,install.DEFAULT_COMPONENTS)
    def test_crlf_baseline_is_accepted(self):
        path=self.roots['vt']/'heart_realtime_client.py'
        path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'))
        self.assertTrue(all(r['state']!='conflict' for r in self.plan()))
    def test_renamed_plugin_updated_in_place(self):
        old=self.roots['maibot']/'plugins/heart-realtime';new=old.with_name('伙伴重命名实时插件')
        old.rename(new)
        self.assertEqual(self.apply()['changed'],11)
        self.assertFalse(old.exists());self.assertTrue((new/'engine.py').is_file())
    def test_duplicate_plugin_blocks(self):
        duplicate=self.roots['maibot']/'plugins/another-copy';duplicate.mkdir()
        (duplicate/'_manifest.json').write_text('{"id":"heart.realtime"}')
        with self.assertRaisesRegex(ValueError,'重复'):self.apply()
    def test_write_failure_rolls_back_both_projects(self):
        original=install.os.replace;count=0
        def fail(source,dest):
            nonlocal count
            count+=1
            if count==10:raise OSError('模拟后半批写失败')
            return original(source,dest)
        with patch.object(install.os,'replace',side_effect=fail),self.assertRaises(OSError):self.apply()
        self.assertEqual({r['state'] for r in self.plan()},{'new','update'})
    def test_missing_vt_root_does_not_apply_maibot_half(self):
        with self.assertRaises(ValueError):install.apply({'maibot':self.roots['maibot']},install.DEFAULT_COMPONENTS,stopped=True,reviewed=True)
        self.assertFalse((self.roots['maibot']/'heart_shared/console_view.py').exists())

if __name__=='__main__':unittest.main()
