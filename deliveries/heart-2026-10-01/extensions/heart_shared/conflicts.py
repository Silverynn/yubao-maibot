"""个人事实写入前的确认流程。只通过适配器调用原生服务，不直接修改记忆库。"""

import asyncio
import hashlib
import json
import math
import re
import time
from collections import defaultdict


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalized_fact_text(value):
    """只消除排版空白；不删除否定词、时间或其他可能改变事实的字符。"""
    return re.sub(r"\s+", "", str(value or "")).casefold()


def own_hits(result, person_id):
    if not isinstance(result, dict) or result.get("success") is False or result.get("error"):
        raise ValueError("记忆检索失败，暂缓写入")
    hits = []
    for hit in result.get("hits", []):
        meta = hit.get("metadata") or {}
        owners = set(meta.get("person_ids") or [])
        if meta.get("person_id"):
            owners.add(meta["person_id"])
        if owners != {person_id} or hit.get("type") != "paragraph" or not hit.get("hash"):
            continue
        if meta.get("source_type") != "person_fact":
            continue
        change = meta.get("memory_change") or {}
        if change.get("change_type") == "mark_superseded" or change.get("valid_to"):
            continue
        hits.append(hit)
    return hits


def validate_plan(record, proposal):
    """模型只能提计划；严格限制到用户看到的那几条旧事实和那一句新事实。"""
    plan = record.get("plan") or {}
    args = proposal["args"]
    pid, chat = proposal["owner"]["person_id"], args["chat_id"]
    if plan.get("person_id") != pid or plan.get("chat_id") != chat:
        raise ValueError("修改计划的归属不匹配")
    expected = {hit["hash"] for hit in proposal["old"]}
    marked, writes, refreshed = set(), 0, False
    for operation in plan.get("operations") or []:
        action = operation.get("action")
        if action == "mark_superseded":
            if operation.get("target_type") != "paragraph" or operation.get("hash") not in expected or operation.get("valid_to"):
                raise ValueError("修改计划扩大了旧记忆范围")
            marked.add(operation["hash"])
        elif action == "ingest_text":
            writes += 1
            if (operation.get("text") != args["text"] or operation.get("chat_id") != chat
                    or set(operation.get("person_ids") or []) != {pid}
                    or operation.get("source_type") != "person_fact" or operation.get("relations")
                    or not set(operation.get("participants") or []) <= {proposal["owner"].get("name", "")}
                    or operation.get("valid_from")):
                raise ValueError("修改计划改变了已确认内容或范围")
        elif action == "refresh_person_profile":
            if operation.get("person_id") != pid:
                raise ValueError("不能刷新他人的人物画像")
            refreshed = True
        else:
            raise ValueError("修改计划包含未授权操作")
    if writes != 1 or marked != expected or not refreshed:
        raise ValueError("修改计划不完整")
    return plan


class ConflictGuard:
    def __init__(self, store, backend, clock=time.time):
        self.store, self.backend, self.clock = store, backend, clock
        self.locks = defaultdict(asyncio.Lock)
        self.tasks = set()
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS heart_proposals(
                id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, chat TEXT NOT NULL,
                signature TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
                payload TEXT NOT NULL)""")

    def event(self, proposal, status, reason=""):
        args = proposal["args"]
        self.store.append("记忆冲突确认", args["chat_id"], proposal=proposal.get("id"), status=status,
            reason=reason, old_memories=[hit["content"] for hit in proposal.get("old", [])], new_memory=args["text"],
            evidence_message_ids=(args.get("metadata") or {}).get("evidence_message_ids", []))

    def duplicate(self, proposal, matches, reason):
        """同义旧事实已足够表达新事实；只拦截本次写入，不改旧记忆。"""
        args = proposal["args"]
        self.store.append("记忆去重", args["chat_id"],
                          status="已跳过重复写入", reason=reason,
                          old_memories=[hit["content"] for hit in matches],
                          new_memory=args["text"],
                          evidence_message_ids=(args.get("metadata") or {}).get("evidence_message_ids", []))
        return {"success": True, "stored_ids": [],
                "skipped_ids": [hit["hash"] for hit in matches],
                "detail": "已有同义长期记忆，本次未重复写入"}

    def pending(self, chat):
        with self.store.connect() as db:
            return db.execute("SELECT 1 FROM heart_proposals WHERE chat=? AND status IN ('pending','executing') AND expires>?",
                              (chat, self.clock())).fetchone() is not None

    async def check(self, args, settings):
        """返回None才允许写入；错误/不确定/待确认均不放行此条候选。"""
        proposal = {"args": args, "old": []}
        try:
            owner = await self.backend.owner(args)
        except Exception as exc:  # noqa: BLE001 -- 写入边界必须对任何适配器故障关闭放行。
            self.event(proposal, "暂缓写入", "来源核实失败：" + type(exc).__name__)
            return {"success": False, "detail": "来源核实失败，已暂缓写入"}
        if not owner:
            self.event(proposal, "暂缓写入", "无法核实候选事实的原始发言者")
            return {"success": False, "detail": "无法核实候选事实的原始发言者，已暂缓写入"}
        proposal = {"owner": owner, "args": args, "old": []}
        async with self.locks[owner["person_id"]]:
            try:
                hits = own_hits(await self.backend.search(args, settings["candidate_limit"]), owner["person_id"])
                self.store.append('记忆冲突候选检索', args['chat_id'],
                                  evidence_message_ids=(args.get('metadata') or {}).get('evidence_message_ids', []),
                                  new_memory=args['text'], old_memories=[hit['content'] for hit in hits],
                                  status='检索到可比较旧事实' if hits else '本会话暂无可比较的旧事实')
                exact = [hit for hit in hits
                         if normalized_fact_text(hit["content"]) == normalized_fact_text(args["text"])]
                if exact:
                    return self.duplicate(proposal, exact[:1], "仅空白或大小写不同；无需调用模型")
                if not hits:
                    self.event(proposal, "检查完成", "未检索到可比较的本人旧事实，允许原生写入；不保证检索覆盖全部历史")
                    return None
                judgment = await asyncio.wait_for(self.backend.judge(args, hits, owner), settings["timeout_seconds"])
                confidence = judgment.get("confidence")
                if ((judgment.get("supported") is not True and not settings.get("admin_reviewed", False)) or not isinstance(confidence, (int, float))
                        or isinstance(confidence, bool) or not math.isfinite(confidence)
                        or not settings.get("conflict_min_confidence", 0.8) <= confidence <= 1):
                    raise ValueError("新事实来源或判断把握不足，暂不写入")
                if judgment.get("verdict") == "duplicate":
                    ids = judgment.get("duplicate_ids")
                    by_id = {hit["hash"]: hit for hit in hits}
                    if (confidence < settings.get("duplicate_min_confidence", 0.9)
                            or not isinstance(ids, list) or not 1 <= len(ids) <= 3
                            or any(not isinstance(i, str) for i in ids)
                            or not set(ids) <= set(by_id)
                            or judgment.get("conflict_ids")):
                        raise ValueError("同义重复判断不可靠，暂缓写入")
                    matches = [by_id[i] for i in dict.fromkeys(ids)]
                    return self.duplicate(proposal, matches, f"AI判断与旧事实同义且无新增信息；把握程度{confidence:.0%}")
                if judgment.get("verdict") == "clear":
                    if judgment.get("conflict_ids") or judgment.get("duplicate_ids"):
                        raise ValueError("判断结果自相矛盾，暂不写入")
                    self.event(proposal, "检查完成", "未发现明确冲突，允许原生写入")
                    return None
                if judgment.get("verdict") != "conflict":
                    raise ValueError("无法确认新事实得到用户原文支持，或冲突判断不确定")
                ids = judgment.get("conflict_ids")
                if (not isinstance(ids, list) or not ids or len(ids) > 3
                        or any(not isinstance(i, str) for i in ids) or judgment.get("duplicate_ids")):
                    raise ValueError("冲突结果格式无效")
                by_id = {hit["hash"]: hit for hit in hits}
                if not set(ids) <= set(by_id):
                    raise ValueError("冲突结果引用了候选范围外的记忆")
                proposal["old"] = [by_id[i] for i in dict.fromkeys(ids)]
                signature = fingerprint([owner["person_id"], args["text"], sorted(ids)])
                with self.store.connect() as db:
                    duplicate = db.execute("SELECT id,status FROM heart_proposals WHERE signature=? AND expires>? ORDER BY id DESC LIMIT 1",
                                           (signature, self.clock())).fetchone()
                if duplicate:
                    return {"success": False, "pending": duplicate["status"] in {"pending", "executing"},
                            "detail": "该候选已经等待确认或已被处理，不重复写入/追问"}
                preview = await asyncio.wait_for(self.backend.preview(proposal), settings["timeout_seconds"] * 2)
                if preview.get("success") is not True:
                    raise ValueError("原生记忆修改计划生成失败")
                record = preview.get("plan") or {}
                validate_plan(record, proposal)
                proposal["plan_id"] = preview["plan_id"]
                proposal["plan_hash"] = fingerprint(record["plan"])
                proposal["private_session"] = await self.backend.private_session(owner) if not hasattr(self.backend, "confirmation_destination") else await self.backend.confirmation_destination(owner, args, proposal["old"])
                proposal["confirmation_session"] = proposal["private_session"]
                proposal["confirmation_mode"] = "group" if proposal["confirmation_session"] == args["chat_id"] and args.get("group_id") else "private"
                with self.store.transaction() as db:
                    cursor = db.execute("INSERT INTO heart_proposals(owner,chat,signature,status,expires,payload) VALUES(?,?,?,?,?,?)",
                        (owner["person_id"], args["chat_id"], signature, "pending", self.clock() + settings["confirmation_minutes"] * 60,
                         json.dumps(proposal, ensure_ascii=False)))
                    proposal["id"] = cursor.lastrowid
                    db.execute("UPDATE heart_proposals SET payload=? WHERE id=?", (json.dumps(proposal, ensure_ascii=False), proposal["id"]))
                text = f"【记忆更新确认】请 {owner.get('name', '原发言者')} 本人确认，其他成员不能代答。\n我发现新旧信息可能冲突，暂时保留旧记忆。\n旧：" + "；".join(h["content"] for h in proposal["old"])
                text += f"\n新：{args['text']}\n若要替换，请回复 /确认记忆更新 {proposal['id']}\n若保留原记忆，请回复 /取消记忆更新 {proposal['id']}\n{settings['confirmation_minutes']}分钟内未确认则不更新。"
                sent = await self.backend.send(proposal["private_session"], text)
                if not sent:
                    self.set_status(proposal["id"], "failed")
                    self.event(proposal, "确认消息发送失败", "新旧记忆均未修改")
                    return {"success": False, "detail": "冲突确认消息发送失败，未更新记忆；请管理员检查日志"}
                else:
                    self.event(proposal, "等待用户确认", "已在原群询问本人；只展示本群旧事实" if proposal["confirmation_mode"] == "group" else "已私聊原发言者；此时未写入新记忆")
                return {"success": False, "pending": True, "confirmation_mode": proposal["confirmation_mode"],
                        "detail": "新旧信息疑似冲突，等待本人确认后更新"}
            except Exception as exc:  # noqa: BLE001 -- 模型/检索失败不能让未确认候选继续写入。
                self.event(proposal, "暂缓写入", type(exc).__name__ + ": " + str(exc))
                return {"success": False, "detail": "冲突核查未完成，未写入这条候选，详见日志"}

    def set_status(self, request_id, status):
        with self.store.connect() as db:
            db.execute("UPDATE heart_proposals SET status=? WHERE id=?", (status, request_id))
            row = db.execute("SELECT payload FROM heart_proposals WHERE id=?", (request_id,)).fetchone()
            if row:
                self.sync_candidate(db, json.loads(row[0]), status)

    @staticmethod
    def sync_candidate(db, proposal, status):
        """审核转入冲突后，后台状态必须跟着真正的确认结果更新，不能永久显示待确认。"""
        review = (proposal["args"].get("metadata") or {}).get("heart_review") or {}
        candidate_id = review.get("candidate_id")
        target = {"applied": "written", "cancelled": "conflict_cancelled", "expired": "conflict_expired", "failed": "failed"}.get(status)
        if candidate_id and target:
            db.execute("UPDATE heart_candidates SET status=? WHERE id=? AND owner=? AND status IN ('pending','conflict_pending')",
                       (target, candidate_id, proposal["owner"]["person_id"]))

    async def resolve(self, session, expected_message_id=""):
        actor = (await self.backend.confirmation_actor(session, expected_message_id)
                 if expected_message_id else await self.backend.confirmation_actor(session))
        if not actor:
            return "请由本人在收到追问的会话中发送完整的确认或取消指令。"
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM heart_proposals WHERE id=?", (actor["request_id"],)).fetchone()
            if row is None:
                return "没有找到属于你的待确认更新。"
            proposal = json.loads(row["payload"])
            if row["owner"] != actor["person_id"] or proposal.get("confirmation_session", proposal["private_session"]) != session:
                return "没有找到属于你的待确认更新。"
            if row["status"] != "pending":
                return "这条更新已处理或正在执行，不会重复执行。"
            status = "expired" if row["expires"] <= self.clock() else "cancelled" if actor["cancel"] else "executing"
            db.execute("UPDATE heart_proposals SET status=? WHERE id=? AND status='pending'", (status, actor["request_id"]))
            self.sync_candidate(db, proposal, status)
        if status != "executing":
            self.event(proposal, "已超时，保持旧记忆" if status == "expired" else "用户取消，保持旧记忆")
            return "已超时，未修改记忆。" if status == "expired" else "已取消，旧记忆保持不变。"
        task = asyncio.create_task(self.execute(proposal))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return "已收到你的确认，正在核对并更新；完成后会告诉你结果。"

    async def execute(self, proposal):
        """先验证计划与旧内容未变化，再确认新事实确实存储，最后使旧事实失效。"""
        async with self.locks[proposal["owner"]["person_id"]]:
            message = ""
            try:
                record = await self.backend.get_plan(proposal["plan_id"])
                if record.get("status") != "awaiting_confirmation" or fingerprint(record.get("plan")) != proposal["plan_hash"]:
                    raise ValueError("原修改计划已变化或已执行，请重新核实")
                validate_plan(record, proposal)
                current = {h["hash"]: h for h in own_hits(await self.backend.search(proposal["args"], 30), proposal["owner"]["person_id"])}
                if any(h["hash"] not in current or current[h["hash"]]["content"] != h["content"] for h in proposal["old"]):
                    raise ValueError("旧记忆已变化或已无法确认，不能按过时确认覆盖")
                # 原生修正器内部并非单事务。先以同一计划的幂等外部ID写入新内容，
                # 只有服务报告实际存储ID后才允许其执行旧事实失效动作。
                prepared = await self.backend.prepare_new(proposal, record)
                if not prepared.get("stored_ids") or prepared.get("success") is False:
                    raise ValueError("新记忆未确认写入成功，旧记忆保持不变")
                result = await self.backend.execute_plan(proposal["plan_id"])
                changed = {x.get("hash") for x in result.get("execution", {}).get("superseded_targets", [])}
                expected = {h["hash"] for h in proposal["old"]}
                if result.get("success") is not True or changed != expected:
                    self.set_status(proposal["id"], "failed")
                    self.event(proposal, "更新未完整完成", "新记忆已存储，但旧记忆失效未全部确认；请检查原生后台，不自动重试")
                    message = "新记忆已保存，但旧记忆更新未全部完成，请检查后台日志；我不会宣称全部成功。"
                else:
                    self.set_status(proposal["id"], "applied")
                    self.event(proposal, "用户确认后已更新", "新事实已存储，旧事实由原生系统标记失效，保留审计历史")
                    message = "已按你的确认更新记忆，旧信息已标记为过时。"
            except asyncio.CancelledError:
                self.event(proposal, "执行被中断，结果待核实", "不自动重试；需检查原生记忆状态")
                raise
            except Exception as exc:  # noqa: BLE001 -- 保留部分失败状态，绝不自动重试副作用。
                self.set_status(proposal["id"], "failed")
                self.event(proposal, "更新失败或状态待核实", type(exc).__name__ + ": " + str(exc))
                message = "这次更新未能完整确认，已停止自动处理，请查看日志；不要重复发送确认。"
            if message:
                try:
                    if not await self.backend.send(proposal["private_session"], message):
                        self.event(proposal, "结果通知发送失败", "具体更新结果请查看上一条日志")
                except Exception:  # noqa: BLE001 -- 通知失败不应改变已经完成的执行结果。
                    self.event(proposal, "结果通知发送失败")
