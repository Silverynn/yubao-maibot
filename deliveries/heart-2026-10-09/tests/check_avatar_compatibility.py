"""测试现有动态表情桥接，使用临时的两套模型声明，不更改现有模型或心情代码。"""
from pathlib import Path
from types import SimpleNamespace

import argparse
import importlib.util
import json
import sys
import tempfile

parser=argparse.ArgumentParser();parser.add_argument('--vt-root',required=True)
vt=Path(parser.parse_args().vt_root).resolve();sys.path.insert(0,str(vt))
import expression_catalog as catalog
from heart_bridge import HeartBridge

checks=[]
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder);catalog.__file__=str(root/'expression_catalog.py')
    models=[]
    for name,prefix in [('角色甲','A'),('角色乙','B')]:
        files=root/'live2d-models'/prefix;files.mkdir(parents=True)
        entries=[]
        for label in ('neutral','joy'):
            filename=prefix+'-'+label+'.exp3.json'
            (files/filename).write_text(json.dumps({'Type':'Live2D Expression','Parameters':[]}),encoding='utf-8')
            entries.append({'Name':prefix+'-'+label,'File':filename})
        (files/'model.model3.json').write_text(json.dumps({'FileReferences':{'Expressions':entries}}),encoding='utf-8')
        models.append(SimpleNamespace(live2d_model_name=name,emo_map={'neutral':prefix+'-neutral','joy':prefix+'-joy'},
            model_info={'name':name,'url':'/live2d-models/'+prefix+'/model.model3.json',
                        'expressionAliases':{'开心':prefix+'-joy'}}))
    current=models[0];bridge=HeartBridge(None,model_provider=lambda:current)
    assert bridge._resolve('开心')[0]=='A-joy';checks.append('角色甲读取自身表情')
    current=models[1]
    assert bridge._resolve('开心')[0]=='B-joy';checks.append('换角色后读取新模型而不是本地旧表情')
    assert bridge.expressions()['model']=='角色乙';checks.append('模型来源实时更新')
    assert catalog.model_expression_aliases(current)=={'开心':'B-joy'};checks.append('别名跟随模型')
    assert bridge._resolve('未支持的情绪')[0]=='B-neutral';checks.append('不支持的情绪使用当前模型的中性表情')
print(json.dumps({'scope':__doc__,'passed':len(checks),'checks':checks},ensure_ascii=False))
