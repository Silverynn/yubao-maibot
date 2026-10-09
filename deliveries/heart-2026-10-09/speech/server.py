"""同机语音识别：SenseVoice int8/CPU，QQ与Live2D共用，不调用云服务。"""
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import io
import logging
import time

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
import numpy as np
import sherpa_onnx
import soundfile as sf
import uvicorn

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / 'models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17'
MAX_BYTES = 12 * 1024 * 1024
MAX_SECONDS = 90
SERVICE = 'heart-local-sensevoice'
logger = logging.getLogger(SERVICE)


def read_audio(data: bytes):
    """只解码客户端上传的音频字节，不访问URL或任意文件路径。"""
    if not data or len(data) > MAX_BYTES:
        raise ValueError('audio_size')
    try:
        with sf.SoundFile(io.BytesIO(data)) as audio:
            if audio.samplerate < 8000 or audio.samplerate > 96000 or audio.channels > 2:
                raise ValueError('audio_format')
            seconds = len(audio) / audio.samplerate
            if seconds < .15 or seconds > MAX_SECONDS:
                raise ValueError('audio_duration')
            samples = audio.read(dtype='float32', always_2d=True).mean(axis=1)
            rate = audio.samplerate
    except (sf.LibsndfileError, RuntimeError) as error:
        raise ValueError('audio_format') from error
    if not np.isfinite(samples).all():
        raise ValueError('audio_nonfinite')
    return samples, rate, seconds


class Backend:
    def __init__(self, model_dir=MODEL):
        self.model_dir = Path(model_dir)
        self.recognizer = None
        self.lock = asyncio.Lock()

    def load(self):
        model, tokens = self.model_dir / 'model.int8.onnx', self.model_dir / 'tokens.txt'
        if not model.is_file() or not tokens.is_file():
            raise RuntimeError('本地识别模型不完整，请检查安装文件')
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=4, provider='cpu',
            language='', use_itn=True)

    def decode(self, samples, rate):
        stream = self.recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        self.recognizer.decode_streams([stream])
        return stream.result.text.strip()


def create_app(backend=None):
    backend = backend or Backend()

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(backend.load)
        logger.info('本地SenseVoice识别已就绪：CPU/int8；不保存录音正文')
        yield

    app = FastAPI(title='Heart 本地语音识别', lifespan=lifespan)
    app.state.backend = backend

    @app.middleware('http')
    async def local_only(request, call_next):
        from starlette.responses import JSONResponse
        if request.client is None or request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
            return JSONResponse({'detail': 'local_only'}, status_code=403)
        # 网页只经VT自身WebSocket提交，识别服务不接受跨站浏览器表单。
        if request.headers.get('origin'):
            return JSONResponse({'detail': 'browser_origin_not_allowed'}, status_code=403)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': 'content_length'}, status_code=400)
        if length > MAX_BYTES + 65536:
            return JSONResponse({'detail': 'audio_size'}, status_code=413)
        return await call_next(request)

    @app.get('/health')
    async def health():
        ready = backend.recognizer is not None
        return {'service': SERVICE, 'status': 'ready' if ready else 'loading',
                'ready': ready, 'available': ready and not backend.lock.locked(),
                'model': 'SenseVoiceSmall-int8-20240717', 'device': 'cpu'}

    @app.post('/v1/audio/transcriptions')
    async def transcribe(request: Request, file: UploadFile = File(...), language: str = Form('auto')):
        if language not in {'auto', 'zh', 'en', 'yue', 'ja', 'ko'}:
            raise HTTPException(400, 'language')
        if backend.recognizer is None:
            raise HTTPException(503, 'model_not_ready')
        started = time.monotonic()
        data = await file.read(MAX_BYTES + 1)
        await file.close()
        try:
            samples, rate, seconds = await asyncio.to_thread(read_audio, data)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) < .0002:
            return {'text': '', 'status': 'empty', 'audio_seconds': seconds,
                    'duration_seconds': time.monotonic() - started, 'model': 'SenseVoiceSmall-int8',
                    'sample_rate': rate, 'reason': 'silence'}
        try:
            await asyncio.wait_for(backend.lock.acquire(), timeout=10)
        except asyncio.TimeoutError as error:
            raise HTTPException(503, 'recognizer_busy') from error
        job = asyncio.create_task(asyncio.to_thread(backend.decode, samples, rate))
        try:
            # 取消HTTP等待不表示CPU线程停止，线程退出前不解锁启动另一推理。
            text = await asyncio.shield(job)
        finally:
            if job.done():
                backend.lock.release()
            else:
                def release_after_done(done):
                    backend.lock.release()
                    if not done.cancelled():
                        done.exception()
                job.add_done_callback(release_after_done)
        elapsed = time.monotonic() - started
        logger.info('语音识别完成：音频%.2f秒，处理%.2f秒，结果%d字', seconds, elapsed, len(text))
        return {'text': text, 'status': 'ok' if text else 'empty', 'audio_seconds': seconds,
                'duration_seconds': round(elapsed, 4), 'sample_rate': rate,
                'model': 'SenseVoiceSmall-int8-20240717', 'device': 'cpu',
                'language_mode': 'auto', 'requested_language': language}

    return app


app = create_app()
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host='127.0.0.1', port=12395, log_level='info')
