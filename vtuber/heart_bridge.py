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


_active_bridge = None
MANUAL_SECONDS = 8


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
    def __init__(self, store, allowed_expressions, clock=time.monotonic):
        self.store = store
        self.allowed = set(allowed_expressions)
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

    def _context(self, db):
        # 只能关联原生登记的网页身份，不能把QQ会话或猜测的hash当来源。
        rows = db.execute("""SELECT s.session,s.title,v.detail FROM session_sources src
            JOIN sessions s ON s.session=src.session LEFT JOIN avatar_views v ON v.session=s.session
            WHERE src.channel='Live2D' AND src.platform='webui'
            AND src.user_id IN ('vtuber_local_user','webui_user_vtuber_local_user')
            AND s.chat_type='私聊'""").fetchall()
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
            if expression is not None and expression not in self.allowed:
                raise ValueError('模型没有这个表情：' + str(expression))
            session, _, view, _ = self._context(db)
            self.ignored_performance = view.get('performance', {}).get('id')
            command = secrets.token_hex(16)
            reason = '用户要求恢复自动表情，不修改真实心情' if expression is None else f'用户手动请求 {expression}；临时展示约{MANUAL_SECONDS}秒，不修改真实心情'
            self._record(command, 'manual', 'server', '已接受手动表情请求', session,
                         {**view, 'visual_reason':reason}, expression or '恢复自动')
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
            expression = manual['expression'] if manual else performance['expression'] if performance else view['expression'] if view.get('enabled') else '平静'
            valid = expression in self.allowed
            key = ('manual', manual['id']) if manual else ('performance', performance['id']) if performance else (session, view['revision'], expression)
            if key != self.current_key:
                command = secrets.token_hex(16)
                reason = manual['reason'] if manual else '自然语言表演请求：' + performance['reason'] if performance else '自动表情：' + view.get('reason', '恢复当前状态')
                dispatched = {**view, 'visual_reason':reason}
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
                    "expression": expression, "valid_expression": valid,
                    "notice": (notice or "最近已确认状态") if valid else "配置的表情名不存在，请在插件设置中修正"}

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
                      "unavailable": "前端报告：模型尚未加载或不是鱼宝模型"}
            self._record(report.command_id, report.status, report.client_id, labels[report.status], session, view, expression)
        return {"ok": True}


def create_router(store=None, allowed_expressions=None):
    global _active_bridge
    router = APIRouter()
    if store is None and not enabled():
        return router
    if allowed_expressions is None:
        model = Path(__file__).parent / "live2d-models/ds-whale-girl/c_0120.model3.json"
        allowed_expressions = [e["Name"] for e in json.loads(model.read_text(encoding="utf-8"))["FileReferences"]["Expressions"]]
    bridge = HeartBridge(store or load_store(), allowed_expressions)
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

    @router.post("/heart/avatar-events")
    def receipt(request: Request, report: AvatarReceipt):
        guard(request)
        if not secrets.compare_digest(request.headers.get("x-heart-client", ""), bridge.csrf):
            raise HTTPException(403, "需要当前页面的回执凭据")
        return bridge.receipt(report)

    return router
