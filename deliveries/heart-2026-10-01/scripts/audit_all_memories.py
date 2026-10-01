"""只读导出全部有效正文；用于逐条核对，不将报告上传到仓库。"""
import json
import sqlite3
import sys
from pathlib import Path

from heart_shared.memory_scope import metadata, visible, tokens

root = Path.cwd()
db = sqlite3.connect(f'file:{root / "data/a-memorix/metadata/metadata.db"}?mode=ro',uri=True)
db.row_factory = sqlite3.Row
host = sqlite3.connect(f'file:{root / "data/MaiBot.db"}?mode=ro',uri=True)
sessions = {row[0]:row[1] or row[2] or '未命名会话' for row in host.execute('SELECT session_id,group_name,user_nickname FROM chat_sessions')}
index = 0
for row in db.execute('SELECT * FROM paragraphs WHERE is_deleted=0 ORDER BY source,created_at'):
    item = dict(row)
    meta = metadata(item)
    chats = tokens(meta,'chat_id','chat_ids')
    if item['source'].startswith('chat_summary:'):
        chats.add(item['source'].split(':',1)[1])
    if len(chats) != 1:
        print('来源待核实',item['hash'],item['source'])
        continue
    chat = next(iter(chats))
    if not visible(item,chat):
        continue
    index += 1
    print(json.dumps({'index':index,'hash':item['hash'],'source':item['source'],'chat':chat,
        'session_name':sessions.get(chat,'未知会话'),'people':sorted(tokens(meta,'person_id','person_ids')),
        'text':item['content']},ensure_ascii=False))
print('有效正文总数',index)
for row in db.execute("SELECT scope_type,scope_id,fact_key,value_text,status FROM fact_claims WHERE status IN ('active','conflicted') ORDER BY scope_id,value_text"):
    print('事实账本',json.dumps(dict(row),ensure_ascii=False))
print('情景数量',db.execute('SELECT count(*) FROM episodes').fetchone()[0])
print('画像数量',db.execute('SELECT count(DISTINCT person_id) FROM person_profile_snapshots').fetchone()[0])
events = sqlite3.connect(f'file:{root / "data/heart_observation/events.sqlite3"}?mode=ro',uri=True)
print('候选状态', events.execute('SELECT status,count(*) FROM heart_candidates GROUP BY status').fetchall())
for cid,chat,payload in events.execute("SELECT id,chat,payload FROM heart_candidates WHERE status='pending'"):
    data = json.loads(payload)
    print('候选',cid,sessions.get(chat,'未知会话'), data['owner']['person_id'],data['args']['text'])
for row in db.execute('SELECT episode_id,source,summary FROM episodes ORDER BY source'):
    print('情景',json.dumps(dict(row),ensure_ascii=False))
