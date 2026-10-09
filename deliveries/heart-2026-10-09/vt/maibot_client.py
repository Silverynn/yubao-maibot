import asyncio
import json
import os
import re
from pathlib import Path

from websockets.asyncio.client import connect


def extract_speech_text(data):
    """只朗读正文；富媒体 content 可能是表情包标签或图片描述。"""
    segments = data.get("segments")
    if isinstance(segments, list) and segments:
        return "".join(
            segment["data"]
            for segment in segments
            if isinstance(segment, dict)
            and segment.get("type") == "text"
            and isinstance(segment.get("data"), str)
        ).strip()
    if data.get("message_type", "text") != "text":
        return ""
    content = data.get("content", "")
    return content.strip() if isinstance(content, str) else ""


def visit_user_id(visit_id):
    """A VTuber browser connection gets its own MaiBot chat identity."""
    if visit_id is None:
        return "vtuber_local_user"  # Compatibility without the Heart visit hook.
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", str(visit_id)):
        raise ValueError("无效的 Live2D 会话标识")
    return "vtuber_visit_" + str(visit_id).replace("-", "").lower()


async def stream_maibot(text, visit_id=None):
    from dialogue_trace import current_turn
    trace = current_turn.get()
    # 原生发送多句可能相隔十几秒。延长接收窗口不阻塞已到达的文字/声音。
    reply_idle = max(5, min(60, float(os.environ.get('MAIBOT_REPLY_IDLE_SECONDS', '20'))))
    # 这一部分与上一步一样：读取本机登录凭据
    project_dir = Path(__file__).resolve().parent
    token_file = Path(os.environ.get("MAIBOT_WEBUI_JSON") or project_dir.parent / "qq机器人" / ".runtime" / "MaiBot" / "data" / "webui.json")

    config = json.loads(token_file.read_text(encoding="utf-8"))
    token = config["access_token"]

    # 建立 WebSocket 连接，带上登录凭据
    async with connect(
        os.environ.get("MAIBOT_WEBUI_WS", "ws://127.0.0.1:8001/api/webui/ws"),
        additional_headers={"Cookie": f"maibot_session={token}"},
        proxy=None,
        open_timeout=10,
    ) as websocket:
        # 接收麦麦主动发来的“准备好了”通知
        raw_message = await asyncio.wait_for(websocket.recv(), timeout=10)
        message = json.loads(raw_message)
        print("麦麦发来的事件：", message.get("event"))

        # 第一步：申请打开一个本地聊天会话
        open_request = {
            "op": "call",
            "id": "open-1",
            "domain": "chat",
            "method": "session.open",
            "session": "vtuber-test",
            "data": {
                "user_id": visit_user_id(visit_id),
                "user_name": "Live2D 本地访客",
                "restore": False,
            },
        }

        await websocket.send(json.dumps(open_request))

        # 等待麦麦确认会话打开成功
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            result = json.loads(raw)

            if result.get("op") == "response" and result.get("id") == "open-1":
                if not result.get("ok"):
                    raise RuntimeError('打开 MaiBot 会话失败：' + str(result.get('error')))

                print("本地聊天会话已打开")
                break

        # 第二步：发送一句聊天内容
        chat_request = {
            "op": "call",
            "id": "message-1",
            "domain": "chat",
            "method": "message.send",
            "session": "vtuber-test",
            "data": {
                "content": text,
                "user_name": "Live2D 本地访客",
            },
        }

        from heart_realtime_client import prepare_correction
        from dialogue_trace import current_turn as trace_turn
        from heart_realtime_client import turn as rt_turn
        if rt_turn.get() and rt_turn.get().get("correction"):
            await prepare_correction()
        else:
            await websocket.send(json.dumps(chat_request))
        print("消息已提交，等待回复。")

        # 第三步：接收回答，并限制等待时间
        loop = asyncio.get_running_loop()

        # 最长接收时间：从现在开始 120 秒
        deadline = loop.time() + 120

        # None 表示还没有收到回答
        last_reply_time = None
        received_parts = []

        while True:
            now = loop.time()
            remaining = deadline - now

            # 接收稍晚的后续句子；不把“输入处理结束”误认成“机器人全部回复结束”。
            if last_reply_time is not None:
                remaining = min(
                    remaining,
                    reply_idle - (now - last_reply_time),
                )

            if remaining <= 0:
                break

            try:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break

            result = json.loads(raw)

            if result.get("op") == "response":
                if result.get("ok") is False:
                    raise RuntimeError(
                        f"麦麦请求失败：{result.get('error')}"
                    )

            if result.get("domain") != "chat":
                continue

            if result.get("session") != "vtuber-test":
                continue

            data = result.get("data", {})

            if result.get('event') == 'user_message' and trace and data.get('message_id'):
                from heart_realtime_client import bind_native
                await bind_native(data['message_id'])
                trace.bind(data['message_id'])
                trace.stage('native_accepted', status='MaiBot 已登记本轮输入')
            elif result.get("event") == "bot_message":
                reply_text = extract_speech_text(data)

                # WebUI may broadcast the user's own sentence as a bot_message
                # before the real answer. Do not speak it or start the short
                # post-reply timeout from this echo.
                if reply_text and reply_text == text.strip():
                    continue

                # 纯表情包也是已收到的回复，只是不交给 TTS。
                # 保留后续文字的接收窗口，不因没有可朗读正文报超时。
                if reply_text or data.get("message_type") == "rich" or data.get("segments"):
                    last_reply_time = loop.time()
                if reply_text:
                    from heart_realtime_client import received_draft
                    await received_draft(reply_text)
                    received_parts.append(reply_text)
                    if trace:
                        trace.stage('native_reply_received', key=f'native-reply:{len(received_parts)}',
                                    segment=len(received_parts)-1,
                                    status='MaiBot 正文已到接入层；下一步为语音合成或纯文字送达')
                    yield reply_text
                    if trace and await asyncio.to_thread(trace.received_generated_reply, text, received_parts):
                        trace.stage('reply_complete', status='本轮已生成的正文全部收到，提前结束接收窗口')
                        break

            elif result.get("event") == "error":
                raise RuntimeError(
                    f"麦麦报告错误：{data.get('content', data)}"
                )

        if last_reply_time is None:
            raise TimeoutError("等待超时，没有收到麦麦的文字回复")
        if trace:
            trace.stage('receive_closed', status='后续句子接收窗口结束；不表示所有记忆后台任务结束')
