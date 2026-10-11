"""校验交付文件、禁止运行数据、Python语法与部署清单；不导入服务。"""
from pathlib import Path
import ast
import hashlib
import json

ROOT=Path(__file__).resolve().parents[1]

def main():
    hashes=json.loads((ROOT/'SHA256.json').read_text(encoding='utf-8'))
    for relative,expected in hashes.items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT):raise ValueError('校验路径越界')
        data=path.read_bytes()
        if hashlib.sha256(data.decode('utf-8-sig').replace('\r\n','\n').encode()).hexdigest()!=expected:raise ValueError('文件发生改变：'+relative)
        if path.suffix=='.py':ast.parse(data.decode('utf-8-sig'),filename=relative)
        if path.name in ('config.toml','settings.json','runtime-location.json','conf.yaml','model_dict.json') or path.suffix in ('.sqlite3','.db','.wav','.mp3','.onnx','.pyc'):
            raise ValueError('不应交付的运行数据：'+relative)
        if any(b'Users'+sep+b'32477' in data for sep in (b'\\',b'/')):raise ValueError('个人路径：'+relative)
    manifest=json.loads((ROOT/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))
    targets=set()
    for row in manifest['entries']:
        if hashes[row['path']]!=row['sha256']:raise ValueError('部署指纹不一致：'+row['path'])
        key=(row['project'],row['target'])
        if key in targets:raise ValueError('重复目标')
        targets.add(key)
        if 'baseline' in row and hashes[row['baseline']] not in row['accepted_before']:raise ValueError('基线不一致')
    print(json.dumps({'verified_files':len(hashes),'deployment_entries':len(targets),'python_syntax':'passed'},ensure_ascii=False))

if __name__=='__main__':main()
