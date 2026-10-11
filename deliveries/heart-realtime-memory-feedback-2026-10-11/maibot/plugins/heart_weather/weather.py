"""固定 Open-Meteo 接口：只发送地点，不读取聊天历史，不让模型编造天气。"""

from collections import OrderedDict
import hashlib
from datetime import datetime, timezone
from math import isfinite
from time import monotonic
import asyncio
import copy
import re

import httpx


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CODES = {0: "晴", 1: "大部晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "冻雾",
         51: "小毛毛雨", 53: "中毛毛雨", 55: "强毛毛雨", 56: "轻冻毛毛雨", 57: "强冻毛毛雨",
         61: "小雨", 63: "中雨", 65: "大雨", 66: "轻冻雨", 67: "强冻雨", 71: "小雪",
         73: "中雪", 75: "大雪", 77: "雪粒", 80: "小阵雨", 81: "中阵雨", 82: "强阵雨",
         85: "小阵雪", 86: "强阵雪", 95: "雷暴", 96: "雷暴伴轻冰雹", 99: "雷暴伴强冰雹"}
ALIASES = {"西安市": "西安", "北京市": "北京", "上海市": "上海", "Xi'an": "Xi’an"}


class WeatherError(ValueError):
    pass


def place_label(place):
    return " / ".join(dict.fromkeys(str(place.get(key) or "") for key in ("country", "admin1", "admin2", "name")
                                   if place.get(key)))


def validate_city(city):
    if not isinstance(city, str):
        raise WeatherError("请提供城市名，例如西安；不能提供网址。")
    city = city.strip()
    if not 2 <= len(city) <= 80 or re.search(r"[\r\n/:<>]", city):
        raise WeatherError("城市名须为2～80个字符；请勿输入网址或整段聊天。")
    return city


def value(number, name):
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not isfinite(number):
        raise WeatherError(f"天气服务缺少有效的{name}，本次不提供猜测数据。")
    return number


def select_place(payload):
    places = payload.get("results") or []
    cities = [p for p in places if isinstance(p, dict) and str(p.get("feature_code", "")).startswith("PPL")]
    # 一个明显的省会/首都可确认，否则同名城市应交用户澄清，不按人口猜。
    capitals = [p for p in cities if p.get("feature_code") in {"PPLC", "PPLA"}]
    options = capitals if capitals else cities
    if not options:
        raise WeatherError("没有找到这个城市。请补充省份或准确城市名，例如 西安, 陕西。")
    unique = {(p.get("latitude"), p.get("longitude")): p for p in options}
    if len(unique) != 1:
        labels = "；".join(place_label(p) for p in list(unique.values())[:5])
        raise WeatherError("找到多个同名地点，请补充省份/国家后查询：" + labels)
    place = next(iter(unique.values()))
    lat, lon = value(place.get("latitude"), "纬度"), value(place.get("longitude"), "经度")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise WeatherError("天气服务返回无效地点坐标。")
    return place


def format_weather(place, payload, days, city):
    if not payload.get("timezone"):
        raise WeatherError("天气服务未返回地点时区，无法确认日期。")
    lines = [f"地点：{place_label(place)}（用户查询：{city}）", "数据来源：Open-Meteo；当前值为模型估计，预报不代表已发生。"]
    if days == 0:
        current = payload.get("current") or {}
        units = payload.get("current_units") or {}
        if units.get("temperature_2m") != "°C" or units.get("wind_speed_10m") != "km/h":
            raise WeatherError("天气数据单位不符合预期，不能误报温度或风速。")
        timestamp = current.get("time")
        if not timestamp:
            raise WeatherError("天气服务未返回当前天气时间。")
        temp = value(current.get("temperature_2m"), "温度")
        feel = value(current.get("apparent_temperature"), "体感温度")
        humidity = value(current.get("relative_humidity_2m"), "湿度")
        wind = value(current.get("wind_speed_10m"), "风速")
        description = CODES.get(current.get("weather_code"), "天气类型未知（不能推测）")
        lines += [f"数据时间：{timestamp}（{payload['timezone']}）",
                  f"当前天气：{description}；温度{temp:g}°C；体感{feel:g}°C；湿度{humidity:g}%；风速{wind:g}km/h"]
    else:
        daily = payload.get("daily") or {}
        units = payload.get("daily_units") or {}
        if units.get("temperature_2m_max") != "°C" or units.get("temperature_2m_min") != "°C":
            raise WeatherError("天气预报单位不符合预期。")
        keys = ("time", "weather_code", "temperature_2m_min", "temperature_2m_max", "precipitation_probability_max")
        if any(not isinstance(daily.get(k), list) or len(daily[k]) < days for k in keys):
            raise WeatherError("天气服务返回的预报不完整，本次不编造缺失日期。")
        lines.append(f"以下日期按地点时区 {payload['timezone']}，从今天起共{days}天：")
        for i in range(days):
            low = value(daily['temperature_2m_min'][i], "最低温度")
            high = value(daily['temperature_2m_max'][i], "最高温度")
            rain = value(daily['precipitation_probability_max'][i], "降水概率")
            if high < low or not 0 <= rain <= 100:
                raise WeatherError("天气预报数值异常，不能提供误导结果。")
            lines.append(f"{daily['time'][i]}：{CODES.get(daily['weather_code'][i], '天气类型未知')}；{low:g}～{high:g}°C；最大降水概率{rain:g}%")
    return "\n".join(lines)



def district_query(city):
    match = re.fullmatch(r'(.{2,12}市)(.{1,10}[区县])', city)
    return (match[2][:-1], match[1][:-1]) if match else (ALIASES.get(city, city), '')

def afternoon(place, payload, period):
    hourly = payload.get('hourly') or {}
    times = hourly.get('time') or []
    offset = 1 if period == '明天下午' else 0
    dates = list(dict.fromkeys(t[:10] for t in times))
    if len(dates) <= offset:
        raise WeatherError('天气服务未提供所需下午逐小时预报。')
    indexes = [i for i,t in enumerate(times) if t[:10] == dates[offset] and 13 <= int(t[11:13]) <= 18]
    keys = ('temperature_2m','precipitation_probability','precipitation','weather_code')
    if len(indexes) != 6 or any(not isinstance(hourly.get(k),list) or len(hourly[k]) <= max(indexes) for k in keys):
        raise WeatherError('下午逐小时预报不完整，不能用全天概率替代。')
    units = payload.get('hourly_units') or {}
    if units.get('temperature_2m') != '°C' or units.get('precipitation') != 'mm' or units.get('precipitation_probability') != '%':
        raise WeatherError('逐小时天气单位不符合预期。')
    lines = [f'{period}（{dates[offset]}，当地时间13:00～18:00）逐小时预报：']
    for i in indexes:
        p = value(hourly['precipitation_probability'][i], '降水概率')
        mm = value(hourly['precipitation'][i], '降水量')
        temp = value(hourly['temperature_2m'][i], '温度')
        if not 0 <= p <= 100 or mm < 0: raise WeatherError('逐小时降水数值无效。')
        lines.append(f"{times[i][11:16]}：{CODES.get(hourly['weather_code'][i], '天气类型未知')}；{temp:g}°C；降水概率{p:g}%；降水量{mm:g}mm")
    lines.append('以上为预报，不能保证实际是否下雨；可据降水概率和降水量给出带伞建议。')
    return '\n'.join(lines)

class WeatherClient:
    def __init__(self, timeout=12, cache_seconds=300, country="CN", transport=None):
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.country = country
        self.transport = transport
        self.cache = OrderedDict()

    async def fetch(self, city, days=1, period=""):
        city = validate_city(city)
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 7:
            raise WeatherError("days须为0～7的整数：0查询当前值，1今天预报，2今天和明天。")
        if period not in {'', '今天下午', '明天下午'}:
            raise WeatherError('period仅支持今天下午或明天下午。')
        key = (city.casefold(), days, self.country, period)
        cached = self.cache.get(key)
        if cached and monotonic() - cached[0] < self.cache_seconds:
            self.cache.move_to_end(key)
            result = copy.deepcopy(cached[1])
            result['cached'] = True
            result['content'] += "\n本次使用短时缓存；原查询时间：" + result['retrieved_at']
            return result
        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False, transport=self.transport) as client:
                search, parent = district_query(city)
                params = {'name': search, 'count': 50 if parent else 10, 'language': 'zh', 'format': 'json'}
                if self.country:
                    params['countryCode'] = self.country
                places = await self._get(client, GEOCODING_URL, params)
                if parent:
                    places = {'results': [p for p in places.get('results', []) if str(p.get('admin2', '')).removesuffix('市') == parent]}
                place = select_place(places)
                params = {'latitude': place['latitude'], 'longitude': place['longitude'], 'timezone': 'auto',
                          'temperature_unit': 'celsius', 'wind_speed_unit': 'kmh', 'forecast_days': max(2 if period == '明天下午' else 1, days)}
                if period:
                    params['hourly'] = 'temperature_2m,precipitation_probability,precipitation,weather_code'
                if days == 0:
                    params['current'] = 'temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m'
                else:
                    params['daily'] = 'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max'
                payload = await self._get(client, FORECAST_URL, params)
        result = {'success': True, 'content': format_weather(place, payload, days, city),
                  'source': 'Open-Meteo', 'source_url': 'https://open-meteo.com/', 'location': place_label(place),
                  'days': days, 'timezone': payload['timezone'], 'cached': False,
                  'retrieved_at': datetime.now(timezone.utc).isoformat()}
        if period:
            result['content'] += '\n' + afternoon(place, payload, period)
            result['period'] = period
        result['forecast_key'] = hashlib.sha256(result['content'].encode()).hexdigest()
        result['content'] += '\n查询时间（UTC）：' + result['retrieved_at']
        self.cache[key] = (monotonic(), copy.deepcopy(result))
        self.cache.move_to_end(key)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result

    @staticmethod
    async def _get(client, url, params):
        response = await client.get(url, params=params)
        response.raise_for_status()
        if len(response.content) > 1000000:
            raise WeatherError("天气服务响应过大，拒绝处理。")
        data = response.json()
        if not isinstance(data, dict) or data.get('error'):
            raise WeatherError("天气服务返回错误或无效格式。")
        return data


def failure(exc):
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        text = "天气查询超时，本次未得到有效天气；请稍后重试。"
    elif isinstance(exc, httpx.HTTPStatusError):
        text = f"天气服务HTTP {exc.response.status_code}，本次查询未成功。"
    elif isinstance(exc, httpx.RequestError):
        text = "天气服务网络连接失败；请检查本机网络或代理，本次不提供猜测数据。"
    elif isinstance(exc, WeatherError):
        text = str(exc)
    else:
        text = f"天气数据解析失败（{type(exc).__name__}），本次不提供猜测数据。"
    return {'success': False, 'content': text, 'error': text}
