"""Extract bounded read-only requests without suppressing unrelated clauses."""
import json
import re

READ = r'(?:查一下|查查|查询(?:一下)?|核实(?:一下)?|找一下|还记得|记不记得|你记得|帮我查(?:一下)?|帮我找(?:一下)?)'
NEGATIVE = r'(?:不要|别|不用|取消)\s*(?:再|去|帮我)?\s*$'


def plan(text):
    text = str(text)[:6000]
    memory = ''
    weather = None
    for match in re.finditer(READ, text):
        prefix = re.split(r'[，。！？!?；;\n]', text[:match.start()])[-1]
        if re.search(NEGATIVE, prefix) or re.search(r'提醒|记住|删除|忘记', prefix):
            continue
        tail = text[match.end():]
        # ASR partial text may not yet contain sentence punctuation.
        tail = re.split(READ, tail, maxsplit=1)[0]
        clause = re.split(r'[，。！？!?；;\n]|[吗嘛]|(?:我还|另外|顺便)(?:想|要|需要)?', tail, maxsplit=1)[0].strip(' 、')
        if re.search(r'(?:天气|下雨|降雨)', clause):
            target = re.split(r'(?:今天|明天|后天|下午|上午|晚上|的?天气|会不会下雨|是否下雨)', clause, maxsplit=1)[0].strip(' 的一下')
            if 2 <= len(target) <= 40 and not re.search(r'提醒|发送|删除|记住|忘记|[/\n:]', target):
                when = '明天下午' if '明天' in clause and '下午' in clause else '今天下午' if '下午' in clause else ''
                weather = {'city': target, 'days': 2 if '明天' in clause else 1, 'period': when}
        elif len(clause) >= 2 and clause != '一下' and not re.search(r'记住|忘记|删除|发送|发给|提醒', clause):
            memory = memory or clause
    # Weather-only natural questions can also start without a query verb.
    if weather is None:
        match = re.search(r'(?:^|[，。！？!?；;\n])\s*([\u4e00-\u9fff]{2,12}市(?:[\u4e00-\u9fff]{1,8}区)?)([^，。！？!?；;\n]{0,20}天气)', text)
        if match and not re.search(r'不要|不用|取消|别查', match.group(0)):
            weather = {'city': match[1], 'days': 2 if '明天' in match[2] else 1,
                       'period': ('明天' if '明天' in match[2] else '今天')+'下午' if '下午' in match[2] else ''}
    return {'memory': memory, 'weather': weather}


def encode(text):
    result = plan(text)
    if result['weather']:
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    return result['memory']


def decode(query):
    if str(query).startswith('{'):
        value = json.loads(query)
        return value if isinstance(value, dict) else {'memory': '', 'weather': None}
    return {'memory': query, 'weather': None}
