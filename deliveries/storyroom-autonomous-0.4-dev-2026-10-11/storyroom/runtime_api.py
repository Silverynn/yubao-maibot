from typing import Literal
from fastapi import Request
from fastapi.responses import PlainTextResponse, StreamingResponse
import asyncio
import json
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Control(Strict):
    operation: Literal['pause','resume','takeover','handoff','release']
    revision: int = Field(ge=1)


class Rewind(Strict):
    checkpoint: str = Field(min_length=1,max_length=64)
    revision: int = Field(ge=1)


class FUCard(Strict):
    card: dict


class FUCheck(Strict):
    attributes: list[Literal['DEX','INS','MIG','WLP']] = Field(min_length=2,max_length=2)
    difficulty: int = Field(ge=1,le=30)


class Choice(Strict):
    lease: str = Field(min_length=8,max_length=100)
    indices: list[int] = Field(default_factory=list,max_length=2)
    trait: str = Field(default='',max_length=200)


def install_runtime_api(app, store, host, identity, room_access):
    @app.get('/api/rooms/{room}/runtime-stream')
    async def stream(room: str, request: Request):
        room_access(request,room)
        async def events():
            previous=None
            while not await request.is_disconnected():
                with store.db() as db:
                    value=store.runtime_view(db,room)
                current=json.dumps(value,ensure_ascii=False)
                if current!=previous:
                    yield 'data: '+current+'\n\n'
                    previous=current
                else:
                    yield ': heartbeat\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(events(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.get('/api/scene-assets/{filename}')
    def image(filename: str, request: Request):
        from fastapi import HTTPException
        from fastapi.responses import FileResponse
        import re
        actor=identity(request)
        if not re.fullmatch(r'[0-9a-f]{64}\.png',filename): raise HTTPException(404,'图片不存在')
        with store.db() as db:
            rows=db.execute('SELECT room FROM scenes WHERE hash=?',(filename[:-4],)).fetchall()
            if not any(not actor.get('room') or actor['room']==r['room'] for r in rows):
                raise HTTPException(403,'图片不属于当前房间')
        path=store.root/'scene-assets'/filename
        if not path.is_file(): raise HTTPException(404,'图片不存在')
        return FileResponse(path,media_type='image/png')

    @app.post('/api/rooms/{room}/runtime')
    def control(room: str, data: Control, request: Request):
        host(request,room)
        return store.control_runtime(room,data.operation,data.revision,identity(request)['id'])

    @app.post('/api/rooms/{room}/controller/renew')
    def renew(room: str, request: Request):
        host(request,room)
        return store.renew_controller(room,identity(request)['id'])

    @app.get('/api/rooms/{room}/timeline')
    def timeline(room: str, request: Request):
        host(request,room)
        return store.timeline(room)

    @app.get('/api/rooms/{room}/checkpoints/{checkpoint}')
    def preview(room: str, checkpoint: str, request: Request):
        host(request,room)
        return store.rewind_preview(room,checkpoint)

    @app.post('/api/rooms/{room}/rewind')
    def rewind(room: str, data: Rewind, request: Request):
        host(request,room)
        return store.rewind(room,data.checkpoint,data.revision,identity(request)['id'])

    @app.get('/api/rooms/{room}/script.md')
    def script(room: str, request: Request, branch: str | None = None):
        room_access(request,room)
        if branch: host(request,room)
        return PlainTextResponse(store.script_export(room,branch),media_type='text/markdown; charset=utf-8')

    @app.post('/api/rooms/{room}/roles/{role}/fabula')
    def card(room: str, role: str, data: FUCard, request: Request):
        host(request,room)
        return store.set_fu_card(room,role,data.card)

    @app.post('/api/rooms/{room}/tasks/{task}/fabula-check')
    def check(room: str, task: str, data: FUCheck, request: Request):
        host(request,room)
        return store.fu_check(room,task,data.attributes,data.difficulty)

    @app.get('/api/bot/fabula-choice')
    def choice_task(request: Request):
        actor=identity(request)
        if actor['host']:
            from fastapi import HTTPException
            raise HTTPException(403,'必须由对应PC选择，主持人不能代选')
        return {'choice':store.claim_fu_choice(actor)}

    @app.post('/api/bot/fabula-choice/{task}')
    def choice_result(task: str, data: Choice, request: Request):
        actor=identity(request)
        if actor['host']:
            from fastapi import HTTPException
            raise HTTPException(403,'必须由对应PC选择')
        return store.complete_fu_choice(actor,task,data.lease,data.indices,data.trait)

    @app.get('/api/bot/fabula-choice/{task}/status')
    def choice_status(task: str, request: Request):
        from fastapi import HTTPException
        actor=identity(request)
        if actor['host']:raise HTTPException(403,'请使用PC身份')
        with store.db() as db:
            row=db.execute('SELECT c.data,t.version,t.status FROM fu_checks c JOIN tasks t ON t.id=c.task WHERE c.task=? AND c.room=? AND t.role=?',(task,actor['room'],actor['id'])).fetchone()
            if not row:raise HTTPException(404,'检定不存在')
            data=json.loads(row['data']);r=store.ensure_runtime(db,actor['room'])
            return {'current':row['version']==store.room(db,actor['room'])['version'] and row['status']=='proposed' and r['state']=='RUNNING' and data.get('choice_epoch')==r['epoch'] and data.get('choice_until',0)>store.clock() and not data['finalized']}
