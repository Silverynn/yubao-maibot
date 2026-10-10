"""实时插件的中文过程视图；不把规则检索、草稿和已播放回复混为一谈。"""
import json


SEPARATOR = '-----------'


def text(value):
    # 已在写库前执行 clean；展示不再吞掉结构化工具结果或草稿内的换行。
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def is_progress(event):
    data = event.get('data') or {}
    return (event['kind'] == '实时对话插件' and
            data.get('feature') == '输出与播放账本' and data.get('phase') == 'progress')


def compact_events(events):
    """播放采样折叠为每片段一条摘要；每条原始采样仍在数据库中。"""
    groups = {}
    for event in events:
        if is_progress(event):
            data = event['data']
            key = (event.get('session', ''), data.get('utterance_id'), data.get('clip_id'))
            groups.setdefault(key, []).append(event)
    result = []
    for event in events:
        if not is_progress(event):
            result.append(event)
            continue
        data = event['data']
        peers = groups[(event.get('session', ''), data.get('utterance_id'), data.get('clip_id'))]
        if event is peers[-1]:
            result.append(dict(event, data=dict(data, phase='progress_summary',
                sample_count=len(peers), sample_start=peers[0]['time'])))
    # 证据复查确实执行过，但连续未变化的结果不用每两秒重抄一遍。
    # 首次调用、变化后的结果及异常始终展开；只合并同一轮同一工具的连续相同结果。
    starts, previous, repeats = {}, {}, []
    for event in result:
        data = event['data']
        if not data.get('tool') or data.get('purpose') != '回复期间复查证据':
            continue
        key = (event.get('session'), data.get('utterance_id'), data.get('epoch'), data['tool'], data.get('input'))
        if data.get('phase') == 'started':
            starts[data.get('call_id')] = event
        elif data.get('phase') == 'completed':
            signature = json.dumps(data.get('output'), ensure_ascii=False, sort_keys=True)
            old = previous.get(key)
            if old and old[0] == signature:
                old[1].append(event)
            else:
                bucket = []
                repeats.append(bucket)
                previous[key] = (signature, bucket)
        else:
            previous.pop(key, None)
    suppressed, summaries = set(), {}
    for bucket in repeats:
        if not bucket:
            continue
        for event in bucket:
            suppressed.add(id(event))
            start = starts.get(event['data'].get('call_id'))
            if start:
                suppressed.add(id(start))
        last = bucket[-1]
        summaries[id(last)] = dict(last, data=dict(last['data'], phase='repeat_query_summary',
            repeat_count=len(bucket), sample_start=bucket[0]['time'], output='与上次返回结果相同，正文不重复展开'))
    return [summaries[id(e)] if id(e) in summaries else e for e in result
            if id(e) not in suppressed or id(e) in summaries]


PHASES = {'started':'开始', 'completed':'完成', 'ready':'草稿已完成，尚未展示',
          'partial':'临时转写更新（仍可修改）', 'accepted':'最终输入已确认',
          'native_bound':'正式消息已关联', 'discarded':'丢弃', 'cancelled':'取消',
          'timeout':'超时', 'failed':'失败', 'queued':'输出已排队，尚未播放',
          'playing':'开始播放', 'visible':'文字已显示', 'committed':'已登记实际完成的输出',
          'commit_pending':'输出登记等待确认', 'closed':'关闭', 'changed':'开关变更',
          'mode':'开关同步', 'approval':'草稿复用检查', 'adopted':'已复用提前草稿',
          'regenerated':'依据新材料重新生成', 'generated':'正式回复生成完成',
          'skipped':'未启动', 'requested':'已请求更正', 'deferred':'延后更正',
          'wait_budget_exceeded':'停止等待，后台查询仍继续', 'late_adopted':'查询晚到，已提交回复流程'}
PHASES.update(progress_summary='播放进度摘要', repeat_query_summary='连续复查结果未变化')


def render_realtime(data):
    feature, phase = data.get('feature', '未分类'), data.get('phase', '')
    title = feature
    if feature == '提前生成' and phase in {'generated', 'regenerated', 'budget_exhausted'}:
        title = '正式回复生成'
    if feature == '提前生成' and phase == 'started' and not data.get('temporary'):
        title = '提前生成准备'  # 宿主开始记录通常带 temporary；旧日志不推测。
    lines = [f"功能：{title} · {PHASES.get(phase, phase)}",
             '开关：实时插件' + ('开启' if data.get('session_enabled', data.get('plugin_enabled')) else '关闭') +
             '；提前生成' + ('开启' if data.get('preemptive_enabled') else '关闭')]
    if feature == '提前生成' and phase in {'started', 'ready'}:
        if data.get('plugin_loaded'):
            lines.append('记录层：实时协调器的草稿请求/回执；不能将它与宿主记录重复计为两次模型调用')
        else:
            lines.append('记录层：原生回复器的模型请求/返回')
    if 'listening' in data:
        lines.append('发生阶段：' + ('用户仍在说话' if data['listening'] else '用户已结束输入或当前未在说话'))
    if isinstance(data.get('since_input_ms'), (int, float)):
        lines.append(f"距本次输入开始：{data['since_input_ms']/1000:.3f}秒")
    if data.get('tool'):
        lines.append('工具：' + data['tool'])
        lines.append('触发方式：' + data.get('trigger', '实时插件规则触发的只读查询；不是模型自行选择工具'))
    if data.get('purpose'):
        lines.append('用途：' + data['purpose'])
    for key, label in [('text','转写文字'),('input','输入/查询'),('reason','原因'),('model','模型')]:
        if data.get(key):
            lines.append(label + '：' + text(data[key]))
    if phase == 'approval':
        lines.append('复用检查：' + ('通过；最终采用仍看“已复用提前草稿”' if data.get('approved') else '未通过/没有可复用草稿'))
    if phase == 'progress_summary':
        lines.append(f"播放进度：已折叠{data['sample_count']}次采样；最后播放位置{data.get('position_seconds', 0):.2f}秒；原始采样保留数据库")
    if phase == 'repeat_query_summary':
        lines.append(f"连续复查：结果未变化，合并{data['repeat_count']}次真实查询；首条合并记录{data['sample_start']}；最后一次见本条时间；逐次开始/结果保留数据库")
    if 'output' in data and data['output'] not in ('', None):
        label = ('查询结果' if data.get('tool') else '提前生成内容（隐藏草稿，未送达）'
                 if feature == '提前生成' and phase in {'ready','discarded','adopted'} else '输出/结果')
        lines.append(label + '：\n' + text(data['output']))
    if data.get('usage'):
        lines.append('模型用量：' + text(data['usage']))
    if isinstance(data.get('duration_ms'), (int, float)):
        lines.append(f"工具耗时：{data['duration_ms']/1000:.3f}秒")
    if feature == '提前生成' and phase == 'cancelled':
        lines.append('说明：请求取消不保证远端模型停止计费；未产生的草稿内容无法补记。')
    return lines
