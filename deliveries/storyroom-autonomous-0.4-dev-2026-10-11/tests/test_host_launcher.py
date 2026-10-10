"""主机连接生命周期：服务复用、进程归属、失败提示与地址过滤，不连接公网。"""
from pathlib import Path
from unittest.mock import Mock, patch
import json
import tempfile
import unittest

import host_launcher as host


class HostLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = []
        self.connection = host.Connection(lambda *e: self.events.append(e), Path(self.temp.name))

    def test_only_actual_quick_tunnel_url_is_extracted(self):
        self.assertEqual(host.extract_url('| https://some-live-room.trycloudflare.com |'),
                         'https://some-live-room.trycloudflare.com')
        self.assertIsNone(host.extract_url('https://api.trycloudflare.com/tunnel'))
        self.assertIsNone(host.extract_url('http://insecure.trycloudflare.com'))

    def test_existing_platform_is_reused_and_never_owned(self):
        with patch.object(host, 'read_health', return_value={'service':'storyroom', 'ok':True,
                                                           'version':'0.3.0', 'protocol':'0.3'}), \
                patch.object(host.subprocess, 'Popen') as process:
            self.connection.start_platform({}, 'http://127.0.0.1:8790')
            process.assert_not_called()
            self.assertIsNone(self.connection.platform)

    def test_other_port_service_is_not_reused(self):
        with patch.object(host, 'read_health', return_value={'service':'other', 'ok':True}), \
                patch.object(host.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(RuntimeError, '其他程序'):
                self.connection.start_platform({}, 'http://127.0.0.1:8790')
            process.assert_not_called()

    def test_previous_protocol_is_not_reused(self):
        with patch.object(host, 'read_health', return_value={'service':'storyroom', 'ok':True,
                                                           'version':'0.2.0', 'protocol':'0.2'}):
            with self.assertRaisesRegex(RuntimeError, '另一个版本'):
                self.connection.start_platform({}, 'http://127.0.0.1:8790')

    def test_cleanup_only_terminates_its_children(self):
        own = Mock();own.poll.return_value = None
        self.connection.tunnel = own
        self.connection.cleanup()
        own.terminate.assert_called_once()
        own.wait.assert_called_once_with(timeout=6)
        self.assertEqual(json.loads(self.connection.state.read_text())['status'], 'stopped')

    def test_already_exited_child_is_not_terminated(self):
        own = Mock();own.poll.return_value = 0
        self.connection.platform = own
        self.connection.cleanup()
        own.terminate.assert_not_called()

    def test_stopped_state_never_advertises_expired_url(self):
        self.connection.url = 'https://old-room.trycloudflare.com'
        self.connection.write_state('ready')
        self.assertEqual(json.loads(self.connection.state.read_text())['url'], self.connection.url)
        self.connection.cleanup()
        self.assertIsNone(json.loads(self.connection.state.read_text())['url'])

    def test_preflight_failure_is_observable_and_cleans_up(self):
        with patch.object(self.connection, 'validate', side_effect=ValueError('模拟缺失依赖')):
            self.connection.run()
        self.assertIn(('error', '模拟缺失依赖'), self.events)
        self.assertIn(('stopped', None), self.events)

    def test_process_stall_only_kills_owned_process(self):
        own = Mock();own.poll.return_value = None
        own.wait.side_effect = [host.subprocess.TimeoutExpired('own', 6), 0]
        self.connection.platform = own
        self.connection.cleanup()
        own.kill.assert_called_once()

    def test_tunnel_exit_fails_without_claiming_ready(self):
        executable = Path('not-run.exe')
        process = Mock();process.poll.return_value = 1;process.stdout = []
        with patch.object(host.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, '通道退出'):
                self.connection.start_tunnel(executable, 'http://127.0.0.1:8790')
        self.connection.cleanup()
        self.assertFalse(any(e[0] == 'ready' for e in self.events))
