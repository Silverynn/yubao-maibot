"""语音仅模拟识别；验证输入边界、权限和日志，不录真人声音。"""
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
import asyncio
import base64
import json
import wave
import unittest

import test_storyroom as baseline
from storyroom.speech import decode_wav, validate_endpoint


def audio(rate=16000, seconds=.2):
    stream = BytesIO()
    with wave.open(stream, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b'\0\0' * int(rate * seconds))
    return base64.b64encode(stream.getvalue()).decode()


class AudioTests(unittest.TestCase):
    def test_valid_wav_duration(self):
        raw, seconds = decode_wav(audio())
        self.assertEqual(seconds, .2)
        self.assertTrue(raw.startswith(b'RIFF'))

    def test_reject_bad_base64_or_format_or_duration(self):
        for invalid in ('%%%', audio(48000), audio(seconds=31), audio(seconds=.01)):
            with self.assertRaises(ValueError):
                decode_wav(invalid)

    def test_asr_cannot_relay_to_remote_or_credentials(self):
        self.assertEqual(validate_endpoint('http://localhost:12395/asr'), 'http://localhost:12395/asr')
        for url in ('https://example.com', 'http://example.com', 'http://a:b@localhost/x', 'http://localhost/x?key=secret'):
            with self.assertRaises(ValueError):
                validate_endpoint(url)


class MediaTests(unittest.TestCase):
    setUp = baseline.PlatformTests.setUp
    snapshot = baseline.PlatformTests.snapshot
    request_task = baseline.PlatformTests.request_task

    def payload(self):
        return {'role': self.players[0]['id'], 'client': 'test-browser', 'audio': audio()}

    def configured(self):
        # 不重建房间：创建相同根目录的新服务实例，既有持久化继续使用。
        self.client = baseline.TestClient(baseline.create_app(Path(self.temp.name), 'isolated-host-token',
                                          asr_url='http://127.0.0.1:12395/v1/audio/transcriptions'))

    def test_asr_unconfigured_is_explicit(self):
        self.assertFalse(self.client.get('/api/media', headers=self.host).json()['asr_configured'])
        self.assertEqual(self.client.post(self.prefix+'/voice', headers=self.host, json=self.payload()).status_code, 503)

    def test_recognition_returns_text_without_action_or_recording_file(self):
        self.configured()
        with patch('storyroom.app.transcribe', AsyncMock(return_value='请检查灯塔')) as recognize:
            response = self.client.post(self.prefix+'/voice', headers=self.host, json=self.payload())
        self.assertEqual(response.json(), {'text': '请检查灯塔'})
        recognize.assert_awaited_once()
        self.assertEqual(self.snapshot()['tasks'], [])
        wire = json.dumps(self.snapshot()['events'], ensure_ascii=False)
        self.assertIn('输出文字', wire)
        self.assertNotIn(self.payload()['audio'], wire)
        self.assertFalse(list(Path(self.temp.name).rglob('*.wav')))

    def test_voice_invalid_does_not_call_asr(self):
        self.configured()
        with patch('storyroom.app.transcribe', AsyncMock()) as recognize:
            response = self.client.post(self.prefix+'/voice', headers=self.host, json={**self.payload(), 'audio': 'bad'})
        self.assertEqual(response.status_code, 422)
        recognize.assert_not_called()

    def test_pc_cannot_use_microphone_as_gm(self):
        self.configured()
        self.assertEqual(self.client.post(self.prefix+'/voice', headers=self.pc1, json=self.payload()).status_code, 403)

    def test_asr_timeout_logs_failure_without_task(self):
        self.configured()
        with patch('storyroom.app.transcribe', AsyncMock(side_effect=TimeoutError())):
            response = self.client.post(self.prefix+'/voice', headers=self.host, json=self.payload())
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.snapshot()['tasks'], [])
        self.assertIn('TimeoutError', json.dumps(self.snapshot()['events']))

    def test_browser_switch_receipt_scoped_and_content_audited(self):
        payload = {'role': self.players[0]['id'], 'client': 'test-browser', 'feature': '语音开关', 'enabled': True, 'text': '朗读内容'}
        self.assertEqual(self.client.post(self.prefix+'/media-event', headers=self.pc2, json=payload).status_code, 403)
        self.assertEqual(self.client.post(self.prefix+'/media-event', headers=self.pc1, json=payload).status_code, 200)
        event = self.snapshot()['events'][0]['detail']
        self.assertTrue(event['开关'])
        self.assertEqual(event['内容'], '朗读内容')

    def test_status_endpoint_scoped_and_reports_cancelled(self):
        task = self.request_task()
        path = '/api/bot/tasks/'+task['id']+'/status'
        self.assertEqual(self.client.get(path, headers=self.pc2).status_code, 404)
        self.assertEqual(self.client.get(path, headers=self.host).status_code, 403)
        self.assertEqual(self.client.get(path, headers=self.pc1).json()['status'], 'running')
        self.client.post(self.prefix+'/tasks/'+task['id']+'/cancel', headers=self.host, json={})
        self.assertEqual(self.client.get(path, headers=self.pc1).json()['status'], 'rejected')


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_cancel_stops_generation_and_never_submits_result(self):
        from types import SimpleNamespace
        import tempfile
        task = {'id': 'task', 'lease': 'private-lease', 'question': '检查灯塔', 'character_name': '玩家',
                'world': '有限港口', 'stage': {}, 'flags': [], 'trust': 0, 'history': []}
        cancelled = asyncio.Event()
        async def generate(_):
            try:
                await asyncio.sleep(30)
            finally:
                cancelled.set()
        async def request(path, data=None):
            if path == '/api/bot/task':
                return {'task': task}
            if path.endswith('/status'):
                return {'status': 'rejected', 'current': True}
            self.fail('取消后不应提交结果')
        with tempfile.TemporaryDirectory() as temporary:
            pc = baseline.worker_module
            audit = pc.Audit(Path(temporary)/'logs')
            worker = pc.PCWorker(SimpleNamespace(binding='test',request=request), generate, '', '', 10,
                                 lambda _: None, data_dir=temporary, audit=audit)
            self.assertFalse(await worker.once())
            self.assertTrue(cancelled.is_set())
            self.assertFalse((Path(temporary)/'pending-result.json').exists())
            self.assertIn('行动生成取消', ''.join(f.read_text(encoding='utf-8') for f in audit.directory.glob('*.jsonl')))
