"""执行实际回归文件，隔离主程序初始化、词频库和真实聊天；无需安装 pytest。"""
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import ast
import hashlib
import inspect
import json
import logging
import random
import re
import sys

WORK = Path(__file__).resolve().parents[1]
MAI = WORK / 'maibot'
ROOT = MAI
NAMES = {'ProcessedResponseSegment', 'is_english_letter', 'split_into_sentences_w_remove_punctuation',
         'merge_sentences_to_max_count', '_merge_processed_segments_to_max_count', 'random_remove_punctuation',
         '_reply_structured_spans', '_flatten_reply_prose', '_format_reply_display_text', '_get_random_default_reply', 'process_llm_response_segments',
         'process_llm_response', 'protect_kaomoji', 'recover_kaomoji', 'get_western_ratio'}
checks = []
def ok(name, condition=True):
    assert condition, name
    checks.append(name)
class NoTypo:
    def __init__(self, **kwargs): pass
    def create_typo_sentence(self, text): return text, ''
def load(path, name):
    module = ModuleType(name)
    sys.modules[name] = module
    module.__dict__.update(dataclass=dataclass, re=re, random=random, logger=logging.getLogger(name), ChineseTypoGenerator=NoTypo,
        global_config=NS(bot=NS(nickname='测试角色'), response_post_process=NS(enable_response_post_process=True),
            response_splitter=NS(enable=True, max_length=512, max_sentence_num=8, max_split_num=3,
                enable_kaomoji_protection=False, enable_overflow_return_all=False),
            chinese_typo=NS(enable=False, error_rate=0, min_freq=0, tone_error_rate=0, word_replace_rate=0,
                enable_correction_quote=True, correction_quote_probability=1)))
    nodes = [node for node in ast.parse(path.read_text(encoding='utf-8')).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in NAMES]
    exec(compile(ast.Module(nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
    return module
current = load(ROOT / 'src/chat/utils/utils.py', 'display_current')
class Parametrize:
    def __call__(self, names, values):
        def decorate(function):
            function.cases = (names, values)
            return function
        return decorate
class MonkeyPatch:
    def __init__(self): self.saved = []
    def setattr(self, target, name, value):
        self.saved.append((target, name, getattr(target, name)))
        setattr(target, name, value)
    def undo(self):
        for target, name, value in reversed(self.saved): setattr(target, name, value)
namespace = dict(chat_utils=current, ProcessedResponseSegment=current.ProcessedResponseSegment,
                 pytest=NS(mark=NS(parametrize=Parametrize())))
path = ROOT / 'pytests/chat_test/test_response_post_process.py'
nodes = [node for node in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
exec(compile(ast.Module(nodes, type_ignores=[]), str(path), 'exec'), namespace)
for name, function in sorted(namespace.items()):
    if not name.startswith('test_') or not callable(function): continue
    if hasattr(function, 'cases'):
        names, values = function.cases
        if isinstance(names, str): names = [part.strip() for part in names.split(',')]
        cases = [dict(zip(names, row if isinstance(row, tuple) else (row,), strict=True)) for row in values]
    else:
        cases = [{}]
    for ordinal, args in enumerate(cases):
        monkeypatch = MonkeyPatch()
        if 'monkeypatch' in inspect.signature(function).parameters: args = args | {'monkeypatch': monkeypatch}
        try:
            function(**args)
            ok(f'{name}[{ordinal}]')
        finally:
            monkeypatch.undo()

screenshot = '人是会这样的，一个上午过去都没散掉。\n\n那就别自己待着。'
current.global_config.response_splitter.max_split_num = 1
updated = current.process_llm_response(screenshot)
ok('截图普通回复不换行，保留句内标点，仅去气泡末尾句号', updated == ['人是会这样的，一个上午过去都没散掉。那就别自己待着'])
current.global_config.response_splitter.enable_kaomoji_protection = True
ok('真实颜文字保护/恢复入口兼容', current.process_llm_response('你好(・。・)，明天见。') == ['你好(・。・)，明天见'])
current.global_config.response_splitter.enable_kaomoji_protection = False
reply_tree = ast.parse((MAI / 'src/maisaka/builtin_tool/reply.py').read_text(encoding='utf-8-sig'))
assignment = next(node for node in ast.walk(reply_tree) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == 'combined_reply_text' for target in node.targets))
combined = eval(compile(ast.Expression(body=assignment.value), 'actual-combined-reply', 'eval'), {'reply_segments': ['第一句', '第二句？']})
ok('实际审计/工具结果拼接保留发送消息边界', combined == '第一句\n第二句？')
context_tree = ast.parse((MAI / 'src/maisaka/builtin_tool/context.py').read_text(encoding='utf-8-sig'))
context_nodes = [node for node in ast.walk(context_tree) if isinstance(node, ast.FunctionDef)
                 and node.name in {'post_process_reply_text', 'post_process_reply_segments'}]
context_namespace = dict(List=list, ProcessedResponseSegment=current.ProcessedResponseSegment,
                         process_llm_response=current.process_llm_response,
                         process_llm_response_segments=current.process_llm_response_segments)
for node in context_nodes:
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual-context-wrapper', 'exec'), context_namespace)
for name in ('post_process_reply_text', 'post_process_reply_segments'):
    for skip in (False, True):
        ok(f'外层{name}跳过={skip}不回填空白消息', context_namespace[name](' \n\t\n', skip_post_process=skip) == [])
    ok(f'外层{name}不回填已删除的纯句号', context_namespace[name]('。') == [])
    expected = ['第一句。第二句？'] if name.endswith('_text') else [current.ProcessedResponseSegment('第一句。第二句？')]
    ok(f'外层{name}实际走格式清理结果', context_namespace[name]('第一句。\n\n第二句？') == expected)
for count in (1, 2, 3):
    current.global_config.response_splitter.max_split_num = count
    outputs = current.process_llm_response('你好。\n\n你还好吗？我在！\n\n稍后见。')
    ok(f'消息上限{count}仍有效且所有发送项无空白行', len(outputs) <= count and all(
       item.strip() and '\n' not in item for item in outputs))
print(json.dumps({'passed':len(checks),'scope':'交付版显示函数与回归断言隔离执行，非真实QQ发送'},ensure_ascii=False))
