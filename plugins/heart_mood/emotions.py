"""话题情绪与长期心情分开存储：不计时消失、不自动按分类扣分。"""

import json
import uuid

from heart_shared.storage import dumps


def apply_emotion(store, db, session, message_id, assessment, settings):
    old = store.emotion_snapshot(db, session)
    batch_ids = (assessment or {}).get('batch_message_ids') or []
    source = {'evidence_message_ids':batch_ids} if batch_ids else {'message_id':message_id}
    if not settings.enabled:
        decision = {"action": "clear", "reason": "管理员关闭话题情绪功能"}
    else:
        decision = (assessment or {}).get("emotion")
    if not decision:
        if settings.enabled:
            store.append_in(db, "临时情绪判断", session, {**source,
                "status": "保留原状态", "reason": (assessment or {}).get("emotion_error") or "本轮未获得有效的情绪/话题判断"})
        return old
    action, reason = decision["action"], decision["reason"]
    if action == "keep":
        store.append_in(db, "临时情绪判断", session, {**source,
            "status": "延续原状态", "reason": reason, "emotion": old})
        return old
    if action == "clear":
        if not old.get("active"):
            store.append_in(db, "临时情绪判断", session, {**source,
                "status": "当前无临时情绪", "reason": reason})
            return old
        state = {"active": False, "revision": uuid.uuid4().hex,
                 "reason": reason, "updated": store.clock().timestamp(), "message_id": message_id}
        kind = "临时情绪结束"
    else:
        choice = next((e for e in settings.entries if e.label == decision["label"]), None)
        if choice is None:
            return old  # 配置变更后不接收已经被移除的分类。
        state = {**decision, "active": True, "revision": uuid.uuid4().hex,
                 "expression": choice.expression, "style": choice.style,
                 "updated": store.clock().timestamp(), "message_id": message_id}
        kind = "临时情绪变化" if old.get("active") else "临时情绪产生"
        if old.get("active") and old.get("topic") != decision["topic"]:
            store.append_in(db, "临时情绪结束", session, {**source,
                "emotion": {"active": False}, "previous_emotion": old,
                "reason": "AI判断已转向新话题：" + reason, "status": "原话题情绪被新话题替换"})
            kind = "临时情绪产生"
    state['evidence_message_ids'] = batch_ids or [message_id]
    db.execute("INSERT INTO emotions VALUES(?,?) ON CONFLICT(session) DO UPDATE SET detail=excluded.detail",
               (session, dumps(state)))
    store.append_in(db, kind, session, {**source, "emotion": state,
        "previous_emotion": old, "reason": reason, "status": "已保存"})
    return state


def publish_view(store, db, session, config, assessment=None, message_id=''):
    """一个只含展示状态的快照；不暴露记忆、提示词或其他会话的发言。"""
    mood = store.mood_snapshot(db, session)
    value = mood.get("value")
    if value is None:
        return
    emotion = store.emotion_snapshot(db, session)
    band = "低落" if value < config.prompts.low_below else "开心" if value >= config.prompts.high_at_least else "平静"
    if config.states.enabled:
        band = next(e.name for e in config.states.entries if e.minimum <= value < e.maximum or value == 100 and e.maximum == 100)
    base = next((e.expression for e in config.avatar.entries if e.state == band), "平静")
    active = bool(config.emotions.enabled and emotion.get("active"))
    choice = next((e for e in config.emotions.entries if e.label == emotion.get("label")), None)
    if active and choice is None:
        active = False
    view = {"enabled": config.plugin.enabled, "value": value, "band": band,
            "label": emotion.get("label", "") if active else "无",
            "intensity": emotion.get("intensity", 0) if active else 0,
            "topic": emotion.get("topic", "") if active else "",
            "reason": emotion.get("reason", "") if active else "当前没有临时情绪",
            "active": active, "expression": choice.expression if active else base,
            "emotion_revision": emotion.get("revision", ""),
            "message_id": emotion.get("message_id", "") if active else mood.get("trigger", {}).get("message_id", ""),
            "updated": store.clock().isoformat(timespec="seconds")}
    previous = db.execute("SELECT detail FROM avatar_views WHERE session=?", (session,)).fetchone()
    old = json.loads(previous[0]) if previous else {}
    batch_ids = (assessment or {}).get('batch_message_ids') or []
    event_source = {'evidence_message_ids':batch_ids} if batch_ids else {'message_id':message_id}
    if config.plugin.enabled and config.emotions.enabled and config.emotions.allow_expression_requests:
        if old.get('performance'):
            view['performance'] = old['performance']
        decision = (assessment or {}).get('display')
        source = db.execute('SELECT channel FROM session_sources WHERE session=?', (session,)).fetchone()
        if decision and decision['action'] != 'none':
            choice = next((e for e in config.emotions.entries if e.label == decision['label']), None)
            if source and source[0] == 'Live2D' and (choice or decision['action'] == 'auto'):
                view['performance'] = {'id':uuid.uuid4().hex, 'action':decision['action'],
                    'expression':choice.expression if choice else '', 'label':decision['label'],
                    'reason':decision['reason'], 'message_id':message_id,
                    'expires':store.clock().timestamp()+config.emotions.expression_request_seconds}
                store.append_in(db, '表情表演请求', session, {**event_source,
                    'status':'已生成展示请求，等待前端执行', 'expression':choice.expression if choice else '恢复自动',
                    'reason':decision['reason'], 'seconds':config.emotions.expression_request_seconds})
            else:
                store.append_in(db, '表情表演请求', session, {**event_source,
                    'status':'当前不是Live2D会话或分类已移除，不发送形象动作', 'reason':decision['reason']})
        elif assessment is not None and source and source[0] == 'Live2D':
            store.append_in(db, '表情请求判断', session, {**event_source,
                'status':'不新增表演动作', 'reason':(assessment or {}).get('display_error') or (decision or {}).get('reason', '本轮没有有效的表演请求判断')})
    # 同一份有效状态不产生新的指令版本，网页每秒轮询不会重复表演/刷日志。
    compare = lambda d: {k: v for k, v in d.items() if k not in {"revision", "updated"}}
    view["revision"] = old.get("revision") if compare(old) == compare(view) else uuid.uuid4().hex
    db.execute("INSERT INTO avatar_views VALUES(?,?) ON CONFLICT(session) DO UPDATE SET detail=excluded.detail",
               (session, dumps(view)))
    return view
