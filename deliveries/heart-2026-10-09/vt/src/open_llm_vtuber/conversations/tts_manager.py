import asyncio
import json
import re
import uuid
from datetime import datetime
from typing import List, Optional, Dict
from loguru import logger

from ..agent.output_types import DisplayText, Actions
from ..live2d_model import Live2dModel
from ..tts.tts_interface import TTSInterface
from ..utils.stream_audio import prepare_audio_payload
from .types import WebSocketSend


class TTSTaskManager:
    """Manages TTS tasks and ensures ordered delivery to frontend while allowing parallel TTS generation"""

    def __init__(self, voice_enabled: bool = True) -> None:
        # 每轮自己保存开关，不修改共享 TTS 引擎，避免不同网页互相干扰。
        self.voice_enabled = voice_enabled if type(voice_enabled) is bool else True
        self.task_list: List[asyncio.Task] = []
        self._lock = asyncio.Lock()
        # Queue to store ordered payloads
        self._payload_queue: asyncio.Queue[Dict] = asyncio.Queue()
        # Task to handle sending payloads in order
        self._sender_task: Optional[asyncio.Task] = None
        # Counter for maintaining order
        self._sequence_counter = 0
        self._next_sequence_to_send = 0
        # 纯文字/表情也要等 WebSocket 实际送达，避免对话提前结束清空队列。
        self._delivery: Dict[int, asyncio.Future] = {}
        self._send_error: Optional[Exception] = None

    async def speak(
        self,
        tts_text: str,
        display_text: DisplayText,
        actions: Optional[Actions],
        live2d_model: Live2dModel,
        tts_engine: TTSInterface,
        websocket_send: WebSocketSend,
    ) -> None:
        """
        Queue a TTS task while maintaining order of delivery.

        Args:
            tts_text: Text to synthesize
            display_text: Text to display in UI
            actions: Live2D model actions
            live2d_model: Live2D model instance
            tts_engine: TTS engine instance
            websocket_send: WebSocket send function
        """
        if self._send_error is not None:
            raise self._send_error
        # 只对本地声带的首个长片段做音文等长分段，不等整段长音频才开始。
        # 已被预处理成不同朗读文本的输出不拆，避免字幕/朗读错配。
        if (self.voice_enabled and self._sequence_counter == 0
                and getattr(tts_engine, 'prefer_short_first_chunk', False)
                and tts_text == display_text.text):
            from speech_chunks import short_first_speech
            pieces = short_first_speech(tts_text)
            if len(pieces) > 1:
                for index, piece in enumerate(pieces):
                    await self.speak(piece, DisplayText(piece, display_text.name, display_text.avatar),
                                     actions if index == 0 else None,
                                     live2d_model, tts_engine, websocket_send)
                return
        # 开启：文字等这一段音频准备好再一起送出。关闭：根本不调用声带。
        # /表情等没有朗读内容的本地命令仍立即送出，不受开关影响。
        previewed = False
        if not self.voice_enabled or len(re.sub(r'[\s.,!?，。！？\'"』」）】\s]+', "", tts_text)) == 0:
            logger.debug("Empty TTS text, sending silent display payload")
            # Get current sequence number for silent payload
            current_sequence = self._sequence_counter
            self._sequence_counter += 1

            # Start sender task if not running
            if not self._sender_task or self._sender_task.done():
                self._sender_task = asyncio.create_task(
                    self._process_payload_queue(websocket_send)
                )

            task = asyncio.create_task(
                self._send_silent_payload(display_text, actions, current_sequence, previewed)
            )
            self.task_list.append(task)
            return

        logger.debug(
            f"🏃Queuing TTS task for: '''{tts_text}''' (by {display_text.name})"
        )

        # Get current sequence number
        current_sequence = self._sequence_counter
        self._sequence_counter += 1

        # Start sender task if not running
        if not self._sender_task or self._sender_task.done():
            self._sender_task = asyncio.create_task(
                self._process_payload_queue(websocket_send)
            )

        # Create and queue the TTS task
        task = asyncio.create_task(
            self._process_tts(
                tts_text=tts_text,
                display_text=display_text,
                actions=actions,
                live2d_model=live2d_model,
                tts_engine=tts_engine,
                sequence_number=current_sequence,
            )
        )
        self.task_list.append(task)

    async def _process_payload_queue(self, websocket_send: WebSocketSend) -> None:
        """
        Process and send payloads in correct order.
        Runs continuously until all payloads are processed.
        """
        buffered_payloads: Dict[int, Dict] = {}

        while True:
            try:
                # Get payload from queue
                payload, sequence_number = await self._payload_queue.get()
                buffered_payloads[sequence_number] = payload

                # Send payloads in order
                while self._next_sequence_to_send in buffered_payloads:
                    next_payload = buffered_payloads.pop(self._next_sequence_to_send)
                    from heart_realtime_client import eligible_payload
                    if not eligible_payload(next_payload):
                        raise asyncio.CancelledError()
                    await websocket_send(json.dumps(next_payload))
                    from dialogue_trace import current_turn
                    trace = current_turn.get()
                    if trace and next_payload.get('display_text'):
                        trace.stage('text_sent', key=f'text:{self._next_sequence_to_send}',
                                    response=next_payload['display_text']['text'],
                                    segment=self._next_sequence_to_send,
                                    voice_enabled=self.voice_enabled,
                                    status='文字与音频一起交给网页' if next_payload.get('audio') else
                                           '已交给网页显示文字；语音关闭，未调用声带' if not self.voice_enabled else
                                           '已交给网页显示文字（无音频）')
                    delivery = self._delivery.get(self._next_sequence_to_send)
                    if delivery is not None and not delivery.done():
                        delivery.set_result(None)
                    self._next_sequence_to_send += 1

                self._payload_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as error:
                self._send_error = error
                for delivery in self._delivery.values():
                    if not delivery.done():
                        delivery.set_exception(error)
                logger.warning("Frontend payload delivery failed: {}", type(error).__name__)
                return

    async def _send_silent_payload(
        self,
        display_text: DisplayText,
        actions: Optional[Actions],
        sequence_number: int,
        previewed: bool = False,
    ) -> None:
        """Queue a silent audio payload"""
        audio_payload = prepare_audio_payload(
            audio_path=None,
            display_text=display_text,
            actions=actions,
        )
        from dialogue_trace import annotate
        annotate(audio_payload, sequence_number)
        audio_payload['heart_previewed'] = previewed
        audio_payload['heart_voice_enabled'] = self.voice_enabled
        audio_payload['heart_audio_status'] = 'text_only' if self.voice_enabled else 'disabled'
        await self._queue_and_wait_delivery(audio_payload, sequence_number)

    async def _queue_and_wait_delivery(self, payload: Dict, sequence_number: int) -> None:
        """排入队列不等于发送完成；文字、表情和音频都要等实际发送。"""
        if self._send_error is not None:
            raise self._send_error
        from heart_realtime_client import annotate_payload
        if not await annotate_payload(payload, sequence_number):
            return
        delivery = asyncio.get_running_loop().create_future()
        self._delivery[sequence_number] = delivery
        try:
            await self._payload_queue.put((payload, sequence_number))
            await delivery
        finally:
            self._delivery.pop(sequence_number, None)

    async def _process_tts(
        self,
        tts_text: str,
        display_text: DisplayText,
        actions: Optional[Actions],
        live2d_model: Live2dModel,
        tts_engine: TTSInterface,
        sequence_number: int,
    ) -> None:
        """Process TTS generation and queue the result for ordered delivery"""
        audio_file_path = None
        try:
            audio_file_path = await self._generate_audio(tts_engine, tts_text)
            payload = prepare_audio_payload(
                audio_path=audio_file_path,
                display_text=display_text,
                actions=actions,
            )
        except Exception as e:
            logger.error(f"Error preparing audio payload: {e}")
            from dialogue_trace import current_turn
            trace = current_turn.get()
            if trace:
                trace.stage('voice_failed', key=f'voice-failed:{sequence_number}',
                            segment=sequence_number, status='语音合成失败，降级为文字：'+type(e).__name__)
            # Queue silent payload for error case
            payload = prepare_audio_payload(
                audio_path=None,
                display_text=display_text,
                actions=actions,
            )

        finally:
            if audio_file_path:
                tts_engine.remove_file(audio_file_path)
                logger.debug("Audio cache file cleaned.")

        # 发送错误不再当成 TTS 错误重复入队；交由上层结束本轮。
        from dialogue_trace import annotate
        annotate(payload, sequence_number)
        payload['heart_previewed'] = False
        payload['heart_voice_enabled'] = self.voice_enabled
        payload['heart_audio_status'] = 'ready' if payload.get('audio') else 'unavailable'
        await self._queue_and_wait_delivery(payload, sequence_number)

    async def _generate_audio(self, tts_engine: TTSInterface, text: str) -> str:
        """Generate audio file from text"""
        logger.debug(f"🏃Generating audio for '''{text}'''...")
        return await tts_engine.async_generate_audio(
            text=text,
            file_name_no_ext=f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}",
        )

    def clear(self) -> None:
        """Clear all pending tasks and reset state"""
        for task in self.task_list:
            if not task.done():
                task.cancel()
        self.task_list.clear()
        if self._sender_task:
            self._sender_task.cancel()
        self._sequence_counter = 0
        self._next_sequence_to_send = 0
        self._send_error = None
        # Create a new queue to clear any pending items
        self._payload_queue = asyncio.Queue()
