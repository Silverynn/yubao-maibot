"""离线预检，只报告版本与资源完整性，不打印密钥、不调用模型。"""
from pathlib import Path
from importlib.metadata import version, PackageNotFoundError
import argparse
import json
import sys


def inspect_vt(root):
    failures = []
    for model in json.loads((root/'model_dict.json').read_text(encoding='utf-8')):
        url = model.get('url', '')
        if not url.startswith('/live2d-models/'):
            continue
        file = (root/url.lstrip('/')).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file():
            failures.append(model['name'] + '：缺少模型入口')
            continue
        data = json.loads(file.read_text(encoding='utf-8'))
        refs = data.get('FileReferences', {})
        paths = [refs.get('Moc', '')] + refs.get('Textures', [])
        paths += [e['File'] for e in refs.get('Expressions', [])]
        paths += [m['File'] for group in refs.get('Motions', {}).values() for m in group]
        for relative in filter(None, paths):
            resolved = (file.parent/relative).resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
                failures.append(model['name'] + '：缺少或越界资源 ' + relative)
        names = {e['Name'] for e in refs.get('Expressions', [])}
        for emotion, expression in model.get('emotionMap', {}).items():
            if isinstance(expression, int):
                valid = 0 <= expression < len(names)
            else:
                valid = expression in names
            if not valid:
                failures.append(model['name'] + '：情绪映射无法执行 ' + emotion)
    return failures


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vtuber-root', type=Path)
    args = parser.parse_args()
    try:
        sdk = version('maibot-plugin-sdk')
    except PackageNotFoundError:
        raise SystemExit('当前Python没有MaiBot SDK；请使用MaiBot自己的Python运行预检。')
    print('Python:', sys.version.split()[0], 'MaiBot SDK:', sdk)
    print('已验收环境：Python3.12 / MaiBot1.2.5 / SDK2.8.1；其他版本需重新执行测试。')
    if sdk != '2.8.1':
        raise SystemExit('SDK与已验收版本不同；请先在隔离环境验证，不要覆盖主程序或已有语音插件。')
    if args.vtuber_root:
        errors = inspect_vt(args.vtuber_root)
        for error in errors:
            print(error)
        print('Live2D资源预检：', '通过' if not errors else '需处理上述资源问题')
        raise SystemExit(bool(errors))
