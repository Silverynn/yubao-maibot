"""只缩短 Live2D 的人工发送等待，不改 QQ、普通 WebUI 或回复内容。"""
from typing import TYPE_CHECKING
import re

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage


def skip_live2d_typing(message: "SessionMessage") -> bool:
    # 出站消息的 user_info 是机器人，不是接收人；必须查看已注册的目标会话。
    if message.platform != "webui":
        return False
    from src.chat.message_receive.chat_manager import chat_manager

    session = chat_manager.get_session_by_session_id(message.session_id)
    return bool(
        session is not None
        and session.platform == "webui"
        and not session.is_group_session
        and re.fullmatch(r"webui_user_vtuber_visit_[0-9a-f]{32}", session.user_id)
    )
