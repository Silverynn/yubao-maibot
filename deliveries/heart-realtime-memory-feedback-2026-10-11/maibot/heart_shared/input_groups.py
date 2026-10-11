"""仅依据明确的发言/消息/任务编号归组，绝不依据时间接近猜测。"""
from collections import OrderedDict


def identity(event, value):
    return (event.get('session', ''), str(value))


def group_inputs(events, messages, evidence_ids):
    message_inputs, task_messages, task_inputs = {}, {}, {}
    for event in events:
        data = event['data']
        ids = evidence_ids(data)
        utterance = data.get('utterance_id')
        if utterance and len(ids) == 1:
            message_inputs.setdefault(identity(event, ids[0]), set()).add(identity(event, utterance))
        task = data.get('task_id') or data.get('audit_task_id')
        if task and utterance:
            task_inputs.setdefault(identity(event, task), set()).add(identity(event, utterance))
        if task and len(ids) == 1:
            task_messages.setdefault(identity(event, task), set()).add(ids[0])
    groups = OrderedDict()
    for event in events:
        data, ids = event['data'], evidence_ids(event['data'])
        utterance = data.get('utterance_id')
        task = data.get('task_id') or data.get('audit_task_id')
        # 同一明确任务可以把 ASR、网页进度等接回正式消息。歧义则保持独立。
        if not ids and task:
            candidates = task_messages.get(identity(event, task), set())
            if len(candidates) == 1:
                ids = list(candidates)
        target = identity(event, utterance) if utterance else None
        if not target and task:
            candidates = task_inputs.get(identity(event, task), set())
            if len(candidates) == 1:
                target = next(iter(candidates))
        if not target and len(ids) == 1:
            candidates = message_inputs.get(identity(event, ids[0]), set())
            if len(candidates) == 1:
                target = next(iter(candidates))
        if target:
            key, title = ('utterance', target), '一次语音/实时输入'
        elif len(ids) == 1:
            key, title = ('message', identity(event, ids[0])), '消息' if ids[0] in messages else '消息关联待核实'
        elif len(ids) > 1:
            key, title = ('batch', (event.get('session', ''), tuple(sorted(ids)))), '多条消息的合并处理'
        else:
            key = ('task', identity(event, task)) if task else ('event', event['seq'])
            title = '后台任务'
        if key not in groups:
            groups[key] = {'day':event['day'], 'title':title, 'ids':[], 'events':[], 'key':key}
        group = groups[key]
        group['events'].append(event)
        group['ids'] = list(dict.fromkeys(group['ids'] + ids))
        if len(group['ids']) == 1 and group['ids'][0] in messages:
            group['day'] = messages[group['ids'][0]]['received'][:10]
    return list(groups.values())


def input_header(group):
    events = group['events']
    begin = next((e for e in events if e['kind'] == '实时对话插件'
                  and e['data'].get('feature') == '流式听取' and e['data'].get('phase') == 'started'), None)
    voice = any(e['kind'] == '语音识别' for e in events)
    if begin and 'audio_input' in begin['data']:
        voice = bool(begin['data']['audio_input'])
    lines = ['输入类型：' + ('一次语音输入（含转写、查询、草稿和回复）' if voice else '一次文字/实时输入')]
    modes = [e['data'] for e in events if e['kind'] == '实时对话插件']
    if modes:
        mode = begin['data'] if begin else modes[0]
        lines.append('开始时开关：实时插件' + ('开启' if mode.get('session_enabled', mode.get('plugin_enabled')) else '关闭') +
                     '；提前生成' + ('开启' if mode.get('preemptive_enabled') else '关闭'))
        calls = [e for e in events if e['data'].get('tool') and e['data'].get('phase') == 'started']
        if not calls:
            lines.append('实时提前查询：本次未观测到工具开始记录（旧日志不能补推开始时间；正式链路的记忆操作见下方）')
        drafts = [e for e in events if e['data'].get('feature') == '提前生成'
                  and e['data'].get('phase') == 'started' and e['data'].get('temporary')]
        if not drafts:
            lines.append('提前生成：本次未观测到临时草稿启动；开启开关不代表每轮都会调用模型')
    return lines
