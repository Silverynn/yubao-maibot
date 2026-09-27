import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

stage = Path(__file__).resolve().parent
root = stage.parent if (stage.parent / 'maibot_client.py').is_file() else stage.parents[1] / 'Open-LLM-VTuber'
sys.path.insert(0, str(root))
name = 'src.open_llm_vtuber.agent.agents.maibot_agent'
source = stage / 'maibot_agent.py'
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

async def stream(_):
    for text in ['晚上这顿我自己掏钱还不行', '哈哈，好耶', '嘿嘿', '好难过']:
        yield text

async def check():
    agent.stream_maibot = stream
    bot = agent.MaiBotAgent(SimpleNamespace(emo_map={'neutral': 0, 'joy': '开心兴奋', 'sadness': '悲伤'}))
    inputs = BatchInput(texts=[TextData(source=TextSource.INPUT, content='吃什么')])
    for _ in range(2):
        replies = [reply async for reply in bot.chat(inputs)]
        assert [r.actions.expressions for r in replies] == [[0], ['开心兴奋'], [0], [0]]
        assert replies[0].tts_text.endswith('？')
        assert all(r.display_text.text == r.tts_text for r in replies)

asyncio.run(check())
print('PASS: punctuation, declarative counterexamples, echo preservation, neutral default, one reaction per turn')
