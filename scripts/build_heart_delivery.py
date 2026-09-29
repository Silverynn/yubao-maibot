"""生成新版 Heart 独立交付包；只采用明确列出的源码，不复制运行数据。"""

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "heart-2026-09-29-dedup"

CORE_DIRECTORIES = (
    "extensions/heart_shared",
    "plugins/heart_memory_audit",
    "plugins/heart_mood",
)
CORE_FILES = (
    "extensions/heart_host_observer.py",
    "extensions/heart_memory_backend.py",
    "extensions/heart_memory_scope.py",
    "extensions/heart_llm_budget.py",
    "patches/heart-memory-observer.patch",
    "patches/heart-memory-confirmation.patch",
    "patches/heart-memory-entry.patch",
    "patches/heart-memory-prompt.patch",
    "patches/heart-memory-decision-log.patch",
    "patches/heart-llm-budget.patch",
    "patches/heart-memory-scope.patch",
    "scripts/install_heart_plugins.py",
    "scripts/build_heart_delivery.py",
    "upstreams.json",
    "licenses/maibot-LICENSE",
    "docs/Heart交付说明-2026-09-29.md",
    "docs/Heart给同伴GPT安装-2026-09-29.md",
    "docs/Heart功能覆盖与验收-2026-09-29.md",
    "docs/Heart同义去重修复与旧记忆整理.md",
    "docs/MaiBot原生记忆与Heart插件说明.md",
    "docs/Heart话题情绪与Live2D接入.md",
)
# 同伴已有 Live2D 项目。下面是可选桥接代码，不包含模型、音频和私人配置；
# 已修改的上游文件必须由同伴的 GPT 比较后合并，不能直接整目录覆盖。
OPTIONAL_LIVE2D_FILES = (
    "vtuber/README.md",
    "vtuber/heart_bridge.py",
    "vtuber/frontend/heart-avatar.mjs",
    "vtuber/frontend/heart-controller.mjs",
    "vtuber/frontend/yubao-expression-timing.mjs",
    "vtuber/maibot_client.py",
    "vtuber/model_dict.json",
    "vtuber/scripts/install_heart_bridge.py",
    "vtuber/scripts/apply_yubao_expression_timing.py",
    "vtuber/scripts/test_agent_changes.py",
    "vtuber/scripts/test_expression_timing.mjs",
    "vtuber/scripts/test_heart_controller.mjs",
    "vtuber/scripts/test_silent_delivery.py",
    "vtuber/scripts/test_silent_expression.mjs",
    "vtuber/src/open_llm_vtuber/agent/agent_factory.py",
    "vtuber/src/open_llm_vtuber/agent/agents/maibot_agent.py",
    "vtuber/src/open_llm_vtuber/agent/agents/maibot_expression.py",
    "vtuber/src/open_llm_vtuber/conversations/tts_manager.py",
    "vtuber/表情修复与测试说明.md",
)
ALLOWED_PLUGIN_SUFFIXES = {".py", ".toml", ".json", ".md"}


def source_files():
    """只枚举可审阅的交付文件；不会扫描 .runtime、日志或密钥目录。"""
    paths = set(CORE_FILES) | set(OPTIONAL_LIVE2D_FILES)
    for directory in CORE_DIRECTORIES:
        root = ROOT / directory
        if not root.is_dir():
            raise FileNotFoundError(root)
        paths.update(path.relative_to(ROOT).as_posix() for path in root.rglob("*")
                     if path.is_file() and not path.is_symlink()
                     and "__pycache__" not in path.parts and path.suffix in ALLOWED_PLUGIN_SUFFIXES)
    paths.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_heart*.py"))
    for relative in sorted(paths):
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"交付清单文件不存在或为链接：{relative}")
        yield relative, path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output):
    output = output.resolve()
    archive = output.with_name(output.name + ".zip")
    if output.exists() or archive.exists():
        raise FileExistsError("新交付目标已存在；换一个名称，不覆盖旧交付包")
    files = list(source_files())
    output.mkdir(parents=True)
    for relative, source in files:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copy2(ROOT / "docs/Heart交付说明-2026-09-29.md", output / "README.md")
    hashes = {path.relative_to(output).as_posix(): digest(path)
              for path in sorted(output.rglob("*")) if path.is_file()}
    (output / "SHA256.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(output).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise RuntimeError("压缩包校验失败")
    return output, archive, len(hashes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "deliveries" / PACKAGE_NAME)
    args = parser.parse_args()
    output, archive, count = build(args.output)
    print(f"交付目录：{output}\n压缩包：{archive}\n已校验 {count} 个文件；未包含日志、记忆库和密钥。")


if __name__ == "__main__":
    main()
