"""主持人裁决是唯一写入世界状态的入口；玩家只提交提案。"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from .room_features import RoomFeatures


actor_name = ContextVar("storyroom_actor", default="主持人")


class RoomError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class RoomStore(RoomFeatures):
    def __init__(self, root: Path, story_dir: Path, clock=time.time, roll=secrets.randbelow):
        self.root, self.clock, self.roll = Path(root), clock, roll
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stories = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in story_dir.glob("*.json")}
        from .authoring import validate_story
        self.stories = {key: validate_story(value) for key, value in self.stories.items()}
        for story in self.stories.values():
            if story["start"] not in story["nodes"]:
                raise ValueError("剧本起点不存在")
            for node in story["nodes"].values():
                if node.get("ending") and node["next"]:
                    raise ValueError("结局不能定义新的主线跳转")
                if any(e["id"] not in story["nodes"] for e in node["next"]):
                    raise ValueError("剧本含超出有限世界线的跳转")
                if any(f not in story["flags"] for e in node["next"] for f in e.get("requires", [])):
                    raise ValueError("剧本条件使用了未定义的标记")
        existing = self.root / "rooms.sqlite3"
        if existing.exists():
            # SQLite backup API 包含 WAL 中的已提交事务，不能只复制主数据库文件。
            source = sqlite3.connect(existing)
            try:
                old_columns = {r[1] for r in source.execute("PRAGMA table_info(rooms)")}
                has_talk = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_talk'").fetchone()
                if 'script' not in old_columns or not has_talk:
                    backups = self.root / "备份"
                    backups.mkdir(exist_ok=True)
                    target = sqlite3.connect(backups / (("升级0.2前-" if 'script' not in old_columns else '升级0.3前-') + uuid.uuid4().hex[:8] + ".sqlite3"))
                    try:
                        source.backup(target)
                    finally:
                        target.close()
            finally:
                source.close()
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS rooms(id TEXT PRIMARY KEY,name TEXT,story TEXT,node TEXT,
              flags TEXT,version INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS roles(id TEXT PRIMARY KEY,room TEXT,name TEXT,key_hash TEXT UNIQUE,
              avatar TEXT,visual TEXT,trust INTEGER DEFAULT 0,last_seen REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,room TEXT,role TEXT,node TEXT,version INTEGER,
              question TEXT,status TEXT,lease_hash TEXT,lease_until REAL DEFAULT 0,attempt INTEGER DEFAULT 0,result TEXT);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,room TEXT,
              at TEXT,kind TEXT,actor TEXT,detail TEXT);
            CREATE INDEX IF NOT EXISTS task_queue ON tasks(role,status);
            CREATE TABLE IF NOT EXISTS experiences(id TEXT PRIMARY KEY,room TEXT,role TEXT,task TEXT UNIQUE,
              node TEXT,text TEXT,active INTEGER DEFAULT 1,created REAL);
            CREATE TABLE IF NOT EXISTS checks(task TEXT PRIMARY KEY,skill TEXT,dc INTEGER,modifier INTEGER,
              roll INTEGER,total INTEGER,success INTEGER);
            CREATE TABLE IF NOT EXISTS hosts(id TEXT PRIMARY KEY,room TEXT,name TEXT,key_hash TEXT UNIQUE);
            """)
            # 加列而不重建旧表，既有房间、玩家密钥与记录全部保留。
            for table, column, definition in (
                ("rooms", "script", "TEXT"), ("rooms", "script_hash", "TEXT"),
                ("roles", "card", "TEXT DEFAULT '{}'"), ("roles", "mood", "INTEGER DEFAULT 50"),
                ("tasks", "context", "TEXT")):
                if column not in {r["name"] for r in db.execute("PRAGMA table_info(" + table + ")")}:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
            for old in db.execute("SELECT id,story FROM rooms WHERE script IS NULL").fetchall():
                if old["story"] not in self.stories:
                    raise ValueError("旧房间剧本文件缺失，请恢复剧本后再启动")
                script = dump(self.stories[old["story"]])
                db.execute("UPDATE rooms SET script=?,script_hash=? WHERE id=?", (script, digest(script), old["id"]))
                self.event(db, old["id"], "剧本版本锁定", "平台", {
                    "版本": digest(script), "说明": "旧版没有保存剧本副本，本次锁定当前文件，无法还原更早文件版本"})
            self.init_features(db)

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.root / "rooms.sqlite3", timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def event(self, db, room: str, kind: str, actor: str, detail: Dict):
        if actor == "主持人":
            actor = actor_name.get()
        at = datetime.fromtimestamp(self.clock(), timezone.utc).astimezone().isoformat(timespec="seconds")
        return db.execute("INSERT INTO events(room,at,kind,actor,detail) VALUES(?,?,?,?,?)",
                          (room, at, kind, actor, dump(detail))).lastrowid

    def export(self, room: str):
        """SQLite 是审计事实来源；可读日志按房间、日期导出，不记录访问密钥。"""
        with self.db() as db:
            info = self.room(db, room)
            rows = db.execute("SELECT * FROM events WHERE room=? ORDER BY seq", (room,)).fetchall()
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", info["name"])[:35]
            directory = self.root / "日志" / (name + "_" + room[:6])
            directory.mkdir(parents=True, exist_ok=True)
            days = {}
            for row in rows:
                details = json.loads(row["detail"])
                text = "\n".join(f"{k}：{v}" for k, v in details.items())
                days.setdefault(row["at"][:10], []).append(
                    "------------------------------------------------------------------------\n"
                    f"时间：{row['at']}｜{row['kind']}\n人物：{row['actor']}\n{text}\n"
                    "------------------------------------------------------------------------\n")
            for day, blocks in days.items():
                target = directory / (day + ".txt")
                temporary = target.with_suffix(".tmp")
                temporary.write_text("\n".join(blocks), encoding="utf-8-sig")
                temporary.replace(target)

    def room(self, db, room: str):
        result = db.execute("SELECT * FROM rooms WHERE id=?", (room,)).fetchone()
        if result is None:
            raise RoomError("房间不存在", 404)
        return dict(result)

    def role(self, token: str):
        with self.db() as db:
            result = db.execute("SELECT * FROM roles WHERE key_hash=?", (digest(token),)).fetchone()
        if result is None:
            raise RoomError("玩家连接密钥无效", 401)
        return dict(result)

    def cohost(self, token: str):
        with self.db() as db:
            row = db.execute("SELECT id,room,name FROM hosts WHERE key_hash=?", (digest(token),)).fetchone()
        return dict(row) if row else None

    def invite_host(self, room: str, name: str):
        key, identifier = secrets.token_urlsafe(32), uuid.uuid4().hex
        with self.db() as db:
            self.room(db, room)
            db.execute("INSERT INTO hosts VALUES(?,?,?,?)", (identifier, room, name, digest(key)))
            self.event(db, room, "邀请共同主持人", "主持人", {"名字": name, "权限": "仅此房间，可裁决；不能创建其他房间或邀请主持人"})
        self.export(room)
        return {"id": identifier, "name": name, "token": key}

    def story(self, state: Dict):
        return json.loads(state["script"])

    def create(self, name: str, names, avatars, story_id="lighthouse"):
        templates = {s['id']: s['script'] for s in self.templates()}
        if story_id not in templates:
            raise RoomError("剧本不存在")
        room, credentials = uuid.uuid4().hex, []
        with self.db() as db:
            script = dump(templates[story_id])
            db.execute("INSERT INTO rooms(id,name,story,node,flags,version,created,script,script_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                       (room, name, story_id, templates[story_id]["start"], "[]", 1, self.clock(), script, digest(script)))
            db.execute('INSERT INTO room_talk(room) VALUES(?)', (room,))
            for role_name, avatar in zip(names, avatars):
                key, role = secrets.token_urlsafe(32), uuid.uuid4().hex
                db.execute("INSERT INTO roles(id,room,name,key_hash,avatar,visual) VALUES(?,?,?,?,?,?)",
                           (role, room, role_name, digest(key), avatar,
                            dump({"emotion": "平静", "revision": uuid.uuid4().hex})))
                credentials.append({"id": role, "name": role_name, "token": key})
            self.event(db, room, "世界线开始", "主持人", {"房间": name, "剧本": templates[story_id]["title"],
                       "剧本版本": digest(script), "说明": "房间已锁定剧本副本；玩家行动需主持人裁决，机器人不能自行开启新主线"})
        self.export(room)
        return {"room_id": room, "connections": credentials}

    def list_rooms(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,name,story,node,created FROM rooms ORDER BY created DESC")]

    def snapshot(self, room: str):
        with self.db() as db:
            state = self.room(db, room)
            story = self.story(state)
            node = story["nodes"][state["node"]]
            roles = [dict(r) for r in db.execute(
                "SELECT id,name,avatar,visual,trust,last_seen,card,mood FROM roles WHERE room=? ORDER BY rowid", (room,))]
            for r in roles:
                r["visual"] = json.loads(r["visual"])
                r["card"] = json.loads(r["card"])
                r["connected"] = r["last_seen"] > 0 and self.clock() - r["last_seen"] < 45
            events = [dict(r) for r in db.execute(
                "SELECT seq,at,kind,actor,detail FROM events WHERE room=? ORDER BY seq DESC LIMIT 150", (room,))]
            for event in events:
                event["detail"] = json.loads(event["detail"])
            tasks = [dict(r) for r in db.execute(
                "SELECT id,role,node,version,question,status,attempt,result,autonomous FROM tasks WHERE room=? ORDER BY rowid DESC LIMIT 50", (room,))]
            talk = self.talk_view(db, room)
            for t in tasks:
                t["result"] = json.loads(t["result"]) if t["result"] else None
                t['held'] = bool(talk['host_speaking'] and t['status'] in ('proposed', 'discussed'))
                if t['held']:
                    t['result'] = None
                check = db.execute("SELECT * FROM checks WHERE task=?", (t["id"],)).fetchone()
                t["check"] = dict(check) if check else None
            experiences = [dict(r) for r in db.execute("SELECT * FROM experiences WHERE room=? ORDER BY created DESC LIMIT 100", (room,))]
            hosts = [dict(r) for r in db.execute("SELECT id,name FROM hosts WHERE room=?", (room,))]
            public_state = {k: v for k, v in state.items() if k != "script"}
            return {**public_state, "flags": json.loads(state["flags"]), "world": story["world"],
                    "story_title": story["title"], "stage": node, "roles": roles,
                    "events": events, "tasks": tasks, "experiences": experiences, "hosts": hosts, "allowed_flags": story["flags"],
                    'talk': talk, 'room_memory': self.memory_view(db, room)}

    def narrate(self, room: str, text: str):
        with self.db() as db:
            state = self.room(db, room)
            if self.story(state)["nodes"][state["node"]].get("ending"):
                raise RoomError("世界线已经结束，不能在原房间自动开启新故事", 409)
            db.execute("UPDATE rooms SET version=version+1 WHERE id=?", (room,))
            db.execute('UPDATE room_talk SET opened=0,remaining=0 WHERE room=?', (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            seq = self.event(db, room, "主持人叙事", "主持人", {"内容": text, "说明": "旧的待执行任务已取消，避免依据过时现场行动"})
            self.remember_narration(db, room, seq, text)
        self.export(room)

    def advance(self, room: str, target: str):
        with self.db() as db:
            state = self.room(db, room)
            story = self.story(state)
            edge = next((e for e in story["nodes"][state["node"]]["next"] if e["id"] == target), None)
            if edge is None:
                raise RoomError("不允许跳出当前阶段定义的分支", 409)
            if not set(edge.get("requires", [])).issubset(json.loads(state["flags"])):
                raise RoomError("尚未满足该分支的条件", 409)
            db.execute("UPDATE rooms SET node=?,version=version+1 WHERE id=?", (target, room))
            db.execute('UPDATE room_talk SET opened=0,remaining=0 WHERE room=?', (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            self.event(db, room, "世界线推进", "主持人", {
                "原阶段": story["nodes"][state["node"]]["title"], "新阶段": story["nodes"][target]["title"],
                "当前看法": story["nodes"][target]["outlook"]})
        self.export(room)

    def request_turn(self, room: str, role: str, question: str):
        with self.db() as db:
            state = self.room(db, room)
            owner = db.execute("SELECT name FROM roles WHERE id=? AND room=?", (role, room)).fetchone()
            if owner is None:
                raise RoomError("玩家不属于本房间", 404)
            if self.story(state)["nodes"][state["node"]].get("ending"):
                raise RoomError("故事已经结束", 409)
            if db.execute("SELECT 1 FROM tasks WHERE role=? AND status IN ('queued','running','proposed')", (role,)).fetchone():
                raise RoomError("该玩家已有待完成或待裁决任务，请先处理", 409)
            task = uuid.uuid4().hex
            db.execute("INSERT INTO tasks(id,room,role,node,version,question,status,queued_at) VALUES(?,?,?,?,?,?,'queued',?)",
                       (task, room, role, state["node"], state["version"], question, self.clock()))
            self.event(db, room, "请求玩家行动", "主持人", {"任务": task, "玩家": owner["name"], "问题": question, "状态": "排队等待，尚未调用模型"})
        self.export(room)
        return {"task_id": task}

    def claim(self, owner: Dict):
        room, role = owner["room"], owner["id"]
        claimed = None
        changed = False
        with self.db() as db:
            db.execute("UPDATE roles SET last_seen=? WHERE id=?", (self.clock(), role))
            unavailable = db.execute("SELECT id FROM tasks WHERE room=? AND status='queued' AND queued_at>0 AND queued_at<?", (room, self.clock()-120)).fetchall()
            for old in unavailable:
                db.execute("UPDATE tasks SET status='failed' WHERE id=?", (old['id'],))
                db.execute('UPDATE room_talk SET opened=0 WHERE room=?', (room,))
                self.event(db, room, '玩家连接等待超时', '平台', {'任务': old['id'], '说明': '120秒未领取，暂停自主讨论；请检查对应PC连接再继续'})
                changed = True
            expired = db.execute("SELECT t.id,t.attempt,r.name FROM tasks t JOIN roles r ON r.id=t.role WHERE t.room=? AND t.status='running' AND t.lease_until<?",
                                 (room, self.clock())).fetchall()
            for old in expired:
                status = "failed" if old["attempt"] >= 3 else "queued"
                db.execute("UPDATE tasks SET status=?,lease_hash=NULL,queued_at=? WHERE id=?", (status, self.clock(), old["id"]))
                self.event(db, room, "玩家任务等待超时", old['name'], {
                    "次数": old["attempt"], "处理": "停止自动重试，请主持人重新邀请" if status == "failed" else "重新排队",
                    "说明": "没有收到有效玩家结果，没有改变世界线"})
                changed = True
            # 即使未被点名也轮询旁听；只有房间空闲时才排下一位自主思考。
            self.schedule(db, room)
            task = db.execute("SELECT * FROM tasks WHERE role=? AND status='queued' ORDER BY rowid LIMIT 1", (role,)).fetchone()
            if task is not None:
                task = dict(task)
                state = self.room(db, room)
                if task["node"] != state["node"] or task["version"] != state["version"]:
                    db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
                    self.event(db, room, "旧玩家任务已取消", owner["name"], {"原因": "现场已变化，不再调用模型处理旧问题"})
                else:
                    talk = self.talk_view(db, room)
                    active = db.execute("SELECT 1 FROM tasks WHERE room=? AND status IN ('running','proposed') AND id<>?", (room, task['id'])).fetchone()
                    first = db.execute("SELECT id FROM tasks WHERE room=? AND status='queued' ORDER BY rowid LIMIT 1", (room,)).fetchone()
                    if not owner.get('demo') and (talk['host_speaking'] or talk['audio_playing'] or (task['autonomous'] and talk['ready_at'] > self.clock()) or active or first['id'] != task['id']):
                        return None
                    lease = secrets.token_urlsafe(24)
                    db.execute("UPDATE tasks SET status='running',lease_hash=?,lease_until=?,attempt=attempt+1 WHERE id=?",
                               (digest(lease), self.clock() + 120, task["id"]))
                    self.event(db, room, "玩家开始思考", owner["name"], {"任务": task["id"], "插件": "heart.storyroom-pc", "插件开启": not owner.get("demo", False), "功能": "离线演示" if owner.get("demo") else '自主思考与发言' if task['autonomous'] else "角色行动生成", "问题": task["question"], "状态": "处理中"})
                    claimed = (task, lease)
                changed = True
        if changed:
            self.export(room)
        if claimed is None:
            return None
        task, lease = claimed
        view = self.snapshot(room)
        if view["version"] != task["version"]:
            raise RoomError("领取期间现场变化，本次不调用模型", 409)
        history, used = [], 0
        # 先按事件性质筛选，再取最近记录；大量舞台回执不能挤掉最新现场叙事。
        with self.db() as db:
            narrative = [dict(r) for r in db.execute(
                "SELECT seq,at,kind,actor,detail FROM events WHERE room=? AND kind IN ('主持人叙事','世界线推进','世界线开始','主持人裁决') ORDER BY seq DESC LIMIT 40", (room,))]
        for item in narrative:
            item["detail"] = json.loads(item["detail"])
            if item['kind'] == '主持人叙事':
                with self.db() as db:
                    note = db.execute('SELECT text,active FROM room_notes WHERE room=? AND event_seq=?', (room, item['seq'])).fetchone()
                if note and not note['active']:
                    continue
                if note:
                    item['detail']['内容'] = note['text']
            if item["kind"] == "主持人裁决" and item["detail"].get("结果") != "通过":
                continue
            if item["kind"] == "主持人裁决" and item["detail"].get("任务"):
                with self.db() as db:
                    memory = db.execute("SELECT active,text FROM experiences WHERE task=?", (item["detail"]["任务"],)).fetchone()
                if memory and not memory["active"]:
                    continue
                if memory:
                    item = {**item, "detail": {**item["detail"], "裁决": memory["text"]}}
            size = len(dump(item).encode("utf-8"))
            if used + size > 48000 or len(history) >= 20:
                break
            history.append(item)
            used += size
        # 只提供本房间的公共故事事实，不读取 MaiBot/QQ/私聊的数据库。
        with self.db() as db:
            person = dict(db.execute("SELECT card,mood,trust FROM roles WHERE id=?", (role,)).fetchone())
            # 同阶段优先，其次最近经历；仅注入本角色已确认且未撤销的经历。
            memories = [dict(r) for r in db.execute(
                "SELECT id,node,text FROM experiences WHERE room=? AND role=? AND active=1 ORDER BY (node=?) DESC,created DESC LIMIT 8",
                (room, role, task["node"]))]
        context = {"question": task["question"], "character_name": owner["name"],
                   "world": view["world"], "stage": view["stage"], "flags": view["flags"],
                   "trust": person["trust"], "mood": person["mood"], "card": json.loads(person["card"]),
                   "memories": memories, "history": list(reversed(history)), "script_hash": view["script_hash"]}
        with self.db() as db:
            if self.room(db, room)["version"] != task["version"]:
                raise RoomError("准备上下文期间现场变化，本次不调用模型", 409)
            context.update(self.extra_context(db, room, task['question']))
            context['autonomous'] = bool(task.get('autonomous'))
            db.execute("UPDATE tasks SET context=? WHERE id=?", (dump(context), task["id"]))
            self.event(db, room, "本次上下文与回忆", owner["name"], {"任务": task["id"], "资料": context,
                       "说明": "已确认故事与隔离的跑团记忆用于回忆；玩家谈话单独标注，提案不是事实；不读取QQ或普通Live2D记忆"})
        self.export(room)
        return {"id": task["id"], "lease": lease, "protocol": "0.3", "version": task["version"], **context}

    def complete(self, owner: Dict, task_id: str, lease: str, result: Dict, failed=False):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND role=?", (task_id, owner["id"])).fetchone()
            if row is None:
                raise RoomError("任务不属于该玩家", 404)
            task = dict(row)
            if not task["lease_hash"] or not secrets.compare_digest(task["lease_hash"], digest(lease)):
                raise RoomError("任务租约无效", 403)
            if task["status"] in ("proposed", "discussed", "failed", "approved", "rejected"):
                if task["result"] == dump(result):
                    return {"ok": True, "status": task["status"], "duplicate": True}
                raise RoomError("不能覆盖已经提交的结果", 409)
            state = self.room(db, owner["room"])
            if (task["status"] != "running" or task["lease_until"] < self.clock()
                    or task["node"] != state["node"] or task["version"] != state["version"]):
                raise RoomError("任务已经过时或取消，结果未写入世界线", 409)
            intent = result.get('intent', 'action')
            status = 'failed' if failed else 'discussed' if intent == 'discussion' else 'proposed'
            db.execute("UPDATE tasks SET status=?,result=? WHERE id=?", (status, dump(result), task_id))
            if not failed:
                db.execute('UPDATE room_talk SET ready_at=? WHERE room=?', (self.clock() + max(3, min(15, len(result.get('speech', '')) / 8)), owner['room']))
            if failed or intent != 'discussion':
                db.execute('UPDATE room_talk SET opened=0 WHERE room=?', (owner['room'],))
            self.event(db, owner["room"], "玩家调用失败" if failed else '玩家讨论' if intent == 'discussion' else '玩家提问' if intent == 'question' else "玩家提交提案", owner["name"],
                       {"任务": task_id, "插件": "heart.storyroom-pc", "插件开启": not owner.get("demo", False), "功能": "离线演示" if owner.get("demo") else '讨论发言' if intent == 'discussion' else '向主持人提问' if intent == 'question' else "行动提案", "发言": result.get("speech", ""), "行动": result.get("action", ""),
                        "展示情绪": result.get("emotion", "平静"), '意图': intent,
                        "说明": "讨论仅进入对话；提问或行动等待主持人回应，尚未改变世界状态"})
            if not failed:
                db.execute("UPDATE roles SET visual=? WHERE id=?",
                           (dump({"emotion": result["emotion"], "revision": uuid.uuid4().hex}), owner["id"]))
        self.export(owner["room"])
        return {"ok": True, "status": status}

    def decide(self, room: str, task_id: str, approve: bool, ruling: str, flags, trust_delta: int, mood_delta: int = 0):
        with self.db() as db:
            state = self.room(db, room)
            task = db.execute("SELECT * FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()
            if task is None or task["status"] != "proposed":
                raise RoomError("该提案不存在或已裁决", 409)
            if task["node"] != state["node"] or task["version"] != state["version"]:
                raise RoomError("提案依据的世界状态已变化，请退回并重新邀请行动", 409)
            if not set(flags).issubset(self.story(state)["flags"]):
                raise RoomError("裁决包含世界线之外的标记", 409)
            check = db.execute("SELECT * FROM checks WHERE task=?", (task_id,)).fetchone()
            if approve and check and check["roll"] is None:
                raise RoomError("已指定检定，请先实际投骰再裁决", 409)
            person = db.execute("SELECT name,trust,mood FROM roles WHERE id=?", (task["role"],)).fetchone()
            new_trust = person["trust"]
            new_mood = person["mood"]
            if approve:
                active = sorted(set(json.loads(state["flags"])) | set(flags))
                db.execute("UPDATE rooms SET flags=?,version=version+1 WHERE id=?", (dump(active), room))
                new_trust = max(-5, min(5, person["trust"] + trust_delta))
                db.execute("UPDATE roles SET trust=? WHERE id=?", (new_trust, task["role"]))
                new_mood = max(0, min(100, person["mood"] + mood_delta))
                emotion = "开心" if new_mood >= 65 else "难过" if new_mood <= 35 else "平静"
                db.execute("UPDATE roles SET mood=?,visual=? WHERE id=?", (new_mood,
                    dump({"emotion": emotion, "revision": uuid.uuid4().hex}), task["role"]))
                db.execute("INSERT INTO experiences VALUES(?,?,?,?,?,?,1,?)",
                           (uuid.uuid4().hex, room, task["role"], task_id, task["node"], ruling, self.clock()))
                # 失败行动也可通过裁决：记录的是实际后果，而不是模型设想的成功。
            db.execute("UPDATE tasks SET status=? WHERE id=?", ("approved" if approve else "rejected", task_id))
            if task['autonomous']:
                db.execute('UPDATE room_talk SET opened=1,remaining=limit_turns,ready_at=? WHERE room=?', (self.clock() + 1, room))
            self.event(db, room, "主持人裁决", "主持人", {"任务": task_id, "玩家": person["name"], "结果": "通过" if approve else "退回",
                       "裁决": ruling, "已确认标记": flags if approve else [],
                       "关系信任变化": f"{person['trust']} → {new_trust}（实际变化 {new_trust - person['trust']:+d}）",
                       "心情变化": f"{person['mood']} → {new_mood}", "实际检定": dict(check) if check else None,
                       "说明": "只有通过裁决的行动与裁决内容是故事事实"})
        self.export(room)
        return {"ok": True}

    def set_card(self, room: str, role: str, card: Dict):
        with self.db() as db:
            self.room(db, room)
            person = db.execute("SELECT name FROM roles WHERE id=? AND room=?", (role, room)).fetchone()
            if person is None:
                raise RoomError("角色不存在", 404)
            if db.execute("SELECT 1 FROM tasks WHERE role=? AND status IN ('queued','running','proposed')", (role,)).fetchone():
                raise RoomError("请先处理这个角色的任务再修改角色卡", 409)
            db.execute("UPDATE roles SET card=? WHERE id=?", (dump(card), role))
            db.execute("UPDATE rooms SET version=version+1 WHERE id=?", (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            self.event(db, room, "角色卡更新", "主持人", {"玩家": person["name"], "角色卡": card})
        self.export(room)

    def define_check(self, room: str, task_id: str, skill: str, dc: int):
        with self.db() as db:
            state = self.room(db, room)
            task = db.execute("SELECT * FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()
            if task is None or task["status"] != "proposed" or task["version"] != state["version"]:
                raise RoomError("只能为当前现场的待裁决行动指定检定", 409)
            if db.execute("SELECT 1 FROM checks WHERE task=?", (task_id,)).fetchone():
                raise RoomError("检定已锁定，不能修改难度或重复指定", 409)
            person = db.execute("SELECT card,name FROM roles WHERE id=?", (task["role"],)).fetchone()
            modifier = json.loads(person["card"]).get("skills", {}).get(skill, 0)
            db.execute("INSERT INTO checks(task,skill,dc,modifier) VALUES(?,?,?,?)", (task_id, skill, dc, modifier))
            self.event(db, room, "指定行动检定", "主持人", {"任务": task_id, "技能": skill, "难度": dc, "角色加值": modifier})
        self.export(room)

    def roll_check(self, room: str, task_id: str):
        with self.db() as db:
            state = self.room(db, room)
            task = db.execute("SELECT * FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()
            check = db.execute("SELECT * FROM checks WHERE task=?", (task_id,)).fetchone()
            if not task or task["status"] != "proposed" or task["version"] != state["version"] or not check:
                raise RoomError("检定不存在或行动已经过时", 409)
            if check["roll"] is not None:
                return {**dict(check), "duplicate": True}
            value = self.roll(20) + 1
            total = value + check["modifier"]
            db.execute("UPDATE checks SET roll=?,total=?,success=? WHERE task=?", (value, total, int(total >= check["dc"]), task_id))
            result = dict(db.execute("SELECT * FROM checks WHERE task=?", (task_id,)).fetchone())
            self.event(db, room, "行动实际投骰", "平台", {"任务": task_id, "检定": result,
                       "说明": "此行动只投一次；自然1/20不自动改变成功条件；后果由主持人裁决"})
        self.export(room)
        return result

    def revise_experience(self, room: str, experience: str, text: str, active: bool):
        with self.db() as db:
            old = db.execute("SELECT * FROM experiences WHERE id=? AND room=?", (experience, room)).fetchone()
            if old is None:
                raise RoomError("经历不存在", 404)
            db.execute("UPDATE experiences SET text=?,active=? WHERE id=?", (text, int(active), experience))
            db.execute("UPDATE rooms SET version=version+1 WHERE id=?", (room,))
            db.execute("UPDATE tasks SET status='cancelled' WHERE room=? AND status IN ('queued','running')", (room,))
            self.event(db, room, "经历修正或撤销", "主持人", {"经历": experience, "旧内容": old["text"], "新内容": text,
                       "进入回忆": active, "说明": "修改回忆不自动回滚已经裁定的世界标记，旧任务不能依据过时现场提交"})
        self.export(room)

    def dice(self, room: str, actor: str, expression: str, dc: Optional[int]):
        match = re.fullmatch(r"(\d{1,2})d(\d{1,3})([+-]\d{1,3})?", expression.lower().replace(" ", ""))
        if not match:
            raise RoomError("骰式应类似 1d20、2d6+3")
        count, sides, modifier = int(match[1]), int(match[2]), int(match[3] or 0)
        if not 1 <= count <= 20 or not 2 <= sides <= 100 or abs(modifier) > 100:
            raise RoomError("限制为1～20颗、2～100面、加值绝对值不超过100")
        values = [self.roll(sides) + 1 for _ in range(count)]
        total = sum(values) + modifier
        detail = {"骰式": expression, "每颗点数": values, "加值": modifier, "总点数": total,
                  "难度": dc, "检定": "未设置难度" if dc is None else "成功" if total >= dc else "失败",
                  "说明": "由平台实际投骰；故事后果仍由主持人裁决"}
        with self.db() as db:
            self.room(db, room)
            self.event(db, room, "投骰检定", actor, detail)
        self.export(room)
        return detail

    def reject_stale(self, room: str, task_id: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND room=?", (task_id, room)).fetchone()
            if row is None or row["status"] not in ("queued", "running", "proposed", "failed"):
                raise RoomError("任务不存在或已裁决", 409)
            db.execute("UPDATE tasks SET status='rejected' WHERE id=?", (task_id,))
            self.event(db, room, "任务退回", "主持人", {"原因": "主持人取消／状态已变化，可重新邀请玩家行动"})
        self.export(room)
