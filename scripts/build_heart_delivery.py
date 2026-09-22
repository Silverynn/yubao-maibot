"""只打包 Heart 新增源码和虚构示例，不打包机器人数据、登录态和私有配置。"""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo", type=Path, required=True, help="离线集成测试产生的虚构日志目录")
    args = parser.parse_args()
    output = args.output.resolve()
    archive = output.with_name(output.name + ".zip")
    if output.exists() or archive.exists():
        raise SystemExit("交付目录或压缩包已存在，请使用新名字，不覆盖旧交付。")
    directories = ["extensions/heart_shared", "plugins/heart_memory_audit", "plugins/heart_mood"]
    relative_paths = [
        "docs/Heart伙伴交接先读我.md", "docs/Heart伙伴安装与使用教程.md",
        "docs/Heart底层改动与文件清单.md", "docs/Heart400输出预算故障复盘.md",
        "docs/Heart给Codex的安装交接.md",
        "tests/test_heart_ai_mood.py",
        "extensions/heart_memory_backend.py", "patches/heart-memory-confirmation.patch",
        "tests/test_heart_conflicts.py",
        "extensions/.gitignore", "extensions/heart_host_observer.py", "patches/heart-memory-observer.patch",
        "scripts/install_heart_plugins.py", "scripts/verify_heart_integration.py", "scripts/build_heart_delivery.py",
        "tests/test_heart_memory.py", "tests/test_heart_mood.py", "licenses/maibot-LICENSE", ".gitignore",
    ]
    for directory in directories:
        relative_paths.extend(p.relative_to(ROOT).as_posix() for p in (ROOT / directory).rglob("*")
                              if p.is_file() and p.suffix in {".py", ".toml", ".json"} and "__pycache__" not in p.parts)
    for relative in relative_paths:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copy2(ROOT / "docs/Heart伙伴交接先读我.md", output / "先读我.md")
    sample = output / "虚构示例日志"
    sample.mkdir()
    for source in [args.demo / "当前心情.txt", *sorted((args.demo / "logs").glob("*.txt"))]:
        # 集成测试专用输出；不接受运行实例的任意日志目录。
        if "heart-demo" not in source.resolve().parts:
            raise SystemExit("只允许打包 heart-demo 中的虚构示例")
        shutil.copy2(source, sample / source.name)
    hashes = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "SHA256.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(output).as_posix())
    with zipfile.ZipFile(archive) as zf:
        assert zf.testzip() is None
    print(f"交付目录：{output}\n压缩包：{archive}\n共 {len(hashes)} 个已校验文件（另附SHA256.json）")


if __name__ == "__main__":
    main()
