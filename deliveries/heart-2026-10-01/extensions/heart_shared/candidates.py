"""人物事实和会话摘要的候选区；摘要仅允许管理员审核，正式写入复用原生接口。"""

import asyncio
import hashlib
import json
import time
from collections import defaultdict

from .conflicts import normalized_fact_text


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

    async def capture(self, args, limit=20, reason="原生AI从本人原话提取；尚未写入长期记忆"):
        """原生AI已经提取事实；此处只改写入时机，不重复调用模型。"""
        owner = await self.backend.owner(args)
        if not owner:
            self.store.append("候选记忆", args.get("chat_id", ""), status="暂缓",
                              reason="无法核实原发言者，既不进入候选也不写入长期记忆",
                              new_memory=args.get("text", ""))
            return {"success": False, "detail": "无法核实原发言者，未进入候选记忆"}
        signature = hashlib.sha256(json.dumps([
            args['chat_id'], owner["person_id"], normalized_fact_text(args["text"]),
            (args.get("metadata") or {}).get("evidence_message_ids") or []
        ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.store.transaction() as db:
            existing = db.execute("SELECT id,status FROM heart_candidates WHERE signature=?", (signature,)).fetchone()
            if existing:
                return {"success": False, "detail": f"候选记忆 #{existing['id']} 已处理或等待确认；未重复写入"}
            count = db.execute("SELECT count(*) FROM heart_candidates WHERE owner=? AND status='pending'",
                               (owner["person_id"],)).fetchone()[0]
            if count >= limit:
                return {"success": False, "detail": "本人候选记忆已满，未写入；请先私聊处理候选"}
            payload = {"args": args, "owner": owner, "reason": reason}
            cursor = db.execute("INSERT INTO heart_candidates(signature,owner,chat,status,created,payload) VALUES(?,?,?,?,?,?)",
                                (signature, owner["person_id"], args["chat_id"], "pending", time.time(),
                                 json.dumps(payload, ensure_ascii=False)))
            candidate_id = cursor.lastrowid
            self.store.append_in(db, "候选记忆", args["chat_id"], {
                "candidate_id": candidate_id, "status": "待本人确认", "new_memory": args["text"],
                "reason": reason,
                "evidence_message_ids": (args.get("metadata") or {}).get("evidence_message_ids") or [],
            })
        return {"success": False, "detail": f"已进入候选记忆 #{candidate_id}；尚未写入长期记忆"}

    def list_for(self, owner):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,payload FROM heart_candidates WHERE owner=? AND status='pending' ORDER BY id DESC LIMIT 10",
                (owner,)).fetchall()]

    async def delete_review_record(self, candidate_id, expected_text):
        """删除审核列表记录，不删除原生长期记忆；进行中的冲突须先取消。"""
        async with self.locks[candidate_id]:
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM heart_candidates WHERE id=?", (candidate_id,)).fetchone()
                if not row:
                    raise ValueError("候选记录不存在，请刷新")
                payload = json.loads(row["payload"])
                if payload["args"]["text"] != expected_text:
                    raise ValueError("候选内容已变化，请刷新")
                if row["status"] == "conflict_pending":
                    raise ValueError("此记录正在等待冲突确认，请先由本人取消或完成确认")
                db.execute("DELETE FROM heart_candidates WHERE id=?", (candidate_id,))
                self.store.append_in(db, "候选记忆", row["chat"], {
                    "candidate_id": candidate_id, "status": "管理员删除候选／审核记录",
                    "new_memory": expected_text, "reason": "仅删除列表记录，未删除原生长期记忆；操作：WebUI管理员",
                })
        return {"success": True, "message": "候选／审核记录已删除；原生长期记忆未删除"}

    async def review(self, candidate_id, approve, expected_text, edited_text=None):
        """仅供已鉴权的宿主 WebUI 调用；归属始终取候选记录，不接收前端指定人物。"""
        with self.store.connect() as db:
            row = db.execute("SELECT owner FROM heart_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise ValueError("候选不存在")
        return await self.resolve(row["owner"], candidate_id, approve, reviewer="WebUI管理员",
                                  expected_text=expected_text, edited_text=edited_text)

    async def resolve(self, owner, candidate_id, approve, *, reviewer="", expected_text=None, edited_text=None):
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
            original_text = args["text"]
            if payload.get('kind') == 'summary' and not reviewer:
                return '会话摘要只能由WebUI管理员审核，不接受成员代替整个会话确认。'
            if expected_text is not None and expected_text != original_text:
                raise ValueError("候选内容已变化，请刷新后重新审核")
            if edited_text is not None:
                if not reviewer or not edited_text.strip() or len(edited_text) > 2000:
                    raise ValueError("审核后的内容须为1至2000字")
                args = {**args, "text": edited_text.strip(),
                        "external_id": f"heart_review:{candidate_id}:{hashlib.sha256(edited_text.strip().encode()).hexdigest()}"}
                if payload.get('kind') == 'summary':
                    # 编辑摘要后旧实体/关系未必还成立，不能沿用到图谱。
                    args = {**args, 'entities': [], 'relations': []}
            if not approve:
                status, message = "ignored", "已忽略，未写入长期记忆。"
            else:
                if payload.get('kind') == 'summary':
                    if payload.get('decision') == 'conflict':
                        self.store.append('候选记忆',args['chat_id'],candidate_id=candidate_id,status='摘要冲突暂缓审核',
                                          new_memory=args['text'],reason='必须先核实并修正原生旧记忆，不允许直接批准造成新旧冲突')
                        return '摘要与旧信息可能冲突，不能直接批准覆盖。请先在群回忆／原生记忆页面核实并修正旧内容，再忽略本候选；私聊摘要请在原生记忆页面处理。'
                    if not await self.backend.summary_snapshot_matches(args['chat_id'],payload.get('snapshot') or {}):
                        self.store.append('候选记忆',args['chat_id'],candidate_id=candidate_id,status='摘要审核材料已过时',
                                          new_memory=args['text'],reason='原生旧摘要变化或失效，未按过时材料写入')
                        return '比较过的旧摘要已改变，请忽略旧候选并重新核查，不能按过时审核继续写入。'
                # 主进程会再次走原生写入与冲突关卡；候选确认不代替冲突确认。
                approved = {**args, "metadata": {**(args.get("metadata") or {}),
                    "fact_claim": {"trust": "manual_confirmed", "authority": "manual",
                                   "stability": "stable", "profile_section": "stable_facts",
                                   "reason": "管理员审核收录，非本人确认" if reviewer else "本人确认候选"},
                    "heart_review": {"confirmed_by": "administrator" if reviewer else "user",
                                     "candidate_id": candidate_id,
                                     "reviewer": reviewer or payload["owner"].get("name", "本人"),
                                     "original_text": original_text, "edited": args["text"] != original_text}}}
                result = (await self.backend.write_reviewed_candidate(approved) if reviewer
                          else await self.backend.write_approved_candidate(approved))
                if result.get("success") and result.get("stored_ids"):
                    status, message = "written", "原生记忆服务报告写入成功。"
                elif result.get("success") and result.get("skipped_ids"):
                    status, message = "skipped", "原生记忆服务报告内容已存在，本次未新增。"
                elif self.backend.conflict_pending(args["chat_id"], result):
                    status, message = "conflict_pending", "与旧记忆可能冲突，已向本人追问确认；此时尚未写入。"
                else:
                    self.store.append("候选记忆", args["chat_id"], candidate_id=candidate_id, status="写入未完成",
                                      new_memory=args["text"], reason=str(result.get("detail") or "原生服务未确认写入"))
                    return "原生记忆未确认写入，候选仍保留；请查看日志后重试。"
            with self.store.transaction() as db:
                db.execute("UPDATE heart_candidates SET status=? WHERE id=? AND owner=? AND status='pending'",
                           (status, candidate_id, owner))
                self.store.append_in(db, "候选记忆", args["chat_id"], {
                    "candidate_id": candidate_id, "status": status, "new_memory": args["text"],
                    "reason": ("管理员审核（不等于本人确认）：" if reviewer else "") + message,
                    "reviewer": reviewer or "原用户", "original_text": original_text,
                    "evidence_message_ids": (args.get("metadata") or {}).get("evidence_message_ids") or [],
                })
            return message
