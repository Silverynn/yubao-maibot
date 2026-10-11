from pathlib import Path
import hashlib,importlib.util,json,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('feedback_installer',ROOT/'tools/install_delivery.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

class Installer(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.root=self.base/'bot';self.pkg=self.base/'package'
        for name in ('src/services/heart_realtime_drafts.py','src/services/heart_memory_backend.py'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# baseline\n')
        folder=self.root/'plugins/renamed-realtime';folder.mkdir(parents=True)
        (folder/'_manifest.json').write_text(json.dumps({'id':'heart.realtime'}))
        dest=self.root/'heart_shared/test.py';dest.parent.mkdir(parents=True);dest.write_text('# old\n')
        source=self.pkg/'maibot/heart_shared/test.py';source.parent.mkdir(parents=True);source.write_text('# new\n')
        self.entry={'path':'maibot/heart_shared/test.py','target':'heart_shared/test.py','sha256':installer.digest(source),'accepted_before':[installer.digest(dest)]}
        self.save_manifest();(self.root/'config').mkdir();(self.root/'config/private.toml').write_text('unchanged')
    def tearDown(self):self.temp.cleanup()
    def save_manifest(self):
        (self.pkg/'INSTALL_MANIFEST.json').write_text(json.dumps({'entries':[self.entry]}))
    def test_dry_run_leaves_source_and_configuration_untouched(self):
        self.assertEqual(installer.plan(self.root,self.pkg)[0]['state'],'update')
        self.assertEqual((self.root/'heart_shared/test.py').read_text(),'# old\n')
        self.assertFalse((self.root/'data').exists())
    def test_apply_backups_and_preserves_private_config(self):
        rows=installer.plan(self.root,self.pkg);result=installer.apply(self.root,rows,stopped=True,package=self.pkg)
        self.assertEqual((Path(result['backup'])/'heart_shared/test.py').read_text(),'# old\n')
        self.assertEqual((self.root/'heart_shared/test.py').read_text(),'# new\n')
        self.assertEqual((self.root/'config/private.toml').read_text(),'unchanged')
        self.assertEqual(installer.apply(self.root,installer.plan(self.root,self.pkg),stopped=True,package=self.pkg),{'changed':0})
    def test_unknown_customizations_block_entire_apply(self):
        (self.root/'heart_shared/test.py').write_text('# custom\n')
        rows=installer.plan(self.root,self.pkg)
        self.assertEqual(rows[0]['state'],'conflict')
        with self.assertRaisesRegex(ValueError,'未知自改版'):installer.apply(self.root,rows,stopped=True,package=self.pkg)
        self.assertEqual((self.root/'heart_shared/test.py').read_text(),'# custom\n')
    def test_stopped_ack_required_and_postcheck_changes_rejected(self):
        rows=installer.plan(self.root,self.pkg)
        with self.assertRaises(ValueError):installer.apply(self.root,rows,package=self.pkg)
        (self.root/'heart_shared/test.py').write_text('# newer\n')
        with self.assertRaisesRegex(ValueError,'预检后'):installer.apply(self.root,rows,stopped=True,package=self.pkg)
    def test_traversal_and_tampered_source_rejected(self):
        self.entry['target']='../outside.py';self.save_manifest()
        with self.assertRaises(ValueError):installer.plan(self.root,self.pkg)
        self.entry['target']='heart_shared/test.py';self.save_manifest()
        (self.pkg/self.entry['path']).write_text('# tampered\n')
        with self.assertRaisesRegex(ValueError,'校验失败'):installer.plan(self.root,self.pkg)
    def test_plugin_relocation_and_duplicate_detection(self):
        self.assertEqual(installer.plugin_folder(self.root,'heart.realtime','ignored',True),'plugins/renamed-realtime')
        another=self.root/'plugins/duplicate';another.mkdir();(another/'_manifest.json').write_text(json.dumps({'id':'heart.realtime'}))
        with self.assertRaisesRegex(ValueError,'插件重复'):installer.plan(self.root,self.pkg)

if __name__=='__main__':unittest.main(verbosity=2)
