"""SQLite 保存事实事件，按天导出人类可读日志；两个插件共用同一个写锁。"""

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .readable import render_event


def clean(value):
    """不记录已知密钥字段；普通对话仍是私密明文，不能公开上传。"""
    if isinstance(value, dict):
        return {str(k): ("[已隐藏]" if re.search(r"(?i)token|password|secret|api.?key|authorization", str(k))
                         else clean(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._-]+", r"\1[已隐藏]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[已隐藏的密钥]", value)
        return value[:12000] + ("…[超过12000字符，已截断]" if len(value) > 12000 else "")
    if value is None or isinstance(value, (bool, float, int)):
        return value
    return str(value)


def dumps(value):
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False)


def default_root():
    # 安装器将 heart_shared 放在 MaiBot 根目录；不依赖当前终端目录。
    return Path(__file__).resolve().parents[1] / "data" / "heart_observation"


class ClosingConnection(sqlite3.Connection):
    """标准 sqlite 上下文只提交事务；这里额外关闭连接，避免长时间运行泄漏。"""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class AuditStore:
    def __init__(self, root=None, clock=None):
        self.root = Path(root) if root else default_root()
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages(
                    session TEXT NOT NULL, message_id TEXT NOT NULL, user TEXT,
                    name TEXT, text TEXT, received TEXT,
                    PRIMARY KEY(session,message_id));
                CREATE TABLE IF NOT EXISTS moods(
                    session TEXT PRIMARY KEY, value REAL NOT NULL, updated REAL NOT NULL,
                    detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mood_processed(
                    session TEXT NOT NULL, message_id TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY(session,message_id));
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                    day TEXT NOT NULL, time TEXT NOT NULL, session TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_day ON events(day,seq);
            """)

    def connect(self):
        db = sqlite3.connect(self.root / "events.sqlite3", timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def message_fields(message):
        info = message.get("message_info", {})
        user = info.get("user_info", {})
        group = info.get("group_info") or {}
        return {
            "session": str(message.get("session_id") or ""),
            "message_id": str(message.get("message_id") or ""),
            "user": str(user.get("user_id") or ""),
            "name": (f"{group['group_name']} / " if group.get("group_name") else "")
                    + str(user.get("user_nickname") or "未命名人物"),
            "text": str(message.get("processed_plain_text") or "[非文本消息/尚未转写]"),
        }

    def save_message(self, db, message):
        fields = self.message_fields(message)
        if not fields["session"] or not fields["message_id"]:
            return fields
        db.execute("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?)", (
            fields["session"], fields["message_id"], fields["user"], fields["name"],
            clean(fields["text"]), self.clock().isoformat(timespec="milliseconds")))
        return fields

    def mood_snapshot(self, db, session):
        row = db.execute("SELECT detail FROM moods WHERE session=?", (session,)).fetchone()
        return json.loads(row[0]) if row else {"status": "心情插件未处理此会话", "value": None}

    def append(self, kind, session="", **payload):
        with self.transaction() as db:
            return self.append_in(db, kind, session, payload)

    def append_in(self, db, kind, session, payload):
        now = self.clock()
        event_id = str(payload.pop("event_id", "") or uuid.uuid4().hex)
        ids = payload.get("evidence_message_ids") or []
        if payload.get("message_id"):
            ids = [payload["message_id"]]
        rows = []
        # 有证据 ID 时严格按当前会话匹配，绝不拿别人的最近消息充数。
        for message_id in ids:
            row = db.execute("SELECT * FROM messages WHERE session=? AND message_id=?", (session, str(message_id))).fetchone()
            if row:
                rows.append(dict(row))
        if ids:
            relation = "证据消息ID关联" if len(rows) == len(ids) else "部分或全部证据未观测到；不猜测原文"
        else:
            relation = "仅会话关联；下面是最近上下文，不代表该事件由它触发"
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM messages WHERE session=? ORDER BY rowid DESC LIMIT 3", (session,)).fetchall()][::-1]
        mood = self.mood_snapshot(db, session)
        if len(ids) == 1:
            source_mood = db.execute("SELECT detail FROM mood_processed WHERE session=? AND message_id=?", (session, str(ids[0]))).fetchone()
            if source_mood:
                mood = json.loads(source_mood[0])
        body = {"relation": relation, "dialogue": rows, "mood": mood, **payload}
        db.execute("INSERT OR IGNORE INTO events(event_id,day,time,session,kind,payload) VALUES(?,?,?,?,?,?)",
                   (event_id, now.date().isoformat(), now.isoformat(timespec="milliseconds"), session, kind, dumps(body)))
        # 写库和生成文本在同一数据库写锁内，跨插件/跨进程不会互相覆盖。
        self.export_day(db, now.date().isoformat())
        return event_id

    def record_message(self, message):
        with self.transaction() as db:
            fields = self.save_message(db, message)
            return self.append_in(db, "收到对话", fields["session"], {
                "event_id": f"incoming:{fields['session']}:{fields['message_id']}" if fields["message_id"] else "",
                "message_id": fields["message_id"], "text": fields["text"],
                "memory_status": "此时尚未观测到本条消息的记忆结果；后台结果会另记，不等于未使用记忆",
            })

    def export_day(self, db, day):
        blocks = [
            render_event(event, json.loads(event["payload"]))
            for event in db.execute("SELECT * FROM events WHERE day=? ORDER BY seq", (day,))
        ]
        target = self.root / "logs" / f"{day}.txt"
        target.parent.mkdir(exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")
        temporary.replace(target)

    def rebuild(self):
        """崩溃或手动编辑导出文本后，可由数据库重建；数据库才是原始凭证。"""
        with self.transaction() as db:
            for row in db.execute("SELECT DISTINCT day FROM events").fetchall():
                self.export_day(db, row[0])
