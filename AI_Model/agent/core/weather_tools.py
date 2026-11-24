"""天气查询工具 —— 用免费无 key 的 Open-Meteo / wttr.in，供旅游规划和日常查询。

两个来源都实测可用（2026-11-24）：
- Open-Meteo：国际公开 API，按经纬度，返回结构化天气。
- wttr.in：支持城市名，返回 JSON（含体感、湿度、风速、天气描述）。

不内置任何 API key，不访问内网，失败如实报告不用记忆编造天气。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime

from .tools import ToolResult

UA = "LocalAgent/1.0 (weather)"


def _fetch_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# WMO 天气代码 → 中文
_WMO = {
    0: "晴", 1: "基本晴朗", 2: "局部多云", 3: "阴", 45: "雾", 48: "冻雾",
    51: "毛毛雨(小)", 53: "毛毛雨", 55: "毛毛雨(大)", 61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "中阵雨", 82: "强阵雨",
    95: "雷阵雨", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}


def current_weather_by_coords(lat: float, lon: float) -> str:
    params = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": "auto", "forecast_days": 3,
    })
    data = _fetch_json("https://api.open-meteo.com/v1/forecast?" + params)
    cur = data.get("current", {})
    daily = data.get("daily", {})
    lines = [f"数据来源：Open-Meteo（免费公开 API）",
             f"坐标：{data.get('latitude')}, {data.get('longitude')}  时区：{data.get('timezone')}",
             f"观测时间：{cur.get('time')}"]
    code = cur.get("weather_code")
    lines.append(f"当前：{_WMO.get(code, f'天气代码 {code}')}，"
                 f"温度 {cur.get('temperature_2m')}°C（体感 {cur.get('apparent_temperature')}°C），"
                 f"湿度 {cur.get('relative_humidity_2m')}%，风速 {cur.get('wind_speed_10m')} km/h，"
                 f"降水 {cur.get('precipitation')} mm")
    lines.append("未来 3 天：")
    for i, date in enumerate(daily.get("time", [])):
        hi = daily.get("temperature_2m_max", [None] * 3)[i]
        lo = daily.get("temperature_2m_min", [None] * 3)[i]
        pop = daily.get("precipitation_probability_max", [None] * 3)[i]
        dcode = daily.get("weather_code", [None] * 3)[i]
        lines.append(f"  {date}: {_WMO.get(dcode, dcode)}，{lo}~{hi}°C，降水概率 {pop}%")
    lines.append("说明：预报有不确定性，出行前请再核对当地气象台。")
    return "\n".join(lines)


def current_weather_by_city(city: str) -> str:
    q = urllib.parse.quote(city.strip())
    data = _fetch_json(f"https://wttr.in/{q}?format=j1")
    cur = (data.get("current_condition") or [{}])[0]
    area = data.get("nearest_area") or [{}]
    area_name = area[0].get("areaName", [{}])[0].get("value", city) if area else city
    desc = (cur.get("weatherDesc") or [{}])[0].get("value", "未知")
    lines = [f"数据来源：wttr.in（免费公开服务）",
             f"地点：{area_name}",
             f"当前：{desc}，温度 {cur.get('temp_C')}°C（体感 {cur.get('FeelsLikeC')}°C），"
             f"湿度 {cur.get('humidity')}%，风速 {cur.get('windspeedKmph')} km/h，"
             f"降水 {cur.get('precipMM')} mm"]
    weather = data.get("weather") or []
    if weather:
        lines.append("未来几天：")
        for day in weather[:3]:
            date = day.get("date")
            maxc = day.get("maxtempC")
            minc = day.get("mintempC")
            # 取当天中午的天气描述
            hourly = day.get("hourly") or []
            noon = next((h for h in hourly if h.get("time") == "1200"), hourly[0] if hourly else {})
            noondesc = (noon.get("weatherDesc") or [{}])[0].get("value", "")
            lines.append(f"  {date}: {noondesc}，{minc}~{maxc}°C")
    lines.append("说明：数据来自公开气象服务，可能有时延，出行前核对当地气象台。")
    return "\n".join(lines)


def register_weather_tools(registry, confirm=None):
    def weather(args):
        city = str(args.get("city", "")).strip()
        lat = args.get("latitude")
        lon = args.get("longitude")
        try:
            if lat is not None and lon is not None:
                return ToolResult(True, current_weather_by_coords(float(lat), float(lon)))
            if city:
                return ToolResult(True, current_weather_by_city(city))
            return ToolResult(False, "请提供城市名 city（如 '武汉'）或经纬度 latitude/longitude。")
        except Exception as exc:
            return ToolResult(False, f"天气查询失败：{type(exc).__name__}: {exc}。请如实说明无法获取，不要用记忆编造天气。")

    registry.register(
        "weather", "查询当前天气与未来 2-3 天预报（免费无 key）。给城市名 city（如 武汉/北京）或经纬度 latitude/longitude。用于出行、旅游规划、日常问答",
        {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，如 '武汉'、'Beijing'"},
            "latitude": {"type": "number", "description": "纬度（与 longitude 一起用）"},
            "longitude": {"type": "number", "description": "经度（与 latitude 一起用）"},
        }, "required": []}, weather, source="weather")
