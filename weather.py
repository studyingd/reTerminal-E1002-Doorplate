# -*- coding: utf-8 -*-
"""实时气温 + 天气现象(Open-Meteo 接口,免费、无需 API Key),带 10 分钟缓存"""
import json
import time
import urllib.request

_cache = {"time": 0.0, "temp": None, "text": None}

# WMO weather interpretation codes -> 屏显短文案(越短越好,墨水屏宽度有限)
_WMO = {
    0: "晴", 1: "晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨",
    56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪",
    80: "阵雨", 81: "阵雨", 82: "暴雨",
    85: "阵雪", 86: "阵雪",
    95: "雷暴", 96: "冰雹", 99: "冰雹",
}


def current_weather(lat: float, lon: float, cache_seconds: int = 600):
    """返回 (气温°C, 天气文案)。同一位置 10 分钟内复用缓存,避免频繁请求。"""
    if _cache["temp"] is not None and time.time() - _cache["time"] < cache_seconds:
        return _cache["temp"], _cache["text"]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,weather_code&timezone=auto")
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cur = data["current"]
    _cache["temp"] = float(cur["temperature_2m"])
    _cache["text"] = _WMO.get(int(cur.get("weather_code", -1)), "")
    _cache["time"] = time.time()
    return _cache["temp"], _cache["text"]


def current_temperature(lat: float, lon: float, cache_seconds: int = 600) -> float:
    """只要气温时的兼容入口"""
    return current_weather(lat, lon, cache_seconds)[0]


if __name__ == "__main__":
    t, w = current_weather(22.533, 113.923)
    print(f"深圳南山当前:{t}°C {w}")
