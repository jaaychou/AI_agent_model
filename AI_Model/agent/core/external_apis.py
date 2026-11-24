"""外部付费/需 key API 的统一接入层 —— 地图、车票、航班。

设计原则：
- 默认无 key：工具诚实报告"未配置"，告诉 Agent 该让用户提供什么，绝不编造数据。
- key 从 agent/api_keys.json 读取（该文件被 .gitignore 排除，不会泄露）。
- 地图合规：只用腾讯地图（WebService API），不碰 Google/Apple/Bing/OSM 海外源。
- 用户给出 key 后，对应能力立即启用，无需改代码。

腾讯地图 WebService 官方文档：https://lbs.qq.com/service/webService/webServiceGuide/webServiceOverview
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

from .tools import ToolResult

BASE = Path(__file__).resolve().parent.parent
KEYS_FILE = BASE / "api_keys.json"

TMAP_GEOCODER = "https://apis.map.qq.com/ws/geocoder/v1/"
TMAP_ROUTE = "https://apis.map.qq.com/ws/direction/v1/driving/"
TMAP_SEARCH = "https://apis.map.qq.com/ws/place/v1/search"


def load_keys() -> dict:
    if not KEYS_FILE.exists():
        return {}
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _key(name: str) -> str:
    return str(load_keys().get(name, "") or "").strip()


def _tmap_get(url: str, params: dict) -> dict:
    params = dict(params)
    params["key"] = _key("tencent_map_key")
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "LocalAgent/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def register_external_api_tools(registry, confirm=None):
    def geocode(args):
        key = _key("tencent_map_key")
        if not key:
            return ToolResult(False, "地图能力未配置：需要在 agent/api_keys.json 填入 tencent_map_key（腾讯位置服务 Key）。"
                              "申请入口 https://lbs.qq.com/dev/console/application/mine：注册→创建应用→勾选 WebServiceAPI→复制 Key。"
                              "未配置前不要编造坐标或路线。")
        address = str(args.get("address", "")).strip()
        if not address:
            return ToolResult(False, "请提供地址 address。")
        try:
            data = _tmap_get(TMAP_GEOCODER, {"address": address})
            if data.get("status") != 0:
                return ToolResult(False, f"腾讯地图地理编码失败：{data.get('message', data)}")
            result = data.get("result", {})
            loc = result.get("location", {})
            return ToolResult(True, f"地址：{address}\n坐标(GCJ-02)：{loc.get('lat')}, {loc.get('lng')}\n"
                                    f"标准化地址：{result.get('address')}\n来源：腾讯位置服务（合规地图源）")
        except Exception as exc:
            return ToolResult(False, f"地理编码失败：{type(exc).__name__}: {exc}")

    def route_plan(args):
        key = _key("tencent_map_key")
        if not key:
            return ToolResult(False, "路线规划未配置：需要在 agent/api_keys.json 填入 tencent_map_key（腾讯位置服务 Key）。"
                              "申请入口 https://lbs.qq.com/dev/console/application/mine。"
                              "未配置前不要编造路线或距离。")
        origin = str(args.get("origin", "")).strip()
        dest = str(args.get("destination", "")).strip()
        if not origin or not dest:
            return ToolResult(False, "请提供起点 origin 和终点 destination（GCJ-02 坐标 'lat,lng'）。可先用 geocode 查坐标。")
        try:
            data = _tmap_get(TMAP_ROUTE, {"from": origin, "to": dest})
            if data.get("status") != 0:
                return ToolResult(False, f"腾讯地图路线规划失败：{data.get('message', data)}")
            route = (data.get("result", {}).get("routes") or [{}])[0]
            distance = route.get("distance")
            duration = route.get("duration")
            lines = [f"驾车路线（来源：腾讯位置服务，合规地图源）",
                     f"距离：{distance} 米，预计用时：{duration} 分钟"]
            steps = route.get("steps") or []
            for i, step in enumerate(steps[:8], 1):
                lines.append(f"  {i}. {step.get('instruction', '')}（{step.get('distance')} 米）")
            return ToolResult(True, "\n".join(lines))
        except Exception as exc:
            return ToolResult(False, f"路线规划失败：{type(exc).__name__}: {exc}")

    def search_place(args):
        key = _key("tencent_map_key")
        if not key:
            return ToolResult(False, "地点搜索未配置：需要在 agent/api_keys.json 填入 tencent_map_key（腾讯位置服务 Key）。"
                              "申请入口 https://lbs.qq.com/dev/console/application/mine。")
        keyword = str(args.get("keyword", "")).strip()
        city = str(args.get("city", "")).strip()
        if not keyword:
            return ToolResult(False, "请提供搜索关键词 keyword。")
        params = {"keyword": keyword, "boundary": f"region({city},0)" if city else "region(全国,0)",
                  "page_size": 10}
        try:
            data = _tmap_get(TMAP_SEARCH, params)
            if data.get("status") != 0:
                return ToolResult(False, f"腾讯地图地点搜索失败：{data.get('message', data)}")
            items = data.get("data") or []
            lines = [f"地点搜索 '{keyword}'（来源：腾讯位置服务）"]
            for item in items[:10]:
                loc = item.get("location", {})
                lines.append(f"- {item.get('title')}｜{item.get('address')}｜{loc.get('lat')},{loc.get('lng')}")
            return ToolResult(True, "\n".join(lines) if items else "没有找到匹配地点。")
        except Exception as exc:
            return ToolResult(False, f"地点搜索失败：{type(exc).__name__}: {exc}")

    def ticket_query(args):
        kind = str(args.get("kind", "train")).strip().casefold()
        field = "train_ticket_api" if kind == "train" else "flight_api"
        label = "火车票" if kind == "train" else "机票"
        if not _key(field):
            return ToolResult(False,
                f"{label}实时查询未配置：12306/航司没有公开免费 API，需要在 agent/api_keys.json 填入 {field}。"
                "可去聚合数据/阿里云市场等申请第三方车票或航班数据 API，或留空——此时只做行程规划，"
                "票价和班次让用户去 12306/航司官方购票页确认。未配置前绝不编造车次/票价。")
        return ToolResult(False, f"{label} API key 已检测到，但尚未对接具体服务商的响应格式。"
                          "请告诉 Agent 你用的是哪家数据服务，把接口文档给 Agent，即可用 create_tool 写一个专用查询工具。")

    registry.register(
        "geocode", "地址→经纬度坐标（腾讯地图，合规地图源）。需要在 api_keys.json 配置 tencent_map_key；未配置会明确告知申请方法，不编造坐标",
        {"type": "object", "properties": {"address": {"type": "string", "description": "要查询的地址"}},
         "required": ["address"]}, geocode, source="external")
    registry.register(
        "route_plan", "驾车路线规划（腾讯地图，合规地图源）。需 tencent_map_key，坐标用 GCJ-02。未配置会告知申请方法",
        {"type": "object", "properties": {
            "origin": {"type": "string", "description": "起点坐标 lat,lng"},
            "destination": {"type": "string", "description": "终点坐标 lat,lng"},
        }, "required": ["origin", "destination"]}, route_plan, source="external")
    registry.register(
        "search_place", "地点/景点搜索（腾讯地图，合规地图源）。需 tencent_map_key。未配置会告知申请方法",
        {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索关键词"},
            "city": {"type": "string", "description": "城市名，可选"},
        }, "required": ["keyword"]}, search_place, source="external")
    registry.register(
        "ticket_query", "火车票/机票实时查询。12306 与航司无公开免费 API，需自行申请第三方数据服务并在 api_keys.json 配置；未配置时只做行程规划，让用户去官方购票页核实",
        {"type": "object", "properties": {
            "kind": {"type": "string", "description": "train 或 flight"},
        }, "required": ["kind"]}, ticket_query, source="external")
