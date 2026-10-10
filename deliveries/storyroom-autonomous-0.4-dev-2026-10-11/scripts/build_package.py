"""Build a public source-only delivery with an independent SHA-256 manifest."""
import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    '.gitignore', 'run.py', 'requirements.txt', 'README.md', 'README.upstream.md',
    'LICENSE', 'THIRD_PARTY.md', 'model-config.example.json', 'local-config.example.json',
    'vendor-manifest.json', 'launch.py', 'start_local.py', 'host_launcher.py', 'VALIDATION.json',
}
SOURCE_DIRS = {'storyroom', 'web', 'plugins', 'stories', 'tests', 'scripts'}
SKIP_PARTS = {'__pycache__', '.git', '.venv', '.runtime', 'logs', '.test-tmp', '.secrets'}


def source_files():
    selected = []
    for file in ROOT.rglob('*'):
        if not file.is_file() or file.is_symlink():
            continue
        rel = file.relative_to(ROOT)
        if any(p in SKIP_PARTS for p in rel.parts):
            continue
        if file.suffix in {'.pyc', '.log', '.sqlite3'} or file.name in {'config.toml', 'model-config.json', 'local-config.json'}:
            continue
        if rel.as_posix() == 'docs/本次实现与验收.md' or rel.parts[0] in SOURCE_DIRS or rel.as_posix() in ROOT_FILES:
            selected.append(file)
    return sorted(selected, key=lambda f: f.relative_to(ROOT).as_posix())


def build(destination):
    files = source_files()
    for file in files:
        if re.search(rb'\bsk-(?:fish-)?[A-Za-z0-9_-]{24,}', file.read_bytes()):
            raise ValueError('可能包含真实凭据，请检查：' + str(file.relative_to(ROOT)))
    manifest = {'version': '0.4-dev', 'scope': 'public-source-only',
                'files': [{'path': f.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(f.read_bytes()).hexdigest()} for f in files]}
    (ROOT / 'INSTALL_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in files + [ROOT / 'INSTALL_MANIFEST.json']:
            archive.write(file, 'storyroom-next/' + file.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():
            raise ValueError('压缩包校验失败')
        for entry in manifest['files']:
            if hashlib.sha256(archive.read('storyroom-next/' + entry['path'])).hexdigest() != entry['sha256']:
                raise ValueError('文件指纹不一致')
    fingerprint = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(fingerprint + '  ' + destination.name + '\n', encoding='utf-8')
    print(f'公开交付包：{destination.name}，{len(files)+1} 个文件，{destination.stat().st_size} 字节。CRC 与 SHA-256 校验通过。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    build(parser.parse_args().output)
