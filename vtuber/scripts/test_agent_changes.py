import asyncio
import importlib.util
import sys
import os
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

stage = Path(__file__).resolve().parent
root = Path(os.environ['VTUBER_TEST_ROOT']) if os.environ.get('VTUBER_TEST_ROOT') else stage.parent if (stage.parent / 'maibot_client.py').is_file() else stage.parents[1] / 'Open-LLM-VTuber'
sys.path.insert(0, str(root))
name = 'src.open_llm_vtuber.agent.agents.maibot_agent'
source = stage / 'maibot_agent.py'
if not source.is_file():
    source = stage.parent / 'src/open_llm_vtuber/agent/agents/maibot_agent.py'
    if not source.is_file():
        source = root / 'src/open_llm_vtuber/agent/agents/maibot_agent.py'
spec = importlib.util.spec_from_file_location(name, source)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
from src.open_llm_vtuber.agent.input_types import BatchInput, TextData, TextSource

examples = {
    '晚上这顿我自己掏钱还不行': '晚上这顿我自己掏钱还不行？',
    '我来买单还不行': '我来买单还不行？',
    '这样行不行': '这样行不行？',
    '我试过了还是不行': '我试过了还是不行。',
    '看来我自己掏钱还不行': '看来我自己掏钱还不行。',
    '现在还不行': '现在还不行。',
    '我自己掏钱': '我自己掏钱。',
    '晚上这顿我自己掏钱还不行？': '晚上这顿我自己掏钱还不行？',
    '“晚上这顿我自己掏钱还不行”': '“晚上这顿我自己掏钱还不行？”',
    '好耶': '好耶！',
    '晚安。': '晚安。',
    '': '',
}
for text, expected in examples.items():
    assert agent.add_sentence_final_punctuation(text) == expected, text
assert agent.remove_leading_question_echo('你好呀，我是鱼宝', '你好') == '你好呀，我是鱼宝'
assert agent.remove_leading_question_echo('今天吃什么\n吃面', '今天吃什么') == '吃面'
for text in ['我喜欢这个', '今天挺开心的', '明天见']:
    assert agent.infer_reply_emotion(text) == 'neutral', text

async def stream(_, visit_id=None):
    for text in ['晚上这顿我自己掏钱还不行', '哈哈，好耶', '嘿嘿', '好难过']:
        yield text

async def check():
    agent.stream_maibot = stream
    bot = agent.MaiBotAgent(SimpleNamespace(emo_map={'neutral': 0, 'joy': '开心兴奋', 'sadness': '悲伤'}))
    inputs = BatchInput(texts=[TextData(source=TextSource.INPUT, content='吃什么')])
    for _ in range(2):
        replies = [reply async for reply in bot.chat(inputs)]
        assert [r.actions.expressions for r in replies] == [[0], ['开心兴奋'], None, None]
        assert replies[0].tts_text.endswith('？')
        assert all(r.display_text.text == r.tts_text for r in replies)

    async def forbidden(_, visit_id=None):
        raise AssertionError('manual expressions must not call MaiBot')
        yield
    agent.stream_maibot = forbidden
    bot = agent.MaiBotAgent(SimpleNamespace(emo_map={'neutral': '平静', 'joy': '开心兴奋', 'confusion': '问号'}))
    for text, expected in [('/表情 开心', ['开心兴奋']), ('/表情 疑惑', ['问号']),
                           ('/表情 平静', ['平静']), ('/表情 不存在', None)]:
        inputs = BatchInput(texts=[TextData(source=TextSource.INPUT, content=text)])
        replies = [reply async for reply in bot.chat(inputs)]
        assert len(replies) == 1
        assert replies[0].actions.expressions == expected
        assert replies[0].tts_text == ''

    with patch.dict(os.environ,{'HEART_MAIBOT_ROOT':'offline-test'}), patch('heart_bridge.request_manual_expression',create=True) as request:
        for text,expected in [('/表情 开心','开心兴奋'),('/表情 疑惑','问号'),('/表情 自动',None)]:
            inputs=BatchInput(texts=[TextData(source=TextSource.INPUT,content=text)])
            replies=[reply async for reply in bot.chat(inputs)]
            request.assert_called_with(expected)
            assert replies[0].actions.expressions is None,'统一控制，不走旧的表情通道'
            assert '已请求' in replies[0].display_text.text
        with patch('heart_bridge.request_manual_expression',side_effect=RuntimeError('bridge not ready')):
            replies=[reply async for reply in bot.chat(inputs)]
            assert '未完成' in replies[0].display_text.text

        ordinary = BatchInput(texts=[TextData(source=TextSource.INPUT,content='你好')])
        with patch.object(agent, 'stream_maibot', stream):
            try:
                [reply async for reply in bot.chat(ordinary)]
            except RuntimeError as error:
                assert '尚未绑定' in str(error)
            else:
                raise AssertionError('Heart must not fall back to the old shared chat')

asyncio.run(check())
print('PASS: punctuation, declarative counterexamples, echo preservation, neutral default, one reaction per turn')
