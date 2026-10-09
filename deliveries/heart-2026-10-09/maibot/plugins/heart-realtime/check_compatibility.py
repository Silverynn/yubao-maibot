"""离线检查本机流式识别配置，不启动 MaiBot、不调用模型、不修改文件。"""
from pathlib import Path

import json

from runtime_config import check_asr_runtime, resolve_settings


if __name__ == '__main__':
    root=Path(__file__).resolve().parent
    path=root/'settings.json'
    overrides=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    try:
        settings=resolve_settings(root,overrides)
        result=check_asr_runtime(root,settings)
    except (ValueError,OSError) as error:
        result={'ready':False,'errors':[str(error)]}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['ready'] else 1)
