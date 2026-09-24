"""只有一个持久化心情值；确定性规则方便解释和面试演示。"""

import json
import re

from heart_shared.readable import render_status
from heart_shared.storage import dumps


def appraise(text, rules):
    """返回变化和原文证据；不假装能识别所有反讽或真实心理状态。"""
    text = text.strip()
    if not text or text.startswith(("/", "!")):
        return 0.0, "命令/空消息不改变心情", []
    if any(word in text for word in ("假如", "假设", "如果", "举个例子", "他说", "她说", "有人说")):
        return 0.0, "可能是转述或假设，本版保守地不计分", []
    # 引号和代码中的字面内容不是用户直接对机器人的态度。
    visible = re.sub(r'```[\s\S]*?```|`[^`]*`|“[^”]*”|「[^」]*」|"[^"]*"', "", text)
    categories = [
        ("感谢/认可", rules.gratitude_terms, rules.gratitude_delta),
        ("直接贬低", rules.insult_terms, rules.insult_delta),
        ("需要共情", rules.distress_terms, rules.distress_delta),
    ]
    matches = []
    for label, terms, delta in categories:
        for term in terms:
            if not term:
                continue
            start = visible.find(term)
            if start < 0:
                continue
            prefix = visible[max(0, start - 4):start]
            if any(negation in prefix for negation in ("不", "没", "别", "并非")):
                continue
            matches.append({"category": label, "evidence": term, "delta": float(delta)})
            break  # 同一类重复说十遍，也只计一次。
    if not matches:
        return 0.0, "未匹配明确规则，保持当前状态（只应用时间恢复）", []
    if len({m["category"] for m in matches}) > 1:
        return 0.0, "同时出现不同情绪线索，本版不武断计分", matches
    return matches[0]["delta"], "命中可解释规则：" + matches[0]["category"], matches


class MoodEngine:
    def __init__(self, store):
        self.store = store

    def update(self, message, settings, rules, assessment=None):
        with self.store.transaction() as db:
            f = self.store.save_message(db, message)
            session, message_id = f["session"], f["message_id"]
            if not session or not message_id:
                return {"status": "缺少真实会话ID或消息ID，不建立推测状态"}
            previous = db.execute("SELECT detail FROM mood_processed WHERE session=? AND message_id=?",
                                  (session, message_id)).fetchone()
            if previous:
                return json.loads(previous[0])  # 重试、重复投递不会再扣分/加分。
            now = self.store.clock().timestamp()
            state = db.execute("SELECT * FROM moods WHERE session=?", (session,)).fetchone()
            before = float(state["value"]) if state else settings.baseline
            elapsed = max(0, now - float(state["updated"])) if state else 0
            previous_detail = json.loads(state["detail"]) if state else {}
            previous_target = previous_detail.get("recovery_target")
            distance = settings.baseline - before
            recovered = min(abs(distance), elapsed / 3600 * settings.recovery_per_hour)
            decay = recovered if distance > 0 else -recovered
            delta, reason, evidence = appraise(f["text"], rules)
            if assessment is not None:
                delta, reason, evidence = assessment["delta"], assessment["reason"], assessment.get("evidence", [])
            proposed_delta = delta
            if message.get("is_notify") or message.get("is_command"):
                delta, reason, evidence = 0.0, "通知/命令不计分", []
            if delta:
                recent = db.execute("""SELECT detail FROM mood_processed
                    WHERE session=? AND json_extract(detail,'$.trigger.user')=?
                    AND json_extract(detail,'$.stimulus_delta') != 0
                    ORDER BY rowid DESC LIMIT 1""", (session, f["user"])).fetchone()
                if recent and now - json.loads(recent[0])["timestamp"] < settings.cooldown_seconds:
                    delta, reason = 0.0, "同一用户仍在心情刺激冷却期，避免刷屏反复改变数值"
            delta = max(-settings.max_delta, min(settings.max_delta, delta))
            value = round(max(0, min(100, before + decay + delta)), 4)
            detail = {"status": "持久化心情", "before": before, "recovery": round(decay, 4),
                      "recovery_target": settings.baseline,
                      "recovery_per_hour": settings.recovery_per_hour,
                      "recovery_elapsed_seconds": round(elapsed, 1),
                      "previous_recovery_target": previous_target,
                      "stimulus_delta": delta, "actual_delta": round(value - before, 4), "value": value,
                      "reason": reason, "evidence": evidence, "timestamp": now,
                      "assessment": assessment or {"method": "关键词", "reason": reason},
                      "proposed_delta": proposed_delta,
                      "trigger": {"message_id": message_id, "user": f["user"], "name": f["name"], "text": f["text"]}}
            db.execute("INSERT INTO moods VALUES(?,?,?,?) ON CONFLICT(session) DO UPDATE SET value=excluded.value,updated=excluded.updated,detail=excluded.detail",
                       (session, value, now, dumps(detail)))
            db.execute("INSERT INTO mood_processed VALUES(?,?,?)", (session, message_id, dumps(detail)))
            self.store.append_in(db, "心情变化", session, {"message_id": message_id, "mood": detail})
            self.export_status(db)
            return detail

    def export_status(self, db):
        lines = ["角色的当前心情（0～100，每个会话独立；不是用户的心理评分）", "刷新方式：重新打开此文件。时间恢复在收到下一条消息时计算。", ""]
        for index, row in enumerate(db.execute("SELECT * FROM moods ORDER BY session"), 1):
            detail = json.loads(row["detail"])
            lines += render_status(detail, index)
        target = self.store.root / "当前心情.txt"
        temporary = target.with_suffix(".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8-sig")
        temporary.replace(target)

    def style(self, session, prompts, states=None):
        with self.store.connect() as db:
            state = self.store.mood_snapshot(db, session)
        value = state.get("value")
        if value is None:
            return "", state, "未初始化，不注入"
        band = "low" if value < prompts.low_below else "high" if value >= prompts.high_at_least else "neutral"
        style = {"low": prompts.low, "neutral": prompts.neutral, "high": prompts.high}[band]
        if states is not None and states.enabled:
            selected = next(item for item in states.entries if item.minimum <= value < item.maximum
                            or value == 100 and item.maximum == 100)
            band, style = selected.name, selected.style
        text = f"【角色心情风格】\n当前角色心情值：{value}/100，档位：{band}。\n{prompts.safety}\n{style}"
        return text, state, band
