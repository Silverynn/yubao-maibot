"""流式 Zipformer CPU 进程；复用 VT 已安装的 sherpa-onnx，原 ASR 服务不改。"""
from pathlib import Path

import asyncio
import os
import secrets
import sys
import time

if os.environ.get('HEART_RT_ASR_DEPS'):
    sys.path.insert(0,os.environ['HEART_RT_ASR_DEPS'])

from aiohttp import web, WSMsgType
import numpy as np
import sherpa_onnx

MODEL = Path(os.environ['HEART_RT_MODEL'])
KEY = os.environ['HEART_RT_KEY']


def load():
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(MODEL/'tokens.txt'), encoder=str(MODEL/'encoder.int8.onnx'),
        decoder=str(MODEL/'decoder.onnx'), joiner=str(MODEL/'joiner.int8.onnx'),
        num_threads=2, decoding_method='greedy_search', model_type='zipformer2', provider='cpu')


async def serve():
    recognizer = await asyncio.to_thread(load)
    lock = asyncio.Lock()
    clients = set()

    def decode(stream, audio, final=False):
        stream.accept_waveform(16000, audio)
        if final:
            stream.accept_waveform(16000,np.zeros(16000,dtype=np.float32))
            stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        return recognizer.get_result(stream)

    async def health(request):
        return web.json_response({'service':'heart-streaming-zipformer','ready':True,
            'instance':os.environ.get('HEART_RT_ASR_INSTANCE',''),
            'sherpa_version':sherpa_onnx.__version__,'connections':len(clients)})

    async def stream(request):
        if request.headers.get('Origin') or not secrets.compare_digest(
                request.headers.get('X-Heart-Realtime',''),KEY):
            raise web.HTTPForbidden()
        if len(clients)>=4:
            raise web.HTTPServiceUnavailable(text='流式识别连接已满')
        socket=web.WebSocketResponse(max_msg_size=65536,heartbeat=10)
        await socket.prepare(request)
        clients.add(socket)
        audio_stream=recognizer.create_stream()
        last='',0
        total=0
        try:
            async for msg in socket:
                final=msg.type==WSMsgType.TEXT and msg.data=='finish'
                if not final and msg.type!=WSMsgType.BINARY:
                    raise ValueError('只接收16kHz float32单声道')
                data=np.frombuffer(msg.data,dtype='<f4').copy() if not final else np.zeros(0,dtype=np.float32)
                if len(data)>8000 or not np.isfinite(data).all() or np.any(np.abs(data)>1.01):
                    raise ValueError('录音帧格式不合法')
                total+=len(data)
                if total>16000*90:
                    raise ValueError('一段语音超过90秒')
                started=time.monotonic()
                async with lock:
                    # 断线时也等工作线程完成再释放互斥，不并发解码同一个识别器。
                    job=asyncio.create_task(asyncio.to_thread(decode,audio_stream,data,final))
                    try:
                        text=await asyncio.shield(job)
                    except asyncio.CancelledError:
                        await job
                        raise
                if text!=last[0] or final:
                    last=text,last[1]+1
                    await socket.send_json({'text':text,'revision':last[1], 'finished':final,
                        'audio_seconds':total/16000,'decode_ms':(time.monotonic()-started)*1000})
                if final:
                    break
        except Exception as error:
            if not socket.closed:
                await socket.send_json({'error':type(error).__name__})
        finally:
            clients.discard(socket)
            await socket.close()
        return socket

    app=web.Application(client_max_size=65536)
    app.router.add_get('/health',health)
    app.router.add_get('/stream',stream)
    runner=web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('HEART_RT_ASR_PORT', '12397'))
    await web.TCPSite(runner,'127.0.0.1',port).start()
    print(f'Heart 流式识别就绪：127.0.0.1:{port}；不记录原始录音',flush=True)
    try:
        await asyncio.Future()
    finally:
        await runner.cleanup()


if __name__=='__main__':
    asyncio.run(serve())
