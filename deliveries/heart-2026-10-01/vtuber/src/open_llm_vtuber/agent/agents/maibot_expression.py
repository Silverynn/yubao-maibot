"""Pick a Live2D reaction for a MaiBot reply without changing its spoken text."""

import asyncio
import json

from loguru import logger


EXPRESSION_HINTS = {
    "neutral": "平常叙述、解释、无法确定情绪",
    "blush": "害羞、脸红、被夸后不好意思",
    "love_eyes": "明显心动、喜欢到两眼冒爱心",
    "star_eyes": "期待、崇拜、眼睛发亮",
    "joy": "开心、兴奋、欢呼",
    "sadness": "失落、难过，但还没哭出来",
    "crying": "大哭、泪崩",
    "anger": "生气、愤怒",
    "dizzy": "晕头转向、困惑到发晕",
    "gloomy": "阴沉、沮丧、低气压",
    "playful": "调皮、开玩笑、恶作剧",
    "drooling": "想吃东西、馋、流口水",
    "soul_out": "累到灵魂出窍、极度崩溃",
    "tongue_out": "吐舌、俏皮卖萌",
    "blank_eyes": "呆住、发愣、愣神",
    "confusion": "疑惑、不理解、思索",
    "surprise": "惊讶、意外、感叹",
}


def parse_expression_result(raw: str, allowed: set[str]) -> str:
    """Accept one exact key only; unexpected model output is a neutral face."""
    result = raw.strip().lower()
    return result if result in allowed else "neutral"


async def classify_reply_expression(llm, reply: str, emotion_map: dict) -> str:
    """Use one bounded model request for each MaiBot text reply."""
    allowed = {key for key in EXPRESSION_HINTS if key in emotion_map}
    if not reply.strip() or not allowed:
        return "neutral"

    choices = "\n".join(f"{key}: {EXPRESSION_HINTS[key]}" for key in EXPRESSION_HINTS if key in allowed)
    system = (
        "你只负责判断鱼宝刚说完的话适合哪种 Live2D 表情。"
        "依据鱼宝自己的语气和态度判断，不要把用户的问题当成鱼宝的心情。"
        "普通说明或不确定时选 neutral；强烈表情只在语气明确时选。"
        "回复只包含下列一个英文键名，不要加括号、标点或解释。\n"
        + choices
    )
    messages = [{"role": "user", "content": json.dumps({"reply": reply[:800]}, ensure_ascii=False)}]

    async def collect() -> str:
        chunks = []
        async for chunk in llm.chat_completion(messages=messages, system=system):
            if isinstance(chunk, str):
                chunks.append(chunk)
            if sum(map(len, chunks)) > 100:
                break
        return "".join(chunks)

    try:
        result = await asyncio.wait_for(collect(), timeout=8)
    except Exception as error:
        logger.warning("MaiBot Live2D expression classification failed: {}", type(error).__name__)
        return "neutral"
    return parse_expression_result(result, allowed)
