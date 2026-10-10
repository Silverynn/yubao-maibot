"""停止/关闭跑团插件后升级。只覆盖已知发布源码，保留配置、数据、独立Git及其他插件。"""
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import tomllib
import uuid


ROOT = Path(__file__).resolve().parents[1]
FILES = ('.gitignore', '_manifest.json', 'config.example.toml', 'plugin.py', 'worker.py')


def update(maibot: Path, apply=False, backup_root=None):
    target = maibot.resolve() / 'plugins' / 'storyroom_pc'
    if not (maibot / 'src/plugin_runtime').is_dir() or not target.is_dir() or target.resolve().parent != (maibot / 'plugins').resolve():
        raise ValueError('不是当前SDK版MaiBot的既有跑团插件目录')
    version = json.loads((target / '_manifest.json').read_text(encoding='utf-8'))['version']
    known = json.loads((ROOT / 'scripts/pc-v02-hashes.json').read_text(encoding='utf-8'))
    profiles = [known]
    if version in ('0.3.0','0.4.0'):
        known = {name: hashlib.sha256((ROOT / 'plugins/storyroom_pc' / name).read_bytes()).hexdigest() for name in FILES}
        # 上次0.3发布缺失清单字段；只接受该已发布版本的完整指纹，不能放行伙伴任意修改。
        profiles = [known, json.loads((ROOT / 'scripts/pc-v03-hashes.json').read_text(encoding='utf-8')), json.loads((ROOT/'scripts/pc-v03-fixed-hashes.json').read_text(encoding='utf-8'))]
    elif version != '0.2.0':
        raise ValueError('只自动升级已验收的0.2或核对同版本0.3；其他版本需人工比较')
    actual = {name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in FILES}
    changed = [name for name in FILES if actual[name] != known[name]]
    if not any(actual == profile for profile in profiles):
        raise ValueError('发现额外源码修改，停止覆盖，请人工比较：' + ','.join(changed))
    config = target / 'config.toml'
    if config.exists() and tomllib.loads(config.read_text(encoding='utf-8-sig'))['connection']['enabled']:
        raise ValueError('请先在管理页关闭跑团插件或在停机状态下关闭其enabled，避免读到半份源码')
    print('预检通过；保留config.toml、插件数据、Git和其他插件；平台兼容协议0.3；新运行控制和最终物语选择需双方使用本交付的0.4插件。')
    if not apply:
        return None
    directory = Path(backup_root) if backup_root else maibot / 'data/plugins/heart.storyroom-pc/升级备份'
    backup = directory / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    for name in (*FILES, 'config.toml'):
        if (target / name).exists():
            shutil.copy2(target / name, backup / name)
    before_config = config.read_bytes() if config.exists() else None
    try:
        for name in FILES:
            staged = target / (name + '.upgrade.tmp')
            shutil.copy2(ROOT / 'plugins/storyroom_pc' / name, staged)
            staged.replace(target / name)
        if before_config is not None and config.read_bytes() != before_config:
            raise RuntimeError('配置在升级期间发生变化，请检查管理员并发修改')
    except BaseException:
        for name in FILES:
            shutil.copy2(backup / name, target / name)
        raise
    print('源码升级完成，配置未变；备份：', backup)
    return backup


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('maibot', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-root', type=Path)
    args = parser.parse_args()
    update(args.maibot, args.apply, args.backup_root)
