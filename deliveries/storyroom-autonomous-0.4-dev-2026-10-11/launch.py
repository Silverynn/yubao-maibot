"""只启动独立跑团平台；不启动 QQ、MaiBot 或 Open-LLM-VTuber。"""
from pathlib import Path
import argparse
import os
import secrets
import uvicorn

from storyroom.app import create_app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8790, type=int)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / ".runtime")
    parser.add_argument("--vtuber-root", type=Path, default=os.environ.get("STORYROOM_VTUBER_ROOT"))
    parser.add_argument('--asr-url', default=os.environ.get('STORYROOM_ASR_URL'))
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    credential = args.data_dir / "主持人登录密钥.txt"
    if not credential.exists():
        credential.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    print("StoryRoom 地址：http://" + args.host + ":" + str(args.port))
    print("主持人密钥在：" + str(credential.resolve()) + "（请勿发到群聊或GitHub）")
    print("仅启动跑团平台，不会启动机器人或产生模型费用。")
    uvicorn.run(create_app(args.data_dir, credential.read_text(encoding="utf-8").strip(), args.vtuber_root, args.asr_url),
                host=args.host, port=args.port, access_log=False)
