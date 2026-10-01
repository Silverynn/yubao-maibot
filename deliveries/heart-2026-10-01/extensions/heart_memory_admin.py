"""Heart 的 WebUI 接入：候选审批及原生群回忆修正，共用原生正式记忆库。"""

from collections import defaultdict
import asyncio
import json
import time

from heart_shared.conflicts import fingerprint
from heart_shared.memory_scope import metadata, visible
from src.A_memorix.core.utils.hash import compute_hash, normalize_text
from src.services import heart_memory_backend as backend
from src.services import heart_memory_scope as scope


_edit_locks = defaultdict(asyncio.Lock)


def ensure_enabled():
    if not backend.settings()["plugin_enabled"]:
        raise ValueError("记忆插件未启用，请先在插件配置中开启")


def group_session(chat_id):
    session = scope.session_for({"chat_id": chat_id})
    if not session or not session.group_id or session.session_id != chat_id:
        raise ValueError("请选择已存在的真实群聊，不接受未知会话或私聊")
    return session


def candidate_list(chat_id="", status="pending", limit=50):
    """返回审核所需内容，不把内部模型请求、配置密钥等放进页面。"""
    inbox = backend.candidate_inbox()
    clauses, params = [], []
    if chat_id:
        clauses.append("chat=?")
        params.append(chat_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    with inbox.store.connect() as db:
        rows = db.execute("SELECT * FROM heart_candidates" + (" WHERE " + " AND ".join(clauses) if clauses else "")
                          + " ORDER BY id DESC LIMIT ?", (*params, min(100, max(1, limit)))).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload"])
        args, owner = payload["args"], payload["owner"]
        items.append({"id": row["id"], "chat_id": row["chat"], "status": row["status"],
                      "created": row["created"], "name": owner.get("name", "未命名用户"),
                      "text": args["text"], "evidence": owner.get("evidence", []),
                      "reason": payload.get("reason", "旧版候选未保存详细暂存原因")})
    return {"success": True, "items": items}


async def review_candidate(candidate_id, approve, expected_text, edited_text=None):
    ensure_enabled()
    inbox = backend.candidate_inbox()
    try:
        message = await inbox.review(candidate_id, approve, expected_text, edited_text)
    except BaseException as exc:
        with inbox.store.connect() as db:
            row = db.execute("SELECT chat FROM heart_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row:
            inbox.store.append("后台记忆管理", row[0], reviewer="WebUI管理员", status="候选审核未完成",
                               new_memory=edited_text or expected_text, reason=type(exc).__name__ + ": " + str(exc))
        raise
    with backend.candidate_inbox().store.connect() as db:
        row = db.execute("SELECT status FROM heart_candidates WHERE id=?", (candidate_id,)).fetchone()
    status = row[0] if row else "missing"
    return {"success": status in {"written", "skipped", "ignored", "conflict_pending"},
            "status": status, "message": message}


async def group_memories(chat_id, limit=50):
    group_session(chat_id)
    hits = await scope.scoped_group_person_facts(chat_id, limit)
    hits += await scope.scoped_paragraphs(chat_id, "", limit)
    items = []
    for hit in sorted(hits, key=lambda item: float(item.get("created_at") or 0), reverse=True)[:limit]:
        meta = metadata(hit)
        kind = "person_fact" if str(hit.get("source") or "").startswith("person_fact:") else "chat_summary"
        items.append({"hash": hit["hash"], "text": hit["content"], "type": kind,
                      "created": hit.get("created_at", 0), "name": meta.get("person_name", ""),
                      "editable": True})
    return {"success": True, "items": items}


async def current_group_record(chat_id, paragraph_hash):
    group_session(chat_id)
    kernel = await scope.kernel_for_read()
    row = kernel.metadata_store.get_paragraph(paragraph_hash)
    if not row or not visible(row, chat_id):
        raise ValueError("该记忆不属于所选群、已失效或无法核实来源")
    hits = kernel._get_search_hit_service()._filter_user_visible_hits([dict(row, metadata=metadata(row))])
    if not hits:
        raise ValueError("旧记忆已被更新或撤回，请刷新列表")
    if not str(row.get("source") or "").startswith(("person_fact:", "chat_summary:")):
        raise ValueError("此入口仅修改本群人物事实和摘要")
    return row


async def preview_group_edit(chat_id, paragraph_hash, expected_text, new_text, reason):
    """精确文本编辑不用 LLM 猜测目标；只创建原生计划，不更改实际记忆。"""
    ensure_enabled()
    row = await current_group_record(chat_id, paragraph_hash)
    text = normalize_text(new_text)
    if not text or len(text) > 2000 or text == row["content"]:
        raise ValueError("新内容须为1至2000字且与旧内容不同")
    if row["content"] != expected_text:
        raise ValueError("旧内容已经变化，请刷新后再编辑")
    meta = metadata(row)
    kernel = await scope.kernel_for_read()
    if kernel.metadata_store.get_paragraph(compute_hash(text)):
        raise ValueError("原生库已有相同正文；此入口不自动合并或覆盖其他记忆，请使用原生记忆检修")
    kind = "person_fact" if str(row["source"]).startswith("person_fact:") else "chat_summary"
    people = list(meta.get("person_ids") or [])
    if meta.get("person_id") and meta["person_id"] not in people:
        people.append(meta["person_id"])
    if kind == "person_fact" and len(people) != 1:
        raise ValueError("人物归属不唯一，不能用此入口改写")
    operations = [{"action": "mark_superseded", "target_type": "paragraph", "hash": paragraph_hash,
                   "reason": reason, "valid_to": None},
                  {"action": "ingest_text", "text": text, "source_type": kind, "chat_id": chat_id,
                   "person_ids": people, "participants": list(meta.get("participants") or []),
                   "tags": ["heart_admin_edit"], "relations": [], "valid_from": None, "reason": reason}]
    if kind == "person_fact":
        operations.append({"action": "refresh_person_profile", "person_id": people[0]})
    plan = {"scope": "memory", "chat_id": chat_id, "person_id": people[0] if kind == "person_fact" else "",
            "request_text": "管理员精确修正一条本群长期记忆", "confidence": 1.0,
            "operations": operations, "reason": reason}
    kernel = await scope.kernel_for_read()
    record = kernel.metadata_store.create_fuzzy_modify_plan(
        request_text=plan["request_text"], scope="memory", target_chat_id=chat_id,
        target_person_id=plan["person_id"], plan=plan, confidence=1.0,
        requested_by="heart.webui_admin", reason=reason,
        preview={"heart_admin_editor": True, "old_hash": paragraph_hash, "old_text": row["content"],
                 "old_fingerprint": fingerprint([row["content"], meta]), "new_text": text,
                 "plan_fingerprint": fingerprint(plan), "created": time.time(),
                 "chat_id": chat_id, "requires_confirmation": True})
    backend.AuditStore().append("后台记忆管理", chat_id, status="已生成修正预览，尚未修改",
                               old_memories=[row["content"]], new_memory=text, reason=reason,
                               reviewer="WebUI管理员")
    return {"success": True, "plan_id": record["plan_id"], "old_text": row["content"], "new_text": text,
            "message": "仅预览。确认后新内容进入原生库，旧内容标记过时并保留审计历史。"}


async def execute_group_edit(plan_id):
    try:
        return await _execute_group_edit(plan_id)
    except BaseException as exc:
        kernel = await scope.kernel_for_read()
        record = kernel.metadata_store.get_fuzzy_modify_plan(plan_id)
        preview = (record or {}).get("preview") or {}
        if preview.get("heart_admin_editor"):
            backend.AuditStore().append("后台记忆管理", preview["chat_id"], reviewer="WebUI管理员",
                status="修正中止，实际状态请结合原生操作记录核实", old_memories=[preview["old_text"]],
                new_memory=preview["new_text"], reason=type(exc).__name__ + ": " + str(exc))
        raise


async def _execute_group_edit(plan_id):
    ensure_enabled()
    # 与自动冲突更新共用人物锁；编辑同一群摘要则用本接口的会话锁。
    async with _edit_locks[plan_id]:
        native = backend.NativeBackend()
        record = await native.get_plan(plan_id)
        preview, plan = record.get("preview") or {}, record.get("plan") or {}
        if not preview.get("heart_admin_editor") or record.get("requested_by") != "heart.webui_admin":
            raise ValueError("此入口只能执行由群回忆编辑器生成的计划")
        if record.get("status") != "awaiting_confirmation":
            raise ValueError("计划已处理或正在执行，不能重复提交")
        if time.time() - preview.get("created", 0) > 600:
            raise ValueError("预览超过10分钟，请重新预览")
        if fingerprint(plan) != preview.get("plan_fingerprint"):
            raise ValueError("计划内容已变化，请重新预览")
        pid = str(plan.get("person_id") or "")
        lock = backend.guardian().locks[pid] if pid else _edit_locks["chat:" + preview["chat_id"]]
        async with lock:
            row = await current_group_record(preview["chat_id"], preview["old_hash"])
            if fingerprint([row["content"], metadata(row)]) != preview.get("old_fingerprint"):
                raise ValueError("旧记忆已变化，拒绝用过期预览覆盖")
            op = next(item for item in plan["operations"] if item["action"] == "ingest_text")
            kernel = await scope.kernel_for_read()
            if kernel.metadata_store.get_paragraph(compute_hash(normalize_text(op["text"]))):
                raise ValueError("预览后已有相同正文写入，请刷新；不自动覆盖或合并")
            token = backend.CONFIRMED_WRITE.set(True)
            try:
                prepared = await native.invoke("ingest_text", {
                    "external_id": f"{plan_id}:ingest:1", "source_type": op["source_type"],
                    "text": op["text"], "chat_id": op["chat_id"], "person_ids": op["person_ids"],
                    "participants": op["participants"], "tags": op["tags"], "respect_filter": True,
                    "group_id": str(group_session(op["chat_id"]).group_id),
                    "metadata": {**metadata(row), "external_id": f"{plan_id}:ingest:1",
                                 "heart_review": {"confirmed_by": "administrator", "original_text": row["content"]},
                                 "fact_claim": {"trust": "manual_confirmed", "authority": "manual",
                                                "stability": "stable", "profile_section": "stable_facts",
                                                "reason": "管理员修正，非本人确认"},
                                 "memory_change": {"change_id": plan_id, "change_type": "ingest_text",
                                                   "changed_by": "heart.webui_admin", "supersedes_hashes": [preview["old_hash"]]}}})
                if prepared.get("success") is False or not prepared.get("stored_ids"):
                    raise ValueError("未核实新内容存储成功，旧内容保持不变")
                result = await native.invoke("memory_correction_admin", {"action": "execute", "plan_id": plan_id,
                    "confirmed": True, "requested_by": "heart.webui_admin", "reason": plan["reason"]})
            finally:
                backend.CONFIRMED_WRITE.reset(token)
            changed = {item.get("hash") for item in (result.get("execution") or {}).get("superseded_targets", [])}
            success = result.get("success") is True and changed == {preview["old_hash"]}
            if success:
                # 情景记忆是原生段落的派生摘要，不能仅修改正文后永远保留旧派生结果。
                kernel.metadata_store.enqueue_episode_source_rebuild(str(row["source"]),
                    reason="heart_webui_memory_correction", debounce_seconds=0.0)
            backend.AuditStore().append("后台记忆管理", preview["chat_id"], reviewer="WebUI管理员",
                status="原生记忆已修正" if success else "更新未完整完成，需要后台核实；不会自动重试",
                old_memories=[preview["old_text"]], new_memory=preview["new_text"], reason=plan["reason"])
            return {"success": success, "message": "已更新原生长期记忆，旧内容已标记过时；关联情景记忆重建已排队。" if success
                    else "新内容已存，但未核实全部更新完成，请查看原生纠错历史和日志。", "plan_id": plan_id}
