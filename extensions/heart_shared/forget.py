"""本人私聊查看和忘记原生人物事实；删除前后都验证原生服务结果。"""

import asyncio
import json
import math
import re
import time
from collections import defaultdict

NATURAL_FORGET_PATTERN = (r"^(?!/)(?=.{3,300}$)(?:(?=.*(?:忘记|忘掉|忘了|删掉|删除|清除|抹掉))"
                          r"(?=.*(?:记忆|记住|记得|关于|以前|我的|我认为|这件事|这条))|"
                          r"(?=.*(?:别再记|不要再记|不再记|别记|不想让你记))).+$")
NATURAL_RESOLVE_PATTERN = (r"^(?:确认忘记|取消忘记|(?:对|是的|我确认)[，, ]*忘掉吧|"
                           r"(?:不|不用)[，, ]*还是保留吧)$")


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
        if action == "natural_forget":
            return await self.natural_propose(actor)
        if action in {"natural_confirm", "natural_cancel"}:
            return await self.resolve_natural(actor, action == "natural_confirm")
        return await self.resolve(actor, action == "confirm")

    async def natural_propose(self, actor):
        """模型只定位本人旧事实；任何判断都不能直接触发删除。"""
        facts = await self.backend.person_facts(actor["person_id"])
        if not facts:
            self.event(actor, "自然语言请求未匹配", reason="本人当前无可定位的长期人物事实")
            return "我没有找到属于你的可删除长期人物事实，所以没有改动记忆。"
        if len(facts) > 80:
            self.event(actor, "自然语言请求未匹配", reason="本人事实超过80条，拒绝截断后猜测目标")
            return "你的记忆较多，我不想猜错要删哪条。请先用 /我的记忆 查看，再用 /忘记 关键词。"
        try:
            judgment = await asyncio.wait_for(
                self.backend.match_natural_forget(actor, facts), timeout=25)
        except Exception as exc:
            self.event(actor, "自然语言识别失败，未删除", reason=type(exc).__name__)
            return "这次没能可靠判断你要忘记哪条；长期记忆没有改动。请用 /忘记 关键词。"
        confidence = judgment.get("confidence")
        ids = judgment.get("target_ids")
        if (judgment.get("intent") is not True or not isinstance(confidence, (int, float))
                or isinstance(confidence, bool) or not math.isfinite(confidence) or confidence < 0.85
                or not isinstance(ids, list) or not 1 <= len(ids) <= 3
                or any(not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= len(facts) for i in ids)):
            self.event(actor, "自然语言识别不确定，未删除", reason="意图、把握程度或目标编号未通过检查")
            return "我不确定你要删除哪条长期记忆，因此没有改动。请说得更具体，或用 /忘记 关键词。"
        selected = [facts[i - 1] for i in dict.fromkeys(ids)]
        keys = {self.normalized_content(item["content"]) for item in selected}
        if len(keys) != 1:
            self.event(actor, "自然语言识别不确定，未删除", reason="模型选中了含义不同的多条事实")
            return "我找到了不止一种可能的记忆，没有擅自删除。请说得更具体。"
        # 同一事实可能被原生系统以空格差异存成多份；一并展示、一起确认。
        matches = [item for item in facts if self.normalized_content(item["content"]) in keys]
        if len(matches) > 5:
            self.event(actor, "自然语言识别不确定，未删除", reason="相同文字变体超过5条")
            return "相似记忆过多，暂不提出批量删除。请用 /忘记 关键词进一步定位。"
        self.event(actor, "自然语言已定位目标，待确认", reason=f"AI把握程度{confidence:.0%}；匹配{len(matches)}条排版相同的事实",
                   content="；".join(item["content"] for item in matches))
        return await self.propose_items(actor, matches)

    @staticmethod
    def normalized_content(content):
        return re.sub(r"\s+", "", content).casefold()

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
        return await self.propose_items(actor, matches)

    async def propose_items(self, actor, items):
        if not items or len(items) > 5 or len({item["hash"] for item in items}) != len(items):
            raise ValueError("删除目标数量或编号无效")
        async with self.locks[actor["person_id"]]:
            preview = await self.backend.invoke("memory_delete_admin", {
                "action": "preview", "mode": "paragraph", "selector": {"hashes": [item["hash"] for item in items]}})
            self.validate_preview(preview, items, actor["person_id"])
            with self.store.transaction() as db:
                cursor = db.execute("INSERT INTO heart_forget_requests(owner,session,status,expires,payload) VALUES(?,?,?,?,?)",
                    (actor["person_id"], actor["session_id"], "pending", time.time() + 600,
                     json.dumps(items, ensure_ascii=False)))
                request_id = cursor.lastrowid
        contents = "\n".join(f"{index}. {item['content']}" for index, item in enumerate(items, 1))
        self.event(actor, "等待本人确认；原生记忆尚未删除", "；".join(item["content"] for item in items),
                   request_id=request_id)
        return (f"找到以下{len(items)}条长期记忆，当前都还没有删除：\n{contents}\n"
                f"核对后，请在10分钟内回复“确认忘记”或 /确认忘记 {request_id}。\n"
                f"保留请回复“取消忘记”或 /取消忘记 {request_id}。")

    @staticmethod
    def validate_preview(preview, items, person_id):
        paragraphs = [entry for entry in preview.get("items") or [] if entry.get("item_type") == "paragraph"]
        expected = {}
        for item in items:
            normalized = " ".join(item["content"].split())
            expected[item["hash"]] = normalized if len(normalized) <= 220 else normalized[:220] + "..."
        if (preview.get("success") is not True or preview.get("mode") != "paragraph"
                or int((preview.get("counts") or {}).get("paragraphs", 0)) != len(items)
                or len(paragraphs) != len(items)
                or {entry.get("item_hash") for entry in paragraphs} != set(expected)
                or any(entry.get("source") != f"person_fact:{person_id}"
                       or entry.get("preview") != expected.get(entry.get("item_hash")) for entry in paragraphs)):
            raise ValueError("原生删除预览与本人目标不一致，已停止删除")

    async def resolve_natural(self, actor, confirm):
        with self.store.connect() as db:
            rows = db.execute("SELECT id FROM heart_forget_requests WHERE owner=? AND session=? "
                              "AND status='pending' AND expires>? ORDER BY id DESC LIMIT 2",
                              (actor["person_id"], actor["session_id"], time.time())).fetchall()
        if len(rows) != 1:
            self.event(actor, "自然语言确认未执行", reason=f"当前有效待确认请求{len(rows)}条，需要明确编号")
            return "当前没有唯一一条待确认删除；没有改动记忆。请按机器人提示发送带编号的确认或取消。"
        return await self.resolve({**actor, "value": str(rows[0]["id"])}, confirm)

    async def resolve(self, actor, confirm):
        request_id = int(actor["value"])
        async with self.locks[actor["person_id"]]:
            with self.store.transaction() as db:
                row = db.execute("SELECT * FROM heart_forget_requests WHERE id=? AND owner=? AND session=?",
                                 (request_id, actor["person_id"], actor["session_id"])).fetchone()
                if row is None or row["status"] != "pending":
                    return "没有找到属于你的待确认删除，或它已经处理。"
                payload = json.loads(row["payload"])
                items = payload if isinstance(payload, list) else [payload]  # 兼容升级前的待确认请求。
                status = "expired" if row["expires"] <= time.time() else "executing" if confirm else "cancelled"
                db.execute("UPDATE heart_forget_requests SET status=? WHERE id=? AND status='pending'", (status, request_id))
            if status != "executing":
                self.event(actor, "已超时，未删除" if status == "expired" else "用户取消，未删除",
                           "；".join(item["content"] for item in items), request_id=request_id)
                return "确认已超时，没有删除。" if status == "expired" else "已取消，没有删除。"
            try:
                current = {fact["hash"]: fact for fact in await self.backend.person_facts(actor["person_id"])}
                if any(current.get(item["hash"]) != item for item in items):
                    raise ValueError("目标记忆已改变或不再属于本人")
                selector = {"hashes": [item["hash"] for item in items]}
                preview = await self.backend.invoke("memory_delete_admin", {
                    "action": "preview", "mode": "paragraph", "selector": selector})
                self.validate_preview(preview, items, actor["person_id"])
                result = await self.backend.invoke("memory_delete_admin", {
                    "action": "execute", "mode": "paragraph", "selector": selector,
                    "requested_by": "heart.confirmed_user", "reason": "本人私聊确认忘记"})
                if result.get("success") is not True or int(result.get("deleted_paragraph_count") or 0) != len(items):
                    raise RuntimeError("原生删除服务未确认删除全部目标段落")
                remaining = {fact["hash"] for fact in await self.backend.person_facts(actor["person_id"])}
                if any(item["hash"] in remaining for item in items):
                    raise RuntimeError("原生删除已报告成功，但本人有效事实中仍有目标；需人工检查")
            except (ValueError, RuntimeError) as exc:
                with self.store.connect() as db:
                    db.execute("UPDATE heart_forget_requests SET status='failed' WHERE id=?", (request_id,))
                self.event(actor, "删除未能确认完成", "；".join(item["content"] for item in items), str(exc), request_id)
                return "未能确认删除完成，已停止自动处理；请检查日志。"
            with self.store.connect() as db:
                db.execute("UPDATE heart_forget_requests SET status='deleted' WHERE id=?", (request_id,))
            self.event(actor, "原生长期记忆已删除并复核", "；".join(item["content"] for item in items),
                       request_id=request_id)
            return f"已从可用的原生长期记忆中删除{len(items)}条目标内容；审计日志仍保留操作记录。"
