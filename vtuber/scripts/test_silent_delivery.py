"""验证实际发送完成后，纯表情任务才算结束，并保持音频/文字顺序。"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.open_llm_vtuber.agent.output_types import Actions, DisplayText
from src.open_llm_vtuber.conversations.tts_manager import TTSTaskManager


class LocalSilentTTS:
    async def async_generate_audio(self, **kwargs):
        await asyncio.sleep(0.01)
        return None


async def check():
    for mixed in (False, True):
        manager = TTSTaskManager()
        packets = []

        async def send(raw):
            await asyncio.sleep(0)
            packets.append(json.loads(raw))

        if mixed:
            await manager.speak('previous', DisplayText('previous'), None, None, LocalSilentTTS(), send)
        await manager.speak('', DisplayText('manual'), Actions(expressions=['生气']), None, LocalSilentTTS(), send)
        await asyncio.wait_for(asyncio.gather(*manager.task_list), 2)
        manager.clear()  # 模拟一轮结束，不能把尚未送出的表情取消。
        assert [p['display_text']['text'] for p in packets] == (['previous', 'manual'] if mixed else ['manual'])
        assert packets[-1]['audio'] is None
        assert packets[-1]['actions']['expressions'] == ['生气']
    # 普通文字虽然经过 TTS 分支，在 silent_tts 模式同样没有音频。
    manager = TTSTaskManager()
    packets = []

    async def slow_send(raw):
        await asyncio.sleep(0.03)
        packets.append(json.loads(raw))

    await manager.speak('好耶', DisplayText('好耶'), Actions(expressions=['开心兴奋']), None, LocalSilentTTS(), slow_send)
    await asyncio.wait_for(asyncio.gather(*manager.task_list), 2)
    manager.clear()
    assert len(packets) == 1 and packets[0]['actions']['expressions'] == ['开心兴奋']

    # 断线必须能结束等待，不得死等，也不能把同一条回复重复排入队列。
    for text in ('', '普通回复'):
        manager = TTSTaskManager()

        async def failed_send(raw):
            raise ConnectionError('offline test')

        await manager.speak(text, DisplayText('test'), None, None, LocalSilentTTS(), failed_send)
        try:
            await asyncio.wait_for(asyncio.gather(*manager.task_list), 2)
        except ConnectionError:
            pass
        else:
            raise AssertionError('delivery error must be surfaced')
        manager.clear()

    # 中断时清理等待中的任务，不能留下悬挂的后台发送。
    manager = TTSTaskManager()
    gate = asyncio.Event()

    async def blocked_send(raw):
        await gate.wait()

    await manager.speak('', DisplayText('cancel'), None, None, LocalSilentTTS(), blocked_send)
    tasks = manager.task_list[:]
    await asyncio.sleep(0)
    manager.clear()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    assert not manager._delivery
    print('PASS: silent/manual/ordinary delivery, ordering, disconnect, cancellation, cleanup')


asyncio.run(check())
