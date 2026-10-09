from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import json
import os
import sys
import tempfile
import unittest

from support import ROOT, engine, load

config=load('route2plugin.runtime_config',ROOT/'maibot/plugins/heart-realtime/runtime_config.py')
client=load('compat_client',ROOT/'vt/heart_realtime_client.py')


class ConfigurationTests(unittest.TestCase):
    def test_relative_paths_follow_new_plugin_folder(self):
        for name in ('伙伴甲的目录','another-user'):
            root=Path(Path.cwd().anchor)/name/'heart-realtime'
            settings=config.resolve_settings(root,{},environment={})
            self.assertEqual(Path(settings['model']),root.resolve()/'models'/config.MODEL_NAME)
            self.assertEqual(Path(settings['asr_python']),Path(sys.executable).resolve())
            self.assertNotIn('32477',str(settings['model']))

    def test_environment_overrides_local_settings(self):
        values=config.resolve_settings(ROOT,{'model':'old'},environment={'HEART_RT_MODEL':'new'})
        self.assertEqual(Path(values['model']),ROOT/'new')

    def test_original_explicit_python_path_preserved(self):
        values=config.resolve_settings(ROOT,{'asr_python':sys.executable},environment={})
        self.assertEqual(Path(values['asr_python']),Path(sys.executable).resolve())

    def test_empty_dependencies_uses_selected_python_environment(self):
        values=config.resolve_settings(ROOT,{'asr_dependencies':''},environment={})
        self.assertEqual(values['asr_dependencies'],'')

    def test_bad_ports_and_names_rejected(self):
        for bad in [{'enabled':'false'},{'port':True},{'asr_port':12396},
                    {'character_names':'角色甲'},{'character_names':['']},{'model':123}]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                config.resolve_settings(ROOT,bad,environment={})

    def test_binding_requires_matched_host_api(self):
        with self.assertRaisesRegex(RuntimeError,'不能只复制插件'):
            config.binding_names({'success':True},config.DEFAULTS)

    def test_current_character_and_aliases_received(self):
        binding={'api_version':1,'character_names':['角色甲','Alice']}
        self.assertEqual(config.binding_names(binding,config.DEFAULTS),['角色甲','Alice'])
        self.assertEqual(config.binding_names(binding,{**config.DEFAULTS,'character_names':['本地别名']}),['本地别名'])

    def test_missing_model_fails_without_starting_a_process(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(config.subprocess,'run') as run:
            result=config.check_asr_runtime(Path(folder),config.resolve_settings(Path(folder),{},environment={}))
            self.assertFalse(result['ready']);run.assert_not_called()

    def probe(self,details):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ('tokens.txt','encoder.int8.onnx','decoder.onnx','joiner.int8.onnx'):
                (root/name).touch()
            settings=config.resolve_settings(root,{'model':str(root)},environment={})
            result=SimpleNamespace(returncode=0,stdout=json.dumps(details),stderr='')
            with patch.object(config.subprocess,'run',return_value=result):
                return config.check_asr_runtime(root,settings)

    def test_old_sherpa_rejected(self):
        self.assertFalse(self.probe({'sherpa_version':'1.10.46','api_available':True})['ready'])

    def test_current_sherpa_accepted(self):
        self.assertTrue(self.probe({'sherpa_version':'1.13.8','api_available':True})['ready'])

    def test_cross_python_binary_import_error_reported(self):
        result=self.probe({'error':'ImportError: 二进制不匹配'})
        self.assertFalse(result['ready']);self.assertIn('二进制不匹配',result['errors'][0])

    def test_adapter_supports_renamed_plugin_directory(self):
        with patch.dict(os.environ,{'HEART_REALTIME_PLUGIN_DIR':str(ROOT/'改过名字的插件')},clear=True):
            self.assertEqual(client.plugin_root(),ROOT/'改过名字的插件')

    def test_adapter_supports_new_maibot_root(self):
        with patch.dict(os.environ,{'HEART_MAIBOT_ROOT':str(ROOT/'伙伴的MaiBot')},clear=True):
            self.assertEqual(client.plugin_root(),ROOT/'伙伴的MaiBot/plugins/heart-realtime')

    def test_adapter_missing_settings_is_actionable(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(RuntimeError,'HEART_MAIBOT_ROOT'):
            client.plugin_root()


class CharacterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_ack_uses_configured_names_instead_of_local_character(self):
        events=[]
        async def call(name,**kwargs):return {'success':True}
        async def emit(event):events.append(event)
        c=engine.Conversation('session','visitor','connection',call,emit,lambda *a,**k:None)
        c.character_names=['角色甲','Alice']
        try:
            await c.begin('u')
            await c.partial_input('u',1,'鱼宝在吗')
            self.assertFalse(any(e['type']=='heart-rt-ack' for e in events))
            await c.partial_input('u',2,'角色甲在吗')
            self.assertTrue(any(e['type']=='heart-rt-ack' for e in events))
            events.clear();await c.partial_input('u',3,'ALICE')
            self.assertTrue(any(e['type']=='heart-rt-ack' for e in events))
        finally:await c.close()


if __name__=='__main__':unittest.main()
