"""只读核对消息卡片与每日概览，不启动机器人、不打印私人对话、不自动修复。"""

from pathlib import Path
from types import SimpleNamespace
import argparse
import json
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from heart_shared.card_writer import CardWriter
from heart_shared.cards import render_cards, render_overview
from heart_shared.recall_snapshot import annotate, read_catalog


def inspect(root: Path) -> int:
    db = sqlite3.connect((root / 'events.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    reader = CardWriter(SimpleNamespace(root=root))
    errors = 0
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        print('数据库完整性：', db.execute('PRAGMA quick_check').fetchone()[0])
        exports = db.execute('SELECT * FROM card_exports ORDER BY day,session').fetchall()
        catalog = read_catalog(root.parent / 'a-memorix/metadata/metadata.db')
        for row in exports:
            file = root / '消息卡片' / row['filename']
            events = [dict(e, data=json.loads(e['payload'])) for e in db.execute(
                'SELECT * FROM events WHERE session=? ORDER BY seq', (row['session'],))]
            messages = {m['message_id']: dict(m) for m in db.execute('SELECT * FROM messages WHERE session=?', (row['session'],))}
            info = reader.info(db, row['session'])
            annotate(events, catalog, row['session'])
            expected = render_cards(row['day'], info, events, messages)
            if not file.is_file() or file.read_text(encoding='utf-8-sig') != expected or file.stat().st_size != row['byte_size']:
                errors += 1
                print('卡片文件不存在或与原始事件不符：', row['filename'])
        overviews = db.execute('SELECT * FROM daily_overviews ORDER BY day').fetchall()
        for row in overviews:
            file = root / '每日概览' / row['filename']
            if not file.is_file():
                errors += 1
                print('概览文件不存在：', row['filename'])
                continue
            text = file.read_text(encoding='utf-8-sig')
            generated = next((line.removeprefix('生成时间：') for line in text.splitlines() if line.startswith('生成时间：')), '')
            sessions = [dict(json.loads(r['info']), counts=json.loads(r['counts'])) for r in db.execute(
                'SELECT * FROM daily_totals WHERE day=? ORDER BY session', (row['day'],))]
            if text != render_overview(row['day'], generated, sessions) or file.stat().st_size != row['byte_size']:
                errors += 1
                print('概览统计或正文不符（可能待下次启动更新）：', row['filename'])
        pending = db.execute('SELECT COUNT(*) FROM card_pending').fetchone()[0]
        print(json.dumps({'消息卡片文件':len(exports), '每日概览文件':len(overviews),
                          '差异文件':errors, '尚待后台补齐的文件':pending}, ensure_ascii=False))
        print('未打印聊天正文，也未修改任何文件或数据库。')
        return 1 if errors else 0
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT / 'data/heart_observation')
    args = parser.parse_args()
    raise SystemExit(inspect(args.root))
