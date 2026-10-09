from pathlib import Path
from unittest.mock import patch
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import install_delivery as install
import prepare_frontend as frontend

class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.package=self.root/'package';self.target=self.root/'同伴 MaiBot'
        (self.target/'src').mkdir(parents=True);self.package.mkdir()
        self.rows=[]
        for name in ('a.py','b.py'):
            old=b'old\n';new=b'new\n';(self.target/'src'/name).write_bytes(old)
            (self.package/name).write_bytes(new)
            self.rows.append(dict(project='maibot',target='src/'+name,path=name,component='native',
                                 sha256=hashlib.sha256(new).hexdigest(),before_sha256=install.digest(old)))
        self.manifest()
        self.roots={'maibot':self.target}
    def manifest(self):
        (self.package/'INSTALL_MANIFEST.json').write_text(json.dumps({'entries':self.rows}),encoding='utf-8')
    def plan(self):return install.plan(self.roots,{'native'},self.package)
    def apply(self):return install.apply(self.roots,{'native'},stopped=True,reviewed=True,package=self.package)
    def test_dry_run_does_not_write(self):
        self.assertEqual([r['state'] for r in self.plan()],['update','update'])
        self.assertEqual((self.target/'src/a.py').read_bytes(),b'old\n')
        self.assertFalse((self.target/'.runtime').exists())
    def test_unknown_partner_change_blocks_whole_batch(self):
        (self.target/'src/b.py').write_text('伙伴的表情适配',encoding='utf-8')
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual((self.target/'src/a.py').read_bytes(),b'old\n')
    def test_missing_baseline_is_not_treated_as_new_file(self):
        (self.target/'src/b.py').unlink()
        self.assertEqual(self.plan()[1]['state'],'conflict')
    def test_apply_backup_idempotent_and_preserves_config(self):
        (self.target/'config.toml').write_text('my_voice="existing"',encoding='utf-8')
        result=self.apply();self.assertEqual(result['changed'],2)
        backup=Path(result['backups']['maibot'])
        self.assertEqual((backup/'src/a.py').read_bytes(),b'old\n')
        self.assertEqual(self.apply()['changed'],0)
        self.assertEqual((self.target/'config.toml').read_text(),'my_voice="existing"')
    def test_crlf_existing_source_is_known(self):
        (self.target/'src/a.py').write_bytes(b'old\r\n')
        self.assertEqual(self.plan()[0]['state'],'update')
    def test_corrupted_package_stops_before_write(self):
        (self.package/'b.py').write_text('changed')
        with self.assertRaises(ValueError):self.apply()
        self.assertFalse((self.target/'.runtime').exists())
    def test_path_traversal_is_rejected(self):
        self.rows[1]['target']='../outside.py';self.manifest()
        with self.assertRaises(ValueError):self.plan()
    def test_failure_rolls_back_earlier_write(self):
        original=install.os.replace;counter=0
        def fail_second(source,target):
            nonlocal counter
            counter+=1
            if counter==2:raise OSError('模拟写入失败')
            return original(source,target)
        with patch.object(install.os,'replace',side_effect=fail_second),self.assertRaises(OSError):self.apply()
        self.assertEqual((self.target/'src/a.py').read_bytes(),b'old\n')
        self.assertEqual((self.target/'src/b.py').read_bytes(),b'old\n')
    def test_existing_voice_and_mood_not_selected_by_default(self):
        self.assertTrue({'asr','mood','vt','vt-voice-reference','vt-legacy-reference'}.isdisjoint(install.DEFAULT_COMPONENTS))
    def test_frontend_mismatch_is_explicit(self):
        with self.assertRaises((ValueError,RuntimeError)):frontend.prepare_js('different build')
    def test_html_preserves_partner_content_and_is_idempotent(self):
        source='<html><head><title>伙伴角色</title></head><body>我的语音按钮</body></html>'
        result=frontend.prepare_html(source)
        self.assertIn('我的语音按钮',result);self.assertIn('伙伴角色',result)
        self.assertEqual(result,frontend.prepare_html(result))

spec=importlib.util.spec_from_file_location('delivery_voice_status',ROOT/'vt/voice_status.py')
health=importlib.util.module_from_spec(spec);spec.loader.exec_module(health)

class VoiceCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_existing_voice_is_not_probed_or_misreported(self):
        with patch.dict(health.os.environ,{},clear=True),patch.object(health.httpx,'AsyncClient') as client:
            for kind in ('VOICE','ASR'):
                result=await health.local_service_status(kind)
                self.assertEqual(result['state'],'external');self.assertIsNone(result['ready'])
            client.assert_not_called()
    async def test_remote_health_url_not_requested(self):
        with patch.dict(health.os.environ,{'HEART_VOICE_HEALTH_URL':'https://example.org/private'}),patch.object(health.httpx,'AsyncClient') as client:
            self.assertFalse((await health.local_voice_status())['ready']);client.assert_not_called()
    async def test_explicit_local_probe_ready_and_unknown_schema(self):
        class Client:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def get(self,url):return response
        from types import SimpleNamespace
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'ready':True})
        with patch.dict(health.os.environ,{'HEART_VOICE_HEALTH_URL':'http://127.0.0.1:9123/health'}),patch.object(health.httpx,'AsyncClient',return_value=Client()):
            self.assertEqual((await health.local_voice_status())['state'],'ready')
            response.json=lambda:{'status':'custom'}
            self.assertEqual((await health.local_voice_status())['state'],'external')
    def test_display_name_follows_partner_model(self):
        import ast,os
        from types import SimpleNamespace
        tree=ast.parse((ROOT/'vt/src/open_llm_vtuber/agent/agents/maibot_agent.py').read_text(encoding='utf-8'))
        names=[kw.value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)
               and n.func.id=='DisplayText' for kw in n.keywords if kw.arg=='name' and isinstance(kw.value,ast.BoolOp)]
        self.assertEqual(len(names),2)
        for value in names:
            code=compile(ast.Expression(value),'display-name','eval')
            env={'os':os,'self':SimpleNamespace(_live2d_model=SimpleNamespace(live2d_model_name='伙伴形象'))}
            with patch.dict(os.environ,{},clear=True):self.assertEqual(eval(code,env),'伙伴形象')
            with patch.dict(os.environ,{'HEART_CHARACTER_NAME':'自定义称呼'}):self.assertEqual(eval(code,env),'自定义称呼')

if __name__=='__main__':unittest.main()
