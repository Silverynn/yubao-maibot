"""从原始事件生成消息卡片。只改变展示，不推测触发者或修改机器人状态。"""

from collections import Counter, OrderedDict
from datetime import datetime
from typing import Any, Dict, List

from .readable import number, plain, render_event


SEPARATOR = "-" * 72
FORMAT_VERSION = 2
CATEGORIES = ("记忆", "心情与临时情绪", "回复与发送", "表情与动作", "其他处理")


def evidence_ids(data: Dict[str, Any]) -> List[str]:
    """显式证据优先；绝不依据最近消息、相似文本或心情快照猜测归属。"""
    if data.get("message_id"):
        return [str(data["message_id"])]
    return list(dict.fromkeys(str(item) for item in data.get("evidence_message_ids", []) if item))


def category(kind: str) -> str:
    if "记忆" in kind or kind == "摘要去重判断":
        return "记忆"
    if "心情" in kind or "临时情绪" in kind:
        return "心情与临时情绪"
    if kind in {"生成回复（尚非送达）", "发送结果"}:
        return "回复与发送"
    if "表情" in kind or kind == "Live2D动作":
        return "表情与动作"
    return "其他处理"


def group_events(events: List[Dict[str, Any]], messages: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一消息归组；多消息任务单独归组，不能给每个人重复计算一次心情。"""
    groups: Dict[Any, Dict[str, Any]] = OrderedDict()
    for event in events:
        data = event["data"]
        ids = evidence_ids(data)
        if len(ids) == 1:
            key = ("message", ids[0])
            source = messages.get(ids[0])
            day = source["received"][:10] if source else event["day"]
            title = "消息" if source else "消息关联待核实"
        elif len(ids) > 1:
            key = ("batch", tuple(sorted(ids)))
            day, title = event["day"], "多条消息的合并处理"
        else:
            # 只有明确的任务编号才合并后台生命周期；没有编号就保持独立，不硬凑。
            task = data.get("task_id") or data.get("audit_task_id")
            key = ("task", str(task)) if task else ("event", event["seq"])
            day, title = event["day"], "后台任务"
        if key not in groups:
            groups[key] = {"day": day, "title": title, "ids": ids, "events": [], "key": key}
        groups[key]["events"].append(event)
    return list(groups.values())


def recall_hits(event: Dict[str, Any]) -> List[Any]:
    data = event['data']
    if event['kind'] == '群聊记忆检索':
        return data.get('hits') or []
    if event['kind'] == '记忆操作结果' and data.get('operation') in {'search_memory', 'heart_scoped_list'}:
        return (data.get('outcome') or {}).get('hits') or []
    return []


def recall_lines(event: Dict[str, Any], repeat: bool = False) -> List[str]:
    data = event['data']
    outcome = data.get('outcome') or {}
    hits = recall_hits(event)
    view = data.get('_card_recall_view') or {}
    states = view.get('states') or []
    lines = ['历史检索快照：这是此时间当时返回的结果，不是当前长期记忆清单。',
             f'当时命中：{len(hits)} 条候选（不代表回复实际采用，更不是新增记忆）。',
             '调用：' + ('当前群聊记忆检索' if event['kind'] == '群聊记忆检索' else '原生记忆检索'),
             '查询：' + plain(data.get('query') or outcome.get('query') or '')]
    if outcome.get('error') or '失败' in str(outcome.get('status', '')):
        lines.append('当时结果：检索未成功，详细错误保留审计明细。')
    if data.get('status'):
        lines.append('当时结果：' + plain(data['status']))
    if data.get('target_name'):
        lines.append('当时查看对象：@' + plain(data['target_name'], 80))
    if outcome.get('scope_note'):
        lines.append('当时可见范围：' + plain(outcome['scope_note']))
    if not view.get('available'):
        lines.append('当前状态：未完成原生库只读核对，不能据此判断仍存在或已删除。')
    else:
        lines.append('当前状态旁注：以本文件最近生成时的只读核对为准，与当时的检索状态分开。')
    folded = [i for i, state in enumerate(states) if state.get('fold')]
    if folded:
        lines.append(f'现已清理/失效：{len(folded)} 条历史候选，正文已折叠；原样保留在旧明细和审计数据库。')
    if repeat:
        lines.append('命中正文与本卡片前面的检索项完全相同，此处不重复展开；操作时间与结果仍分别记录。')
        return lines
    shown = 0
    for index, hit in enumerate(hits):
        state = states[index] if index < len(states) else {'label':'当前状态未核实', 'fold':False}
        if state.get('fold'):
            continue
        if shown >= 5:
            lines.append('其余未折叠候选的完整内容保留审计明细。')
            break
        text = hit.get('content', '') if isinstance(hit, dict) else str(hit)
        meta = hit.get('metadata') or {} if isinstance(hit, dict) else {}
        source = meta.get('source_type', '') if isinstance(meta, dict) else ''
        types = data.get('hit_types') or []
        label = {'chat_summary':'聊天摘要', 'person_fact':'人物事实'}.get(source, types[index] if index < len(types) else '记忆')
        lines.append(f'历史候选 {index + 1}：[{label}] ' + plain(text))
        lines.append('  当前核对：' + state['label'])
        shown += 1
    if folded:
        sample = hits[folded[0]]
        text = sample.get('content', '') if isinstance(sample, dict) else str(sample)
        lines.append('已清理历史内容示例（仅一条，不代表现在仍记得）：' + plain(text, 220))
    return lines


def detail_lines(event: Dict[str, Any], repeat_recall: bool = False) -> List[str]:
    """复用已有真实结果解释，去掉重复抬头和仅供排查的技术指标。"""
    if event['kind'] == '群聊记忆检索' or (event['kind'] == '记忆操作结果' and event['data'].get('operation') in {'search_memory', 'heart_scoped_list'}):
        return recall_lines(event, repeat_recall)
    text = render_event(event, event["data"])
    excluded = ("#", "会话：", "人物：", "心情：", "临时情绪：", "相关对话：",
                "触发对话：", "本批发言：", "AI返回：", "强度设置：", "AI把握程度：",
                "向量相似度：", "AI判断把握：")
    lines = [line for line in text.splitlines() if not line.strip().startswith(excluded)]
    if event["kind"] == "心情变化":
        mood = event["data"].get("mood") or {}
        lines.insert(0, f"心情值：{number(mood.get('before'))} → {number(mood.get('value'))} / 100")
    if event["kind"] in {"临时情绪产生", "临时情绪变化", "临时情绪结束", "临时情绪判断"}:
        emotion = event["data"].get("emotion") or {}
        old = event["data"].get("previous_emotion") or {}
        before = plain(old.get("label", "")) if old.get("active") else "无"
        after = plain(emotion.get("label", "")) if emotion.get("active") else "无"
        lines.insert(0, f"临时情绪：{before} → {after}" if old else f"当前临时情绪：{after}")
    return lines


def card_status(group: Dict[str, Any]) -> str:
    events = group["events"]
    finished = [e for e in events if e["data"].get("processing_finished") is True]
    if finished:
        return "已结束（已收到明确的处理结束记录）"
    sends = [e for e in events if e["kind"] == "发送结果"]
    if sends:
        return ("回复已发送；后台结果持续补齐（不代表全部处理结束）" if any(e["data"].get("sent") for e in sends)
                else "发送失败；其他后台结果仍可能补齐")
    if group["title"] == "后台任务":
        return "已记录本次操作；关联后续结果会补齐，未关联的任务另记"
    return "处理中（尚未观测到发送或明确结束结果；也可能本轮无需回复）"


def render_cards(day: str, info: Dict[str, Any], events: List[Dict[str, Any]],
                 messages: Dict[str, Dict[str, Any]]) -> str:
    groups = group_events(events, messages)
    chosen = [g for g in groups if g["day"] == day]
    selected_messages = [m for m in messages.values() if m["received"][:10] == day]
    lines = [f"{day}｜{info['channel']}｜{info['chat_type']}｜{plain(info['title'], 80)}",
             f"会话开始：{info['started']}；当天用户消息：{len(selected_messages)} 条；处理卡片：{len(chosen)} 个",
             "阅读说明：按消息归组，卡片内分模块显示并标注实际操作时间；迟到结果会更新原卡片。",
             "未明确关联的后台操作单独记录；检索候选不等于回复采用，生成回复不等于发送成功。",
             "记忆检索为操作发生时的历史快照，不是当前记忆清单；有明确清理凭证的旧候选会折叠显示。", ""]
    message_groups = {g["ids"][0]: g for g in groups if g["key"][0] == "message"}
    batch_groups = [g for g in groups if g["key"][0] == "batch"]
    card_numbers = {g["key"]: i for i, g in enumerate(chosen, 1)}
    batch_refs: Dict[str, List[str]] = {}
    for g in batch_groups:
        if g["key"] in card_numbers:
            for mid in g["ids"]:
                batch_refs.setdefault(mid, []).append(str(card_numbers[g["key"]]))
    for index, group in enumerate(chosen, 1):
        first, last = group["events"][0], group["events"][-1]
        lines += [SEPARATOR, f"【卡片 {index}｜{group['title']}】{first['time'][:19].replace('T', ' ')}",
                  "状态：" + card_status(group)]
        mood_event = next((e for e in reversed(group['events']) if e['kind'] == '心情变化'), last)
        mood = mood_event['data'].get('mood') or {}
        lines.append('心情快照：' + (f"{number(mood['value'])} / 100" if mood.get('value') is not None else '尚未观测到'))
        emotion = last['data'].get('emotion') or {}
        lines.append('临时情绪快照：' + (plain(emotion.get('label', '')) if emotion.get('active') else '无已记录的临时情绪'))
        if group["ids"]:
            for mid in group["ids"]:
                msg = messages.get(mid)
                if msg:
                    lines += ["人物：" + plain(msg["name"], 100), "用户说：" + plain(msg["text"], 1600)]
                else:
                    lines.append("人物/原文：未观测到对应的原始消息，不猜测发言者")
        else:
            lines.append("来源：后台操作，未提供可核实的触发消息（不能认作最近发言者触发）")
        if group["key"][0] == "batch":
            refs = [str(card_numbers[message_groups[mid]["key"]]) for mid in group["ids"]
                    if mid in message_groups and message_groups[mid]["key"] in card_numbers]
            lines.append("关联消息卡片：" + ("、".join(refs) if refs else "原消息在其他日期或未观测到"))
            lines.append("说明：这一组消息共同产生一次处理，不代表每个人各自引起一次变化。")
        elif group["key"][0] == "message":
            refs = batch_refs.get(group["ids"][0], [])
            if refs:
                lines.append("参与合并处理：卡片 " + "、".join(refs) + "（结果只记录一次）")
        source = next((messages[mid] for mid in group["ids"] if mid in messages), None)
        started = source["received"] if source and group["key"][0] == "message" else first["time"]
        elapsed = max(0, (datetime.fromisoformat(last["time"]) - datetime.fromisoformat(started)).total_seconds())
        lines.append(f"已观测处理跨度：{elapsed:.1f} 秒（不是模型耗时，也不是全部任务完成耗时）")
        operations = [e for e in group["events"] if e["kind"] != "收到对话"]
        seen_recall = set()
        for label in CATEGORIES:
            selected = [e for e in operations if category(e["kind"]) == label]
            if not selected:
                if label == "记忆" and group["key"][0] == "message":
                    lines.append("记忆：尚未观测到关联操作（不等于未使用记忆）")
                continue
            lines.append(label + "：")
            for event in selected:
                stamp = event["time"][:19].replace("T", " ")
                lines.append(f"  · {stamp}｜{event['kind']}")
                signature = tuple(hit.get('content', '') if isinstance(hit, dict) else str(hit) for hit in recall_hits(event))
                repeat = bool(signature) and signature in seen_recall
                if signature:
                    seen_recall.add(signature)
                lines.extend("    " + line for line in detail_lines(event, repeat))
        lines += [SEPARATOR, ""]
    return "\n".join(lines) + "\n"


def summary_counts(events: List[Dict[str, Any]]) -> Counter:
    """概览统计观察到的操作，不把存储ID数误称为新增记忆数。"""
    counts: Counter = Counter()
    for event in events:
        kind, data = event["kind"], event["data"]
        counts["原始事件"] += 1
        if kind == "收到对话":
            counts["用户消息"] += 1
        if kind in {"记忆去重", "摘要去重判断"} and any(
                word in str(data.get("status", "")) for word in ("重复", "同义", "无新增")):
            counts["重复跳过操作"] += 1
        if kind == "记忆冲突确认":
            counts["冲突确认操作"] += 1
        if kind == "候选记忆":
            counts["候选处理操作"] += 1
        if kind == "忘记记忆":
            counts["忘记处理操作"] += 1
        if kind == "心情变化":
            counts["心情结算"] += 1
        if kind == "临时情绪产生":
            counts["临时情绪产生"] += 1
        if kind == "临时情绪结束":
            counts["临时情绪结束"] += 1
        if kind == "发送结果":
            counts["发送成功操作" if data.get("sent") else "发送失败操作"] += 1
        outcome = data.get("outcome") or {}
        if kind == "记忆操作结果":
            if data.get("operation") in {"search_memory", "heart_scoped_list"}:
                counts["记忆检索操作"] += 1
            if data.get("operation") in {"ingest_text", "ingest_summary"}:
                if outcome.get("stored_contents") or outcome.get("stored_ids"):
                    counts["报告存储成功操作（含更新）"] += 1
        error = data.get("error") or outcome.get("error")
        status = str(data.get("status") or outcome.get("status") or "")
        if error or "失败" in status or (kind == "发送结果" and not data.get("sent")):
            counts["失败或异常操作"] += 1
        assessment = (data.get('mood') or {}).get('assessment') or {}
        code = str(assessment.get('error_code') or '')
        diagnostic = ' '.join(str(value or '') for value in (
            error, status, data.get('reason'), outcome.get('detail'), assessment.get('reason'), code))
        if code and code != 'busy_pause' and not error and '失败' not in status:
            counts['失败或异常操作'] += 1
        if "超时" in diagnostic or "timeout" in diagnostic.lower():
            counts["包含超时说明的操作"] += 1
    return counts


def render_overview(day: str, generated: str, sessions: List[Dict[str, Any]]) -> str:
    total: Counter = Counter()
    for item in sessions:
        total.update(item["counts"])
    lines = [f"每日概览｜{day}", "生成时间：" + generated,
             "生成规则：次日或之后首次启动插件时补生成；本文件不随三天日志清理。",
             "统计口径：按事件实际发生日期；存储成功可能包含更新，不等于新增记忆条数。",
             "这是当时可观测结果的快照，不代表每条消息的所有异步任务均已结束。", "",
             f"会话数量：{len(sessions)}", "当天总计："]
    metrics = ('用户消息', '记忆检索操作', '报告存储成功操作（含更新）', '重复跳过操作', '候选处理操作',
               '冲突确认操作', '忘记处理操作', '心情结算', '临时情绪产生', '临时情绪结束',
               '发送成功操作', '发送失败操作', '失败或异常操作', '包含超时说明的操作', '原始事件')
    lines.extend(f"  {key}：{total[key]}" for key in metrics)
    if not total:
        lines.append("  当天未观测到事件（不代表程序当天在运行，也不代表其他未观测渠道无人聊天）")
    for item in sessions:
        lines += ["", SEPARATOR, f"{item['channel']}｜{item['chat_type']}｜{plain(item['title'], 80)}",
                  "会话开始：" + item["started"]]
        lines.extend(f"  {key}：{value}" for key, value in item["counts"].items())
        lines.append(SEPARATOR)
    return "\n".join(lines) + "\n"
