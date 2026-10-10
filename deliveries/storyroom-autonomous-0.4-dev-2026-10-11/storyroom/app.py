"""HTTP 房间中继：默认仅本机；主持人与每个 PC 使用不同的密钥。"""
from pathlib import Path
from typing import Literal, Optional
import secrets
import time
import mimetypes
import asyncio
import wave

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .store import RoomError, RoomStore, actor_name, actor_id, digest
from .speech import decode_wav, transcribe, validate_endpoint


EMOTIONS = Literal["平静", "开心", "疑惑", "生气", "难过", "惊讶", "害羞", "思考"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    token: str = Field(min_length=8, max_length=200)


class CreateRoom(Input):
    name: str = Field(default="雾港双人跑团", min_length=1, max_length=50)
    names: list[str] = Field(default=["故机", "鱼宝"], min_length=2, max_length=2)
    avatars: Optional[list[str]] = Field(default=None, min_length=2, max_length=2)
    story: str = "lighthouse"


class Text(Input):
    text: str = Field(min_length=1, max_length=2000)


class Turn(Input):
    role: str = Field(min_length=1, max_length=64)
    question: str = Field(default="你打算怎样行动？只控制自己的角色。", min_length=1, max_length=2000)


class Advance(Input):
    target: str = Field(min_length=1, max_length=50)


class Result(Input):
    lease: str = Field(min_length=8, max_length=128)
    speech: str = Field(max_length=3000)
    action: str = Field(max_length=3000)
    emotion: EMOTIONS = "平静"
    failed: bool = False
    intent: Optional[Literal['discussion', 'question', 'action']] = None


class Decision(Input):
    approve: bool = True
    ruling: str = Field(min_length=1, max_length=2000)
    flags: list[str] = Field(default_factory=list, max_length=20)
    trust_delta: int = Field(default=0, ge=-2, le=2)
    mood_delta: int = Field(default=0, ge=-10, le=10)


class Skills(Input):
    investigate: int = Field(default=0, ge=0, le=3)
    craft: int = Field(default=0, ge=0, le=3)
    social: int = Field(default=0, ge=0, le=3)


class Card(Input):
    background: str = Field(default="", max_length=2000)
    goal: str = Field(default="", max_length=500)
    skills: Skills = Field(default_factory=Skills)


class Check(Input):
    skill: Literal["investigate", "craft", "social"]
    dc: int = Field(ge=1, le=23)


class ExperienceEdit(Input):
    text: str = Field(min_length=1, max_length=2000)
    active: bool = True


class PluginState(Input):
    enabled: bool
    version: Literal["0.2.0", "0.3.0"] = "0.3.0"


class ScriptEdit(Input):
    script: dict
    expected_hash: str = Field(default='', max_length=64)


class NoteEdit(Input):
    id: str = Field(default='', max_length=64)
    subject: str = Field(default='故事', min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    active: bool = True


class ParticipantEdit(Input):
    id: str = Field(default='', max_length=64)
    name: str = Field(min_length=1, max_length=40)
    profile: str = Field(default='', max_length=2000)
    active: bool = True


class TalkControl(Input):
    opened: bool
    limit: int = Field(default=4, ge=1, le=20)


class HostSpeech(Input):
    client: str = Field(min_length=1, max_length=64)
    speaking: bool


class AudioLease(Input):
    client: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=64)
    playing: bool


class HostInvite(Input):
    name: str = Field(min_length=1, max_length=40)


class VoiceInput(Input):
    role: str = Field(min_length=1, max_length=64)
    client: str = Field(min_length=1, max_length=64)
    audio: str = Field(min_length=1, max_length=1280064)


class MediaEvent(Input):
    role: str = Field(min_length=1, max_length=64)
    client: str = Field(min_length=1, max_length=64)
    feature: Literal['语音开关', '实时聊天开关', '播放开始', '播放完成', '播放停止', '播放失败', '麦克风启动', '麦克风失败', '快速信号', '输入提交']
    enabled: bool = False
    text: str = Field(default='', max_length=3000)
    detail: str = Field(default='', max_length=300)


class Dice(Input):
    expression: str = Field(default="1d20", max_length=20)
    dc: Optional[int] = Field(default=None, ge=1, le=200)


class VisualReceipt(Input):
    role: str = Field(max_length=64)
    revision: str = Field(max_length=64)
    client: str = Field(max_length=64)
    expression: str = Field(max_length=80)
    label: str = Field(max_length=30)
    status: str = Field(max_length=300)


def create_app(root: Path, host_token: str, vtuber_root: Optional[Path] = None, asr_url: Optional[str] = None, gm_config: Optional[dict] = None, image_config: Optional[dict] = None):
    # Windows 注册表可能把 .js 识别成 text/plain；模块脚本配合 nosniff 时会被浏览器拒绝。
    # 明确规定静态资源类型，保留浏览器的安全检查，而不是关闭 nosniff。
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    project = Path(__file__).resolve().parents[1]
    store = RoomStore(root, project / "stories")
    app = FastAPI(title="StoryRoom", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    store.gm_configured = bool(gm_config and all(gm_config.get(k) for k in ("base_url","api_key","model")))
    store.image_configured = bool(image_config and all(image_config.get(k) for k in ('base_url','api_key','model')))
    if store.image_configured:
        from .scenes import run_images
        @app.on_event('startup')
        async def start_images():
            app.state.image_job=asyncio.create_task(run_images(store,image_config))
        @app.on_event('shutdown')
        async def stop_images():
            app.state.image_job.cancel()
            await asyncio.gather(app.state.image_job,return_exceptions=True)
    if store.gm_configured:
        from .gm import run_gm
        @app.on_event("startup")
        async def start_gm():
            app.state.gm_job = asyncio.create_task(run_gm(store, gm_config))
        @app.on_event("shutdown")
        async def stop_gm():
            app.state.gm_job.cancel()
            await asyncio.gather(app.state.gm_job, return_exceptions=True)
    host_hash = digest(host_token)
    if asr_url:
        validate_endpoint(asr_url)
    asr_gate = asyncio.Semaphore(2)
    failures = {}
    catalog = {}
    if vtuber_root:
        import json
        for model in json.loads((vtuber_root / "model_dict.json").read_text(encoding="utf-8")):
            name, url = model["name"], model["url"]
            if url.startswith("/live2d-models/") and (vtuber_root / url.lstrip("/")).is_file():
                catalog[name] = {"name": name, "url": "/models/" + url[len("/live2d-models/"):],
                                 "emotionMap": model.get("emotionMap", {}),
                                 "expressionMotions": model.get("expressionMotions", {})}

    def identity(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.cookies.get("storyroom_session", "")
        if not token:
            raise HTTPException(401, "请先登录或配置连接密钥")
        if secrets.compare_digest(digest(token), host_hash):
            return {"host": True, "name": "主持人", "id": host_hash}
        cohost = store.cohost(token)
        if cohost:
            return {"host": True, **cohost}
        try:
            return {"host": False, **store.role(token)}
        except RoomError as e:
            raise HTTPException(e.status, e.message)

    def host(request: Request, room=None):
        actor = identity(request)
        if not actor["host"]:
            raise HTTPException(403, "只有主持人可以裁决或推进世界线")
        if actor.get("room") and actor["room"] != room:
            raise HTTPException(403, "共同主持人只能管理受邀房间")

    def room_access(request: Request, room: str):
        actor = identity(request)
        if actor.get("room") and actor["room"] != room:
            raise HTTPException(403, "不能读取其他房间")
        return actor

    @app.exception_handler(RoomError)
    async def error_handler(_, error):
        return JSONResponse({"detail": error.message}, status_code=error.status)

    @app.middleware("http")
    async def boundaries(request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "请求长度非法"}, status_code=400)
        limit = 1300000 if request.url.path.endswith('/voice') else 110000 if request.url.path.endswith(('/stories/save', '/script')) else 32768
        if length > limit:
            return JSONResponse({"detail": "请求过大"}, status_code=413)
        if request.url.path.startswith(("/models/", "/core/")):
            try:
                identity(request)
            except HTTPException as e:
                return JSONResponse({"detail": e.detail}, status_code=e.status_code)
        marker = None
        identity_marker = None
        if request.url.path.startswith("/api/"):
            try:
                actor = identity(request)
                if actor["host"]:
                    marker = actor_name.set(actor["name"])
                    identity_marker = actor_id.set(actor["id"])
            except HTTPException:
                pass  # 登录入口仍未带密钥；权限检查由具体接口负责。
        try:
            response = await call_next(request)
        finally:
            if marker is not None:
                actor_name.reset(marker)
                actor_id.reset(identity_marker)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        return FileResponse(project / "web/index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "service": "storyroom", "version": "0.3.0", "protocol": "0.3", "model_count": len(catalog), "runtime_version":"0.4-dev", "features":["runtime-control","timeline-branches","human-gm","fabula-choice","gm-worker"]}

    @app.post("/api/login")
    def login(data: Login, request: Request, response: Response):
        ip = request.client.host
        now = time.monotonic()
        recent = [t for t in failures.get(ip, []) if now - t < 60]
        if len(recent) >= 8:
            raise HTTPException(429, "登录失败过多，请一分钟后再试")
        try:
            if secrets.compare_digest(digest(data.token), host_hash):
                actor = {"host": True, "name": "主持人"}
            else:
                cohost = store.cohost(data.token)
                actor = {"host": True, **cohost} if cohost else {"host": False, **store.role(data.token)}
        except RoomError:
            failures[ip] = recent + [now]
            raise HTTPException(401, "登录密钥错误")
        failures.pop(ip, None)
        response.set_cookie("storyroom_session", data.token, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", max_age=86400)
        return {"host": actor["host"], "name": actor["name"], "room": actor.get("room")}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie("storyroom_session")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        actor = identity(request)
        return {"host": actor["host"], "name": actor["name"], "room": actor.get("room")}

    @app.get("/api/models")
    def models(request: Request):
        identity(request)
        return list(catalog.values())

    @app.get('/api/media')
    def media(request: Request):
        identity(request)
        return {'asr_configured': bool(asr_url), 'tts': 'browser', 'realtime': 'voice-turns',
                'description': '每个角色单独朗读；同一浏览器实时收音只选一名角色。未接入原Lv4.5提前生成。'}

    @app.post('/api/rooms/{room}/media-event')
    def media_event(room: str, data: MediaEvent, request: Request):
        actor = room_access(request, room)
        with store.db() as db:
            role = db.execute('SELECT name FROM roles WHERE room=? AND id=?', (room, data.role)).fetchone()
            if role is None or not actor['host'] and actor['id'] != data.role:
                raise HTTPException(403, '不能报告另一角色的播放状态')
            store.event(db, room, 'StoryRoom语音与实时状态', actor['name'], {
                '角色': role['name'], '浏览器': data.client, '功能': data.feature, '开关': data.enabled,
                '内容': data.text, '详情': data.detail, '说明': '浏览器报告，只作用于本浏览器，不覆盖伙伴语音包或日常实时插件'})
        store.export(room)
        return {'ok': True}

    @app.post('/api/rooms/{room}/voice')
    async def voice(room: str, data: VoiceInput, request: Request):
        host(request, room)
        with store.db() as db:
            role = db.execute('SELECT name FROM roles WHERE id=? AND room=?', (data.role, room)).fetchone()
            if role is None:
                raise HTTPException(404, '角色不属于此房间')
        if not asr_url:
            raise HTTPException(503, '平台未配置本地语音识别；请设置asr_url，仍可键盘点名')
        try:
            raw, seconds = decode_wav(data.audio)
        except (ValueError, EOFError, wave.Error):
            raise HTTPException(422, '录音格式不合法，要求16kHz单声道PCM WAV且不超过30秒')
        with store.db() as db:
            store.event(db, room, 'StoryRoom语音识别', '平台', {'状态': '开始', '角色': role['name'], '浏览器': data.client, '音频秒数': seconds, '服务': '本地ASR', '保存录音': False})
        started = time.monotonic()
        try:
            async def recognize():
                async with asr_gate:
                    return await transcribe(asr_url, raw)
            text = await asyncio.wait_for(recognize(), 22)
        except Exception as error:
            with store.db() as db:
                store.event(db, room, 'StoryRoom语音识别', '平台', {'状态': '失败', '角色': role['name'], '浏览器': data.client, '错误类型': type(error).__name__})
            store.export(room)
            raise HTTPException(502, '本地语音识别失败，请检查已有识别服务；未调用角色模型')
        with store.db() as db:
            store.event(db, room, 'StoryRoom语音识别', '平台', {'状态': '完成', '角色': role['name'], '浏览器': data.client,
                '输出文字': text, '耗时毫秒': round((time.monotonic()-started)*1000), '说明': '仅返回文字，尚未调用PC或修改剧情事实'})
        store.export(room)
        return {'text': text}

    @app.get('/api/bot/tasks/{task}/status')
    def task_status(task: str, request: Request):
        actor = identity(request)
        if actor['host']:
            raise HTTPException(403, '请使用PC密钥')
        with store.db() as db:
            row = db.execute('SELECT status,version FROM tasks WHERE id=? AND role=?', (task, actor['id'])).fetchone()
            if row is None:
                raise HTTPException(404, '任务不属于此PC')
            current = store.room(db, actor['room'])
            db.execute('UPDATE roles SET last_seen=? WHERE id=?', (time.time(), actor['id']))
            runtime=store.ensure_runtime(db,actor['room'])
            return {'status': row['status'], 'current': row['version'] == current['version'] and (not runtime['enabled'] or runtime['state']=='RUNNING')}

    @app.get("/api/rooms")
    def rooms(request: Request):
        actor = identity(request)
        return store.list_rooms() if actor["host"] and not actor.get("room") else [{"id": actor["room"], "name": store.snapshot(actor["room"])["name"]}]

    @app.post("/api/rooms")
    def new_room(data: CreateRoom, request: Request):
        host(request)
        if any(not n.strip() or len(n) > 40 for n in data.names):
            raise HTTPException(422, "角色名字需非空且不超过40字")
        avatars = data.avatars
        if avatars is None:
            available = list(catalog)
            own = 'moonbell-20261003' if 'moonbell-20261003' in catalog else next(iter(available), 'none')
            partner = 'yubao-fullbody' if 'yubao-fullbody' in catalog else next((name for name in available if name != own), own)
            avatars = [own, partner]
        if catalog and any(a not in catalog for a in avatars):
            raise HTTPException(422, "只能选择本机已配置的模型")
        return store.create(data.name, data.names, avatars, data.story)

    @app.get("/api/rooms/{room}")
    def snapshot(room: str, request: Request):
        room_access(request, room)
        return store.snapshot(room)

    @app.post("/api/rooms/{room}/narrate")
    def narrate(room: str, data: Text, request: Request):
        host(request, room)
        store.narrate(room, data.text)
        return {"ok": True}

    @app.post("/api/rooms/{room}/advance")
    def advance(room: str, data: Advance, request: Request):
        host(request, room)
        store.advance(room, data.target)
        return {"ok": True}

    @app.post("/api/rooms/{room}/turn")
    def turn(room: str, data: Turn, request: Request):
        host(request, room)
        return store.request_turn(room, data.role, data.question)

    @app.post("/api/rooms/{room}/tasks/{task}/decide")
    def decide(room: str, task: str, data: Decision, request: Request):
        host(request, room)
        return store.decide(room, task, data.approve, data.ruling, data.flags, data.trust_delta, data.mood_delta)

    @app.post("/api/rooms/{room}/roles/{role}/card")
    def card(room: str, role: str, data: Card, request: Request):
        host(request, room)
        store.set_card(room, role, data.model_dump())
        return {"ok": True}

    @app.post("/api/rooms/{room}/tasks/{task}/check")
    def define_check(room: str, task: str, data: Check, request: Request):
        host(request, room)
        store.define_check(room, task, data.skill, data.dc)
        return {"ok": True}

    @app.post("/api/rooms/{room}/tasks/{task}/roll")
    def roll_check(room: str, task: str, request: Request):
        host(request, room)
        return store.roll_check(room, task)

    @app.post("/api/rooms/{room}/experiences/{experience}")
    def edit_experience(room: str, experience: str, data: ExperienceEdit, request: Request):
        host(request, room)
        store.revise_experience(room, experience, data.text, data.active)
        return {"ok": True}

    @app.get("/api/rooms/{room}/tasks/{task}/audit")
    def audit(room: str, task: str, request: Request):
        host(request, room)
        import json
        with store.db() as db:
            row = db.execute("SELECT id,status,context,result FROM tasks WHERE id=? AND room=?", (task, room)).fetchone()
            if row is None:
                raise HTTPException(404, "任务不存在")
            events = [dict(r) for r in db.execute("SELECT * FROM events WHERE room=? ORDER BY seq", (room,))]
        return {"task": row["id"], "status": row["status"],
                "context": json.loads(row["context"]) if row["context"] else None,
                "result": json.loads(row["result"]) if row["result"] else None,
                "events": [{**e, "detail": json.loads(e["detail"])} for e in events if json.loads(e["detail"]).get("任务") == task]}

    @app.post("/api/bot/state")
    def plugin_state(data: PluginState, request: Request):
        actor = identity(request)
        if actor["host"]:
            raise HTTPException(403, "请使用PC密钥报告状态")
        with store.db() as db:
            store.event(db, actor["room"], "跑团插件开关", actor["name"], {
                "插件": "heart.storyroom-pc", "插件开启": data.enabled, "版本": data.version,
                "说明": "客户端报告；网络断开时以本机日志为准，在线状态来自轮询心跳"})
            if not data.enabled:
                db.execute("UPDATE roles SET last_seen=0 WHERE id=?", (actor["id"],))
        store.export(actor["room"])
        return {"ok": True}

    @app.post("/api/rooms/{room}/tasks/{task}/cancel")
    def cancel(room: str, task: str, request: Request):
        host(request, room)
        store.reject_stale(room, task)
        return {"ok": True}

    @app.post("/api/rooms/{room}/dice")
    def dice(room: str, data: Dice, request: Request):
        actor = room_access(request, room)
        if not actor["host"] and data.dc is not None:
            raise HTTPException(403, "检定难度应由主持人决定")
        return store.dice(room, actor["name"], data.expression, data.dc)

    @app.get("/api/bot/task")
    def claim(request: Request):
        actor = identity(request)
        if actor["host"]:
            raise HTTPException(403, "请使用玩家专用连接密钥")
        return {"task": store.claim(actor)}

    @app.post("/api/bot/tasks/{task}/result")
    def complete(task: str, data: Result, request: Request):
        actor = identity(request)
        if actor["host"]:
            raise HTTPException(403, "主持人不能冒充玩家提交结果")
        if not data.failed and not (data.speech.strip() or data.action.strip()):
            raise HTTPException(422, "玩家提案不能同时没有发言和行动")
        if not data.failed and data.intent in ('discussion', 'question') and (data.action.strip() or not data.speech.strip()):
            raise HTTPException(422, '讨论或提问须有发言且不能包含执行行动')
        if not data.failed and data.intent == 'action' and not data.action.strip():
            raise HTTPException(422, '行动须包含具体提案')
        return store.complete(actor, task, data.lease,
                              {"speech": data.speech, "action": data.action, "emotion": data.emotion,
                               **({'intent': data.intent} if data.intent is not None else {})}, data.failed)

    @app.get('/api/stories')
    def stories(request: Request):
        host(request)
        return store.templates()

    @app.post('/api/stories/save')
    def save_story(data: ScriptEdit, request: Request):
        host(request)
        return store.save_template(data.script, data.expected_hash)

    @app.get('/api/rooms/{room}/script')
    def room_script(room: str, request: Request):
        host(request, room)
        with store.db() as db:
            state = store.room(db, room)
            return {'script': store.story(state), 'hash': state['script_hash']}

    @app.post('/api/rooms/{room}/script')
    def apply_story(room: str, data: ScriptEdit, request: Request):
        host(request, room)
        return store.apply_script(room, data.script, data.expected_hash)

    @app.post('/api/rooms/{room}/memory')
    def note_edit(room: str, data: NoteEdit, request: Request):
        host(request, room)
        return store.set_note(room, data.id, data.subject, data.text, data.active)

    @app.post('/api/rooms/{room}/participants')
    def participant(room: str, data: ParticipantEdit, request: Request):
        host(request, room)
        return store.set_participant(room, data.id, data.name, data.profile, data.active)

    @app.post('/api/rooms/{room}/dialogue')
    def dialogue(room: str, data: Text, request: Request):
        actor = room_access(request, room)
        return store.dialogue(room, actor['name'], data.text)

    @app.post('/api/rooms/{room}/talk')
    def talk_control(room: str, data: TalkControl, request: Request):
        host(request, room)
        return store.control_talk(room, data.opened, data.limit)

    @app.post('/api/rooms/{room}/tasks/{task}/answer')
    def answer_question(room: str, task: str, data: Text, request: Request):
        host(request, room)
        return store.answer_question(room, task, data.text)

    @app.post('/api/rooms/{room}/host-speech')
    def host_speech(room: str, data: HostSpeech, request: Request):
        host(request, room)
        return store.host_speech(room, data.client, data.speaking)

    @app.post('/api/rooms/{room}/audio-lease')
    def audio_lease(room: str, data: AudioLease, request: Request):
        room_access(request, room)
        return store.audio_lease(room, data.client, data.task, data.playing)

    @app.get('/api/bot/observe')
    def observe(request: Request, after: int = 0):
        actor = identity(request)
        if actor['host']:
            raise HTTPException(403, '请使用PC密钥')
        if after < 0:
            raise HTTPException(422, '游标不能为负')
        return store.observe(actor, after)

    @app.post("/api/rooms/{room}/roles/{role}/rotate")
    def rotate(room: str, role: str, request: Request):
        host(request, room)
        key = secrets.token_urlsafe(32)
        with store.db() as db:
            row = db.execute("SELECT name FROM roles WHERE room=? AND id=?", (room, role)).fetchone()
            if row is None:
                raise HTTPException(404, "角色不存在")
            db.execute("UPDATE roles SET key_hash=? WHERE id=?", (digest(key), role))
            store.event(db, room, "连接密钥更换", "主持人", {"玩家": row["name"], "说明": "旧密钥已失效，新密钥仅返回一次"})
        store.export(room)
        return {"token": key}

    @app.post("/api/rooms/{room}/visual-receipt")
    def visual_receipt(room: str, data: VisualReceipt, request: Request):
        room_access(request, room)
        import json
        with store.db() as db:
            row = db.execute("SELECT name,visual FROM roles WHERE room=? AND id=?", (room, data.role)).fetchone()
            if row is None:
                raise HTTPException(404, "舞台角色不存在")
            if json.loads(row["visual"]).get("revision", "") != data.revision:
                return {"ok": True, "ignored": "旧的舞台状态不覆盖新状态"}
            key = digest(room + data.role + data.revision + data.client + data.expression + data.status)
            db.execute("CREATE TABLE IF NOT EXISTS visual_receipts(id TEXT PRIMARY KEY)")
            if db.execute("SELECT 1 FROM visual_receipts WHERE id=?", (key,)).fetchone():
                return {"ok": True, "duplicate": True}
            db.execute("INSERT INTO visual_receipts VALUES(?)", (key,))
            store.event(db, room, "Live2D舞台回执", "舞台网页", {"玩家": row["name"],
                        "分类": data.label, "表情": data.expression, "前端报告": data.status,
                        "说明": "前端自行报告执行结果，不代表动作已经完整播放完毕"})
        store.export(room)
        return {"ok": True}

    @app.post("/api/rooms/{room}/demo")
    def demo(room: str, request: Request):
        host(request, room)
        state = store.snapshot(room)
        if any(r["connected"] for r in state["roles"]):
            raise HTTPException(409, "请先暂停真实PC连接，再运行固定句子的离线演示，避免混用真实模型")
        if any(t["status"] in ("queued", "running", "proposed") for t in state["tasks"]):
            raise HTTPException(409, "先处理已有任务，再运行离线演示")
        with store.db() as db:
            store.event(db, room, "离线演示模式", "主持人",
                        {"说明": "下面两位PC使用固定测试句子，没有调用MaiBot或模型"})
        speeches = [("我先检查工作台，看看有没有能修好灯塔的零件。", "仔细寻找备用齿轮", "疑惑"),
                    ("我帮你照亮零件，也留意门外的天气。", "协助同伴调查并观察天气", "开心")]
        for role, (speech, action, emotion) in zip(state["roles"], speeches):
            owner = {"room": room, "id": role["id"], "name": role["name"], "trust": role["trust"], "demo": True}
            store.request_turn(room, role["id"], "【离线演示】描述你的行动；这不是实际模型调用")
            task = store.claim(owner)
            store.complete(owner, task["id"], task["lease"],
                           {"speech": "【离线演示】" + speech, "action": action, "emotion": emotion})
            with store.db() as db:
                # 内部模拟领取任务不等于真实电脑在线；保留原来的实际连接时间。
                db.execute("UPDATE roles SET last_seen=? WHERE id=?", (role["last_seen"], role["id"]))
        return {"ok": True, "model_calls": 0}

    @app.post("/api/rooms/{room}/hosts")
    def invite_host(room: str, data: HostInvite, request: Request):
        host(request)  # 仅平台管理员可分发主持权限，受邀GM不能继续转授。
        return store.invite_host(room, data.name)

    @app.post("/api/rooms/{room}/hosts/{identifier}/revoke")
    def revoke_host(room: str, identifier: str, request: Request):
        host(request)
        with store.db() as db:
            store.room(db, room)
            row = db.execute("SELECT name FROM hosts WHERE id=? AND room=?", (identifier, room)).fetchone()
            if row is None:
                raise HTTPException(404, "受邀主持人不存在")
            db.execute("DELETE FROM hosts WHERE id=? AND room=?", (identifier, room))
            store.event(db, room, "撤销主持人权限", "主持人", {"名字": row["name"]})
        store.export(room)
        return {"ok": True}

    from .runtime_api import install_runtime_api
    install_runtime_api(app, store, host, identity, room_access)
    if vtuber_root:
        app.mount("/models", StaticFiles(directory=vtuber_root / "live2d-models"))
        app.mount("/core", StaticFiles(directory=vtuber_root / "frontend/libs"))
    app.mount("/static", StaticFiles(directory=project / "web"))
    return app
