"""天气工具是一个独立插件；原生Planner决定何时查询，插件执行真实HTTP请求。"""

from time import perf_counter
import asyncio

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .weather import WeatherClient, failure


class WeatherSettings(PluginConfigBase):
    __ui_label__ = "天气查询设置"
    config_version: str = Field(default="1.0.1", description="配置版本")
    enabled: bool = Field(default=True, description="启用真实天气查询；只发送城市/坐标，不发送聊天历史")
    country: str = Field(default="CN", max_length=2, description="国家过滤：CN中国；留空可查全球，同名地点需要澄清")
    timeout_seconds: int = Field(default=12, ge=3, le=30, description="全部网络步骤最长等待秒数")
    cache_seconds: int = Field(default=300, ge=0, le=900, description="同城市同查询短时缓存秒数；0关闭缓存")


class Config(PluginConfigBase):
    plugin: WeatherSettings = Field(default_factory=WeatherSettings)


class WeatherPlugin(MaiBotPlugin):
    config_model = Config

    @classmethod
    def build_config_schema(cls, **kwargs):
        schema = super().build_config_schema(**kwargs)
        labels = {'config_version': '配置版本', 'enabled': '启用天气查询', 'country': '国家范围',
                  'timeout_seconds': '查询最长等待（秒）', 'cache_seconds': '短时缓存（秒）'}
        for key, field in schema['sections']['plugin']['fields'].items():
            field['label'] = labels.get(key, key)
        return schema

    async def on_load(self):
        self._make_client()

    async def on_unload(self):
        """每次查询均自行关闭HTTP连接；没有常驻连接或后台任务需要释放。"""

    def _make_client(self):
        config = self.config.plugin
        self.client = WeatherClient(config.timeout_seconds, config.cache_seconds, config.country.upper())

    async def on_config_update(self, scope, config_data, version):
        self._make_client()

    @Tool("query_weather", description="天气查询 weather：查询指定城市今天/未来最多7天预报，或当前模型估计值。需要新鲜天气数据时使用；city必填，缺城市先询问，不可猜用户位置。days=0当前、1今天、2含明天。失败/地点歧义应如实告知。",
          parameters=[ToolParameterInfo(name="period", param_type=ToolParamType.STRING, description="可选：今天下午或明天下午，查询13至18点逐小时预报", required=False),
                      ToolParameterInfo(name="city", param_type=ToolParamType.STRING, description="明确城市名，如西安；不要传聊天历史", required=True),
                      ToolParameterInfo(name="days", param_type=ToolParamType.INTEGER, description="0当前，1今天，2今天和明天，最多7天", required=False),
                      ToolParameterInfo(name="purpose", param_type=ToolParamType.STRING, description="可选：一句话说明为何需要天气数据，不要输出内部推理", required=False)])
    async def query_weather(self, city: str = "", days: int = 1, purpose: str = "", period: str = "", **kwargs):
        started = perf_counter()
        # 只写本插件运行日志；共享消息卡片如已接入，仍由原有宿主统一负责。
        if not self.config.plugin.enabled:
            result = {'success': False, 'content': '天气插件已关闭，未进行联网查询。', 'error': '天气插件已关闭'}
        else:
            try:
                result = await self.client.fetch(city, days, period) if period else await self.client.fetch(city, days)
            except asyncio.CancelledError:
                self._get_logger().info('天气查询已取消，未返回有效天气。')
                raise
            except Exception as exc:
                result = failure(exc)
        status = '成功' if result['success'] else '失败'
        self._get_logger().info(f"天气查询｜{status}｜耗时{perf_counter() - started:.2f}秒\n{result['content']}")
        return result

    @Command("weather", description="直接查询城市今天预报；不依赖模型选择工具", pattern=r"^/天气\s+(?P<city>[^\r\n]{2,80})$")
    async def weather_command(self, city: str = "", stream_id: str = "", **kwargs):
        # 宿主把正则捕获值放在matched_groups中，不会直接作为city参数传入。
        city = (kwargs.get('matched_groups') or {}).get('city', city).strip()
        started = perf_counter()
        result = await self.query_weather(city=city)
        # 指令绕过Planner，所以单独留下明确来源的记录。
        message = kwargs.get('message') or {}
        try:
            # 可选接入已有日志，不要求同伴安装/覆盖旧心情和记忆交付包。
            from heart_shared.tool_audit import record_tool
            await asyncio.to_thread(record_tool, stream_id, 'query_weather', {'city': city, 'days': 1}, result,
                                    evidence_ids=[str(message.get('message_id'))] if message.get('message_id') else [],
                                    duration_ms=(perf_counter() - started) * 1000, source='用户天气指令')
        except ModuleNotFoundError as exc:
            if exc.name in {'heart_shared', 'heart_shared.tool_audit'}:
                self._get_logger().warning('未安装可选共享工具日志接口；天气已查询，结果保留普通运行日志，回复不受影响。')
            else:
                self._get_logger().exception('已有共享日志接口缺少内部依赖；天气仍返回，不重复查询。')
        except Exception:
            self._get_logger().exception('天气已查询，但指令审计写入失败；不会重复请求天气')
        await self.ctx.send.text(result['content'], stream_id)
        return bool(result['success']), result['content'], True


def create_plugin():
    return WeatherPlugin()
