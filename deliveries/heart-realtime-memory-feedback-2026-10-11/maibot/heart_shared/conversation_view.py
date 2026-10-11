"""日常阅读版：一次输入一个区块。详细凭证保留数据库，不重抄协议过程。"""
from collections import Counter
from datetime import datetime

from .input_groups import group_inputs
from .readable import plain


SEPARATOR = '-' * 72


def ids(data):
    return [str(data['message_id'])] if data.get('message_id') else list(data.get('evidence_message_ids') or [])


def stamp(event):
    return datetime.fromisoformat(event['data'].get('observed_at') or event['time']).strftime('%H:%M:%S.%f')[:-3]


def content(value, limit=1200):
    if isinstance(value, str):
        return plain(value, limit)
    if isinstance(value, list):
        return '；'.join(filter(None, (content(item, limit) for item in value)))[:limit]
    if isinstance(value, dict):
        for key in ('items', 'hits', 'facts'):
            if key in value:
                return content(value[key], limit) or '无命中'
        for key in ('content', 'value', 'text', 'summary', 'profile_text', 'reason', 'status'):
            if value.get(key):
                return content(value[key], limit)
        return '结果已保存原始审计'
    return ''


def input_group(group):
    return group['key'][0] in {'utterance', 'message', 'batch'} or any(
        e['kind'] == '语音识别' or (e['kind'] == 'Live2D交互' and e['data'].get('user_text')) for e in group['events'])


def render_input(group, messages, index):
    events = group['events']
    rt = [e for e in events if e['kind'] == '实时对话插件']
    begin = next((e for e in rt if e['data'].get('feature') == '流式听取' and e['data'].get('phase') == 'started'), None)
    end = next((e for e in rt if e['data'].get('feature') == '流式听取' and e['data'].get('phase') == 'ended'), None)
    final = next((e for e in rt if e['data'].get('feature') == '正式输入' and e['data'].get('phase') == 'accepted'), None)
    asr = [e for e in events if e['kind'] == '语音识别']
    voice = bool(begin['data'].get('audio_input')) if begin and 'audio_input' in begin['data'] else bool(asr)
    first = begin or (asr[0] if asr else events[0])
    lines = [SEPARATOR, f"【{'语音输入' if voice else '消息'} {index}】{stamp(first)}"]
    mode = begin or next((e for e in events if e['kind'] == 'Live2D交互' and e['data'].get('phase') == 'realtime_plugin'), None)
    if mode:
        data = mode['data']
        lines.append('开关：实时插件' + ('开' if data.get('session_enabled', data.get('plugin_enabled')) else '关') +
                     '｜提前生成' + ('开' if data.get('preemptive_enabled') else '关'))
    else:
        lines.append('开关：本轮未记录实时状态' if not rt else '开关：实时已接入；完整开关状态未记录')
    utterance = next((e['data'].get('text') for e in reversed(asr) if e['data'].get('text')), '')
    if final:
        utterance = final['data'].get('text', '')
    original = [messages[mid] for mid in group['ids'] if mid in messages]
    if original:
        for msg in original:
            lines.append(plain(msg.get('name', '用户'), 80) + '：' + plain(msg['text'], 4000))
    elif utterance:
        lines.append('用户：' + plain(utterance, 4000))
    else:
        body = next((e['data'].get('user_text') for e in events if e['data'].get('user_text')), '')
        if body:
            lines.append('输入：' + plain(body, 4000))
    if end:
        lines.append('录音结束：' + stamp(end) + (f"｜最终转写提交：{stamp(final)}" if final else '｜尚未提交正式文字'))
    elif final:
        lines.append('最终转写提交：' + stamp(final) + '（旧记录未采集录音结束时间）')
    if voice:
        partials = [e for e in rt if e['data'].get('phase') == 'partial']
        successful = next((e for e in reversed(asr) if e['data'].get('text')), None)
        detail = ('｜整段识别' + str(successful['data'].get('model', '')) +
                  (f"，耗时{successful['data']['duration_seconds']:.2f}秒" if isinstance(successful['data'].get('duration_seconds'), (int, float)) else '')) if successful else ''
        lines.append(f"语音识别：临时转写{len(partials)}次更新已合并" + detail)
    timeline = []
    def add(e, value):timeline.append((e['time'], e['seq'], stamp(e) + ' ' + value))
    # 一次工具调用只显示一行开始与一行结果；内部 request/ack 不展开。
    tools = [e for e in rt if e['data'].get('tool')]
    seen = set()
    for e in tools:
        data = e['data']
        if data.get('purpose') == '回复期间复查证据':
            continue
        phase = data.get('phase')
        if phase == 'started':
            when = '录音未结束' if data.get('listening') else '输入已结束'
            add(e, f"查询开始 [{when}] {data['tool']}：{plain(data.get('input', ''), 300)}")
        elif phase == 'completed':
            result = content(data.get('output'))
            add(e, '查询结果：' + result)
        elif phase in {'failed', 'cancelled', 'timeout'}:
            add(e, f"查询{ {'failed':'失败','cancelled':'取消','timeout':'超时'}[phase]}：{plain(data.get('reason', ''), 200)}")
    checks = [e for e in tools if e['data'].get('purpose') == '回复期间复查证据' and e['data'].get('phase') == 'completed']
    if checks:
        previous, changed = {}, 0
        for check in checks:
            data = check['data']
            key, value = data['tool'], content(data.get('output'))
            if key in previous and previous[key] != value:
                changed += 1
            previous[key] = value
        add(checks[-1], f"后台证据复查：完成{len(checks)}次工具读取，结果变化{changed}次（逐次详情保留原始审计）")
    drafts = [e for e in rt if e['data'].get('feature') == '提前生成']
    starts = [e for e in drafts if e['data'].get('phase') == 'started' and e['data'].get('temporary')]
    if starts:
        actual = next((e for e in starts if e['data'].get('model_task')), starts[0])
        compare = end or final
        earlier = compare and actual['time'] < compare['time']
        when = '录音结束前' if end and earlier else '最终提交前；未采集录音结束' if earlier else '输入结束后'
        add(actual, f"提前生成开始 [{when}]：{plain(actual['data'].get('input', ''), 500)}")
        ready = next((e for e in drafts if e['data'].get('phase') == 'ready' and e['data'].get('output')), None)
        if ready:
            add(ready, '提前草稿（尚未展示）：' + plain(ready['data']['output'], 2000))
        outcome = next((e for e in reversed(drafts) if e['data'].get('phase') in {'adopted','discarded','cancelled','failed'}), None)
        if outcome:
            phase = outcome['data']['phase']
            add(outcome, '草稿' + {'adopted':'已采用','discarded':'已丢弃','cancelled':'已取消','failed':'失败'}[phase] +
                ('：' + plain(outcome['data'].get('reason', ''), 300) if outcome['data'].get('reason') else ''))
    else:
        skipped = next((e for e in reversed(drafts) if e['data'].get('phase') == 'skipped'), None)
        reason = skipped['data'].get('reason') if skipped else None
        decision = skipped['data'].get('decision_by') if skipped else None
        code = skipped['data'].get('decision_code') if skipped else None
        lines.append('提前生成：未启动' + ('，' + plain(reason, 220) if reason else '（历史记录未提供具体原因，无法回溯判断）') +
                     ('｜判断来源：' + plain(decision, 100) if decision else '') +
                     ('｜原因码：' + plain(code, 60) if code else ''))
    handoffs = [e for e in rt if e['data'].get('feature') == '回复查询依据']
    for e in handoffs:
        add(e, '回复模型查询依据 [' + plain(e['data'].get('generation_mode', ''), 30) + ']：' +
            plain(e['data'].get('summary', ''), 800) + '（已构建请求上下文，实际生成另见回复记录）')
    if not tools:
        lines.append('提前查询：未触发（当前工具为本人记忆/本地事实查询，需要查询意图）')
    generated = [e for e in events if e['kind'] == '生成回复（尚非送达）']
    if not generated:
        generated = [e for e in drafts if e['data'].get('phase') in {'generated','regenerated'}]
    for e in generated:
        response = e['data'].get('response') or e['data'].get('output')
        if response and response not in seen:
            add(e, '正式回复生成完成：' + plain(response, 4000))
            seen.add(response)
    moods = [e for e in events if e['kind'] == '心情变化']
    for e in moods:
        mood = e['data'].get('mood') or {}
        assessment = mood.get('assessment') or {}
        add(e, f"心情：{mood.get('before', '?')} → {mood.get('value', '?')}" +
            ('｜' + plain(assessment.get('reason') or mood.get('reason', ''), 240) if assessment.get('reason') or mood.get('reason') else ''))
    emotions = [e for e in events if e['kind'] in {'临时情绪产生','临时情绪变化','临时情绪结束','临时情绪判断'}]
    if emotions:
        e = emotions[-1]
        add(e, '话题情绪：' + plain((e['data'].get('emotion') or {}).get('label') or e['data'].get('status','已评估'), 100) +
            ('｜' + plain(e['data'].get('reason',''), 240) if e['data'].get('reason') else ''))
    interaction = [e for e in events if e['kind'] == 'Live2D交互']
    clips = [e for e in rt if e['data'].get('feature') == '输出与播放账本' and e['data'].get('phase') in {'playing','completed','visible','stopped','failed'}]
    if clips:
        counts = Counter(e['data']['phase'] for e in clips)
        add(clips[-1], f"输出：已播放完成{counts['completed']}段、仅文字显示{counts['visible']}段" +
            (f"、停止/失败{counts['stopped']+counts['failed']}段" if counts['stopped']+counts['failed'] else ''))
    elif interaction:
        terminal = next((e for e in reversed(interaction) if e['data'].get('phase') in {'failed','interrupted','turn_end'}), None)
        if terminal:
            add(terminal, '交互结果：' + plain(terminal['data'].get('status', ''), 240))
    # 保留真正的失败、更正、心情等待失败等；进度协议、表情发送/回执只做计数。
    for e in rt:
        data = e['data']
        if data.get('feature') in {'故障隔离','证据更正','插话与取消'} and data.get('phase') not in {'completed'}:
            add(e, data.get('feature', '') + '：' + plain(data.get('reason') or content(data.get('output')), 500))
    operations = [e for e in events if e['kind'] == '记忆操作结果']
    if operations:
        counts = Counter(e['data'].get('operation','未知') for e in operations)
        labels = {'memory_profile_admin':'人物事实读取', 'search_memory':'记忆检索',
                  'ingest_text':'记忆写入', 'ingest_summary':'摘要写入', 'heart_scoped_list':'范围检索'}
        lines.append('记忆处理：' + '、'.join(f'{labels.get(key,key)}{n}次' for key,n in counts.items()) + '（详细凭证保留后台）')
    lines.extend(row[2] for row in sorted(timeline))
    return '\n'.join(lines)


def render_conversation(day, events, messages, title='Live2D'):
    groups = [g for g in group_inputs(events, messages, ids) if g['day'] == day]
    inputs = [g for g in groups if input_group(g)]
    background = [e for g in groups if not input_group(g) for e in g['events']]
    lines = [f'{day}｜{title}｜输入{len(inputs)}次',
             '每条横线开始一次输入；识别、查询、草稿、心情及输出均在该区块内。原始凭证保留数据库。']
    if background:
        counts = Counter(e['kind'] for e in background)
        lines.append('未明确关联输入的后台记录已合并：' + '、'.join(f'{key} {n}条' for key,n in counts.items()))
        failures = [e for e in background if e['data'].get('phase') in {'failed','timeout'} or e['data'].get('error')]
        for e in failures:
            lines.append(stamp(e) + ' 后台异常：' + plain(e['data'].get('reason') or str(e['data'].get('error')), 300))
    for index, group in enumerate(inputs, 1):
        lines.append(render_input(group, messages, index))
    return '\n\n'.join(lines) + '\n'
