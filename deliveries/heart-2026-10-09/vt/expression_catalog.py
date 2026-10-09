"""Read expressions declared by the currently selected Open-LLM-VTuber model.

The model URL must stay inside this VTuber installation's live2d-models dir.
No remote fetches and no arbitrary file reads are needed for /表情列表.
"""

import json
import re
from pathlib import Path


def model_settings_path(model):
    info = getattr(model, 'model_info', None) or {}
    url = info.get('url', '')
    if not isinstance(url, str) or not url.startswith('/live2d-models/'):
        return None
    models_root = (Path(__file__).resolve().parent / 'live2d-models').resolve()
    path = (Path(__file__).resolve().parent / url.lstrip('/')).resolve()
    if not path.is_relative_to(models_root) or not path.is_file():
        return None
    return path


def model_file_references(model):
    path = model_settings_path(model)
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))['FileReferences']
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def model_expression_names(model):
    """有实际可读文件才算可用；不能仅凭配置里写了一个名字就列出来。"""
    entries = model_file_references(model).get('Expressions', [])
    settings = model_settings_path(model)
    if settings is None or not isinstance(entries, list):
        return ()
    names = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('Name'), str) or not entry['Name']:
            continue
        filename = entry.get('File')
        if not isinstance(filename, str):
            continue
        file = (settings.parent / filename).resolve()
        if not file.is_relative_to(settings.parent) or not file.is_file():
            continue
        try:
            expression = json.loads(file.read_text(encoding='utf-8'))
            if (expression.get('Type') != 'Live2D Expression'
                    or not isinstance(expression.get('Parameters'), list)):
                continue
        except (OSError, ValueError, AttributeError):
            continue
        names.append(entry['Name'])
    return tuple(dict.fromkeys(names))


def model_expression_aliases(model):
    """中文名称由当前模型配置提供；只接受模型实际声明过的表情。"""
    names = set(model_expression_names(model))
    aliases = (getattr(model, 'model_info', None) or {}).get('expressionAliases', {})
    return {key: value for key, value in aliases.items()
            if isinstance(key, str) and isinstance(value, str) and value in names}


def _target_name(target, names, entries):
    if isinstance(target, str) and target in names:
        return target
    if type(target) is int and 0 <= target < len(entries):
        name = entries[target].get('Name') if isinstance(entries[target], dict) else None
        return name if name in names else None
    return None


def expression_target_name(model, target):
    """把字符串名称／原始数字索引统一成同一个真实表情，方便去重。"""
    return _target_name(target, model_expression_names(model), model_file_references(model).get('Expressions', []))


def model_expression_choices(model):
    """每个实际表情只选一个中文名称；旧别名仍然能用，但不重复展示。"""
    preferred = (
        ('neutral', '平静'), ('joy', '开心'), ('sadness', '难过'), ('anger', '生气'),
        ('confusion', '疑惑'), ('surprise', '惊讶'), ('blush', '害羞'), ('crying', '大哭'),
        ('distressed', '委屈'), ('curiosity', '好奇'), ('thinking', '思考'), ('comfort', '安慰'),
        ('sleepy', '困倦'), ('playful', '调皮'), ('listening', '倾听'), ('talk', '说话'),
    )
    mapping = model.emo_map
    names = model_expression_names(model)
    entries = model_file_references(model).get('Expressions', [])
    result, seen = {}, set()

    def add(label, target):
        name = _target_name(target, names, entries)
        if name is not None and name not in seen and label not in result:
            result[label] = target
            seen.add(name)

    for key, label in preferred:
        add(label, mapping.get(key))
    for label, target in model_expression_aliases(model).items():
        if re.search(r'[\u4e00-\u9fff]', label) and not re.search(r'[A-Za-z]', label):
            add(label, target)
    for index, name in enumerate(names, 1):
        label = name if re.search(r'[\u4e00-\u9fff]', name) and not re.search(r'[A-Za-z]', name) else '自定义表情' + str(index)
        add(label, name)
    return result


def model_expression_label(model, expression):
    names = model_expression_names(model)
    entries = model_file_references(model).get('Expressions', [])
    name = _target_name(expression, names, entries)
    for label, target in model_expression_choices(model).items():
        if _target_name(target, names, entries) == name:
            return label
    return '自定义表情'


def model_expression_list_text(model, automatic=False):
    choices = model_expression_choices(model)
    if choices:
        text = f'当前形象可用表情（共{len(choices)}种，已去重）：\n'
        commands = ['/表情 ' + label for label in choices]
        # 每行三个，避免列表变成很高的字幕，挡住整个形象。
        text += '\n'.join('、'.join(commands[start:start + 3]) for start in range(0, len(commands), 3))
    else:
        text = '当前形象未检测到可读取的表情文件。'
    if automatic:
        text += '\n/表情 自动：恢复心情自动控制。'
    return text


def model_expression_motion(model, expression):
    """可选的表情配套动作；旧模型未配置时保持原来的仅表情行为。"""
    entry = (getattr(model, 'model_info', None) or {}).get('expressionMotions', {}).get(expression)
    if entry is None:
        return None
    group, index = entry.get('group'), entry.get('index', 0)
    motions = model_file_references(model).get('Motions', {}).get(group, [])
    if not isinstance(group, str) or not isinstance(index, int) or not 0 <= index < len(motions):
        raise ValueError('当前模型的表情动作配置无效：' + str(expression))
    return {'group': group, 'index': index}
