"""宿主侧的可选模型调用预算；RPC停止等待不等于HTTP请求已取消。"""
import asyncio
import math


async def generate_with_budget(generate, request, seconds=None):
    """旧调用不变；提供预算时，在真正执行模型请求的宿主进程计时和取消。

    asyncio.wait_for会等待被取消协程清理。不能保证提供商停止已开始的计费，
    但本地请求和连接会走原生取消流程，不继续静默重试到任务默认硬超时。
    """
    if seconds is None:
        return await generate(request)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 120:
        raise ValueError('request_timeout_seconds必须是1到120之间的有限秒数')
    return await asyncio.wait_for(generate(request), timeout=seconds)
