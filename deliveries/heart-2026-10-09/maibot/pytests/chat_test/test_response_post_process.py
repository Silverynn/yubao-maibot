"""回复后处理元数据测试。"""

import pytest

from src.chat.utils import utils as chat_utils
from src.chat.utils.utils import ProcessedResponseSegment


def test_splitter_preserves_punctuation(monkeypatch) -> None:
    examples = [
        "你好，今天过得怎么样？我很好。",
        "先吃饭,再出门;晚上见。",
        "等等，，真的吗？！好吧。。。",
        "第一句。\n第二句， 好的。",
        "，开头有逗号。",
        "。 ， ;",
        "好。",
        "你好！“明天见，别迟到。”",
        "Hello, world; see you tomorrow.",
    ]
    for probability in (0.0, 0.5, 1.0):
        monkeypatch.setattr(chat_utils.random, "random", lambda: probability)
        for text in examples:
            sentences = chat_utils.split_into_sentences_w_remove_punctuation(text)
            # 分段可以改变空白，但正文和标点必须完整、有序地保留。
            assert "".join("".join(sentences).split()) == "".join(text.split())
            assert chat_utils.random_remove_punctuation(text) == text


def test_response_removes_only_message_terminal_period_when_merging(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils, "ChineseTypoGenerator", _FixedTypoGenerator)
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_length", 500)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_sentence_num", 20)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_split_num", 1)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", False)
    monkeypatch.setattr(chat_utils.random, "random", lambda: 1.0)

    text = "你好，今天吃饭了吗？我吃过了。明天见。"
    expected = ["你好，今天吃饭了吗？我吃过了。明天见"]
    assert chat_utils.process_llm_response(text) == expected
    assert chat_utils.process_llm_response(text, enable_splitter=False) == expected


@pytest.mark.parametrize(
    ("text", "max_split_num", "expected"),
    [
        ("你好，今天吃饭了吗？我吃过了。明天见。", 20, ["你好，今天吃饭了吗？", "我吃过了", "明天见"]),
        ("第一句。第二句，第三句。第四句。", 2, ["第一句。第二句，第三句", "第四句"]),
        ("第一句。\n第二句， 好的。", 20, ["第一句", "第二句， 好的"]),
        ("先吃饭,再出门;晚上见。", 20, ["先吃饭,再出门;", "晚上见"]),
        ("等等。。。\n真的吗？！\n好吧……\n明天见。", 20, ["等等。。。", "真的吗？！", "好吧……", "明天见"]),
        ('他说"明天见，别迟到。"\n好的。', 20, ['他说"明天见，别迟到。"', "好的"]),
        ("数值是3.14\nHello world.\n结束。", 20, ["数值是3.14", "Hello world", "结束"]),
        ("第一句；\n第二句。", 20, ["第一句；", "第二句"]),
        ("。\n好的。", 20, ["好的"]),
        ("好。", 20, ["好"]),
        ("好。。。", 20, ["好。。。"]),
        ("人是会这样的，一个上午过去都没散掉。\n\n那就别自己待着。", 1,
         ["人是会这样的，一个上午过去都没散掉。那就别自己待着"]),
        ("第一行\n \t\n\n第二行。", 1, ["第一行。第二行"]),
    ],
)
def test_response_display_at_final_message_boundaries(
    monkeypatch, text: str, max_split_num: int, expected: list[str]
) -> None:
    monkeypatch.setattr(chat_utils, "ChineseTypoGenerator", _FixedTypoGenerator)
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_length", 500)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_sentence_num", 20)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_split_num", max_split_num)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", False)
    monkeypatch.setattr(chat_utils.random, "random", lambda: 1.0)

    assert chat_utils.process_llm_response(text) == expected
    unsplit = chat_utils.process_llm_response(text, enable_splitter=False)
    assert unsplit == [chat_utils._format_reply_display_text(text)]


@pytest.mark.parametrize(("text", "expected"), [
    ("你好。再见。", "你好。再见"),
    ("你好？我在！放心，没事；慢慢说：我听着。", "你好？我在！放心，没事；慢慢说：我听着"),
    ("等一下……好吧。。。Really...", "等一下……好吧。。。Really..."),
    ("数值3.14，版本v1.2.3，文件README.md。", "数值3.14，版本v1.2.3，文件README.md"),
    ("数值3.14", "数值3.14"),
    ("请看https://example.com/a?q=x;y=2。", "请看https://example.com/a?q=x;y=2"),
    ("Dr. Smith said don't panic. All is well.", "Dr. Smith said don't panic. All is well"),
    ("Dr.", "Dr."),
    ("她说：“我没事。\n\n别担心。”然后点头。", "她说：“我没事。\n\n别担心。”然后点头"),
    ("保留`print('hi.'); value=3.14`，继续。", "保留`print('hi.'); value=3.14`，继续"),
    ("你好(・。・)，今天怎么样？", "你好(・。・)，今天怎么样？"),
    ("  第一行。\r\n\r\n\t\r\n第二行。  ", "第一行。第二行"),
    ("\n \t\n", ""),
    ("。", ""),
])
def test_display_preserves_special_punctuation_and_is_idempotent(text: str, expected: str) -> None:
    formatted = chat_utils._format_reply_display_text(text)
    assert formatted == expected
    assert chat_utils._format_reply_display_text(formatted) == formatted


def test_postprocess_disabled_still_has_no_blank_lines_or_terminal_periods(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", False)
    assert chat_utils.process_llm_response("第一句。\n\n第二句？") == ["第一句。第二句？"]
    assert chat_utils.process_llm_response("\n \t\n") == []


def test_splitter_overflow_still_formats_display(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", False)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_sentence_num", 1)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable_overflow_return_all", True)
    assert chat_utils.process_llm_response("第一句。\n\n第二句。") == ["第一句。第二句"]


def test_comma_clauses_do_not_split_randomly(monkeypatch) -> None:
    text = "嗯，还在啊，玩了一晚上都没散掉那今天就不跟你讲道理了，讲了也没用这种时候就别较劲问自己缺什么了，问不出来的一点都不奇怪你就先当它是个阴天，晾着，等它自己过想聊点别的就聊，不想聊我陪着也行，反正这个点我也不困"
    for probability in (0.0, 0.5, 1.0):
        monkeypatch.setattr(chat_utils.random, "random", lambda: probability)
        assert chat_utils.split_into_sentences_w_remove_punctuation(text) == [text]


@pytest.mark.parametrize("text", [
    "你好，先吃饭，再出门。晚上见！",
    "她说：“先等等，别走。”然后点头。明天见。",
    "他说“她说‘别走。’”，我答应了。",
    "Dr. Smith said don't panic. All is well.",
    "数值3.14，价格1,234元。网址https://example.com/a?q=x;y=2，稍后看。",
    "保留`print('hi'); value=3.14`，别拆代码。然后继续。",
    "第一段\n第二段，说明\n第三段。",
])
def test_splitter_does_not_lose_or_reorder_content(text: str) -> None:
    fragments = chat_utils.split_into_sentences_w_remove_punctuation(text)
    assert "".join(fragments) == text


def test_abbreviation_url_and_quoted_punctuation_are_not_boundaries() -> None:
    assert chat_utils.split_into_sentences_w_remove_punctuation("Dr. Smith said don't panic. All is well.") == [
        "Dr. Smith said don't panic.", " All is well."
    ]
    text = "请看https://example.com/a?q=x;y=2，稍后看。"
    assert chat_utils.split_into_sentences_w_remove_punctuation(text) == [text]
    assert chat_utils.split_into_sentences_w_remove_punctuation("她说：“先等等，别走。”然后点头。明天见。") == [
        "她说：“先等等，别走。”然后点头。", "明天见。"
    ]


class _FixedTypoGenerator:
    """返回固定错别字与纠正内容，隔离词频数据和拼音随机性。"""

    def __init__(self, **_kwargs: object) -> None:
        pass

    def create_typo_sentence(self, _sentence: str) -> tuple[str, str]:
        return "今田见", "天"


def test_typo_correction_marks_quote_previous(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils, "ChineseTypoGenerator", _FixedTypoGenerator)
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", False)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable_correction_quote", True)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "correction_quote_probability", 1.0)
    monkeypatch.setattr(chat_utils.random, "random", lambda: 0.0)

    segments = chat_utils.process_llm_response_segments("今天见", enable_splitter=False)

    assert segments == [
        ProcessedResponseSegment("今田见"),
        ProcessedResponseSegment("天", quote_previous=True),
    ]
    assert chat_utils.process_llm_response("今天见", enable_splitter=False) == ["今田见", "天"]


def test_typo_correction_quote_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils, "ChineseTypoGenerator", _FixedTypoGenerator)
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", False)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable_correction_quote", False)
    monkeypatch.setattr(chat_utils.random, "random", lambda: 0.0)

    segments = chat_utils.process_llm_response_segments("今天见", enable_splitter=False)

    assert segments == [
        ProcessedResponseSegment("今田见"),
        ProcessedResponseSegment("天"),
    ]


def test_typo_correction_quote_respects_probability(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils, "ChineseTypoGenerator", _FixedTypoGenerator)
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", False)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable_correction_quote", True)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "correction_quote_probability", 0.0)
    monkeypatch.setattr(chat_utils.random, "random", lambda: 0.0)

    segments = chat_utils.process_llm_response_segments("今天见", enable_splitter=False)

    assert segments == [
        ProcessedResponseSegment("今田见"),
        ProcessedResponseSegment("天"),
    ]


def test_merge_keeps_typo_correction_at_start_of_quoted_message() -> None:
    segments = [
        ProcessedResponseSegment("今田见"),
        ProcessedResponseSegment("天", quote_previous=True),
        ProcessedResponseSegment("晚点聊"),
    ]

    merged_segments = chat_utils._merge_processed_segments_to_max_count(segments, 2)

    assert merged_segments == [
        ProcessedResponseSegment("今田见"),
        ProcessedResponseSegment("天晚点聊", quote_previous=True),
    ]


@pytest.mark.parametrize("text", [
    "买菜清单：\n1. 牛奶\n2. 面包\n3. 鸡蛋",
    "买菜清单：\n1.牛奶\n2、面包\n3)鸡蛋",
    "- 牛奶\n- 面包\n- 鸡蛋",
    "> 今天不方便。\n> 明天再聊。",
    "```python\nx = 1\ny = 2\nprint(x + y)\n```",
    "说明：\n~~~text\n1. literal period.\n~~~",
])
def test_structured_content_stays_intact(monkeypatch, text: str) -> None:
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_split_num", 1)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", False)
    assert "".join(chat_utils.split_into_sentences_w_remove_punctuation(text)) == text
    assert chat_utils.process_llm_response(text) == [text]
    assert chat_utils._format_reply_display_text(text) == text


def test_six_sentences_keep_three_bubbles_without_chat_newlines(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_sentence_num", 8)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_split_num", 3)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", False)
    assert chat_utils.process_llm_response("我在。慢慢说。别着急。先喝点水。歇一会儿。想聊就找我。") == [
        "我在。慢慢说", "别着急。先喝点水", "歇一会儿。想聊就找我"]


def test_numbered_list_does_not_consume_one_sentence_per_number(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils.global_config.response_post_process, "enable_response_post_process", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "enable", True)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_sentence_num", 8)
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_split_num", 3)
    monkeypatch.setattr(chat_utils.global_config.chinese_typo, "enable", False)
    text = "购物清单：\n1. 牛奶\n2. 面包\n3. 鸡蛋\n4. 水果\n5. 茶叶"
    assert chat_utils.process_llm_response(text) == [text]


def test_structured_block_does_not_leave_quote_stack_open(monkeypatch) -> None:
    monkeypatch.setattr(chat_utils.global_config.response_splitter, "max_split_num", 3)
    text = "```text\nBob's example\n```\n第一句。第二句。"
    parts = chat_utils.split_into_sentences_w_remove_punctuation(text)
    assert "".join(parts) == text
    assert parts[-1] == "第二句。"


def test_quote_at_line_end_does_not_gain_duplicate_punctuation() -> None:
    assert chat_utils._format_reply_display_text('她说：“没事。”\n我会陪她。') == '她说：“没事。”我会陪她'
    assert chat_utils._format_reply_display_text('He said "OK."\nThen left.') == 'He said "OK." Then left'
