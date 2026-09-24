"""面向日常查看的中文日志；内部编号、JSON、排序分数只保留在数据库。"""

import json
import re
from datetime import datetime


def plain(value, limit=600):
    if not isinstance(value, str):
        return ""
    text = re.sub(r"(?:[A-Za-z_]+:)?[0-9a-fA-F]{24,}", "[内部编号已省略]", value)
    text = " ".join(text.split())
    if text.startswith(("{", "[")):
        try:
            structured = json.loads(value)
        except ValueError:
            pass
        else:
            values = contents(structured)
            return "；".join(values)[:limit] if values else "结构化详情已保留后台"
    return text[:limit] + ("…（内容较长，已节选）" if len(text) > limit else "")


def contents(value):
    """只选取可读正文，不遍历 metadata 等内部字段。"""
    if isinstance(value, list):
        return [text for item in value for text in contents(item)]
    if not isinstance(value, dict):
        return []
    result = []
    for key in ("content", "text", "summary", "profile_text"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            result.append(plain(text))
    for key in ("hits", "facts", "claims", "paragraphs", "items"):
        if isinstance(value.get(key), list):
            result.extend(contents(value[key]))
    return list(dict.fromkeys(result))


def number(value):
    return f"{value:.1f}".removesuffix(".0") if isinstance(value, (int, float)) else "未知"


def session_label(data):
    """显示聊天流名称，避免把不同会话的心情值读成一次跳变。"""
    dialogue = data.get("dialogue") or []
    name = plain(data.get("session_name") or (dialogue[-1].get("name", "") if dialogue else ""), 80)
    if " / " in name:
        return "群聊 " + name.rsplit(" / ", 1)[0]
    return "与" + name + "的会话" if name else "名称未记录"


def render_event(event, data):
    kind = event["kind"]
    mood = data.get("mood") or {}
    dialogue = data.get("dialogue") or []
    names = list(dict.fromkeys(plain(m.get("name", ""), 80) for m in dialogue if m.get("name")))
    if not names and mood.get("trigger", {}).get("name"):
        names = [plain(mood["trigger"]["name"], 80)]
    timestamp = datetime.fromisoformat(event["time"]).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"#{event['seq']} 时间：{timestamp}｜{kind}",
             f"会话：{session_label(data)}（心情按会话分别计算）",
             f"人物：{'、'.join(names) or '后台操作（未明确关联人物）'}"]
    relation = data.get("relation", "")
    if dialogue and relation != "证据消息ID关联":
        lines.append("说明：以下人物和对话仅供上下文参考，不能确定为此次调用的触发来源。")
    if mood.get("value") is None:
        lines.append("心情：暂无可关联的记录")
    elif kind == "心情变化":
        lines.append(f"心情：{number(mood.get('before'))} → {number(mood['value'])} / 100")
    else:
        lines.append(f"心情：{number(mood['value'])} / 100")
    if kind == "心情变化":
        lines += ["调用：心情插件 · 已更新状态", "触发对话：" + plain(mood.get("trigger", {}).get("text", ""))]
        recovery = mood.get("recovery", 0)
        target = mood.get("recovery_target")
        elapsed = mood.get("recovery_elapsed_seconds")
        rate = mood.get("recovery_per_hour")
        if recovery and isinstance(target, (int, float)) and isinstance(elapsed, (int, float)) and isinstance(rate, (int, float)):
            hours = elapsed / 3600
            lines.append(f"数值变化原因：距上次结算约{number(hours)}小时，按每小时{number(rate)}分向当前恢复目标{number(target)}靠近，时间恢复{number(recovery)}分；与本条对话的AI判断分开计算。")
        elif recovery:
            lines.append(f"数值变化原因：时间恢复{number(recovery)}分；旧记录缺少恢复目标和间隔，不能从日志确定更具体的原因。")
        previous_target = mood.get("previous_recovery_target")
        if isinstance(previous_target, (int, float)) and isinstance(target, (int, float)) and previous_target != target:
            lines.append(f"配置变化：上次记录的恢复目标为{number(previous_target)}，本次为{number(target)}；本次把上次结算以来的间隔按新目标计算，无法确定配置具体何时改动。")
        if recovery:
            lines.append("说明：单条对话的变化上限只约束情绪刺激；时间恢复按经过时长另行结算。重启本身不会重置已有心情。")
        assessment = mood.get("assessment") or {}
        lines.append("判断方式：" + plain(assessment.get("method", "关键词")))
        lines.append("判断原因：" + plain(assessment.get("reason", mood.get("reason", ""))))
        lines.append(f"建议刺激：{number(mood.get('proposed_delta'))}；限幅/冷却后刺激：{number(mood.get('stimulus_delta'))}；时间恢复：{number(mood.get('recovery'))}；实际总变化：{number(mood.get('actual_delta'))}")
        if "confidence" in assessment:
            lines.append(f"AI把握程度：{number(assessment['confidence'] * 100)}%；AI原始建议：{number(assessment.get('model_delta'))}")
        if "multiplier" in assessment:
            lines.append(f"强度设置：倍率{number(assessment['multiplier'])}；倍率后建议{number(assessment.get('scaled_delta'))}；AI上限加{number(assessment.get('positive_limit'))}/减{number(assessment.get('negative_limit'))}")
        diagnostics = assessment.get("diagnostics") or {}
        if "response_chars" in diagnostics:
            lines.append(f"AI返回：正文{number(diagnostics['response_chars'])}字符；输出用量{number(diagnostics.get('output_units_used'))} / 预算{number(diagnostics.get('output_budget'))}（不是心情分数）")
    elif kind == "AI心情评估跳过":
        lines.append("调用：AI心情判断 · " + plain(data.get("reason", "未完成")))
    elif kind == "心情影响回复风格":
        band = {"low": "安静克制", "neutral": "平和自然", "high": "轻快温暖"}.get(data.get("band"), "当前设定")
        if data.get("style_name"):
            band = plain(data["style_name"], 30)
        lines.append(f"调用：心情插件 · 已使用“{band}”风格要求（实际效果看回复）")
    elif kind == "收到对话":
        lines += ["对话：" + plain(data.get("text", "")), "调用：对话记录 · 已记录，记忆结果如有产生会另记"]
    elif kind == "记忆操作结果":
        lines.extend(render_memory(data))
    elif kind == "记忆冲突确认":
        lines.append("调用：记忆冲突保护 · " + plain(data.get("status", "")))
        for text in data.get("old_memories", []):
            lines.append("旧记忆：" + plain(text))
        lines.append("新内容：" + plain(data.get("new_memory", "")))
        if data.get("reason"):
            lines.append("说明：" + plain(data["reason"]))
    elif kind == "模型请求中的记忆参考":
        refs = data.get("references") or []
        stage = "思考阶段" if data.get("stage") == "planner" else "回复阶段"
        lines.append(f"调用：记忆参考观察 · {stage} · {'发现记忆参考，不代表最终采用' if refs else '未发现可识别的记忆参考'}")
        for ref in refs[:3]:
            lines.append("参考内容：" + plain(ref.get("text", "")))
        if len(refs) > 3:
            lines.append(f"另有 {len(refs) - 3} 段参考，完整内容保留后台。")
    elif kind in ("生成回复（尚非送达）", "发送结果"):
        state = ("发送成功（不表示已读）" if data.get("sent") else "发送失败") if kind == "发送结果" else "回复已生成，尚非送达"
        lines += ["调用：回复观察 · " + state, "机器人回复：" + plain(data.get("response", ""))]
    else:
        lines.append("调用：已记录；详细参数保留后台")
    if kind == "记忆操作结果" and dialogue:
        lines.append("相关对话：" + "；".join(plain(m.get("text", ""), 200) for m in dialogue[-2:]))
    return "\n".join(lines)


def render_memory(data):
    outcome = data.get("outcome") or {}
    operation = data.get("operation", "")
    labels = {"search_memory": "检索记忆", "ingest_text": "存储人物事实/文本", "ingest_summary": "存储摘要",
              "get_person_profile": "读取人物画像", "memory_profile_admin": "人物画像操作",
              "maintain_memory": "记忆维护", "memory_delete_admin": "记忆删除操作",
              "memory_fact_admin": "人物事实操作", "memory_correction_admin": "记忆纠正操作",
              "enqueue_feedback_task": "安排记忆反馈检查"}
    title = "调用：记忆插件 · " + labels.get(operation, "后台记忆操作")
    if isinstance(data.get("duration_ms"), (int, float)):
        title += f" · 耗时 {data['duration_ms'] / 1000:.2f} 秒"
    lines = [title]
    result = outcome.get("result") or {}
    error = outcome.get("error") or (result.get("error") if isinstance(result, dict) else "")
    if error or "失败" in outcome.get("status", ""):
        reason = "等待超时" if "timeout" in str(error).lower() else "调用未成功，详细错误保留后台"
        lines.append("结果：未成功 · " + reason)
        if outcome.get("submitted_text"):
            lines.append("待写入内容：" + plain(outcome["submitted_text"]))
        if outcome.get("detail"):
            lines.append("说明：" + plain(outcome["detail"]))
        return lines
    if operation == "search_memory":
        hits = outcome.get("hits") or []
        lines += ["查询：" + plain(outcome.get("query", "")), f"记忆：检索返回 {len(hits)} 条候选（不代表最终采用）"]
        for index, hit in enumerate(hits[:5], 1):
            lines.append(f"  {index}. {plain(hit.get('content', ''))}")
        if len(hits) > 5:
            lines.append(f"  另有 {len(hits) - 5} 条候选，完整内容保留后台。")
    elif operation in {"ingest_text", "ingest_summary"}:
        stored, skipped = outcome.get("stored_ids") or [], outcome.get("skipped_ids") or []
        if stored:
            lines.append(f"结果：服务报告已存储 {len(stored)} 条（可能包含更新，不保证都是新增）")
        elif skipped:
            lines.append(f"结果：跳过 {len(skipped)} 条，本次未报告新存储")
        else:
            lines.append("结果：未确认存储成功")
        lines.append("记忆内容：" + plain(outcome.get("submitted_text", "")))
    else:
        lines.append("结果：服务已返回；具体效果以原生记忆系统为准")
        for text in contents(result)[:5]:
            lines.append("记忆内容：" + text)
    return lines


def render_status(detail, index):
    trigger = detail.get("trigger") or {}
    lines = [f"人物/聊天 {index}：{plain(trigger.get('name', '未命名聊天'), 80)}",
             f"心情：{number(detail.get('before'))} → {number(detail.get('value'))} / 100"]
    if detail.get("recovery"):
        lines.append(f"时间恢复：{number(detail['recovery'])}分，向当前目标{number(detail.get('recovery_target'))}靠近；本条对话刺激{number(detail.get('stimulus_delta'))}分")
    lines.extend(["对话判断原因：" + plain(detail.get("reason", "")),
                  "触发对话：" + plain(trigger.get("text", "")), ""])
    return lines
