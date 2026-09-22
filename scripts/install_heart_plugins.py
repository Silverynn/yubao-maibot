"""在已准备好的 MaiBot 中部署 Heart 插件；不启动机器人，不覆盖用户修改/配置。"""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(target, *args):
    return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, encoding="utf-8", check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=ROOT / ".runtime/MaiBot")
    parser.add_argument("--apply", action="store_true", help="实际部署；不带此选项只检查")
    parser.add_argument("--memory-only", action="store_true")
    args = parser.parse_args()
    target = args.target.resolve()
    if not (target / "src/services/memory_service.py").is_file():
        raise SystemExit("找不到 MaiBot 源码。先按 README 准备上游，或用 --target 指定 MaiBot 根目录。")
    pending_patches = []
    for name in ("heart-memory-observer.patch", "heart-memory-confirmation.patch"):
        patch = ROOT / "patches" / name
        applicable = git(target, "apply", "--check", str(patch))
        applied = git(target, "apply", "--reverse", "--check", str(patch))
        if applicable.returncode and applied.returncode:
            raise SystemExit("接口与此版本冲突，未修改任何文件：\n" + applicable.stderr)
        if applied.returncode:
            pending_patches.append(patch)
    source_pairs = [(ROOT / "extensions" / name, target / "src/services" / name)
                    for name in ("heart_host_observer.py", "heart_memory_backend.py")]
    directories = [(ROOT / "extensions/heart_shared", target / "heart_shared"),
                   (ROOT / "plugins/heart_memory_audit", target / "plugins/heart_memory_audit")]
    if not args.memory_only:
        directories.append((ROOT / "plugins/heart_mood", target / "plugins/heart_mood"))
    for source_dir, dest_dir in directories:
        if not source_dir.is_dir():
            raise SystemExit(f"插件源码不存在：{source_dir}")
        for source in sorted(source_dir.rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts and source.suffix in {".py", ".toml", ".json", ".md"}:
                source_pairs.append((source, dest_dir / source.relative_to(source_dir)))
    manifest_path = target / "data/heart_observation/install-manifest.json"
    previous = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    changed = []
    for source, dest in source_pairs:
        key = dest.relative_to(target).as_posix()
        if dest.exists():
            if dest.name == "config.toml":
                print(f"保留已有配置：{key}")
                continue
            current = digest(dest)
            if current == digest(source):
                continue
            if previous.get(key) != current:
                raise SystemExit(f"检测到未由本安装器管理的修改，停止：{key}。请人工比较，不能整目录覆盖。")
        changed.append((source, dest, key))
    print(f"目标：{target}\n拟复制/更新 {len(changed)} 个文件；待添加接口：{len(pending_patches)}")
    if not args.apply:
        print("检查完成。加 --apply 才会部署；不会连接 QQ 或调用模型。")
        return
    # 全部预检通过以后才复制；运行前应关闭目标 MaiBot，防止载入半更新模块。
    for source, dest, key in changed:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        previous[key] = digest(dest)
    for patch in pending_patches:
        result = git(target, "apply", str(patch))
        if result.returncode:
            raise SystemExit("接口补丁应用失败，机器人未启动：" + result.stderr)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(previous, indent=2, ensure_ascii=False), encoding="utf-8")
    print("部署完成，机器人未启动。日志将位于：" + str(target / "data/heart_observation"))


if __name__ == "__main__":
    main()
