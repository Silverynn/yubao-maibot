"""SQLite 保存审计事件，按会话和日期导出日志；两个插件共用同一个写锁。"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .readable import render_event


RETENTION_DAYS = 3  # 保留今天与前三个自然日；更早的整日记录自动删除。
LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(?:群聊|私聊|未知会话|后台)_.+_[0-9a-f]{10}\.txt$")
LEGACY_LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")
TEMP_LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(?:群聊|私聊|未知会话|后台)_.+_[0-9a-f]{10})?\.tmp$")


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
                CREATE INDEX IF NOT EXISTS messages_received ON messages(received);
                CREATE TABLE IF NOT EXISTS sessions(
                    session TEXT PRIMARY KEY, chat_type TEXT NOT NULL, title TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_files(
                    day TEXT NOT NULL, session TEXT NOT NULL, filename TEXT NOT NULL,
                    PRIMARY KEY(day,session));
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
        group_info = info.get("group_info")
        group = group_info or {}
        is_group = group_info is not None
        title = str((group.get("group_name") or group.get("group_id") or "未命名群聊") if is_group else
                    (user.get("user_nickname") or user.get("user_id") or "未命名用户"))
        return {
            "session": str(message.get("session_id") or ""),
            "message_id": str(message.get("message_id") or ""),
            "user": str(user.get("user_id") or ""),
            "name": (f"{group['group_name']} / " if group.get("group_name") else "")
                    + str(user.get("user_nickname") or "未命名人物"),
            "text": str(message.get("processed_plain_text") or "[非文本消息/尚未转写]"),
            "chat_type": "群聊" if is_group else "私聊",
            "title": title,
        }

    def save_message(self, db, message):
        fields = self.message_fields(message)
        if not fields["session"] or not fields["message_id"]:
            return fields
        db.execute("""INSERT INTO sessions(session,chat_type,title) VALUES(?,?,?)
            ON CONFLICT(session) DO UPDATE SET chat_type=excluded.chat_type,title=excluded.title""",
            (fields["session"], fields["chat_type"], fields["title"]))
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
        # 会话名称只用于辨认聊天流，不把最近发言者误认成此次后台操作的触发者。
        latest = db.execute("SELECT name FROM messages WHERE session=? ORDER BY rowid DESC LIMIT 1", (session,)).fetchone()
        body = {"relation": relation, "dialogue": rows, "session_name": latest[0] if latest else "",
                "mood": mood, **payload}
        db.execute("INSERT OR IGNORE INTO events(event_id,day,time,session,kind,payload) VALUES(?,?,?,?,?,?)",
                   (event_id, now.date().isoformat(), now.isoformat(timespec="milliseconds"), session, kind, dumps(body)))
        # 写库和生成文本在同一数据库写锁内，跨插件/跨进程不会互相覆盖。
        self.export_session_day(db, now.date().isoformat(), session)
        self.prune_in(db, now.date())
        return event_id

    def record_message(self, message):
        with self.transaction() as db:
            fields = self.save_message(db, message)
            return self.append_in(db, "收到对话", fields["session"], {
                "event_id": f"incoming:{fields['session']}:{fields['message_id']}" if fields["message_id"] else "",
                "message_id": fields["message_id"], "text": fields["text"],
                "memory_status": "此时尚未观测到本条消息的记忆结果；后台结果会另记，不等于未使用记忆",
            })

    @staticmethod
    def safe_title(title):
        """文件名可读，但不能含 Windows 非法字符、路径分隔符或末尾空格/点。"""
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title or "").strip())
        value = re.sub(r"\s+", " ", value).strip(" .")[:40].rstrip(" .")
        return value or "未命名"

    def session_filename(self, db, day, session):
        if session:
            row = db.execute("SELECT chat_type,title FROM sessions WHERE session=?", (session,)).fetchone()
            if row is None:
                # 升级前的 messages 只有“群名 / 发言人”；只用于旧日志迁移。
                old = db.execute("SELECT name FROM messages WHERE session=? ORDER BY rowid DESC LIMIT 1",
                                 (session,)).fetchone()
                old_name = str(old[0] or "") if old else ""
                chat_type = "群聊" if " / " in old_name else "私聊" if old_name else "未知会话"
                title = old_name.rsplit(" / ", 1)[0] if chat_type == "群聊" else old_name or "未命名"
                db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?)", (session, chat_type, title))
            else:
                chat_type, title = row[0], row[1]
        else:
            chat_type, title = "后台", "未关联会话"
        short_id = hashlib.sha256(session.encode("utf-8")).hexdigest()[:10]
        return f"{day}_{chat_type}_{self.safe_title(title)}_{short_id}.txt"

    def export_session_day(self, db, day, session):
        events = db.execute("SELECT * FROM events WHERE day=? AND session=? ORDER BY seq", (day, session)).fetchall()
        if not events:
            return
        blocks = [render_event(event, json.loads(event["payload"])) for event in events]
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        filename = self.session_filename(db, day, session)
        target = logs / filename
        temporary = target.with_suffix(".tmp")
        temporary.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")
        temporary.replace(target)
        previous = db.execute("SELECT filename FROM session_files WHERE day=? AND session=?", (day, session)).fetchone()
        db.execute("""INSERT INTO session_files(day,session,filename) VALUES(?,?,?)
            ON CONFLICT(day,session) DO UPDATE SET filename=excluded.filename""", (day, session, filename))
        if previous and previous[0] != filename and self.managed_log_name(previous[0]):
            try:
                (logs / previous[0]).unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).exception("Heart 旧会话文件更名清理失败：%s", previous[0])

    def export_day(self, db, day):
        for row in db.execute("SELECT DISTINCT session FROM events WHERE day=?", (day,)).fetchall():
            self.export_session_day(db, day, row[0])

    @staticmethod
    def managed_log_name(name):
        return bool(LOG_NAME.fullmatch(name) or LEGACY_LOG_NAME.fullmatch(name))

    def prune_in(self, db, today):
        cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute("DELETE FROM events WHERE day<?", (cutoff,))
        db.execute("""DELETE FROM mood_processed WHERE EXISTS(
            SELECT 1 FROM messages WHERE messages.session=mood_processed.session
            AND messages.message_id=mood_processed.message_id AND messages.received<?)""", (cutoff + "T",))
        db.execute("DELETE FROM messages WHERE received<?", (cutoff + "T",))
        db.execute("DELETE FROM session_files WHERE day<?", (cutoff,))
        logs = self.root / "logs"
        if logs.is_dir():
            for path in logs.iterdir():
                match = ((LOG_NAME.fullmatch(path.name) or LEGACY_LOG_NAME.fullmatch(path.name)
                          or TEMP_LOG_NAME.fullmatch(path.name)) if path.is_file() else None)
                if match and match[1] < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        logging.getLogger(__name__).exception("Heart 旧日志删除失败：%s", path)

    def prune(self):
        """供插件启动及定时任务调用；不会删除原生长期记忆或心情当前值。"""
        with self.transaction() as db:
            self.prune_in(db, self.clock().date())

    async def retention_loop(self):
        """即使整天无人聊天，也至少每小时检查一次到期日志。"""
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(self.prune)
            except Exception:
                logging.getLogger(__name__).exception("Heart 定时日志清理失败；下次继续重试")

    def rebuild(self):
        """崩溃或手动编辑导出文本后，可由数据库重建；数据库才是原始凭证。"""
        with self.transaction() as db:
            self.prune_in(db, self.clock().date())
            for row in db.execute("SELECT DISTINCT day FROM events").fetchall():
                self.export_day(db, row[0])
                # 仅在该日全部会话文件成功导出后移除旧版混合文件。
                try:
                    (self.root / "logs" / f"{row[0]}.txt").unlink(missing_ok=True)
                except OSError:
                    logging.getLogger(__name__).exception("Heart 旧版混合日志清理失败：%s", row[0])
