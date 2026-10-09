"""仅本次进程生效的启动隔离策略，不修改插件的永久启用配置。"""

from typing import Dict

import os

LIVE2D_ONLY_ENV = "MAIBOT_LIVE2D_ONLY"
QQ_ADAPTER_ID = "maibot-team.napcat-adapter"


def get_launch_disabled_plugin_reasons() -> Dict[str, str]:
    """Live2D专用入口暂不加载QQ适配器；普通入口保持原来的加载规则。"""
    if os.environ.get(LIVE2D_ONLY_ENV) == "1":
        return {QQ_ADAPTER_ID: "本次为Live2D专用模式，QQ适配器保持离线；请通过普通MaiBot入口启用QQ。"}
    return {}
