"""用模拟健康响应验证重复启动；不打开浏览器、不启动真实服务。"""
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import json
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartLocalTests(unittest.TestCase):
    def launch(self, health):
        config = json.dumps({'python': 'unused-python', 'port': 8790})
        with patch.object(Path, 'read_text', return_value=config), \
                patch('urllib.request.urlopen', return_value=BytesIO(json.dumps(health).encode())), \
                patch('sys.argv', ['start_local.py']), \
                patch('webbrowser.open') as browser, \
                patch('subprocess.Popen') as process:
            try:
                runpy.run_path(str(ROOT / 'start_local.py'), run_name='__main__')
            finally:
                process.assert_not_called()
            return browser

    def test_running_v03_reopens_existing_page_without_new_service(self):
        browser = self.launch({'service': 'storyroom', 'ok': True, 'version': '0.3.0', 'protocol': '0.3'})
        browser.assert_called_once_with('http://127.0.0.1:8790')

    def test_old_v02_requires_normal_platform_shutdown(self):
        with self.assertRaisesRegex(RuntimeError, '另一个版本'):
            self.launch({'service': 'storyroom', 'ok': True, 'version': '0.2.0', 'protocol': '0.2'})

    def test_unrelated_service_is_not_reused(self):
        with self.assertRaisesRegex(RuntimeError, '其他程序'):
            self.launch({'service': 'other', 'ok': True})

    def test_unhealthy_storyroom_is_not_reused(self):
        with self.assertRaisesRegex(RuntimeError, '健康检查'):
            self.launch({'service': 'storyroom', 'ok': False})

    def test_mismatched_protocol_requires_shutdown(self):
        with self.assertRaisesRegex(RuntimeError, '另一个版本'):
            self.launch({'service': 'storyroom', 'ok': True, 'version': '0.3.0', 'protocol': '0.2'})
