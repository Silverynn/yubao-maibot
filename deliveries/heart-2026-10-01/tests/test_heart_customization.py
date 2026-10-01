"""用虚构配置验证 WebUI 字段、宿主读取和两段 AI 提示词的接线。"""
import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'extensions'))
import heart_memory_backend as backend


class CustomizationTests(unittest.TestCase):
    def test_webui_schema_exposes_memory_controls(self):
        spec = importlib.util.spec_from_file_location('heart_memory_ui_schema',
            ROOT / 'plugins/heart_memory_audit/plugin.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        schema = module.Config.model_json_schema()
        self.assertTrue({'auto_candidates', 'conflicts', 'forget', 'group_recall'} <= set(schema['properties']))
        webui = module.MemoryAudit.build_config_schema()
        for section, fields in {'auto_candidates': {'selection_guidance', 'auto_write_verified'},
                                'conflicts': {'guidance', 'min_confidence', 'duplicate_min_confidence'},
                                'forget': {'guidance', 'min_confidence', 'confirmation_minutes'},
                                'group_recall': {'display_limit', 'include_group_summaries'}}.items():
            model = getattr(module.Config(), section)
            self.assertTrue(fields <= set(type(model).model_fields))
            self.assertTrue(fields <= set(webui['sections'][section]['fields']))

    def test_host_reads_custom_values_and_clamps_safety_floor(self):
        contents = '''[plugin]
enabled = true
[auto_candidates]
selection_guidance = "只记长期偏好"
[conflicts]
min_confidence = 0.2
duplicate_min_confidence = 0.1
guidance = "注意时间先后"
[forget]
min_confidence = 0.95
timeout_seconds = 8
confirmation_minutes = 2
list_page_size = 7
guidance = "仅定位明确目标"
[group_recall]
display_limit = 3
include_group_summaries = false
'''
        with patch.object(Path, 'read_text', return_value=contents):
            values = backend.settings()
        self.assertEqual(values['selection_guidance'], '只记长期偏好')
        self.assertEqual(values['conflict_min_confidence'], 0.8)
        self.assertEqual(values['duplicate_min_confidence'], 0.8)
        self.assertEqual(values['conflict_guidance'], '注意时间先后')
        self.assertEqual(values['forget_min_confidence'], 0.95)
        self.assertEqual(values['forget_timeout_seconds'], 8)
        self.assertEqual(values['forget_confirmation_minutes'], 2)
        self.assertEqual(values['forget_list_page_size'], 7)
        self.assertEqual(values['forget_guidance'], '仅定位明确目标')
        self.assertEqual(values['group_recall_display_limit'], 3)
        self.assertFalse(values['group_recall_include_summaries'])

    def test_custom_guidance_reaches_both_model_requests(self):
        prompts = []
        class Client:
            def __init__(self, **kwargs):
                pass
            async def generate_response(self, prompt, **kwargs):
                prompts.append(prompt)
                value = {'supported': True, 'confidence': 1.0, 'verdict': 'clear', 'conflict_ids': []} \
                    if len(prompts) == 1 else {'intent': False, 'confidence': 0, 'target_ids': []}
                return types.SimpleNamespace(response=json.dumps(value))
        fake = types.ModuleType('src.services.llm_service')
        fake.LLMServiceClient = Client
        config = {'conflict_guidance': '用户自选冲突判断原则',
                  'forget_guidance': '用户自选遗忘定位原则'}
        with patch.dict(sys.modules, {'src.services.llm_service': fake}), patch.object(backend, 'settings', return_value=config):
            asyncio.run(backend.NativeBackend().judge({'chat_id': 'fake', 'text': '虚构事实'}, [], {'evidence': ['虚构原话']}))
            asyncio.run(backend.NativeBackend().match_natural_forget(
                {'session_id': 'fake', 'value': '请忘掉虚构事实'}, [{'content': '虚构事实'}]))
        self.assertIn(config['conflict_guidance'], prompts[0])
        self.assertIn(config['forget_guidance'], prompts[1])
        self.assertIn('不能放宽', prompts[0])
        self.assertIn('duplicate_ids', prompts[0])
        self.assertIn('不能放宽', prompts[1])


if __name__ == '__main__':
    unittest.main()
