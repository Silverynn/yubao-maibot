"""长回复先合成一个较短的自然片段；所有文字保留，音文仍同时送达。"""
from typing import List
import re


def short_first_speech(text: str, target: int = 48, minimum_length: int = 60) -> List[str]:
    if len(text) <= minimum_length:
        return [text]
    # 优先在标点/空格处分段，不切英文单词或 URL。
    boundaries = [match.end() for match in re.finditer(r'[。！？；，!?;，\s]', text[:target])]
    boundaries = [point for point in boundaries if point >= 16]
    if boundaries:
        point = boundaries[-1]
    elif re.search(r'[\u4e00-\u9fff]', text[:target]):
        point = target
    else:
        return [text]  # 无安全边界的英文/代码不为了提速强行切词。
    for match in re.finditer(r'https?://[^\s，。！？；]+', text):
        if match.start() < point < match.end():
            point = match.start()
    if point < 16 or point >= len(text):
        return [text]
    return [text[:point], text[point:]]
