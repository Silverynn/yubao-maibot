"""记忆可见性纯函数。群聊内容必须有唯一、可核实的当前会话来源。"""
import json
import time


def metadata(item):
    value = item.get('metadata') or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def tokens(meta, *keys):
    values = set()
    for key in keys:
        value = meta.get(key)
        for token in value if isinstance(value, (list, tuple)) else [value]:
            if isinstance(token, (str, int)) and str(token):
                values.add(str(token))
    return values


def visible(item, chat_id, person_id=''):
    """不根据昵称、文本相似度或当前提问者猜记忆的来源。"""
    meta = metadata(item)
    if meta.get('scope_type') and meta['scope_type'] != 'chat':
        return False
    chats = tokens(meta, 'chat_id', 'chat_ids', 'session_id', 'session_ids', 'stream_id', 'stream_ids')
    source = str(item.get('source') or meta.get('source') or '')
    if source.startswith('chat_summary:'):
        chats.add(source.removeprefix('chat_summary:'))
    if not chat_id or chats != {chat_id}:
        return False
    people = tokens(meta, 'person_id', 'person_ids')
    if person_id and person_id not in people:
        return False
    if item.get('is_deleted') or not str(item.get('content') or '').strip():
        return False
    change = meta.get('memory_change') or {}
    if change.get('change_type') == 'mark_superseded':
        return False
    for expiry in (item.get('expires_at'), change.get('valid_to')):
        if expiry is not None:
            try:
                if float(expiry) <= time.time():
                    return False
            except (TypeError, ValueError):
                return False
    return True


def filter_result(result, chat_id, person_id=''):
    """不把被过滤内容、摘要或旁路字段重新传给模型。"""
    if not isinstance(result, dict):
        return {'success': False, 'hits': [], 'error': '记忆结果格式错误'}
    hits = result.get('hits') or []
    allowed = [hit for hit in hits if isinstance(hit, dict) and visible(hit, chat_id, person_id)]
    return {key: result[key] for key in ('success', 'error', 'filtered') if key in result} | {
        'hits': allowed, 'summary': '\n'.join(str(h.get('content') or '') for h in allowed),
        'scope_note': '仅当前会话；来源不明或混合来源不公开', 'scope_removed_count': len(hits)-len(allowed)}
