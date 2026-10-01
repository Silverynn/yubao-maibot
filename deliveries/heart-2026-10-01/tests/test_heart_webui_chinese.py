"""验证两个 Heart 插件在真正传给 WebUI 的 Schema 中显示中文，且配置键和值不变。"""

import importlib.util
import sys
import unittest
from pathlib import Path

from maibot_sdk import MaiBotPlugin


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "extensions"))
sys.path.insert(0, str(ROOT / "plugins"))

from heart_mood.plugin import MoodPlugin  # noqa: E402


def memory_plugin_class():
    spec = importlib.util.spec_from_file_location("heart_memory_ui_chinese", ROOT / "plugins/heart_memory_audit/plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MemoryAudit


class ChineseWebUITests(unittest.TestCase):
    def test_both_plugin_pages_are_chinese_without_changing_keys_or_defaults(self):
        for plugin_class in (memory_plugin_class(), MoodPlugin):
            with self.subTest(plugin=plugin_class.__name__):
                original = MaiBotPlugin.build_config_schema.__func__(plugin_class)
                shown = plugin_class().get_webui_config_schema(plugin_name="Heart")
                self.assertEqual(set(shown["sections"]), set(original["sections"]))
                self.assertEqual(plugin_class.build_default_config(), plugin_class().get_default_config())
                for section_key, original_section in original["sections"].items():
                    section = shown["sections"][section_key]
                    self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in section["title"]))
                    self.assertEqual(set(section["fields"]), set(original_section["fields"]))
                    for field_key, original_field in original_section["fields"].items():
                        field = section["fields"][field_key]
                        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in field["label"]), field_key)
                        self.assertEqual(field["default"], original_field["default"])
                        self.assertEqual(field["type"], original_field["type"])
                        self.assertEqual(field["min"], original_field["min"])
                        self.assertEqual(field["max"], original_field["max"])
                        self.assertEqual(field["description"], original_field["description"])


if __name__ == "__main__":
    unittest.main()
