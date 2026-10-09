"""SQLite 保存审计事件，按会话和日期导出日志；两个插件共用同一个写锁。"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .readable import render_event


RETENTION_DAYS = 3  # 保留今天与前三个自然日；更早的整日记录自动删除。
LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(?:群聊|私聊|未知会话|后台)_.+_[0-9a-f]{10}\.txt$")
LEGACY_LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")
TEMP_LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(?:群聊|私聊|未知会话|后台)_.+_[0-9a-f]{10})?\.tmp$")
CARD_TEMP_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_(?:群聊|私聊|未知会话|后台)_.+_[0-9a-f]{10}\.[0-9a-f]{32}\.tmp$")


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
                CREATE INDEX IF NOT EXISTS events_session_day ON events(day,session,seq);
                CREATE INDEX IF NOT EXISTS messages_received ON messages(received);
                CREATE TABLE IF NOT EXISTS sessions(
                    session TEXT PRIMARY KEY, chat_type TEXT NOT NULL, title TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_starts(session TEXT PRIMARY KEY, started TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_files(
                    day TEXT NOT NULL, session TEXT NOT NULL, filename TEXT NOT NULL,
                    PRIMARY KEY(day,session));
                CREATE TABLE IF NOT EXISTS session_sources(
                    session TEXT PRIMARY KEY, platform TEXT NOT NULL, user_id TEXT NOT NULL,
                    channel TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS log_exports(
                    day TEXT NOT NULL, session TEXT NOT NULL, filename TEXT NOT NULL,
                    last_seq INTEGER NOT NULL, byte_size INTEGER NOT NULL,
                    PRIMARY KEY(day,session));
                CREATE TABLE IF NOT EXISTS emotions(session TEXT PRIMARY KEY, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS avatar_views(session TEXT PRIMARY KEY, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS card_pending(
                    day TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(day,session));
                CREATE TABLE IF NOT EXISTS card_exports(
                    day TEXT NOT NULL, session TEXT NOT NULL, filename TEXT NOT NULL,
                    last_seq INTEGER NOT NULL, format_version INTEGER NOT NULL, byte_size INTEGER NOT NULL,
                    PRIMARY KEY(day,session));
                CREATE TABLE IF NOT EXISTS daily_overviews(
                    day TEXT PRIMARY KEY, filename TEXT NOT NULL, last_seq INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS daily_totals(
                    day TEXT NOT NULL, session TEXT NOT NULL, counts TEXT NOT NULL,
                    last_seq INTEGER NOT NULL, info TEXT NOT NULL, PRIMARY KEY(day,session));
                CREATE TABLE IF NOT EXISTS audit_migrations(name TEXT PRIMARY KEY);
            """)
            # 一次性导入仍保留的旧事件；此后每条新事件只更新小计，不重复扫描全库。
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO session_starts SELECT session,MIN(received) FROM messages GROUP BY session")
            db.execute("INSERT OR IGNORE INTO session_starts SELECT session,MIN(time) FROM events GROUP BY session")
            if not db.execute("SELECT 1 FROM audit_migrations WHERE name='daily_totals_v1'").fetchone():
                from .card_writer import CardWriter
                from .cards import summary_counts
                writer = CardWriter(self)
                for row in db.execute("SELECT DISTINCT day,session FROM events").fetchall():
                    events = db.execute("SELECT seq,kind,payload FROM events WHERE day=? AND session=? ORDER BY seq", row).fetchall()
                    counts = summary_counts([dict(e, data=json.loads(e['payload'])) for e in events])
                    info = writer.info(db, row['session'])
                    db.execute("INSERT OR REPLACE INTO daily_totals VALUES(?,?,?,?,?)",
                               (row['day'], row['session'], dumps(dict(counts)), events[-1]['seq'], dumps(info)))
                db.execute("INSERT INTO audit_migrations VALUES('daily_totals_v1')")

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
            # 原始凭证提交后仅唤醒后台展示线程，不在回复链路渲染卡片。
            from .card_writer import wake_writer
            wake_writer(self)
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
            "platform": str(info.get("platform") or user.get("platform") or message.get("platform") or "").lower(),
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
        db.execute("INSERT OR IGNORE INTO session_starts VALUES(?,?)",
                   (fields['session'], self.clock().isoformat(timespec='milliseconds')))
        platform, user_id = fields["platform"], fields["user"]
        if platform:
            channel = ("Live2D" if platform == "webui" and (
                       user_id in {"vtuber_local_user", "webui_user_vtuber_local_user"}
                       or user_id.startswith(("vtuber_visit_", "webui_user_vtuber_visit_")))
                       else "QQ" if platform == "qq" else "其他")
            db.execute("""INSERT INTO session_sources VALUES(?,?,?,?) ON CONFLICT(session)
                DO UPDATE SET platform=excluded.platform,user_id=excluded.user_id,channel=excluded.channel""",
                       (fields["session"], platform, user_id, channel))
        return fields

    def emotion_snapshot(self, db, session):
        row = db.execute("SELECT detail FROM emotions WHERE session=?", (session,)).fetchone()
        return json.loads(row[0]) if row else {}

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
            relation = "仅会话关联，不代表最近发言者触发；未提供触发消息，不关联最近发言者"
            # 后台检索/摘要常在另一条聊天后才完成，最近消息不是它的证据。
            rows = []
        mood = self.mood_snapshot(db, session)
        if len(ids) == 1:
            source_mood = db.execute("SELECT detail FROM mood_processed WHERE session=? AND message_id=?", (session, str(ids[0]))).fetchone()
            if source_mood:
                mood = json.loads(source_mood[0])
        # 会话名称只用于辨认聊天流，不把最近发言者误认成此次后台操作的触发者。
        latest = db.execute("SELECT name FROM messages WHERE session=? ORDER BY rowid DESC LIMIT 1", (session,)).fetchone()
        body = {"relation": relation, "dialogue": rows, "session_name": latest[0] if latest else "",
                "mood": mood, "emotion": self.emotion_snapshot(db, session), **payload}
        cursor = db.execute("INSERT OR IGNORE INTO events(event_id,day,time,session,kind,payload) VALUES(?,?,?,?,?,?)",
                            (event_id, now.date().isoformat(), now.isoformat(timespec="milliseconds"), session, kind, dumps(body)))
        if cursor.rowcount:
            from .card_writer import CardWriter
            from .cards import summary_counts
            day = now.date().isoformat()
            old = db.execute("SELECT counts FROM daily_totals WHERE day=? AND session=?", (day, session)).fetchone()
            counts = Counter(json.loads(old[0]) if old else {})
            counts.update(summary_counts([{'kind':kind, 'data':body}]))
            db.execute("INSERT OR REPLACE INTO daily_totals VALUES(?,?,?,?,?)",
                       (day, session, dumps(dict(counts)), cursor.lastrowid, dumps(CardWriter(self).info(db, session))))
        db.execute("INSERT OR IGNORE INTO card_pending VALUES(?,?)", (now.date().isoformat(), session))
        if kind == '后台记忆管理' or (kind == '记忆操作结果' and payload.get('operation') in {
                'ingest_text', 'ingest_summary', 'memory_delete_admin', 'memory_fact_admin', 'memory_correction_admin'}):
            # 当前状态改变后，仍保留的历史卡片也需更新旁注，不改变其历史检索结果。
            db.execute('INSERT OR IGNORE INTO card_pending SELECT day,session FROM card_exports WHERE session=?', (session,))
        # 午夜之后到达的结果仍补回原消息所在日期，不挪到最近一条消息里。
        for row in rows:
            db.execute("INSERT OR IGNORE INTO card_pending VALUES(?,?)", (row["received"][:10], session))
        task = payload.get('task_id') or payload.get('audit_task_id')
        if task:
            first = db.execute("""SELECT MIN(day) FROM events WHERE session=? AND
                (CAST(json_extract(payload,'$.task_id') AS TEXT)=? OR
                 CAST(json_extract(payload,'$.audit_task_id') AS TEXT)=?)""",
                               (session, str(task), str(task))).fetchone()[0]
            if first:
                db.execute("INSERT OR IGNORE INTO card_pending VALUES(?,?)", (first, session))
        # 写库和生成文本在同一数据库写锁内，跨插件/跨进程不会互相覆盖。
        self.export_session_day(db, now.date().isoformat(), session, incremental=True)
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

    def export_session_day(self, db, day, session, incremental=False):
        """常规写入只导出新事件；重建、改名或上次中断时重新生成完整文本。

        写入仍受SQLite跨进程写锁保护。导出游标与事务一起提交：如果文本写完后
        数据库回滚，下次文件长度与已提交游标不符，就会重建，避免重复或漏记。
        """
        logs = self.root / "logs"
        logs.mkdir(exist_ok=True)
        origin = db.execute("SELECT channel FROM session_sources WHERE session=?", (session,)).fetchone()
        folder = origin[0] if origin and origin[0] in {"QQ", "Live2D"} else "其他"
        filename = folder + "/" + self.session_filename(db, day, session)
        target = logs / filename
        target.parent.mkdir(exist_ok=True)
        cursor = db.execute("SELECT * FROM log_exports WHERE day=? AND session=?", (day, session)).fetchone()
        append_ok = (incremental and cursor and cursor['filename'] == filename
                     and target.is_file() and target.stat().st_size == cursor['byte_size'])
        last_seq = cursor['last_seq'] if append_ok else 0
        events = db.execute("SELECT * FROM events WHERE day=? AND session=? AND seq>? ORDER BY seq",
                            (day, session, last_seq)).fetchall()
        if not events:
            return
        blocks = [render_event(event, json.loads(event["payload"])) for event in events]
        if append_ok:
            # 每条记录之间仍空一行；BOM只在完整文件开头写入一次。
            with target.open('ab') as stream:
                stream.write(('\n' + '\n\n'.join(blocks) + '\n').encode('utf-8'))
        else:
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(('\n\n'.join(blocks) + '\n').encode('utf-8-sig'))
            temporary.replace(target)
        db.execute("INSERT OR REPLACE INTO log_exports VALUES(?,?,?,?,?)",
                   (day, session, filename, events[-1]['seq'], target.stat().st_size))
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
        # 仅允许自己管理的一级目录和文件名，数据库里的路径也不能越界。
        parts = str(name).replace("\\", "/").split("/")
        return (len(parts) == 1 or len(parts) == 2 and parts[0] in {"QQ", "Live2D", "其他"}) and bool(
            LOG_NAME.fullmatch(parts[-1]) or LEGACY_LOG_NAME.fullmatch(parts[-1]))

    def prune_in(self, db, today):
        cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
        db.execute("DELETE FROM events WHERE day<?", (cutoff,))
        db.execute("""DELETE FROM mood_processed WHERE EXISTS(
            SELECT 1 FROM messages WHERE messages.session=mood_processed.session
            AND messages.message_id=mood_processed.message_id AND messages.received<?)""", (cutoff + "T",))
        db.execute("DELETE FROM messages WHERE received<?", (cutoff + "T",))
        db.execute("DELETE FROM session_files WHERE day<?", (cutoff,))
        db.execute("DELETE FROM log_exports WHERE day<?", (cutoff,))
        db.execute("DELETE FROM card_pending WHERE day<?", (cutoff,))
        db.execute("DELETE FROM card_exports WHERE day<?", (cutoff,))
        logs = self.root / "logs"
        if logs.is_dir():
            paths = list(logs.iterdir())
            for folder in ("QQ", "Live2D", "其他"):
                directory = logs / folder
                if directory.is_dir() and not directory.is_symlink():
                    paths.extend(directory.iterdir())
            for path in paths:
                match = ((LOG_NAME.fullmatch(path.name) or LEGACY_LOG_NAME.fullmatch(path.name)
                          or TEMP_LOG_NAME.fullmatch(path.name)) if path.is_file() else None)
                if match and match[1] < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        logging.getLogger(__name__).exception("Heart 旧日志删除失败：%s", path)
        # 展示卡片与明细采用同一保留期；每日概览目录刻意不在此清理范围内。
        cards = self.root / "消息卡片"
        for folder in ("QQ", "Live2D", "其他"):
            directory = cards / folder
            if cards.is_symlink() or not directory.is_dir() or directory.is_symlink():
                continue
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    continue
                match = LOG_NAME.fullmatch(path.name) or CARD_TEMP_NAME.fullmatch(path.name)
                if match and match[1] < cutoff:
                    try:
                        path.unlink()
                    except OSError:
                        logging.getLogger(__name__).exception("Heart 过期消息卡片清理失败：%s", path)

    def prune(self):
        """供插件启动及定时任务调用；不会删除原生长期记忆或心情当前值。"""
        with self.transaction() as db:
            self.prune_in(db, self.clock().date())

    def start_card_logs(self):
        """仅在插件启动时调用；先生成前日概览，再执行原有历史清理。"""
        from .card_writer import start_writer
        writer = start_writer(self)
        try:
            writer.startup()
        except Exception:
            # 展示失败不能阻断记忆/心情插件加载；错误必须在控制台明确出现。
            logging.getLogger(__name__).exception("Heart卡片启动恢复或每日概览生成失败；原插件继续加载")

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
