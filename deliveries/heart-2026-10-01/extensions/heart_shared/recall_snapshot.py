"""为历史检索快照附加当前状态；仅作用于卡片展示，不修改记忆或原事件。"""

from pathlib import Path
from typing import Any, Dict, List, Optional
import logging
import sqlite3
import time

from .memory_scope import metadata, visible


def read_catalog(path: Path) -> Optional[List[Dict[str, Any]]]:
    """只读正文索引；不存在的数据库不创建，核对失败明确显示未核实。"""
    if not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return [dict(row) for row in db.execute(
            'SELECT hash,content,source,metadata,is_deleted,expires_at FROM paragraphs')]
    except sqlite3.Error:
        logging.getLogger(__name__).exception('Heart历史检索当前状态只读核对失败；不推断记忆已删除')
        return None
    finally:
        if db is not None:
            db.close()


def annotate(events: List[Dict[str, Any]], catalog: Optional[List[Dict[str, Any]]], session: str) -> None:
    """有明确清理凭证或原生失效标记才折叠，不做模糊/向量/LLM同义猜测。"""
    cleaned = set()
    for event in events:
        data = event['data']
        if (event.get('session', session) == session and event['kind'] == '后台记忆管理'
                and data.get('status') == '已删除核对后的重复或无事实正文'):
            cleaned.update(text for text in data.get('old_memories', []) if isinstance(text, str))
    by_id = {row['hash']: row for row in catalog or []}
    effective_texts = {row['content'] for row in catalog or [] if visible(row, session)}
    for event in events:
        data = event['data']
        if event['kind'] == '群聊记忆检索':
            hits = data.get('hits') or []
        elif event['kind'] == '记忆操作结果' and data.get('operation') in {'search_memory', 'heart_scoped_list'}:
            hits = (data.get('outcome') or {}).get('hits') or []
        else:
            continue
        states = []
        for hit in hits:
            text = hit.get('content', '') if isinstance(hit, dict) else str(hit)
            row = by_id.get(hit.get('hash') or hit.get('id')) if isinstance(hit, dict) else None
            if catalog is None:
                state = {'label':'当前状态未核实（原生库暂不可读）', 'fold':False}
            elif row and visible(row, session):
                state = {'label':'当前仍有效且属于本会话可见范围', 'fold':False}
            elif row and retired(row):
                state = {'label':'原生库已删除或已失效', 'fold':True}
            elif text in effective_texts:
                state = {'label':'当前存在同正文的有效记忆（不保证仍是原命中编号）', 'fold':False}
            elif text in cleaned:
                state = {'label':'已清理，且有本会话的精确正文清理凭证', 'fold':True}
            elif row:
                state = {'label':'当前不属于本会话可见范围；不推断删除原因', 'fold':False}
            else:
                state = {'label':'当前正文库未找到对应条目；原因未确认', 'fold':False}
            states.append(state)
        # 加的是导出时的临时信息，不将它回写审计事件，以免篡改过去的状态。
        data['_card_recall_view'] = {'available':catalog is not None, 'states':states}


def retired(row: Dict[str, Any]) -> bool:
    change = metadata(row).get('memory_change') or {}
    if row.get('is_deleted') or change.get('change_type') == 'mark_superseded':
        return True
    for value in (row.get('expires_at'), change.get('valid_to')):
        if isinstance(value, (int, float)) and value <= time.time():
            return True
    return False
