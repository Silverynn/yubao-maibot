"""读本机配置并启动独立平台。旧的 MaiBot/Live2D 启动器不受影响。"""
from pathlib import Path
from urllib.request import urlopen
import json
import sys
import subprocess
import threading
import time
import webbrowser


ROOT = Path(__file__).resolve().parent


def check_existing(existing):
    """同一版本才复用窗口；旧版需正常停机，其他服务不接管。"""
    if existing.get('service') != 'storyroom':
        raise RuntimeError('这个端口已被其他程序占用，请修改本机配置里的port')
    if not existing.get('ok'):
        raise RuntimeError('已有StoryRoom健康检查未通过，请检查该平台启动窗口')
    if existing.get('version') != '0.3.0' or existing.get('protocol') != '0.3':
        raise RuntimeError('另一个版本的StoryRoom仍在运行，请先关闭该平台启动窗口再启动；不要关闭MaiBot或VTuber。')


if __name__ == "__main__":
    config = json.loads((ROOT / "local-config.json").read_text(encoding="utf-8"))
    port = int(config.get("port", 8790))
    if not 1024 <= port <= 65535:
        raise ValueError("端口须在1024～65535之间")
    address = "http://127.0.0.1:" + str(port)
    if "--check" in sys.argv:
        if not Path(config["python"]).is_file():
            raise FileNotFoundError("本机配置里的 Python 路径不存在")
        if config.get("vtuber_root") and not (Path(config["vtuber_root"]) / "model_dict.json").is_file():
            raise FileNotFoundError("本机配置里的 VTuber 模型目录不正确")
        print("StoryRoom 启动路径检查通过；没有启动任何服务或浏览器。")
        raise SystemExit(0)
    # 已启动时直接打开现有界面，不重复创建服务，也不结束任何别的程序。
    try:
        with urlopen(address + "/health", timeout=1) as response:
            existing = json.loads(response.read())
    except OSError:
        existing = None
    if existing:
        check_existing(existing)
        webbrowser.open(address)
    else:
        command = [config["python"], "-u", "-X", "utf8", str(ROOT / "launch.py"), "--port", str(port)]
        if config.get("vtuber_root"):
            command += ["--vtuber-root", config["vtuber_root"]]
        if config.get('asr_url'):
            command += ['--asr-url', config['asr_url']]
        process = subprocess.Popen(command, cwd=ROOT)

        def open_when_ready():
            for _ in range(40):
                if process.poll() is not None:
                    return
                try:
                    with urlopen(address + "/health", timeout=.5) as response:
                        if json.loads(response.read()).get("service") == "storyroom":
                            webbrowser.open(address)
                            return
                except OSError:
                    time.sleep(.25)
        threading.Thread(target=open_when_ready, daemon=True).start()
        try:
            raise SystemExit(process.wait())
        except KeyboardInterrupt:
            # 只停止此启动器创建的子进程，不扫描或结束用户的其他 Python 程序。
            process.terminate()
            process.wait(timeout=10)
