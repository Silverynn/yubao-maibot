import re
from typing import AsyncIterator

from maibot_client import stream_maibot
from .maibot_expression import classify_reply_expression

from ..input_types import BatchInput, TextSource
from ..output_types import Actions, BaseOutput, DisplayText, SentenceOutput
from .agent_interface import AgentInterface


def infer_reply_emotion(reply: str) -> str:
    """Pick a visual reaction from obvious reply cues, not MaiBot's internal state."""
    cues = (
        ("anger", ("😠", "💢", "可恶", "气死我了")),
        ("sadness", ("😢", "😭", "好难过", "好伤心", "呜呜")),
        ("surprise", ("😮", "真的假的", "不会吧", "诶？", "欸？")),
        ("joy", ("😄", "😊", "🥰", "哈哈", "嘿嘿", "好耶", "太棒了")),
    )
    for emotion, markers in cues:
        if any(marker in reply for marker in markers):
            return emotion
    return "neutral"


def remove_leading_question_echo(reply: str, question: str) -> str:
    """Drop only an exact copy of this turn's question before the answer."""
    reply = reply.strip()
    question = question.strip()
    if not question or not reply.startswith(question):
        return reply

    answer = reply[len(question):]
    if not answer:
        return reply
    # A short prompt could be an ordinary prefix of a real answer (e.g. 你好/你好呀).
    if len(question) < 4 and answer[0] not in " \t\r\n,，:：;；":
        return reply
    return answer.lstrip(" \t\r\n,，:：;；") or reply


def add_sentence_final_punctuation(reply: str) -> str:
    """Give VTuber display and speech the same natural sentence ending."""
    reply = reply.strip()
    if not reply:
        return reply

    # Put punctuation inside a final quote or bracket, when present.
    closing = "”’\"'」』）)]"
    body = reply.rstrip(closing).rstrip()
    suffix = reply[len(body):]
    if not body or body.endswith(("。", "！", "？", ".", "!", "?", "…", "~", "～")):
        return reply

    body = body.rstrip(",，;；:：")
    if not body:
        return reply
    # Conservative fallback for an unpunctuated offer followed by a rhetorical
    # question, e.g. 晚上这顿我自己掏钱还不行. Plain reports such as
    # 我试过了还是不行 must remain statements.
    last_clause = re.split(r"[，,。！？!?；;\n]", body)[-1]
    rhetorical_offer = (
        last_clause.endswith(("还不行", "还不成", "还不可以"))
        and re.search(r"(?:我|我们|咱们)(?:自己|来|出|付|掏|请|买|改|换|去|帮|给|赔)", last_clause)
        and not re.search(r"看来|看起来|似乎|好像|发现|说明|证明|试过|试了|说.*不行", last_clause)
    )
    if rhetorical_offer or body.endswith(("吗", "么", "呢", "谁", "什么", "多少", "几", "哪里", "哪儿", "为什么", "怎么", "怎么样", "行不行", "好不好", "对不对", "是不是", "可不可以", "能不能", "要不要", "总行了吧")):
        ending = "？"
    elif body.endswith(("哈哈", "好耶", "太棒了", "太好了", "耶")):
        ending = "！"
    else:
        ending = "。"
    return body + ending + suffix


class MaiBotAgent(AgentInterface):
    def __init__(self, live2d_model, expression_llm=None):
        self._live2d_model = live2d_model
        self._expression_llm = expression_llm

    async def chat(self, input_data: BatchInput) -> AsyncIterator[BaseOutput]:
        # 取出用户打字或语音识别得到的文字
        parts = [
            item.content
            for item in input_data.texts
            if item.source == TextSource.INPUT
        ]
        user_text = "\n".join(parts).strip()

        if not user_text:
            return

        # 一轮回复最多做一次明显表情，其余段落保持默认表情。
        reaction_shown = False
        # 麦麦每返回一段，就交给界面显示并交给 TTS 朗读
        async for reply in stream_maibot(user_text):
            reply = remove_leading_question_echo(reply, user_text)
            reply = add_sentence_final_punctuation(reply)
            emotion = (
                await classify_reply_expression(self._expression_llm, reply, self._live2d_model.emo_map)
                if self._expression_llm is not None
                else infer_reply_emotion(reply)
            )
            if emotion != "neutral":
                if reaction_shown:
                    emotion = "neutral"
                else:
                    reaction_shown = True
            expression = self._live2d_model.emo_map.get(emotion)
            yield SentenceOutput(
                display_text=DisplayText(text=reply, name="鱼宝"),
                tts_text=reply,
                actions=Actions(expressions=[expression] if expression is not None else None),
            )

    def handle_interrupt(self, heard_response: str) -> None:
        # 虚拟形象程序会取消正在进行的对话任务
        pass

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        # 对话记忆由 MaiBot 管理
        pass
