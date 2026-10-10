"""固定版本与 SHA256 校验；只下载 MIT 前端库，不下载/传播 Live2D Core 或角色。"""
from pathlib import Path
from urllib.request import urlopen
import hashlib
import json


ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    target = ROOT / "web/vendor"
    target.mkdir(parents=True, exist_ok=True)
    for item in json.loads((ROOT / "vendor-manifest.json").read_text(encoding="utf-8")):
        path = target / item["name"]
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]:
            print("已验证：", item["name"])
            continue
        with urlopen(item["url"], timeout=20) as response:
            content = response.read(2_000_000)
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise RuntimeError("文件校验失败，没有安装：" + item["name"])
        temporary = path.with_suffix(".download")
        temporary.write_bytes(content)
        temporary.replace(path)
        print("已下载并校验：", item["name"])
