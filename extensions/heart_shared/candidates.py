"""自动人物事实的待审核区；只有本人私聊确认后才交给原生记忆。"""

import asyncio
import hashlib
import json
import time
from collections import defaultdict


class CandidateInbox:
    def __init__(self, store, backend):
        self.store = store
        self.backend = backend
        self.locks = defaultdict(asyncio.Lock)
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS heart_candidates(
                id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL, chat TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, payload TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS heart_candidates_owner ON heart_candidates(owner,status,id)")

    async def capture(self, args, limit=20):
        """原生AI已经提取事实；此处只改写入时机，不重复调用模型。"""
        owner = await self.backend.owner(args)
        if not owner:
            self.store.append("候选记忆", args.get("chat_id", ""), status="暂缓",
                              reason="无法核实原发言者，既不进入候选也不写入长期记忆",
                              new_memory=args.get("text", ""))
            return {"success": False, "detail": "无法核实原发言者，未进入候选记忆"}
        signature = hashlib.sha256(json.dumps([
            owner["person_id"], args["text"], (args.get("metadata") or {}).get("evidence_message_ids") or []
        ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.store.transaction() as db:
            existing = db.execute("SELECT id,status FROM heart_candidates WHERE signature=?", (signature,)).fetchone()
            if existing:
                return {"success": False, "detail": f"候选记忆 #{existing['id']} 已处理或等待确认；未重复写入"}
            count = db.execute("SELECT count(*) FROM heart_candidates WHERE owner=? AND status='pending'",
                               (owner["person_id"],)).fetchone()[0]
            if count >= limit:
                return {"success": False, "detail": "本人候选记忆已满，未写入；请先私聊处理候选"}
            payload = {"args": args, "owner": owner}
            cursor = db.execute("INSERT INTO heart_candidates(signature,owner,chat,status,created,payload) VALUES(?,?,?,?,?,?)",
                                (signature, owner["person_id"], args["chat_id"], "pending", time.time(),
                                 json.dumps(payload, ensure_ascii=False)))
            candidate_id = cursor.lastrowid
            self.store.append_in(db, "候选记忆", args["chat_id"], {
                "candidate_id": candidate_id, "status": "待本人确认", "new_memory": args["text"],
                "reason": "原生AI从本人原话提取；尚未写入长期记忆",
                "evidence_message_ids": (args.get("metadata") or {}).get("evidence_message_ids") or [],
            })
        return {"success": False, "detail": f"已进入候选记忆 #{candidate_id}；尚未写入长期记忆"}

    def list_for(self, owner):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,payload FROM heart_candidates WHERE owner=? AND status='pending' ORDER BY id DESC LIMIT 10",
                (owner,)).fetchall()]

    async def resolve(self, owner, candidate_id, approve):
        async with self.locks[candidate_id]:
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM heart_candidates WHERE id=? AND owner=?",
                                 (candidate_id, owner)).fetchone()
            if not row:
                return "没有找到属于你的这条候选记忆。"
            if row["status"] != "pending":
                return "这条候选记忆已处理，不能重复操作。"
            payload = json.loads(row["payload"])
            args = payload["args"]
            if not approve:
                status, message = "ignored", "已忽略，未写入长期记忆。"
            else:
                # 主进程会再次走原生写入与冲突关卡；候选确认不代替冲突确认。
                approved = {**args, "metadata": {**(args.get("metadata") or {}),
                    "fact_claim": {"trust": "manual_confirmed", "authority": "manual",
                                   "stability": "stable", "profile_section": "stable_facts"}}}
                result = await self.backend.write_approved_candidate(approved)
                if result.get("success") and result.get("stored_ids"):
                    status, message = "written", "原生记忆服务报告写入成功。"
                elif result.get("success") and result.get("skipped_ids"):
                    status, message = "skipped", "原生记忆服务报告内容已存在，本次未新增。"
                elif self.backend.conflict_pending(args["chat_id"], result):
                    status, message = "conflict_pending", "与旧记忆可能冲突，已另行私聊确认；此时尚未写入。"
                else:
                    self.store.append("候选记忆", args["chat_id"], candidate_id=candidate_id, status="写入未完成",
                                      new_memory=args["text"], reason=str(result.get("detail") or "原生服务未确认写入"))
                    return "原生记忆未确认写入，候选仍保留；请查看日志后重试。"
            with self.store.transaction() as db:
                db.execute("UPDATE heart_candidates SET status=? WHERE id=? AND owner=? AND status='pending'",
                           (status, candidate_id, owner))
                self.store.append_in(db, "候选记忆", args["chat_id"], {
                    "candidate_id": candidate_id, "status": status, "new_memory": args["text"],
                    "reason": message, "evidence_message_ids": (args.get("metadata") or {}).get("evidence_message_ids") or [],
                })
            return message
