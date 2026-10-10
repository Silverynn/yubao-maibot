"""用目标MaiBot真实校验器检查加载入口，不调用模型。"""
from pathlib import Path
import os
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('STORYROOM_MAIBOT_ROOT'), '需要指定目标MaiBot路径')
class NativeManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.maibot = Path(os.environ['STORYROOM_MAIBOT_ROOT']).resolve()
        sys.path.insert(0, str(cls.maibot))
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator
        cls.validator = ManifestValidator(project_root=cls.maibot, log_errors=False)

    def test_source_plugin_passes_native_manifest_validation(self):
        result = self.validator.load_from_plugin_path(ROOT/'plugins/storyroom_pc')
        self.assertIsNotNone(result, self.validator.errors)

    def test_missing_fields_reproduce_original_load_error(self):
        import json
        original = json.loads((ROOT/'plugins/storyroom_pc/_manifest.json').read_text(encoding='utf-8'))
        del original['author']['url'], original['urls'], original['i18n']
        self.assertFalse(self.validator.validate(original))
        for field in ('author.url', 'urls', 'i18n'):
            self.assertIn('缺少必需字段: '+field, self.validator.errors)
