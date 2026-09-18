"""取用固定上游并应用公开补丁；不启动机器人，也不复制私人配置。"""
import argparse
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='实际下载和应用；默认仅展示计划')
    args = parser.parse_args()
    entries = json.loads((ROOT / 'upstreams.json').read_text(encoding='utf-8'))
    for item in entries:
        dest = ROOT / item['directory']
        print(f"{item['name']}: {item['url']} @ {item['commit']}", flush=True)
        if not args.apply:
            continue
        if dest.exists():
            raise SystemExit(f'目录已存在，停止以保护修改：{dest}。请在全新克隆的项目中运行。')
        dest.parent.mkdir(parents=True, exist_ok=True)
        run('git', 'clone', '--no-checkout', item['url'], str(dest))
        run('git', 'checkout', '--detach', item['commit'], cwd=dest)
        if item['patch']:
            patch = str(ROOT / item['patch'])
            run('git', 'apply', '--check', patch, cwd=dest)
            run('git', 'apply', patch, cwd=dest)
    if args.apply:
        print('代码准备完成。请按 README 配置依赖、模型和自己的测试账号；机器人未启动。')


if __name__ == '__main__':
    main()
