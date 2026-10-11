"""无需额外语义判定模型的提前生成门禁；宁可弃稿，不猜测用户改口的含义。"""
import hashlib
import json
import re


def normalize_input(text: str) -> str:
    # 仅忽略识别器常补的空白/句末标点；否定、数字、内部标点仍是有效信息。
    return re.sub(r'\s+', ' ', str(text)).strip().rstrip('。！？!?，,；;')


def eligibility_reason(text: str):
    value = normalize_input(text)
    if len(value) < 6:
        return ('too_short', f'临时转写只有{len(value)}字，至少需要6字')
    if len(value) > 240:
        return ('too_long', f'临时转写有{len(value)}字，超过240字上限')
    if value.startswith('/'):
        return ('command', '临时转写是斜杠命令，必须等正式提交')
    match = re.search(r'记住|忘记|删除|提醒|发送|发给|保存|写入|运行|执行|支付|转账|购买|创建|取消任务', value)
    if match:
        return ('confirmation_required', f'命中“{match.group()}”，可能涉及需确认的操作，等待正式转写；只读查询仍可继续')
    # 轻量的近结束判断，不能声称是通用语义轮次模型。
    match = re.search(r'(但是|然后|因为|如果|还有|以及|并且|例如|比如|或者|首先|查一下|帮我)$', value)
    if match:
        return ('incomplete', f'临时转写以“{match.group()}”结尾，句子可能未说完')
    return None


def eligible(text: str) -> bool:
    return eligibility_reason(text) is None


def evidence_key(result) -> str:
    """来源版本/排序变化不等于事实改变；比较这轮提供给模型的实际材料。"""
    if result is None:
        return ''
    native = sorted((str(x.get('id', '')), str(x.get('content', '')), str(x.get('status', '')))
                    for x in result.get('native', {}).get('items', []))
    local = sorted((str(x['key']), str(x['value'])) for x in result.get('local', {}).get('items', []))
    return hashlib.sha256(json.dumps([native, local, [(k, result[k].get('success', True), result[k].get('error', '')) for k in ('native', 'local', 'weather') if isinstance(result.get(k), dict)], result.get('request', {}), result.get('weather', {}).get('forecast_key') or result.get('weather', {}).get('content', '')], ensure_ascii=False).encode()).hexdigest()
