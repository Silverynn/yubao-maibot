"""同机 Heart 展示桥：只读本地 Live2D 会话状态，写入有回执的动作审计。

不提供任意 SQL/会话查询，不读取 QQ 私聊。只允许本机同源页面访问。
"""

import json
import importlib.util
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from expression_catalog import model_expression_names


_active_bridge = None
MANUAL_SECONDS = 8
EMOTION_KEYS = {
    '开心': 'joy', '兴奋': 'joy', '生气': 'anger', '愤怒': 'anger',
    '难过': 'sadness', '低落': 'sadness', '疑惑': 'confusion',
    '困惑': 'confusion', '惊讶': 'surprise', '害羞': 'blush',
    '平静': 'neutral', '无': 'neutral',
}


def request_manual_expression(expression):
    """由本地/表情命令调用；None表示恢复自动，不触及心情数据库。"""
    if _active_bridge is None:
        raise RuntimeError("心情表情接口尚未初始化，请重启Live2D")
    _active_bridge.manual(expression)


def enabled():
    return bool(os.environ.get("HEART_MAIBOT_ROOT"))


def load_store():
    root = Path(os.environ["HEART_MAIBOT_ROOT"]).resolve()
    if not (root / "heart_shared/storage.py").is_file():
        raise RuntimeError("请先安装 Heart 插件；找不到共用审计模块")
    # 两个项目都叫 src，不能把 MaiBot 整个根目录塞进模块搜索路径。
    # 只加载独立共用包，避免覆盖 Open-LLM-VTuber 的 src 命名空间。
    if 'heart_shared' not in sys.modules:
        spec = importlib.util.spec_from_file_location('heart_shared', root / 'heart_shared/__init__.py',
                                                    submodule_search_locations=[str(root/'heart_shared')])
        module = importlib.util.module_from_spec(spec)
        sys.modules['heart_shared'] = module
        spec.loader.exec_module(module)
    from heart_shared.storage import AuditStore
    return AuditStore(root / "data/heart_observation")


class AvatarReceipt(BaseModel):
    command_id: str = Field(min_length=1, max_length=64)
    client_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    status: str = Field(pattern=r"^(applied|failed|unavailable)$")


class HeartBridge:
    def __init__(self, store, allowed_expressions=None, clock=time.monotonic, model_provider=None):
        self.store = store
        # allowed_expressions is retained for old tests/clients. Production reads
        # the currently loaded Open-LLM-VTuber model on every state request.
        self.allowed = set(allowed_expressions or ())
        self.model_provider = model_provider
        self.csrf = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.commands = {}
        self.current_key = None
        self.current_command = None
        self.clock = clock
        self.override = None
        self.manual_used = False
        self.ignored_performance = None
        self.seen_performance = None
        self.current_user_id = None

    def _catalog(self):
        model = self.model_provider() if self.model_provider else None
        mapping = (getattr(model, 'emo_map', None) or
                   (getattr(model, 'model_info', {}) or {}).get('emotionMap') or {}) if model else {}
        name = (getattr(model, 'live2d_model_name', '') or
                (getattr(model, 'model_info', {}) or {}).get('name', '')) if model else ''
        return str(name), {str(k).lower(): v for k, v in mapping.items()} if mapping else {}

    def _names(self):
        return model_expression_names(self.model_provider()) if self.model_provider else ()

    def _resolve(self, requested, label='', band=''):
        name, mapping = self._catalog()
        if mapping:
            values = list(mapping.values())
            if requested in values or requested in self._names():
                return requested, True, name
            # An unsupported *temporary* emotion is neutral, not a jump to
            # the unrelated long-term mood band (e.g. 80-point confusion).
            candidates = (requested, label) if label and label != '无' else (requested, band)
            for item in candidates:
                key = EMOTION_KEYS.get(str(item), str(item).lower())
                if key in mapping:
                    return mapping[key], True, name
            # An unsupported mood category must not stop the panel or an
            # otherwise valid model. Use its own neutral/first expression.
            return mapping.get('neutral', values[0]), True, name
        return requested, requested in self.allowed or requested in self._names(), name

    def expressions(self):
        name, mapping = self._catalog()
        return {'model': name, 'expressions': mapping, 'model_expression_names': self._names()}

    def begin_visit(self, client_uid):
        """A fresh browser WebSocket starts a fresh visible chat and topic emotion."""
        new_user_id = 'webui_user_vtuber_visit_' + str(client_uid).replace('-', '').lower()
        with self.lock, self.store.connect() as db:
            if self.current_user_id == new_user_id:
                return
            # Only Live2D states are touched. QQ and unrelated WebUI users stay intact.
            previous = db.execute("""SELECT src.session FROM session_sources src
                JOIN emotions e ON e.session=src.session
                WHERE src.channel='Live2D' AND src.platform='webui' AND src.user_id!=?
                AND json_extract(e.detail,'$.active')=1""",
                (new_user_id,)).fetchall()
            for row in previous:
                old_session = row['session']
                old = self.store.emotion_snapshot(db, old_session)
                if old.get('active'):
                    ended = {**old, 'active':False, 'label':'无', 'intensity':0,
                             'topic':'', 'reason':'Live2D 网页重新进入，结束上一轮临时情绪'}
                    db.execute("INSERT INTO emotions VALUES(?,?) ON CONFLICT(session) DO UPDATE SET detail=excluded.detail",
                               (old_session, json.dumps(ended, ensure_ascii=False)))
                    self.store.append_in(db, '临时情绪结束', old_session, {
                        'previous_emotion':old, 'emotion':ended,
                        'reason':'Live2D 网页重新进入，结束上一轮临时情绪'})
            self.current_user_id = new_user_id
            self.current_key = self.current_command = None
            self.override = None
            self.manual_used = False
            self.ignored_performance = self.seen_performance = None
            self.commands.clear()

    def _context(self, db):
        # 只能关联原生登记的网页身份，不能把QQ会话或猜测的hash当来源。
        if not self.current_user_id:
            # Compatibility with the pre-visit bridge and its offline tests.
            rows = db.execute("""SELECT s.session,s.title,v.detail FROM session_sources src
                JOIN sessions s ON s.session=src.session LEFT JOIN avatar_views v ON v.session=s.session
                WHERE src.channel='Live2D' AND src.platform='webui'
                AND src.user_id IN ('vtuber_local_user','webui_user_vtuber_local_user')
                AND s.chat_type='私聊'""").fetchall()
        else:
            rows = db.execute("""SELECT s.session,s.title,v.detail FROM session_sources src
            JOIN sessions s ON s.session=src.session LEFT JOIN avatar_views v ON v.session=s.session
            WHERE src.channel='Live2D' AND src.platform='webui'
            AND src.user_id=? AND s.chat_type='私聊'""", (self.current_user_id,)).fetchall()
        if len(rows) == 1 and rows[0]['detail']:
            return rows[0]['session'], rows[0]['title'], json.loads(rows[0]['detail']), ''
        notice = '等待 MaiBot 处理首次 Live2D 对话' if len(rows) <= 1 else '存在多个Live2D身份，暂不猜测会话归属'
        # 未建立聊天也允许测试外观，但绝不伪造0分或某个人的真实心情。
        return '', '本机网页（尚未关联会话）', {
            'revision':'no-session', 'enabled':True, 'value':None, 'band':'尚无心情记录',
            'active':False, 'label':'无', 'intensity':0, 'topic':'', 'updated':'',
            'expression':'平静', 'reason':notice}, notice

    def manual(self, expression):
        with self.lock, self.store.connect() as db:
            _, mapping = self._catalog()
            permitted = (expression in mapping.values() if mapping else expression in self.allowed) or expression in self._names()
            if expression is not None and not permitted:
                raise ValueError('模型没有这个表情：' + str(expression))
            session, _, view, _ = self._context(db)
            self.ignored_performance = view.get('performance', {}).get('id')
            command = secrets.token_hex(16)
            reason = '用户要求恢复自动表情，不修改真实心情' if expression is None else f'用户手动请求 {expression}；临时展示约{MANUAL_SECONDS}秒，不修改真实心情'
            self._record(command, 'manual', 'server', '已接受手动表情请求', session,
                         {**view, 'visual_reason':reason}, expression if expression is not None else '恢复自动')
            self.override = None if expression is None else {
                'expression':expression, 'until':self.clock()+MANUAL_SECONDS, 'id':command, 'reason':reason}
            self.manual_used = True
            # 即使取消前未发生轮询，也要让下次读取重新确认自动状态。
            self.current_key = None

    def state(self):
        with self.lock, self.store.connect() as db:
            session, title, view, notice = self._context(db)
            if notice and not self.manual_used:
                return {'ready':False, 'notice':notice}
            if self.override and self.clock() >= self.override['until']:
                self.override = None
            performance = view.get('performance') or {}
            eligible = view.get('enabled') and performance.get('expires', 0) > self.store.clock().timestamp()
            if eligible and performance.get('action') == 'auto' and performance.get('id') not in {self.seen_performance, self.ignored_performance}:
                self.override = None
                self.seen_performance = performance['id']
            performance = performance if eligible and performance.get('action') == 'show' and performance.get('id') != self.ignored_performance else None
            manual = self.override
            if manual:
                performance = None
            requested = manual['expression'] if manual else performance['expression'] if performance else view.get('expression', '平静') if view.get('enabled') else '平静'
            label = performance.get('label', '') if performance else view.get('label', '')
            expression, valid, model_name = self._resolve(requested, label, view.get('band', ''))
            mapping_note = (f'；当前模型 {model_name}：{requested} → {expression}'
                            if valid and requested != expression else '')
            key = (model_name, 'manual', manual['id']) if manual else (model_name, 'performance', performance['id']) if performance else (model_name, session, view['revision'], expression)
            if key != self.current_key:
                command = secrets.token_hex(16)
                reason = manual['reason'] if manual else '自然语言表演请求：' + performance['reason'] if performance else '自动表情：' + view.get('reason', '恢复当前状态')
                dispatched = {**view, 'visual_reason':reason + mapping_note}
                if performance:
                    dispatched['message_id'] = performance['message_id']
                status = '已发出手动展示状态' if manual else '已发出自然语言请求的表演状态' if performance else '已发出自动展示状态（临时表演如有则已结束）'
                self._record(command, 'request', 'server', status if valid else '映射错误，未要求执行', session, dispatched, expression)
                self.current_key, self.current_command = key, command
                self.commands[command] = (session, dispatched, expression)
                while len(self.commands) > 64:
                    self.commands.pop(next(iter(self.commands)))
            return {"ready": True, "enabled": view.get("enabled", True), "csrf": self.csrf,
                    'manual':bool(manual), 'manual_expression':expression if manual else '',
                    'performance':bool(performance), 'performance_expression':expression if performance else '',
                    "command_id": self.current_command, "session_name": title,
                    **{k: view.get(k) for k in ("value", "band", "label", "intensity", "topic", "reason", "active", "updated")},
                    "expression": expression, "valid_expression": valid, "model_name": model_name,
                    "notice": ((notice or "最近已确认状态") + mapping_note) if valid else "当前模型没有可用的表情映射，请检查 model_dict.json"}

    def _record(self, command, stage, client, status, session, view, expression):
        emotion = {"active": view.get("active"), "label": view.get("label"),
                   "intensity": view.get("intensity", 0), "topic": view.get("topic", "")}
        self.store.append("Live2D动作", session, event_id=f"avatar:{command}:{client}:{stage}",
                          message_id=view.get("message_id", ""), status=status,
                          expression=expression, reason=view.get('visual_reason') or view.get("reason", "当前心情对应的常态"),
                          mood={"value": view.get("value")}, emotion=emotion)

    def receipt(self, report):
        with self.lock:
            stored = self.commands.get(report.command_id)
            if stored is None:
                raise HTTPException(409, "状态已过期，请重新获取")
            session, view, expression = stored
            labels = {"applied": "前端报告：已执行表情设置", "failed": "前端报告：表情设置失败",
                      "unavailable": "前端报告：当前模型尚未加载或没有对应表情"}
            self._record(report.command_id, report.status, report.client_id, labels[report.status], session, view, expression)
        return {"ok": True}


def create_router(store=None, allowed_expressions=None, model_provider=None):
    global _active_bridge
    router = APIRouter()
    if store is None and not enabled():
        return router
    bridge = HeartBridge(store or load_store(), allowed_expressions, model_provider=model_provider)
    _active_bridge = bridge

    def guard(request):
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"} or not request.client or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "该接口仅用于本机")
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "不允许跨站读取状态或写入日志")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "不允许跨站请求")

    @router.get("/heart/state")
    def state(request: Request):
        guard(request)
        return bridge.state()

    @router.get("/heart/expressions")
    def expressions(request: Request):
        guard(request)
        return bridge.expressions()

    @router.post("/heart/avatar-events")
    def receipt(request: Request, report: AvatarReceipt):
        guard(request)
        if not secrets.compare_digest(request.headers.get("x-heart-client", ""), bridge.csrf):
            raise HTTPException(403, "需要当前页面的回执凭据")
        return bridge.receipt(report)

    return router


def begin_visit(client_uid):
    """Called only after VTuber accepts a fresh browser connection."""
    if _active_bridge is not None:
        _active_bridge.begin_visit(client_uid)
