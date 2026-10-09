"""执行实际选模、Schema与两类查重代码；隔离宿主启动及付费接口。"""
import ast
import functools
import inspect
import json
import logging
import sys
import tomllib
import types
import typing
from pathlib import Path
from types import SimpleNamespace as NS
import pydantic
from pydantic_core import PydanticUndefined

WORK=Path(__file__).resolve().parents[1]
MAI=WORK/'maibot'
BASE=MAI
checks=[]
def ok(name,value):
    assert value,name
    checks.append(name)
def tree(rel): return ast.parse((BASE/rel).read_text(encoding='utf-8-sig'))
def load(nodes, ns, rel): exec(compile(ast.Module(nodes,type_ignores=[]),str(BASE/rel),'exec'),ns)
def run(coroutine):
    # 所有外部接口都是立即返回的异步替身；无需Windows网络事件循环。
    try: coroutine.send(None)
    except StopIteration as result: return result.value
    raise AssertionError('测试意外触发真实异步等待')
ns=dict(vars(typing)); ns.update(ConfigBase=pydantic.BaseModel,Field=pydantic.Field)
load([n for n in tree('src/config/model_configs.py').body if isinstance(n,ast.ClassDef) and n.name in {'TaskConfig','ModelTaskConfig'}],ns,'src/config/model_configs.py')
Task=ns['TaskConfig']; Tasks=ns['ModelTaskConfig']
raw=json.loads((WORK/'tests/legacy_tasks.json').read_text(encoding='utf-8'))
tasks=Tasks.model_validate(raw['model_task_config'])
for name in raw['model_task_config']:
    if name in Tasks.model_fields:
        ok('保留旧任务:'+name, getattr(tasks,name).model_dump()==Task(**raw['model_task_config'][name]).model_dump())
for name in ['heart_mood','heart_memory_dedup']:
    ok(name+'默认留空',getattr(tasks,name).model_list==[])
    f=Tasks.model_fields[name]
    ok(name+'普通列表可见',f.json_schema_extra['advanced'] is False)
    ok(name+'三语标签',set(f.json_schema_extra['label'])=={'zh_CN','en_US','ja_JP'})

# 执行真实WebUI Schema生成器，页面正是据此渲染任务和模型选择器。
schema_ns=dict(vars(typing)); schema_ns.update(ConfigBase=pydantic.BaseModel,inspect=inspect,lru_cache=functools.lru_cache,PydanticUndefined=PydanticUndefined)
schema_path=WORK/'tests/support/config_schema.py'
load([n for n in ast.parse(schema_path.read_text(encoding='utf-8-sig')).body if isinstance(n,(ast.Assign,ast.AnnAssign,ast.ClassDef))],schema_ns,'src/webui/config_schema.py')
gen=schema_ns['ConfigSchemaGenerator']; gen._get_class_field_docs=staticmethod(lambda cls:{})
schema=gen.generate_config_schema(Tasks)
for name in ['heart_mood','heart_memory_dedup']:
    field=next(f for f in schema['fields'] if f['name']==name)
    ok(name+'真实Schema输出',not field['advanced'] and field['label']['zh_CN'])
# Schema在内存中核验，不修改包内文件。
ok('两个任务默认模板存在',all(k in (MAI/'src/config/default_model_config.py').read_text(encoding='utf-8') for k in ['heart_mood','heart_memory_dedup']))

# 实际调度器的空配置继用及热更新行为。
orchns=dict(vars(typing)); orchns.update(TaskConfig=Task,config_manager=NS(get_model_config=lambda:NS(model_task_config=tasks)))
module=tree('src/llm_models/utils_model.py')
fallback=next(n for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='EMPTY_TASK_FALLBACKS' for t in n.targets))
klass=next(n for n in module.body if isinstance(n,ast.ClassDef) and n.name=='LLMOrchestrator')
klass.body=[n for n in klass.body if isinstance(n,ast.FunctionDef) and n.name in {'_get_task_config_or_raise','_refresh_task_config'}]
load([fallback,klass],orchns,'src/llm_models/utils_model.py')
Orch=orchns['LLMOrchestrator']
for name in ['heart_mood','heart_memory_dedup']:
    obj=Orch(); obj.task_name=name
    ok(name+'留空继用utils全配置',obj._get_task_config_or_raise() is tasks.utils)
    target=getattr(tasks,name); target.model_list=['自选文本模型']
    ok(name+'选择专用配置',obj._get_task_config_or_raise() is target)
    obj.model_for_task=target;obj.model_usage={'自选文本模型':(0,0,0)}
    target.model_list=['更换后的文本模型']
    obj._refresh_task_config()
    ok(name+'热更新切换列表',list(obj.model_usage)==['更换后的文本模型'])
    target.model_list=[]
    ok(name+'清空恢复原utils',obj._refresh_task_config() is tasks.utils)
    obj.task_name='拼写错误'
    try: obj._get_task_config_or_raise()
    except ValueError: ok('未知任务不静默回退:'+name,True)
    else: raise AssertionError('未知任务被隐藏')

calls=[]
async def fake_generate(request):
    calls.append(request)
    return NS(to_capability_payload=lambda:{'success':True,'response':'{}','model_name':request.model_name or ('专用模型' if request.task_name=='heart_mood' else '默认模型')})
api=types.ModuleType('src.services.llm_service');api.get_available_models=lambda:{name:getattr(tasks,name) for name in Tasks.model_fields}
api.resolve_task_name=lambda name:name if name in Tasks.model_fields else (_ for _ in ()).throw(ValueError('unknown task'))
api.LLMServiceRequest=lambda **kw:NS(**kw);api.generate=fake_generate
services=types.ModuleType('src.services');services.llm_service=api
src=types.ModuleType('src');src.services=services
budget=types.ModuleType('src.services.heart_llm_budget')
async def fake_budget(fn,request,timeout):
    ok('心情调用方等待时限保留',timeout==20)
    return await fn(request)
budget.generate_with_budget=fake_budget
for name,value in [('src',src),('src.services',services),('src.services.llm_service',api),('src.services.heart_llm_budget',budget)]: sys.modules[name]=value
coretree=tree('src/plugin_runtime/capabilities/core.py')
coreclass=next(n for n in coretree.body if isinstance(n,ast.ClassDef) and n.name=='RuntimeCoreCapabilityMixin')
coreclass.body=[n for n in coreclass.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_cap_llm_generate']
corens=dict(vars(typing));corens.update(logger=logging.getLogger('test'),_DEFAULT_PLUGIN_LLM_TASK='utils')
load([n for n in coretree.body if isinstance(n,ast.FunctionDef) and n.name in {'_resolve_llm_capability_route','_normalize_prompt_arg'}]+[coreclass],corens,'src/plugin_runtime/capabilities/core.py')
manager=corens['RuntimeCoreCapabilityMixin']()
args={'prompt':'虚构验收','task_name':'utils','model_name':'旧手填模型','max_tokens':4096,'temperature':0.2,'request_timeout_seconds':20}
payload=run(manager._cap_llm_generate('heart.mood','llm.generate',args))
ok('未配置专用列表保留旧手填',calls[-1].model_name=='旧手填模型' and calls[-1].task_name=='utils')
ok('旧手填日志来源',payload['model_selection_source']=='插件手填模型')
tasks.heart_mood.model_list=['专用模型']
payload=run(manager._cap_llm_generate('heart.mood','llm.generate',args))
ok('页面选择优先旧手填',calls[-1].task_name=='heart_mood' and calls[-1].model_name is None)
ok('不缩减心情输出预算',calls[-1].max_tokens==4096 and calls[-1].temperature==0.2)
ok('页面选择日志来源',payload['model_selection_source']=='模型配置页面' and payload['model_task']=='heart_mood' and payload['model_name']=='专用模型')
run(manager._cap_llm_generate('other.plugin','llm.generate',args))
ok('其他插件不改变路由',calls[-1].task_name=='utils' and calls[-1].model_name=='旧手填模型')
run(manager._cap_llm_generate('heart.mood','llm.generate',dict(args,task_name='replyer')))
ok('非心情任务保持显式选择',calls[-1].task_name=='replyer')
tasks.heart_mood.model_list=[]
payload=run(manager._cap_llm_generate('heart.mood','llm.generate',dict(args,model_name=None)))
ok('两处都留空恢复utils',calls[-1].task_name=='utils' and calls[-1].model_name is None and payload['model_selection_source']=='utils默认模型')
diag={}
load([n for n in tree('plugins/heart_mood/appraiser.py').body if isinstance(n,ast.FunctionDef) and n.name=='response_diagnostics'],diag,'plugins/heart_mood/appraiser.py')
info=diag['response_diagnostics'](payload,4096)
ok('心情审计保留任务与实际模型',info['model_task']=='utils' and info['model_name']=='默认模型')
ok('心情诊断不保存正文','response' not in info)

# 执行人物事实与摘要judge的真实函数，模型服务替换为可控JSON。
import re
async def fake_response(prompt,**kw): return NS(response='{"verdict":"duplicate"}')
class Client:
    def __init__(self,**kw): calls.append(NS(**kw))
    generate_response=staticmethod(fake_response)
api.LLMServiceClient=Client
backendtree=tree('src/services/heart_memory_backend.py')
backend=next(n for n in backendtree.body if isinstance(n,ast.ClassDef) and n.name=='NativeBackend')
backend.body=[n for n in backend.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='judge']
bns={'json':json,'re':re,'settings':lambda:{'conflict_guidance':''}}
load([backend],bns,'src/services/heart_memory_backend.py')
result=run(bns['NativeBackend']().judge({'text':'虚构事实','chat_id':'test'},[{'hash':'old','content':'虚构旧事实'}],{'evidence':['虚构原话']}))
ok('人物事实查重实际请求专用任务',calls[-1].task_name=='heart_memory_dedup' and calls[-1].request_type=='heart.memory_conflict')
ok('人物判断原JSON解析保留',result['verdict']=='duplicate')
async def summary_generate(req):
    calls.append(req)
    return NS(success=True,completion=NS(response='{"verdict":"new"}'))
api.generate=summary_generate
sns={'json':json,'re':re,'build_prompt':lambda *a:'虚构摘要数据','parse':lambda *a:a[0]}
load([n for n in tree('heart_shared/summary_semantic.py').body if isinstance(n,ast.AsyncFunctionDef) and n.name=='judge'],sns,'heart_shared/summary_semantic.py')
run(sns['judge']('测试摘要',[],[],[],{'guidance':'','max_tokens':4096,'min_confidence':0.9},'test'))
ok('摘要查重实际请求专用任务',calls[-1].task_name=='heart_memory_dedup' and calls[-1].request_type=='heart.summary_semantic')
ok('摘要预算与会话保持',calls[-1].max_tokens==4096 and calls[-1].session_id=='test' and calls[-1].temperature==0)
for fn in ['parse','recall','evaluate','record_decision','capture']:
    before=ast.parse((WORK/'baseline/maibot/heart_shared/summary_semantic.py').read_text(encoding='utf-8-sig'))
    get=lambda t:ast.dump(next(n for n in t.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==fn))
    ok('摘要保护逻辑不变:'+fn,get(before)==get(tree('heart_shared/summary_semantic.py')))
for entry in json.loads((WORK/'INSTALL_MANIFEST.json').read_text(encoding='utf-8'))['entries']:
    if entry['target'].endswith('.py'): tree(entry['target'])
ok('所有补丁Python语法通过',True)
print(json.dumps({'passed':len(checks),'paid_api_calls':0,'bot_started':False},ensure_ascii=False))
