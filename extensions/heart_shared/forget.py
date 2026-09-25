"""本人私聊查看和忘记原生人物事实；删除前后都验证原生服务结果。"""

import asyncio
import json
import time
from collections import defaultdict


class ForgetManager:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend
        self.locks = defaultdict(asyncio.Lock)
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS heart_forget_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, session TEXT NOT NULL,
                status TEXT NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL)""")

    def event(self, actor, status, content="", reason="", request_id=None):
        self.store.append("忘记记忆", actor["session_id"], message_id=actor["message_id"],
                          request_id=request_id, status=status, memory=content, reason=reason)

    async def handle(self, actor):
        action = actor["action"]
        if action == "list":
            page = max(1, min(50, int(actor["value"])))
            facts = await self.backend.person_facts(actor["person_id"])
            selected = facts[(page - 1) * 20:page * 20]
            self.event(actor, "已查看本人长期记忆", reason=f"第{page}页；本次原生列表返回{len(facts)}条")
            if not selected:
                return "这一页没有可查看的长期人物事实。候选记忆请用 /候选记忆 查看。"
            lines = [f"{(page - 1) * 20 + index}. {item['content']}"
                     + ("（原生标记有争议）" if item.get("status") == "conflicted" else "")
                     for index, item in enumerate(selected, 1)]
            return "你的长期人物事实（第%d页）：\n%s\n需要删除时发送 /忘记 记忆中的关键词。" % (page, "\n".join(lines))
        if action == "forget":
            return await self.propose(actor)
        return await self.resolve(actor, action == "confirm")

    async def propose(self, actor):
        query = actor["value"]
        if len(query) < 2:
            return "请提供至少两个字的关键词，避免误删。"
        facts = await self.backend.person_facts(actor["person_id"])
        matches = [item for item in facts if query.casefold() in item["content"].casefold()]
        if len(matches) != 1:
            self.event(actor, "未提出删除", reason=f"关键词匹配{len(matches)}条，必须恰好一条")
            if not matches:
                return "没有找到包含该关键词的本人长期人物事实；可先发送 /我的记忆 查看。"
            lines = [f"- {item['content']}" for item in matches[:5]]
            return "找到多条，请用更具体的关键词缩小到一条：\n" + "\n".join(lines)
        item = matches[0]
        async with self.locks[actor["person_id"]]:
            preview = await self.backend.invoke("memory_delete_admin", {
                "action": "preview", "mode": "paragraph", "selector": {"hashes": [item["hash"]]}})
            self.validate_preview(preview, item, actor["person_id"])
            with self.store.transaction() as db:
                cursor = db.execute("INSERT INTO heart_forget_requests(owner,session,status,expires,payload) VALUES(?,?,?,?,?)",
                    (actor["person_id"], actor["session_id"], "pending", time.time() + 600,
                     json.dumps(item, ensure_ascii=False)))
                request_id = cursor.lastrowid
        self.event(actor, "等待本人确认；原生记忆尚未删除", item["content"], request_id=request_id)
        return (f"找到这一条长期记忆：\n{item['content']}\n"
                f"确认删除请在10分钟内回复 /确认忘记 {request_id}\n"
                f"保留请回复 /取消忘记 {request_id}。候选记忆不受影响。")

    @staticmethod
    def validate_preview(preview, item, person_id):
        paragraphs = [entry for entry in preview.get("items") or [] if entry.get("item_type") == "paragraph"]
        normalized = " ".join(item["content"].split())
        expected_preview = normalized if len(normalized) <= 220 else normalized[:220] + "..."
        if (preview.get("success") is not True or preview.get("mode") != "paragraph"
                or int((preview.get("counts") or {}).get("paragraphs", 0)) != 1
                or len(paragraphs) != 1 or paragraphs[0].get("item_hash") != item["hash"]
                or paragraphs[0].get("source") != f"person_fact:{person_id}"
                or paragraphs[0].get("preview") != expected_preview):
            raise ValueError("原生删除预览与本人目标不一致，已停止删除")

    async def resolve(self, actor, confirm):
        request_id = int(actor["value"])
        async with self.locks[actor["person_id"]]:
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM heart_forget_requests WHERE id=? AND owner=? AND session=?",
                                 (request_id, actor["person_id"], actor["session_id"])).fetchone()
                if row is None or row["status"] != "pending":
                    return "没有找到属于你的待确认删除，或它已经处理。"
                item = json.loads(row["payload"])
                status = "expired" if row["expires"] <= time.time() else "executing" if confirm else "cancelled"
                db.execute("UPDATE heart_forget_requests SET status=? WHERE id=? AND status='pending'", (status, request_id))
            if status != "executing":
                self.event(actor, "已超时，未删除" if status == "expired" else "用户取消，未删除",
                           item["content"], request_id=request_id)
                return "确认已超时，没有删除。" if status == "expired" else "已取消，没有删除。"
            try:
                current = {fact["hash"]: fact for fact in await self.backend.person_facts(actor["person_id"])}
                if current.get(item["hash"]) != item:
                    raise ValueError("目标记忆已改变或不再属于本人")
                selector = {"hashes": [item["hash"]]}
                preview = await self.backend.invoke("memory_delete_admin", {
                    "action": "preview", "mode": "paragraph", "selector": selector})
                self.validate_preview(preview, item, actor["person_id"])
                result = await self.backend.invoke("memory_delete_admin", {
                    "action": "execute", "mode": "paragraph", "selector": selector,
                    "requested_by": "heart.confirmed_user", "reason": "本人私聊确认忘记"})
                if result.get("success") is not True or int(result.get("deleted_paragraph_count") or 0) != 1:
                    raise RuntimeError("原生删除服务未确认删除一条段落")
                remaining = {fact["hash"] for fact in await self.backend.person_facts(actor["person_id"])}
                if item["hash"] in remaining:
                    raise RuntimeError("原生删除已报告成功，但本人有效事实中仍有目标；需人工检查")
            except (ValueError, RuntimeError) as exc:
                with self.store.connect() as db:
                    db.execute("UPDATE heart_forget_requests SET status='failed' WHERE id=?", (request_id,))
                self.event(actor, "删除未能确认完成", item["content"], str(exc), request_id)
                return "未能确认删除完成，已停止自动处理；请检查日志。"
            with self.store.connect() as db:
                db.execute("UPDATE heart_forget_requests SET status='deleted' WHERE id=?", (request_id,))
            self.event(actor, "原生长期记忆已删除并复核", item["content"], request_id=request_id)
            return "已从可用的原生长期记忆中删除这条内容；审计日志仍保留操作记录。"
