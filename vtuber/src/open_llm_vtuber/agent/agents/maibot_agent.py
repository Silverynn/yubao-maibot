import re
import os
from typing import AsyncIterator

from loguru import logger
from expression_catalog import model_expression_names

from maibot_client import stream_maibot
from .maibot_expression import classify_reply_expression

from ..input_types import BatchInput, TextSource
from ..output_types import Actions, BaseOutput, DisplayText, SentenceOutput
from .agent_interface import AgentInterface


EXPRESSION_ALIASES = {
    "平静": "neutral", "开心": "joy", "生气": "anger", "难过": "sadness",
    "疑惑": "confusion", "困惑": "confusion", "惊讶": "surprise",
    "害羞": "blush", "大哭": "crying", "调皮": "playful",
}


def infer_reply_emotion(reply: str) -> str:
    """Pick a visual reaction from obvious reply cues, not MaiBot's internal state."""
    cues = (
        ("anger", ("😠", "💢", "可恶", "气死我了")),
        ("sadness", ("😢", "😭", "好难过", "好伤心", "呜呜")),
        ("surprise", ("😮", "真的假的", "不会吧", "诶？", "欸？")),
        ("confusion", ("🤔", "我没理解", "我不明白", "有点疑惑", "有点困惑")),
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

        # 本地测试指令直接驱动模型，不交给 MaiBot，也不调用语音或大模型。
        manual = re.fullmatch(r"/表情列表|/表情(?:\s+(.*))?", user_text)
        if manual:
            requested = ('列表' if user_text == '/表情列表' else manual.group(1) or '列表').strip()
            mapping = self._live2d_model.emo_map
            model_names = model_expression_names(self._live2d_model)
            model_name = getattr(self._live2d_model, 'live2d_model_name', '当前形象')
            heart_enabled = bool(os.environ.get('HEART_MAIBOT_ROOT'))
            if requested == '列表':
                options = [f'/表情 {alias}' for alias, key in EXPRESSION_ALIASES.items() if key in mapping]
                options += [f'/表情 {value}' for value in mapping.values() if isinstance(value, str)]
                options += [f'/表情 {value}' for value in model_names]
                options = list(dict.fromkeys(options))
                details = '、'.join(f'{key}→{value}' for key, value in mapping.items()) or '模型没有配置语义映射'
                message = f'{model_name} 的常用表情：{details}。模型实际表情：' + ('、'.join(model_names) or '未能读取，按映射为准') + '。可发送：' + '、'.join(options or ['/表情 自动'])
                if heart_enabled:
                    message += '；/表情 自动 可恢复心情自动控制。'
                yield SentenceOutput(display_text=DisplayText(text=message, name='Heart'), tts_text='', actions=Actions(expressions=None))
                return
            key = EXPRESSION_ALIASES.get(requested, requested)
            expression = mapping.get(key)
            if expression is None and requested in mapping.values():
                expression = requested
            if expression is None and requested in model_names:
                expression = requested
            if expression is None and requested.isdecimal() and int(requested) in mapping.values():
                expression = int(requested)
            restore = requested in {'自动', '恢复', '恢复自动'}
            if heart_enabled and (expression is not None or restore):
                try:
                    from heart_bridge import request_manual_expression, MANUAL_SECONDS
                    # 手动请求也交给同一个控制器，不走会被自动控制拦截的旧通道。
                    request_manual_expression(None if restore else expression)
                    message = '已请求恢复自动表情。' if restore else f'已请求展示：{expression}，约{MANUAL_SECONDS}秒后恢复最新心情/话题表情；真实心情不变。'
                except (RuntimeError, ValueError) as error:
                    message = '表情请求未完成：' + str(error)
                yield SentenceOutput(display_text=DisplayText(text=message, name='Heart'), tts_text='', actions=Actions(expressions=None))
                return
            if expression is None:
                message = f'当前形象没有“{requested}”这个表情；发送 /表情列表 查看可用指令。'
            else:
                message = f"表情已切换：{expression}（短暂展示后恢复平静）。"
                logger.info("Live2D 表情 | 来源=手动测试 | 表情={}", expression)
            yield SentenceOutput(
                display_text=DisplayText(text=message, name="Heart"),
                tts_text="",
                actions=Actions(expressions=[expression] if expression is not None else None),
            )
            return

        # 一轮最多做一次明显表情；后续段落不抢先重置，由前端计时恢复。
        reaction_shown = False
        # 麦麦每返回一段，就交给界面显示并交给 TTS 朗读
        visit_id = (input_data.metadata or {}).get("heart_client_uid")
        if os.environ.get("HEART_MAIBOT_ROOT") and not visit_id:
            raise RuntimeError("Live2D 会话尚未绑定，已停止请求以免读到旧网页的聊天记录")
        async for reply in stream_maibot(user_text, visit_id=visit_id):
            reply = remove_leading_question_echo(reply, user_text)
            reply = add_sentence_final_punctuation(reply)
            if os.environ.get("HEART_MAIBOT_ROOT"):
                yield SentenceOutput(display_text=DisplayText(text=reply, name="鱼宝"), tts_text=reply, actions=Actions(expressions=None))
                continue
            emotion = (
                await classify_reply_expression(self._expression_llm, reply, self._live2d_model.emo_map)
                if self._expression_llm is not None
                else infer_reply_emotion(reply)
            )
            expression = None if reaction_shown else self._live2d_model.emo_map.get(emotion)
            if emotion != "neutral" and expression is not None:
                reaction_shown = True
            logger.info("Live2D 表情 | 来源={} | 分类={} | 指令={}",
                        "AI判断" if self._expression_llm is not None else "回复线索",
                        emotion, expression if expression is not None else "保持当前表情")
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
