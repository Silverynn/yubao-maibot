from pathlib import Path
from types import ModuleType, SimpleNamespace
import asyncio
import importlib.util
import sys

ROOT=Path(__file__).resolve().parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


package=ModuleType('route2plugin');package.__path__=[str(ROOT/'maibot/plugins/heart-realtime')]
sys.modules[package.__name__]=package
engine=load('route2plugin.engine',ROOT/'maibot/plugins/heart-realtime/engine.py')
drafts=load('route2drafts',ROOT/'maibot/src/services/heart_realtime_drafts.py')
host=load('route2host',ROOT/'maibot/src/services/heart_realtime.py')


class Item:
    def __init__(self,text=''):self.text=text


class Builder:
    def __init__(self):self.text=''
    def set_role(self,role):return self
    def add_text_content(self,text):self.text=text;return self
    def build(self):return Item(self.text)


class Generator:
    def __init__(self,model):self.express_model=model;self.persona='原角色';self.extra='已确认心情：平静'
    def _get_runtime_manager(self):return self
    async def invoke_hook(self,*args,**kwargs):
        return SimpleNamespace(kwargs={**kwargs,'extra_prompt':self.extra})
    def _build_reply_requirements(self,text,unused):return text
    def _build_history_messages(self,history,visual):return [Item(str(x)) for x in history]
    def _build_keyword_reaction_prompt(self,**kwargs):return ''
    def _build_system_prompt(self,*args,**kwargs):return self.persona
    def _build_request_messages(self,**kwargs):return [Item(self.persona),*[Item(str(x)) for x in kwargs['chat_history']]]


class Model:
    def __init__(self):self.calls=[];self.block_first=False;self.block_all=False;self.cancelled=0
    async def generate_response_with_context(self,*,context_factory,options):
        items=await context_factory(None,None)
        self.calls.append([x.text for x in items])
        try:
            if self.block_all or (self.block_first and len(self.calls)==1):await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled+=1;raise
        return SimpleNamespace(response='原角色回复。'+str(len(self.calls)),tool_calls=None,
            model_name='隔离模型桩',prompt_tokens=10,completion_tokens=5,total_tokens=15)


def module(name,**values):
    value=ModuleType(name);value.__dict__.update(values);return value


def native_modules(runtime,generator):
    class SessionBacked:
        def __init__(self,**kwargs):self.__dict__.update(kwargs)
    async def get_runtime(sid):return runtime
    return {
        'src.services.heart_realtime':host,
        'src.services.heart_realtime_drafts':drafts,
        'src.chat.heart_flow.heartflow_manager':module('manager',heartflow_manager=SimpleNamespace(
            get_or_create_heartflow_chat=get_runtime,heartflow_chat_list={'session':runtime})),
        'src.chat.replyer.replyer_manager':module('replyers',replyer_manager=SimpleNamespace(get_replyer=lambda **k:generator)),
        'src.maisaka.context.messages':module('messages',SessionBackedMessage=SessionBacked),
        'src.common.data_models.llm_service_data_models':module('models',LLMGenerationOptions=lambda **k:SimpleNamespace(**k)),
        'src.llm_models.payload_content.context_item':module('items',ContextItemBuilder=Builder,
            RoleType=SimpleNamespace(System='system',User='user'),get_item_text=lambda x:x.text),
    }
