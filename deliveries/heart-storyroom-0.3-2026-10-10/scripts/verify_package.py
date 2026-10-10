"""只核对交付指纹，不安装、不联网、不读取用户配置。"""
from pathlib import Path
import hashlib
import json


root = Path(__file__).resolve().parents[1]
manifest = root/'INSTALL_MANIFEST.json'
if not manifest.exists():
    raise SystemExit('仅打包交付目录附带INSTALL_MANIFEST.json；本机开发目录可直接运行测试。')
files = json.loads(manifest.read_text(encoding='utf-8'))['files']
errors = []
for entry in files:
    file = (root/entry['path']).resolve()
    if not file.is_relative_to(root.resolve()) or not file.is_file():
        errors.append(entry['path'])
    elif hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
        errors.append(entry['path'])
if errors:
    raise SystemExit('校验失败：' + ', '.join(errors))
print(f'交付指纹核对通过：{len(files)}个文件；没有修改任何文件。')
