from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Tuple

import ast
import json
import os
import random
import re
import time

from src.chat.message_receive.chat_manager import chat_manager as _chat_manager
from src.chat.message_receive.message import SessionMessage
from src.common.data_models.message_component_data_model import AtComponent
from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.person_info import Person
from src.services.bot_account_service import get_bot_accounts
from src.services.embedding_service import EmbeddingServiceClient

from .typo_generator import ChineseTypoGenerator

if TYPE_CHECKING:
    from src.common.data_models.chat_target_info_data_model import ChatTargetInfo

logger = get_logger("chat_utils")
_warned_unconfigured_platforms: set[str] = set()


@dataclass(frozen=True)
class ProcessedResponseSegment:
    """回复后处理产生的单条消息及其发送提示。"""

    text: str
    quote_previous: bool = False


def is_english_letter(char: str) -> bool:
    """检查字符是否为英文字母（忽略大小写）"""
    return "a" <= char.lower() <= "z"


def parse_platform_accounts(platforms: list[str]) -> dict[str, str]:
    """解析 platforms 列表，返回平台到账号的映射

    Args:
        platforms: 格式为 ["platform:account"] 的列表，如 ["tg:123456789", "wx:wxid123"]

    Returns:
        字典，键为平台名，值为账号
    """
    result: dict[str, str] = {}
    for platform_entry in platforms:
        if ":" in platform_entry:
            platform_name, account = platform_entry.split(":", 1)
            normalized_platform = platform_name.lower().strip()
            account_str = account.strip()
            if normalized_platform and account_str:
                result[normalized_platform] = account_str
    return result


def _get_configured_qq_account() -> str:
    qq_account = str(getattr(global_config.bot, "qq_account", "")).strip()
    if qq_account in {"", "0"}:
        return ""
    return qq_account


def get_bot_account(platform: str, preferred_account_id: Optional[str] = None) -> str:
    """解析单个 Bot 账号；多账号且无会话归属时拒绝猜测。"""

    preferred = str(preferred_account_id or "").strip()
    if preferred:
        return preferred
    accounts = get_bot_accounts(platform)
    if len(accounts) == 1:
        return next(iter(accounts))
    if len(accounts) > 1:
        logger.error(f"平台 {platform} 存在多个 Bot 账号，缺少聊天流 account_id，无法选择发送身份")
    return ""


def get_configured_bot_accounts() -> dict[str, str]:
    """读取 legacy 发送链使用的备用配置账号，不包含数据库身份。"""

    bot_accounts: dict[str, str] = {}
    qq_account = _get_configured_qq_account()
    if qq_account:
        primary_platform = str(global_config.bot.platform or "qq").strip().lower()
        bot_accounts[primary_platform] = qq_account

    platforms_list = getattr(global_config.bot, "platforms", []) or []
    platform_accounts = parse_platform_accounts(platforms_list)

    for platform_name, account in platform_accounts.items():
        if platform_name in {"qq", "webui"}:
            continue
        bot_accounts[platform_name] = account

    return bot_accounts


def is_bot_self(platform: str, user_id: str) -> bool:
    """判断给定的平台和用户ID是否是机器人自己

    这个函数统一处理所有平台（包括 QQ、Telegram、WebUI 等）的机器人识别逻辑。

    Args:
        platform: 消息平台（如 "qq", "telegram", "webui" 等）
        user_id: 用户ID

    Returns:
        bool: 如果是机器人自己则返回 True，否则返回 False
    """
    normalized_platform = str(platform or "").strip().lower()
    if not normalized_platform or not user_id:
        return False

    # 将 user_id 转为字符串进行比较
    user_id_str = str(user_id).strip()
    if not user_id_str:
        return False

    bot_accounts = get_bot_accounts(normalized_platform)
    if bot_accounts:
        return user_id_str in bot_accounts

    if normalized_platform not in _warned_unconfigured_platforms:
        _warned_unconfigured_platforms.add(normalized_platform)
        logger.warning(f"平台 {normalized_platform} 未配置机器人账号，无法判断用户 {user_id_str} 是否为机器人自己")
    return False


def _has_at_component_targeting_bot(message: SessionMessage, platform: str) -> bool:
    """检查消息中的结构化 @ 组件是否直接指向当前 bot。"""

    raw_message = getattr(message, "raw_message", None)
    for component in getattr(raw_message, "components", []) or []:
        if isinstance(component, AtComponent) and is_bot_self(platform, component.target_user_id):
            return True
    return False


def is_mentioned_bot_in_message(message: SessionMessage) -> tuple[bool, bool, float]:
    """检查消息是否提到了机器人（统一多平台实现）"""
    text = message.processed_plain_text or ""
    platform = str(message.platform or "").strip().lower()

    current_accounts = get_bot_accounts(platform)

    nickname = str(global_config.bot.nickname or "")
    alias_names = list(getattr(global_config.bot, "alias_names", []) or [])
    keywords = [nickname] + alias_names

    reply_probability = 0.0
    is_at = False
    is_mentioned = False

    # 1) 直接的 additional_config 标记
    add_cfg = getattr(message.message_info, "additional_config", None) or {}
    if isinstance(add_cfg, dict):
        if add_cfg.get("at_bot") or add_cfg.get("is_mentioned"):
            is_mentioned = True
            if add_cfg.get("at_bot"):
                is_at = True
            # 当提供数值型 is_mentioned 时，当作概率提升；布尔提及标记只负责标记命中。
            raw_mention_boost = add_cfg.get("is_mentioned")
            if raw_mention_boost not in (None, "") and not isinstance(raw_mention_boost, bool):
                reply_probability = float(raw_mention_boost)

    # 2) 已经在上游设置过的 message.is_at / message.is_mentioned
    if getattr(message, "is_at", False):
        is_at = True
        is_mentioned = True
    if getattr(message, "is_mentioned", False):
        is_mentioned = True

    # 3) 扫描分段：是否包含 mention_bot（适配器插入）
    def _has_mention_bot(seg) -> bool:
        try:
            if seg is None:
                return False
            if getattr(seg, "type", None) == "mention_bot":
                return True
            if getattr(seg, "type", None) == "seglist":
                for s in getattr(seg, "data", []) or []:
                    if _has_mention_bot(s):
                        return True
            return False
        except Exception:
            return False

    if _has_mention_bot(getattr(message, "message_segment", None)):
        is_at = True
        is_mentioned = True

    # 4) 结构化 @ 组件检测。处理后的文本可能只剩群名片，不能依赖文本里的显示名判断。
    if not is_at and _has_at_component_targeting_bot(message, platform):
        is_at = True
        is_mentioned = True

    # 5) 统一的 @ 检测逻辑
    if current_accounts and not is_at and not is_mentioned:
        for current_account in current_accounts:
            pattern = (
                rf"@<(.+?):{re.escape(current_account)}>"
                if platform == "qq"
                else rf"@{re.escape(current_account)}(\b|$)"
            )
            if re.search(pattern, text, flags=re.IGNORECASE):
                is_at = True
                is_mentioned = True
                break

    # 6) 统一的回复检测逻辑
    if not is_mentioned:
        # 通用回复格式：包含 "(你)" 或 "（你）"
        if re.search(r"\[回复 .*?\(你\)：", text) or re.search(r"\[回复 .*?（你）：", text):
            is_mentioned = True
        # ID 形式的回复检测
        elif current_accounts:
            for current_account in current_accounts:
                if re.search(rf"\[回复 (.+?)\({re.escape(current_account)}\)：(.+?)\]，说：", text):
                    is_mentioned = True
                    break
                if re.search(
                    rf"\[回复<(.+?)(?=:{re.escape(current_account)}>)\:{re.escape(current_account)}>：(.+?)\]，说：",
                    text,
                ):
                    is_mentioned = True
                    break

    # 7) 名称/别名 提及（去除 @/回复标记后再匹配）
    if not is_mentioned and keywords:
        msg_content = text
        # 去除各种 @ 与 回复标记，避免误判
        msg_content = re.sub(r"@(.+?)（(\d+)）", "", msg_content)
        msg_content = re.sub(r"@<(.+?)(?=:(\d+))\:(\d+)>", "", msg_content)
        msg_content = re.sub(r"\[回复 (.+?)\(((\d+)|未知id|你)\)：(.+?)\]，说：", "", msg_content)
        msg_content = re.sub(r"\[回复<(.+?)(?=:(\d+))\:(\d+)>：(.+?)\]，说：", "", msg_content)
        for kw in keywords:
            if kw and kw in msg_content:
                is_mentioned = True
                break

    # 8) 概率设置
    reply_timing_config = global_config.chat.reply_timing
    if is_at and reply_timing_config.inevitable_at_reply:
        reply_probability = 1.0
        logger.debug("被@，回复概率设置为100%")
    elif is_mentioned and reply_timing_config.mentioned_bot_reply:
        reply_probability = max(reply_probability, 1.0)
        logger.debug("被提及，回复概率设置为100%")

    return is_mentioned, is_at, reply_probability


async def get_embedding(text: str, request_type: str = "embedding") -> Optional[List[float]]:
    """获取文本的嵌入向量。

    Args:
        text: 待编码的文本内容。
        request_type: 当前请求的业务类型标识。

    Returns:
        Optional[List[float]]: 成功时返回嵌入向量，失败时返回 `None`。
    """
    embedding_client = EmbeddingServiceClient(task_name="embedding", request_type=request_type)
    try:
        embedding_result = await embedding_client.embed_text(text)
        embedding = embedding_result.embedding
    except Exception as e:
        logger.error(f"获取embedding失败: {str(e)}")
        embedding = None
    return embedding


def split_into_sentences_w_remove_punctuation(text: str) -> list[str]:
    """按完整句子或显式换行分段，保留正文、标点及段间空白。

    兼容旧函数名；逗号和普通空格不是消息边界，不再随机拆分/合并。
    引号、括号内部和英文缩写/小数/网址中的句点不作为句子边界。
    """
    if not text or not text.strip():
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    pairs = {
        "“": "”", "‘": "’", "「": "」", "『": "』",
        "（": "）", "(": ")", "[": "]", "【": "】",
    }
    quote_stack: list[str] = []
    boundaries = {"。", "！", "？", "!", "?", "；", ";"}
    url_positions: set[int] = set()
    for match in re.finditer(r"https?://[^\s，。！？；]+", text):
        # 网址中的 ?、; 是地址的一部分，尾部的英文句读仍属于正文。
        address = match.group().rstrip(".,;!?")
        url_positions.update(range(match.start(), match.start() + len(address)))
    protected_positions = {position for start, end in _reply_structured_spans(text) for position in range(start, end)}
    sentences: list[str] = []
    buffer = ""
    index = 0

    while index < len(text):
        char = text[index]
        buffer += char
        # 列表编号和代码里的句点不能当作句末；整块内容原样进入后续合并。
        if index in protected_positions:
            if index + 1 not in protected_positions and buffer.strip():
                sentences.append(buffer)
                buffer = ""
            index += 1
            continue
        if quote_stack and char == quote_stack[-1]:
            quote_stack.pop()
        elif char in pairs:
            quote_stack.append(pairs[char])
        elif char in {'"', "'", "`"}:
            # don't / user's 中的撇号不能让后面的正文永久处于引号内。
            apostrophe = (
                char == "'" and index > 0 and index + 1 < len(text)
                and is_english_letter(text[index - 1]) and is_english_letter(text[index + 1])
            )
            if not apostrophe:
                quote_stack.append(char)

        # 英文句点只在其后为空白或文本结尾时断句；保护3.14、文件名和网址。
        abbreviation = re.search(
            r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|[A-Za-z]|(?:[A-Za-z]\.)+[A-Za-z])\.$",
            buffer, re.IGNORECASE,
        )
        period_boundary = (
            char == "." and not abbreviation
            and (index + 1 == len(text) or text[index + 1].isspace())
        )
        if (
            not quote_stack and index not in url_positions
            and (char in boundaries or char == "\n" or period_boundary)
        ):
            # 连续问号/句号等附在当前句尾，不产生只有标点的下一条消息。
            while index + 1 < len(text) and text[index + 1] in boundaries | {"."}:
                index += 1
                buffer += text[index]
            if buffer.strip():
                sentences.append(buffer)
            elif sentences:
                sentences[-1] += buffer
            buffer = ""
        index += 1

    if buffer.strip():
        sentences.append(buffer)
    elif sentences:
        sentences[-1] += buffer
    logger.debug(f"按完整句子分段后的文本: {sentences}")
    return sentences


def merge_sentences_to_max_count(sentences: list[str], max_count: int) -> list[str]:
    """按顺序将分句合并到指定条数以内。"""

    if len(sentences) <= max_count:
        return sentences

    merged_sentences: list[str] = []
    sentence_count = len(sentences)
    start_index = 0
    for group_index in range(max_count):
        remaining_sentences = sentence_count - start_index
        remaining_groups = max_count - group_index
        group_size = (remaining_sentences + remaining_groups - 1) // remaining_groups
        merged_sentences.append("".join(sentences[start_index : start_index + group_size]))
        start_index += group_size

    return merged_sentences


def _merge_processed_segments_to_max_count(
    segments: list[ProcessedResponseSegment],
    max_count: int,
) -> list[ProcessedResponseSegment]:
    """压缩回复段数量，并优先让需要引用的纠正内容保持在消息开头。"""

    if len(segments) <= max_count:
        return segments
    if max_count <= 0:
        return []

    segment_count = len(segments)
    required_starts = [index for index, segment in enumerate(segments) if index > 0 and segment.quote_previous]
    group_starts = {0, *required_starts[: max_count - 1]}

    # 在保留纠正消息边界后，沿用原来的均匀分组策略填充其余可用分组。
    evenly_spaced_starts: list[int] = []
    start_index = 0
    for group_index in range(max_count):
        remaining_segments = segment_count - start_index
        remaining_groups = max_count - group_index
        group_size = (remaining_segments + remaining_groups - 1) // remaining_groups
        evenly_spaced_starts.append(start_index)
        start_index += group_size

    for candidate_start in evenly_spaced_starts:
        if len(group_starts) >= max_count:
            break
        group_starts.add(candidate_start)

    sorted_starts = sorted(group_starts)
    merged_segments: list[ProcessedResponseSegment] = []
    for group_index, group_start in enumerate(sorted_starts):
        group_end = sorted_starts[group_index + 1] if group_index + 1 < len(sorted_starts) else segment_count
        group = segments[group_start:group_end]
        merged_segments.append(
            ProcessedResponseSegment(
                text="".join(segment.text for segment in group),
                quote_previous=group[0].quote_previous,
            )
        )

    return merged_segments


def random_remove_punctuation(text: str) -> str:
    """兼容旧调用，原样保留文本，避免随机删除或替换标点。"""
    return text


def _reply_structured_spans(text: str) -> list[tuple[int, int]]:
    """识别应保持整体版式的代码块、列表和多行引用。"""

    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*(?P=fence)[ \t]*(?=\n|$)|\Z)", text):
        spans.append(match.span())
    for match in re.finditer(r'(?s)“.*?”|「.*?」|『.*?』|"[^"\n]*\n[^"]*"', text):
        if "\n" in match.group():
            spans.append(match.span())
    lines = list(re.finditer(r"[^\n]+(?:\n|$)|\n", text))
    marker = re.compile(r"^[ \t]*(?:\d+[.)、](?:[ \t]+|(?=[一-鿿]))|[-+*•][ \t]+|>[ \t]?)")
    index = 0
    while index < len(lines):
        if not marker.match(lines[index].group()):
            index += 1
            continue
        start = lines[index].start()
        end = lines[index].end()
        if index and lines[index - 1].group().rstrip().endswith(("：", ":")):
            start = lines[index - 1].start()
        index += 1
        while index < len(lines):
            line = lines[index].group()
            continuation = bool(line.strip()) and (line.startswith("  ") or line.startswith("\t"))
            blank_between_items = not line.strip() and index + 1 < len(lines) and marker.match(lines[index + 1].group())
            if not (marker.match(line) or continuation or blank_between_items):
                break
            end = lines[index].end()
            index += 1
        spans.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _flatten_reply_prose(text: str) -> str:
    """普通聊天合并到一行，保留已有句内标点与英文词间空格。"""

    result = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if result:
            unquoted_tail = result.rstrip("”’」』）》】\"'")
            if unquoted_tail and unquoted_tail[-1] in "。！？!?；;：:，,、….":
                separator = " " if ord(result[-1]) < 128 else ""
            elif ord(result[-1]) < 128 and ord(line[0]) < 128:
                separator = " "
            else:
                separator = "。"
            result += separator
        result += line
    return result


def _format_reply_display_text(text: str) -> str:
    """方案B：普通消息不换行，只去气泡末尾句号，结构化内容保持版式。"""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    pieces: list[tuple[str, bool]] = []
    offset = 0
    for start, end in _reply_structured_spans(normalized):
        raw_prose = normalized[offset:start]
        prose = _flatten_reply_prose(raw_prose)
        if prose:
            if pieces and raw_prose.startswith("\n") and not pieces[-1][0].endswith("\n"):
                prose = "\n" + prose
            if raw_prose.endswith("\n"):
                prose += "\n"
            pieces.append((prose, False))
        elif pieces and "\n" in raw_prose and not pieces[-1][0].endswith("\n"):
            pieces.append(("\n", True))
        block = normalized[start:end]
        if block.strip():
            pieces.append((block, True))
        offset = end
    raw_prose = normalized[offset:]
    prose = _flatten_reply_prose(raw_prose)
    if prose:
        if pieces and raw_prose.startswith("\n") and not pieces[-1][0].endswith("\n"):
            prose = "\n" + prose
        pieces.append((prose, False))
    if pieces and not pieces[-1][1]:
        body = pieces[-1][0]
        abbreviation = re.search(
            r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|[A-Za-z]|(?:[A-Za-z]\.)+[A-Za-z])\.$",
            body, re.IGNORECASE,
        )
        final_url = re.search(r"https?://[^\s]+$", body)
        if (body.endswith("。") and not body.endswith("。。")
                or body.endswith(".") and not body.endswith("..") and not abbreviation and not final_url):
            body = body[:-1].rstrip()
        pieces[-1] = (body, False)
    return "".join(piece for piece, _ in pieces).strip()


def _get_random_default_reply() -> str:
    """获取随机默认回复"""
    default_replies = [
        f"{global_config.bot.nickname}不知道哦",
        f"{global_config.bot.nickname}不知道",
        "不知道哦",
        "不知道",
        "不晓得",
        "懒得说",
        "()",
    ]
    return random.choice(default_replies)


def process_llm_response_segments(
    text: str,
    enable_splitter: bool = True,
    enable_chinese_typo: bool = True,
) -> list[ProcessedResponseSegment]:
    """处理回复文本，并保留错别字纠正消息的引用提示。"""

    if not text or not text.strip():
        return []
    if not global_config.response_post_process.enable_response_post_process:
        display_text = _format_reply_display_text(text)
        return [ProcessedResponseSegment(display_text)] if display_text else []

    # 先保护颜文字
    if global_config.response_splitter.enable_kaomoji_protection:
        protected_text, kaomoji_mapping = protect_kaomoji(text)
        logger.debug(f"保护颜文字后的文本: {protected_text}")
    else:
        protected_text = text
        kaomoji_mapping = {}
    # 提取被 () 或 [] 或 （）包裹且包含中文的内容
    pattern = re.compile(r"[(\[（](?=.*[一-鿿]).*?[)\]）]")
    _extracted_contents = pattern.findall(protected_text)  # 在保护后的文本上查找
    # 去除 () 和 [] 及其包裹的内容
    cleaned_text = pattern.sub("", protected_text)

    if cleaned_text == "":
        return [ProcessedResponseSegment("呃呃")]

    logger.debug(f"{text}去除括号处理后的文本: {cleaned_text}")

    # 对清理后的文本进行进一步处理
    max_length = global_config.response_splitter.max_length * 2
    max_sentence_num = global_config.response_splitter.max_sentence_num
    max_split_num = global_config.response_splitter.max_split_num
    # 如果基本上是中文，则进行长度过滤
    if get_western_ratio(cleaned_text) < 0.1 and len(cleaned_text) > max_length:
        logger.warning(f"回复过长 ({len(cleaned_text)} 字符)，返回默认回复")
        return [ProcessedResponseSegment(_get_random_default_reply())]

    typo_generator = ChineseTypoGenerator(
        error_rate=global_config.chinese_typo.error_rate,
        min_freq=global_config.chinese_typo.min_freq,
        tone_error_rate=global_config.chinese_typo.tone_error_rate,
        word_replace_rate=global_config.chinese_typo.word_replace_rate,
    )

    if global_config.response_splitter.enable and enable_splitter:
        split_sentences = split_into_sentences_w_remove_punctuation(cleaned_text)
    else:
        split_sentences = [cleaned_text]

    segments: list[ProcessedResponseSegment] = []
    for sentence in split_sentences:
        if global_config.chinese_typo.enable and enable_chinese_typo:
            typoed_text, typo_corrections = typo_generator.create_typo_sentence(sentence)
            if typo_corrections:
                # 50%概率新增正确字/词，50%概率用正确分句替换错别字分句
                if random.random() < 0.5:
                    quote_previous = (
                        global_config.chinese_typo.enable_correction_quote
                        and random.random() < global_config.chinese_typo.correction_quote_probability
                    )
                    segments.append(ProcessedResponseSegment(typoed_text))
                    segments.append(
                        ProcessedResponseSegment(
                            typo_corrections,
                            quote_previous=quote_previous,
                        )
                    )
                else:
                    # 用正确的分句替换错别字分句
                    segments.append(ProcessedResponseSegment(sentence))
            else:
                segments.append(ProcessedResponseSegment(typoed_text))
        else:
            segments.append(ProcessedResponseSegment(sentence))

    if len(segments) > max_sentence_num:
        if global_config.response_splitter.enable_overflow_return_all:
            logger.warning(f"分割后消息数量过多 ({len(segments)} 条)，直接返回原文")
            segments = [ProcessedResponseSegment(cleaned_text)]
        else:
            logger.warning(f"分割后消息数量过多 ({len(segments)} 条)，返回默认回复")
            return [ProcessedResponseSegment(_get_random_default_reply())]

    segments = _merge_processed_segments_to_max_count(segments, max_split_num)

    # 消息分段只决定发送边界，不能删除边界处的逗号、分号或句号。

    # if extracted_contents:
    #     for content in extracted_contents:
    #         sentences.append(content)

    # 在所有句子处理完毕后，对包含占位符的列表进行恢复
    if global_config.response_splitter.enable_kaomoji_protection:
        recovered_sentences = recover_kaomoji([segment.text for segment in segments], kaomoji_mapping)
        segments = [
            ProcessedResponseSegment(
                text=recovered_text,
                quote_previous=segment.quote_previous,
            )
            for segment, recovered_text in zip(segments, recovered_sentences, strict=True)
        ]

    # 合并、错字和颜文字恢复完成后才整理显示，保留引用纠正等发送元数据。
    display_segments: list[ProcessedResponseSegment] = []
    for segment in segments:
        display_text = _format_reply_display_text(segment.text)
        if display_text:
            display_segments.append(ProcessedResponseSegment(display_text, quote_previous=segment.quote_previous))
    logger.debug(f"清理空行和句末句号后的文本: {[segment.text for segment in display_segments]}")
    return display_segments


def process_llm_response(text: str, enable_splitter: bool = True, enable_chinese_typo: bool = True) -> list[str]:
    """处理回复文本，并返回兼容旧调用链的纯文本列表。"""

    return [
        segment.text
        for segment in process_llm_response_segments(
            text,
            enable_splitter=enable_splitter,
            enable_chinese_typo=enable_chinese_typo,
        )
    ]


def calculate_typing_time(
    input_string: str,
    # thinking_start_time: float,
    chinese_time: float = 0.3,
    english_time: float = 0.15,
    is_emoji: bool = False,
) -> float:
    """
    计算输入字符串所需的时间，中文和英文字符有不同的输入时间
        input_string (str): 输入的字符串
        chinese_time (float): 中文字符的输入时间，默认为0.2秒
        english_time (float): 英文字符的输入时间，默认为0.1秒
        is_emoji (bool): 是否为emoji，默认为False

    特殊情况：
    - 如果只有一个中文字符，将使用3倍的中文输入时间
    - 在所有输入结束后，额外加上回车时间0.3秒
    - 如果is_emoji为True，将使用固定1秒的输入时间
    """
    # chinese_time *= 1 / typing_speed_multiplier
    # english_time *= 1 / typing_speed_multiplier
    # 计算中文字符数
    chinese_chars = sum("\u4e00" <= char <= "\u9fff" for char in input_string)

    # 如果只有一个中文字符，使用3倍时间
    if chinese_chars == 1 and len(input_string.strip()) == 1:
        return chinese_time * 3 + 0.3  # 加上回车时间

    # 正常计算所有字符的输入时间
    total_time = 0.0
    for char in input_string:
        total_time += chinese_time if "\u4e00" <= char <= "\u9fff" else english_time
    if is_emoji:
        total_time = 1

    typing_speed = global_config.response_post_process.typing_speed
    if typing_speed <= 0:
        return 0
    total_time *= typing_speed

    # if time.time() - thinking_start_time > 10:
    #     total_time = 1

    # print(f"thinking_start_time:{thinking_start_time}")
    # print(f"nowtime:{time.time()}")
    # print(f"nowtime - thinking_start_time:{time.time() - thinking_start_time}")
    # print(f"{total_time}")

    return total_time  # 加上回车时间


def truncate_message(message: str, max_length=20) -> str:
    """截断消息，使其不超过指定长度"""
    return f"{message[:max_length]}..." if len(message) > max_length else message


def protect_kaomoji(sentence):
    """ "
    识别并保护句子中的颜文字（含括号与无括号），将其替换为占位符，
    并返回替换后的句子和占位符到颜文字的映射表。
    Args:
        sentence (str): 输入的原始句子
    Returns:
        tuple: (处理后的句子, {占位符: 颜文字})
    """
    kaomoji_pattern = re.compile(
        r"("
        r"[(\[（【]"  # 左括号
        r"[^()\[\]（）【】]*?"  # 非括号字符（惰性匹配）
        r"[^一-龥a-zA-Z0-9\s]"  # 非中文、非英文、非数字、非空格字符（必须包含至少一个）
        r"[^()\[\]（）【】]*?"  # 非括号字符（惰性匹配）
        r"[)\]）】"  # 右括号
        r"]"
        r")"
        r"|"
        r"([▼▽・ᴥω･﹏^><≧≦￣｀´∀ヮДд︿﹀へ｡ﾟ╥╯╰︶︹•⁄]{2,15})"
    )

    kaomoji_matches = kaomoji_pattern.findall(sentence)
    placeholder_to_kaomoji = {}

    for match in kaomoji_matches:
        kaomoji = match[0] or match[1]
        if kaomoji.startswith("[表情包") and kaomoji.endswith("]"):
            continue
        idx = len(placeholder_to_kaomoji)
        placeholder = f"__KAOMOJI_{idx}__"
        sentence = sentence.replace(kaomoji, placeholder, 1)
        placeholder_to_kaomoji[placeholder] = kaomoji

    return sentence, placeholder_to_kaomoji


def recover_kaomoji(sentences, placeholder_to_kaomoji):
    """
    根据映射表恢复句子中的颜文字。
    Args:
        sentences (list): 含有占位符的句子列表
        placeholder_to_kaomoji (dict): 占位符到颜文字的映射表
    Returns:
        list: 恢复颜文字后的句子列表
    """
    recovered_sentences = []
    for sentence in sentences:
        for placeholder, kaomoji in placeholder_to_kaomoji.items():
            sentence = sentence.replace(placeholder, kaomoji)
        recovered_sentences.append(sentence)
    return recovered_sentences


def get_western_ratio(paragraph):
    """计算段落中字母数字字符的西文比例
    原理：检查段落中字母数字字符的西文比例
    通过is_english_letter函数判断每个字符是否为西文
    只检查字母数字字符，忽略标点符号和空格等非字母数字字符

    Args:
        paragraph: 要检查的文本段落

    Returns:
        float: 西文字符比例(0.0-1.0)，如果没有字母数字字符则返回0.0
    """
    alnum_chars = [char for char in paragraph if char.isalnum()]
    if not alnum_chars:
        return 0.0

    western_count = sum(bool(is_english_letter(char)) for char in alnum_chars)
    return western_count / len(alnum_chars)


def translate_timestamp_to_human_readable(timestamp: float, mode: str = "normal") -> str:
    # sourcery skip: merge-comparisons, merge-duplicate-blocks, switch
    """将时间戳转换为人类可读的时间格式

    Args:
        timestamp: 时间戳
        mode: 转换模式，"normal"为标准格式，"relative"为相对时间格式

    Returns:
        str: 格式化后的时间字符串
    """
    if mode == "normal":
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    elif mode == "normal_no_YMD":
        return time.strftime("%H:%M:%S", time.localtime(timestamp))
    elif mode == "relative":
        now = time.time()
        diff = now - timestamp

        if diff < 20:
            return "刚刚"
        elif diff < 60:
            return f"{int(diff)}秒前"
        elif diff < 3600:
            return f"{int(diff / 60)}分钟前"
        elif diff < 86400:
            return f"{int(diff / 3600)}小时前"
        elif diff < 86400 * 2:
            return f"{int(diff / 86400)}天前"
        else:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)) + ":"
    else:  # mode = "lite" or unknown
        # 只返回时分秒格式
        return time.strftime("%H:%M:%S", time.localtime(timestamp))


def get_chat_type_and_target_info(chat_id: str) -> Tuple[bool, Optional["ChatTargetInfo"]]:
    """
    获取聊天类型（是否群聊）和私聊对象信息。

    Args:
        chat_id: 聊天流ID

    Returns:
        Tuple[bool, Optional[Dict]]:
            - bool: 是否为群聊 (True 是群聊, False 是私聊或未知)
            - Optional[Dict]: 如果是私聊，包含对方信息的字典；否则为 None。
            字典包含: platform, user_id, user_nickname, person_id, person_name
    """
    is_group_chat = False  # Default to private/unknown
    chat_target_info = None

    try:
        if chat_stream := _chat_manager.get_session_by_session_id(chat_id):
            if chat_stream.is_group_session:
                is_group_chat = True
                chat_target_info = None  # Explicitly None for group chat
            elif chat_stream.user_id:  # It's a private chat
                is_group_chat = False
                platform: str = chat_stream.platform
                user_id: str = chat_stream.user_id

                # Try to get nickname from context
                user_nickname = None
                if (
                    chat_stream.context
                    and chat_stream.context.message
                    and chat_stream.context.message.message_info.user_info
                ):
                    user_nickname = chat_stream.context.message.message_info.user_info.user_nickname

                from src.common.data_models.chat_target_info_data_model import ChatTargetInfo  # 解决循环导入问题

                # Initialize target_info with basic info
                target_info = ChatTargetInfo(
                    platform=platform,
                    user_id=user_id,
                    session_nickname=user_nickname or "",
                    person_id=None,
                    person_name=None,
                )

                # Try to fetch person info
                try:
                    person = Person(platform=platform, user_id=user_id)
                    if not person.is_known:
                        logger.warning(f"用户 {user_nickname} 尚未认识")
                        # 如果用户尚未认识，则返回False和None
                        return False, None
                    target_info.is_known = True
                    if person.person_id:
                        target_info.person_id = person.person_id
                        target_info.person_name = person.person_name
                except Exception as person_e:
                    logger.warning(
                        f"获取 person_id 或 person_name 时出错 for {platform}:{user_id} in utils: {person_e}"
                    )

                chat_target_info = target_info
        else:
            logger.warning(f"无法获取 chat_stream for {chat_id} in utils")
    except Exception as e:
        logger.error(f"获取聊天类型和目标信息时出错 for {chat_id}: {e}", exc_info=True)

    return is_group_chat, chat_target_info


def record_replyer_action_temp(chat_id: str, reason: str, think_level: int) -> None:
    """
    临时记录replyer动作被选择的信息（仅群聊）

    Args:
        chat_id: 聊天ID
        reason: 选择理由
        think_level: 思考深度等级
    """
    try:
        # 确保data/temp目录存在
        temp_dir = "data/temp"
        os.makedirs(temp_dir, exist_ok=True)

        # 创建记录数据
        record_data = {
            "chat_id": chat_id,
            "reason": reason,
            "think_level": think_level,
            "timestamp": datetime.now().isoformat(),
        }

        # 生成文件名（使用时间戳避免冲突）
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"replyer_action_{timestamp_str}.json"
        filepath = os.path.join(temp_dir, filename)

        # 写入文件
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record_data, f, ensure_ascii=False, indent=2)

        logger.debug(f"已记录replyer动作选择: chat_id={chat_id}, think_level={think_level}")
    except Exception as e:
        logger.warning(f"记录replyer动作选择失败: {e}")


def assign_message_ids(messages: List[SessionMessage]) -> List[Tuple[str, SessionMessage]]:
    """
    为消息列表中的每个消息分配唯一的简短随机ID

    Args:
        messages: 消息列表

    Returns:
        List[SessionMessage]: 分配了唯一ID的消息列表
    """
    result: List[Tuple[str, SessionMessage]] = []  # 复制原始消息列表
    used_ids = set()
    len_i = len(messages)
    if len_i > 100:
        a = 10
        b = 99
    else:
        a = 1
        b = 9

    for i, message in enumerate(messages):
        # 生成唯一的简短ID
        while True:
            # 使用索引+随机数生成简短ID
            random_suffix = random.randint(a, b)
            message_id = f"m{i + 1}{random_suffix}"

            if message_id not in used_ids:
                used_ids.add(message_id)
                break
        result.append((message_id, message))

    return result


#                 break
#         result.append((message_id, message))

#     return result


def parse_keywords_string(keywords_input) -> list[str]:
    # sourcery skip: use-contextlib-suppress
    """
    统一的关键词解析函数，支持多种格式的关键词字符串解析

    支持的格式：
    1. 字符串列表格式：'["utils.py", "修改", "代码", "动作"]'
    2. 斜杠分隔格式：'utils.py/修改/代码/动作'
    3. 逗号分隔格式：'utils.py,修改,代码,动作'
    4. 空格分隔格式：'utils.py 修改 代码 动作'
    5. 已经是列表的情况：["utils.py", "修改", "代码", "动作"]
    6. JSON格式字符串：'{"keywords": ["utils.py", "修改", "代码", "动作"]}'

    Args:
        keywords_input: 关键词输入，可以是字符串或列表

    Returns:
        list[str]: 解析后的关键词列表，去除空白项
    """
    if not keywords_input:
        return []

    # 如果已经是列表，直接处理
    if isinstance(keywords_input, list):
        return [str(k).strip() for k in keywords_input if str(k).strip()]

    # 转换为字符串处理
    keywords_str = str(keywords_input).strip()
    if not keywords_str:
        return []

    try:
        # 尝试作为JSON对象解析（支持 {"keywords": [...]} 格式）
        json_data = json.loads(keywords_str)
        if isinstance(json_data, dict) and "keywords" in json_data:
            keywords_list = json_data["keywords"]
            if isinstance(keywords_list, list):
                return [str(k).strip() for k in keywords_list if str(k).strip()]
        elif isinstance(json_data, list):
            # 直接是JSON数组格式
            return [str(k).strip() for k in json_data if str(k).strip()]
    except (json.JSONDecodeError, ValueError):
        pass

    try:
        # 尝试使用 ast.literal_eval 解析（支持Python字面量格式）
        parsed = ast.literal_eval(keywords_str)
        if isinstance(parsed, list):
            return [str(k).strip() for k in parsed if str(k).strip()]
    except (ValueError, SyntaxError):
        pass

    # 尝试不同的分隔符
    separators = ["/", ",", " ", "|", ";"]

    for separator in separators:
        if separator in keywords_str:
            keywords_list = [k.strip() for k in keywords_str.split(separator) if k.strip()]
            if len(keywords_list) > 1:  # 确保分割有效
                return keywords_list

    # 如果没有分隔符，返回单个关键词
    return [keywords_str] if keywords_str else []


