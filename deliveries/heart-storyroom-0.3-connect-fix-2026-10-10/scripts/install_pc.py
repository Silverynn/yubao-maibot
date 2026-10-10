"""只新增一个默认关闭的插件；拒绝覆盖既有目录，不改主程序及其他插件。"""
from pathlib import Path
import argparse
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("maibot", type=Path, help="MaiBot 根目录；安装不会重启或启用插件")
    parser.add_argument("--install", action="store_true", help="不加此参数仅预检")
    args = parser.parse_args()
    parent = (args.maibot.resolve() / "plugins").resolve()
    if not (args.maibot / "src/plugin_runtime").is_dir() or not parent.is_dir():
        raise ValueError("不是当前 SDK 版 MaiBot 目录")
    target = parent / "storyroom_pc"
    if target.exists():
        raise FileExistsError("目标目录已存在，为避免覆盖，请先人工备份并比较：" + str(target))
    print("预检通过，目标：", target)
    print("插件初始关闭，不会连接模型或 QQ；下一次重启或手动刷新插件后可配置。")
    if args.install:
        # 先准备完整插件，再整体移入，避免运行中的扫描器读到半份代码。
        with tempfile.TemporaryDirectory(prefix="storyroom-install-") as temporary:
            staged = Path(temporary).resolve() / "storyroom_pc"
            shutil.copytree(ROOT / "plugins/storyroom_pc", staged,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "config.toml", ".git"))
            shutil.copyfile(staged / "config.example.toml", staged / "config.toml")
            subprocess.run(["git", "init", str(staged)], check=True)
            if target.exists() or target.resolve().parent != parent:
                raise FileExistsError("目标发生变化，停止安装")
            shutil.move(str(staged), str(target))
        print("安装完成；请在插件配置里填写专用玩家密钥后再启用。")
