"""异步卡片展示层：原始事件先落库，后台线程随后生成可重建的文本。"""

from datetime import date, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Dict, Tuple
import hashlib
import json
import logging
import uuid

from .cards import FORMAT_VERSION, render_cards, render_overview
from .recall_snapshot import annotate, read_catalog


LOGGER = logging.getLogger(__name__)
_WRITERS: Dict[Path, "CardWriter"] = {}
_LOCK = Lock()


def wake_writer(store) -> None:
    """收到事件只发一个唤醒信号；不在聊天链路中渲染卡片或等待其他任务。"""
    with _LOCK:
        writer = _WRITERS.get(store.root.resolve())
    if writer is not None:
        writer.wake.set()


def start_writer(store) -> "CardWriter":
    with _LOCK:
        root = store.root.resolve()
        if root not in _WRITERS:
            _WRITERS[root] = CardWriter(store)
            _WRITERS[root].thread.start()
        writer = _WRITERS[root]
    writer.wake.set()
    return writer


class CardWriter:
    def __init__(self, store):
        self.store = store
        self.wake = Event()
        self.stop = Event()
        self.thread = Thread(target=self.run, name="Heart消息卡片", daemon=True)

    def run(self) -> None:
        while not self.stop.is_set():
            self.wake.wait(2)
            self.wake.clear()
            # 合并短时间内的多个更新；不是延迟回复，只延迟展示文件刷新。
            if self.stop.wait(0.15):
                break
            try:
                self.flush()
            except Exception:
                LOGGER.exception("Heart消息卡片导出失败；原始事件仍在数据库，下轮会重试")

    def close(self) -> None:
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=5)

    def info(self, db, session: str) -> Dict[str, str]:
        row = db.execute("SELECT chat_type,title FROM sessions WHERE session=?", (session,)).fetchone()
        source = db.execute("SELECT channel FROM session_sources WHERE session=?", (session,)).fetchone()
        first = db.execute("SELECT started FROM session_starts WHERE session=?", (session,)).fetchone()
        started = first[0] if first else None
        if not started:
            started = db.execute("SELECT MIN(received) FROM messages WHERE session=?", (session,)).fetchone()[0]
        if not started:
            started = db.execute("SELECT MIN(time) FROM events WHERE session=?", (session,)).fetchone()[0]
        return {"title": row["title"] if row else "未关联会话",
                "chat_type": row["chat_type"] if row else "未知会话" if session else "后台",
                "channel": source[0] if source and source[0] in {"QQ", "Live2D"} else "其他",
                "started": str(started or "未记录")[:19].replace("T", " ")}

    def snapshot(self, day: str, session: str) -> Tuple[Dict[str, Any], list, dict, int]:
        with self.store.connect() as db:
            db.execute("BEGIN")  # 所有查询来自同一份已提交快照，避免中途混入其他进程的更新。
            info = self.info(db, session)
            rows = db.execute("SELECT * FROM events WHERE session=? ORDER BY seq", (session,)).fetchall()
            events = [dict(row, data=json.loads(row["payload"])) for row in rows]
            messages = {row["message_id"]: dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE session=?", (session,))}
            revision = max((row["seq"] for row in rows), default=0)
        annotate(events, read_catalog(self.store.root.parent / 'a-memorix/metadata/metadata.db'), session)
        return info, events, messages, revision

    def filename(self, day: str, session: str, info: Dict[str, str]) -> str:
        suffix = hashlib.sha256(session.encode("utf-8")).hexdigest()[:10]
        start = self.store.safe_title(info["started"].replace(":", "-"))
        title = self.store.safe_title(info["title"])
        return f"{info['channel']}/{day}_{info['chat_type']}_{title}_会话开始{start}_{suffix}.txt"

    def export(self, day: str, session: str) -> None:
        info, events, messages, revision = self.snapshot(day, session)
        if not events:
            with self.store.transaction() as db:
                db.execute("DELETE FROM card_pending WHERE day=? AND session=?", (day, session))
            return
        # 最耗时的归组和排版在写锁之外；机器人只需短暂持有 SQLite 写锁。
        body = render_cards(day, info, events, messages).encode("utf-8-sig")
        filename = self.filename(day, session, info)
        directory = self.store.root / "消息卡片"
        target = directory / filename
        with self.store.transaction() as db:
            cutoff = (self.store.clock().date() - timedelta(days=3)).isoformat()
            if day < cutoff:
                db.execute("DELETE FROM card_pending WHERE day=? AND session=?", (day, session))
                return
            previous = db.execute("SELECT * FROM card_exports WHERE day=? AND session=?", (day, session)).fetchone()
            if previous and previous["last_seq"] > revision:
                return  # 其他进程已写入较新的快照，禁止旧结果覆盖新卡片。
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(body)
            temporary.replace(target)  # 用户不会看到只写到一半的文件。
            db.execute("INSERT OR REPLACE INTO card_exports VALUES(?,?,?,?,?,?)",
                       (day, session, filename, revision, FORMAT_VERSION, len(body)))
            if previous and previous["filename"] != filename and self.store.managed_log_name(previous["filename"]):
                old = directory / previous["filename"]
                if not old.is_symlink():
                    old.unlink(missing_ok=True)
            current = db.execute("SELECT MAX(seq) FROM events WHERE session=?", (session,)).fetchone()[0] or 0
            if current == revision:
                db.execute("DELETE FROM card_pending WHERE day=? AND session=?", (day, session))
            # 若渲染期间有新事件提交，保留待更新标记，下一轮继续补齐。

    def flush(self, limit: int = 200) -> int:
        with self.store.connect() as db:
            pending = db.execute("SELECT day,session FROM card_pending ORDER BY day,session LIMIT ?", (limit,)).fetchall()
        for row in pending:
            self.export(row["day"], row["session"])
        return len(pending)

    def startup(self) -> None:
        """每次插件启动补生成历史概览、恢复待更新任务；不新增任何模型请求。"""
        self.overviews()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO card_pending SELECT DISTINCT day,session FROM events")
            # 迟到结果可能属于昨日收到的消息；历史原消息卡片也需要恢复。
            db.execute("INSERT OR IGNORE INTO card_pending SELECT DISTINCT substr(received,1,10),session FROM messages")
        self.wake.set()

    def overviews(self) -> None:
        today = self.store.clock().date()
        with self.store.connect() as db:
            first = db.execute("SELECT MIN(day) FROM daily_totals").fetchone()[0]
        if not first:
            return
        day = date.fromisoformat(first)
        while day < today:
            self.overview(day.isoformat())
            day += timedelta(days=1)

    def overview(self, day: str) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT * FROM daily_totals WHERE day=? ORDER BY session", (day,)).fetchall()
            sessions = [dict(json.loads(row['info']), counts=json.loads(row['counts'])) for row in rows]
            revision = max((row["last_seq"] for row in rows), default=0)
        text = render_overview(day, self.store.clock().isoformat(timespec="seconds"), sessions)
        filename = day + "_每日概览.txt"
        target = self.store.root / "每日概览" / filename
        with self.store.transaction() as db:
            saved = db.execute("SELECT last_seq,byte_size FROM daily_overviews WHERE day=?", (day,)).fetchone()
            if saved and saved[0] >= revision and target.is_file() and target.stat().st_size == saved[1]:
                return  # 多个插件/进程重复启动不会重复追加或改写已完成的概览。
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(text.encode("utf-8-sig"))
            temporary.replace(target)
            db.execute("INSERT OR REPLACE INTO daily_overviews VALUES(?,?,?,?)",
                       (day, filename, revision, target.stat().st_size))


def flush_cards(store) -> int:
    """供离线测试/修复工具显式等待展示层，真实聊天流程不调用此函数。"""
    writer = CardWriter(store)
    total = 0
    while True:
        count = writer.flush()
        total += count
        if not count:
            return total
