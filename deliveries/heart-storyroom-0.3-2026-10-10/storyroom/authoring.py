"""可编辑但有边界的剧本；保存模板不会悄悄改变正在玩的房间。"""
import re
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Shape(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Edge(Shape):
    id: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=100)
    requires: list[str] = Field(default_factory=list, max_length=50)


class Stage(Shape):
    title: str = Field(min_length=1, max_length=100)
    scene: str = Field(default='harbor', max_length=50)
    description: str = Field(min_length=1, max_length=6000)
    outlook: str = Field(default='', max_length=2000)
    next: list[Edge] = Field(default_factory=list, max_length=50)
    ending: bool = False


class Script(Shape):
    id: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=100)
    world: str = Field(min_length=1, max_length=12000)
    start: str = Field(min_length=1, max_length=50)
    flags: list[str] = Field(default_factory=list, max_length=100)
    nodes: dict[str, Stage] = Field(min_length=1, max_length=100)


def validate_story(value):
    from .store import RoomError, dump
    try:
        story = Script.model_validate(value).model_dump()
    except ValidationError as error:
        first = error.errors()[0]
        raise RoomError('剧本字段错误：' + '.'.join(map(str, first['loc'])) + ' ' + first['msg'], 422) from None
    if len(dump(story).encode('utf-8')) > 100000:
        raise RoomError('剧本超过100KB', 422)
    names = [story['id'], *story['nodes'], *story['flags']]
    if any(not re.fullmatch(r'[\w-]{1,50}', name) for name in names):
        raise RoomError('剧本、阶段和标记ID只能使用文字、数字、下划线或短横线', 422)
    if story['start'] not in story['nodes'] or len(set(story['flags'])) != len(story['flags']):
        raise RoomError('起点不存在或标记ID重复', 422)
    for identifier, node in story['nodes'].items():
        targets = [edge['id'] for edge in node['next']]
        if len(set(targets)) != len(targets):
            raise RoomError('同阶段不能重复定义同一分支：' + identifier, 422)
        if node['ending'] and targets:
            raise RoomError('结局不能继续开启主线：' + identifier, 422)
        if not node['ending'] and not targets:
            raise RoomError('无后续分支的阶段应标记为结局：' + identifier, 422)
        if any(t not in story['nodes'] for t in targets):
            raise RoomError('分支指向不存在的阶段：' + identifier, 422)
        if any(f not in story['flags'] for edge in node['next'] for f in edge['requires']):
            raise RoomError('分支使用未定义的标记：' + identifier, 422)
    reachable, todo = set(), [story['start']]
    while todo:
        identifier = todo.pop()
        if identifier in reachable:
            continue
        reachable.add(identifier)
        todo.extend(edge['id'] for edge in story['nodes'][identifier]['next'])
    if reachable != set(story['nodes']):
        raise RoomError('存在从起点无法到达的阶段', 422)
    endings = {key for key, node in story['nodes'].items() if node['ending']}
    can_finish = set(endings)
    while True:
        extended = can_finish | {key for key, node in story['nodes'].items()
                                 if any(edge['id'] in can_finish for edge in node['next'])}
        if extended == can_finish:
            break
        can_finish = extended
    if can_finish != reachable:
        raise RoomError('有阶段无法通向任何结局；请检查闭环或补充结局', 422)
    return story
