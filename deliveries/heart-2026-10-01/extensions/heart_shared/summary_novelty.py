"""摘要只保存新增内容；确定性去重不把相似度当成同义证明。"""
import re
import json
import time


def canonical(text):
    # 仅忽略排版，不删除数字、否定词、人名或运算符。
    return re.sub(r'\s+', '', str(text)).rstrip('。')


def novelty(summary, previous):
    text = str(summary or '').strip()
    if not text:
        return 'empty', ''
    key = canonical(text)
    for row in previous:
        meta = row.get('metadata') or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        change = meta.get('memory_change') or {}
        if change.get('valid_to') is not None and float(change['valid_to']) <= time.time():
            continue
        old = canonical(row['content'])
        # 包含关系不能证明同义：新句可能是旧句中的否定、引用或被纠正内容。
        if key == old:
            return 'duplicate', row['hash']
    return 'new', ''


def record(chat, text, decision, old_hash='', old_text=''):
    from heart_shared.storage import AuditStore
    AuditStore().append('摘要去重判断', chat, status={
        'empty': '无新增事实，未写入', 'duplicate': '内容已存在，跳过重复',
        'new': '存在新增内容，交给原生写入',
    }[decision], new_memory=text, memory_ids=[old_hash] if old_hash else [], old_memories=[old_text] if old_text else [],
        reason='历史摘要只用于识别已记内容；排版归一化比较不等于任意同义判断')


def save_cursor(chat, count):
    from heart_shared.storage import AuditStore
    with AuditStore().transaction() as db:
        db.execute('CREATE TABLE IF NOT EXISTS heart_summary_cursor(chat TEXT PRIMARY KEY, count INTEGER NOT NULL)')
        db.execute('INSERT INTO heart_summary_cursor VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET count=MAX(count,excluded.count)', (chat, count))


def load_cursor(chat):
    from heart_shared.storage import AuditStore
    with AuditStore().connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS heart_summary_cursor(chat TEXT PRIMARY KEY, count INTEGER NOT NULL)')
        row = db.execute('SELECT count FROM heart_summary_cursor WHERE chat=?', (chat,)).fetchone()
        return int(row[0]) if row else 0
