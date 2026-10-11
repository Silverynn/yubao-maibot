from .conversation_view import content
from .readable import plain


def console_summary(feature, data):
    """控制台只报输入边界、行动/草稿结果和错误；完整协议事件仍入审计库。"""
    phase = data.get('phase', '')
    if phase in {'failed', 'timeout', 'close_failed', 'voice_failed'}:
        return feature + '异常：' + plain(data.get('reason') or content(data.get('output')), 240)
    if feature == '只读工具调用':
        if data.get('purpose') == '回复期间复查证据':
            return None
        if phase == 'started':
            return '查询开始 ' + str(data.get('tool', '')) + '：' + plain(data.get('input', ''), 160)
        if phase == 'completed':
            return '查询结果 ' + str(data.get('tool', '')) + '：' + content(data.get('output'), 240)
        return None
    if feature == '提前生成' and phase in {'ready', 'discarded'}:
        return '提前草稿' + ('已完成：' + plain(data.get('output', ''), 240) if phase == 'ready'
                            else '已丢弃：' + plain(data.get('reason', ''), 160))
    if (feature, phase) in {('流式听取', 'started'), ('流式听取', 'ended'),
                            ('插件开关', 'loaded'), ('插件开关', 'changed'),
                            ('正式输入', 'accepted'), ('会话绑定', 'closed')}:
        return feature + '：' + plain(data.get('output') or data.get('text') or phase, 240)
    return None
