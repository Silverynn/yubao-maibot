"""可搬迁的运行配置与识别环境诊断；不安装依赖，不猜测另一台电脑的目录。"""
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import json
import os
import subprocess
import sys


VERSION = '0.2.1'
HOST_API_VERSION = 1
MODEL_NAME = 'sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30'
DEFAULTS = {
    'enabled': True, 'port': 12396, 'asr_port': 12397,
    'asr_python': '', 'model': 'models/'+MODEL_NAME,
    'asr_dependencies': 'asr-dependencies', 'facts_file': 'facts.json',
    'character_names': [],
    'stable_seconds': .65, 'query_wait_seconds': 1.0, 'query_timeout_seconds': 4.0,
}


def resolve_settings(root: Path, overrides: Mapping[str, Any],
                     environment: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    env = os.environ if environment is None else environment
    values = {**DEFAULTS, **overrides}
    for name, setting in [('HEART_RT_ASR_PYTHON','asr_python'),('HEART_RT_MODEL','model'),
                          ('HEART_RT_ASR_DEPS','asr_dependencies')]:
        if env.get(name):values[setting] = env[name]
    if type(values['enabled']) is not bool:raise ValueError('enabled 必须为布尔值')
    for name in ('port','asr_port'):
        if type(values[name]) is not int or not 1<=values[name]<=65535:
            raise ValueError(name+' 必须为1到65535之间的端口')
    if values['port']==values['asr_port']:raise ValueError('插件端口和流式识别端口不能相同')
    names=values['character_names']
    if not isinstance(names,list) or len(names)>16 or any(not isinstance(x,str) or not 1<=len(x)<=80 for x in names):
        raise ValueError('character_names 必须为最多16个非空角色名字/别名')
    for name in ('asr_python','model','asr_dependencies'):
        if name=='asr_dependencies' and values[name]=='':
            continue  # 空值明确表示使用所选解释器自身的依赖，不额外注入目录。
        raw = values[name] or (sys.executable if name=='asr_python' else '')
        if not isinstance(raw,str):raise ValueError(name+' 必须为路径字符串')
        if not raw:raise ValueError(name+' 不能是空路径')
        path=Path(raw).expanduser()
        values[name]=str((path if path.is_absolute() else root/path).resolve())
    return values


def check_asr_runtime(root: Path, settings: Mapping[str, Any]) -> Dict[str, Any]:
    """用选择的解释器实际导入运行库，发现跨Python版本的二进制不匹配。"""
    model=Path(settings['model']);python=Path(settings['asr_python'])
    errors=[]
    if not python.is_file():errors.append('识别解释器不存在：'+str(python))
    missing=[x for x in ('tokens.txt','encoder.int8.onnx','decoder.onnx','joiner.int8.onnx') if not (model/x).is_file()]
    if missing:errors.append('模型文件缺失：'+', '.join(missing)+'；目录：'+str(model))
    report={'ready':False,'python':str(python),'model':str(model),
            'dependencies':str(settings['asr_dependencies']),'errors':errors}
    if errors:return report
    probe="""import sys,json,importlib
import pathlib
deps=sys.argv[1]
if pathlib.Path(deps).is_dir():sys.path.insert(0,deps)
result={'python_version':sys.version.split()[0]}
try:
    import aiohttp,numpy,sherpa_onnx
    result.update(sherpa_version=sherpa_onnx.__version__,numpy_version=numpy.__version__,
        api_available=hasattr(sherpa_onnx.OnlineRecognizer,'from_transducer'))
except Exception as error:
    result['error']=type(error).__name__+': '+str(error)
print(json.dumps(result))
"""
    try:
        result=subprocess.run([str(python),'-c',probe,str(settings['asr_dependencies'])],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            errors.append('识别环境探测失败：'+result.stderr[-1500:])
        else:
            details=json.loads(result.stdout.strip());report.update(details)
            if details.get('error'):errors.append('识别运行库无法导入：'+details['error'])
            else:
                version=tuple(int(x) for x in details['sherpa_version'].split('.')[:3])
                if version<(1,13,8) or not details['api_available']:
                    errors.append('此流式模型需要 sherpa-onnx >=1.13.8 及 OnlineRecognizer.from_transducer')
    except (OSError,subprocess.TimeoutExpired,ValueError) as error:
        errors.append('识别环境探测失败：'+type(error).__name__+': '+str(error))
    report['ready']=not errors
    return report


def binding_names(binding: Mapping[str, Any], settings: Mapping[str, Any]) -> list[str]:
    if binding.get('api_version')!=HOST_API_VERSION:
        raise RuntimeError('MaiBot实时宿主接入版本不匹配；请同步兼容包中的宿主接缝，不能只复制插件')
    names=settings['character_names'] or binding.get('character_names',[])
    if not isinstance(names,list) or any(not isinstance(x,str) or not 1<=len(x)<=80 for x in names):
        raise ValueError('宿主返回的角色名称不合法')
    return names[:16]
