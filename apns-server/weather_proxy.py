"""只读 open-meteo 天气代理（2026-10-17）。

网页 PWA 的 CSP 是 connect-src 'self'，无法像 Android HomeWeatherClient.kt
那样直连第三方，所以由服务端转发。请求形状与 weather_label 文案逐行移植
HomeWeatherClient.kt，两端同一城市同一时刻读数一致。

只出网到 open-meteo 两个固定 host；参数全部校验；结果按取整坐标缓存
15 分钟，避免每个 PWA 会话各自打第三方。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL_SECONDS = 900
HTTP_TIMEOUT_SECONDS = 8
_USER_AGENT = {"User-Agent": "CcCompanion-WeatherProxy/1.0"}


class WeatherProxyError(Exception):
    """Upstream open-meteo failure (network, HTTP, or malformed payload)."""


class WeatherCityNotFound(WeatherProxyError):
    """The geocoder answered successfully but has no such city."""


def weather_label(code: int) -> str:
    """Literal port of homeWeatherLabel() from HomeWeatherClient.kt."""
    if code == 0:
        return "晴朗"
    if code in (1, 2):
        return "少云"
    if code == 3:
        return "阴"
    if code in (45, 48):
        return "有雾"
    if code in (51, 53, 55, 56, 57):
        return "毛毛雨"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "有雨"
    if code in (71, 73, 75, 77, 85, 86):
        return "有雪"
    if code in (95, 96, 99):
        return "雷雨"
    return "天气数据"


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except WeatherProxyError:
        raise
    except Exception as exc:
        raise WeatherProxyError(f"upstream request failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise WeatherProxyError("upstream payload is not an object")
    return payload


def _valid_lat(value: Any) -> bool:
    return isinstance(value, (int, float)) and -90.0 <= float(value) <= 90.0


def _valid_lon(value: Any) -> bool:
    return isinstance(value, (int, float)) and -180.0 <= float(value) <= 180.0


def geocode_city(city: str) -> dict[str, Any]:
    """city -> {name, admin1, latitude, longitude}; first match, like the App."""
    safe = str(city or "").strip()[:80]
    if not safe:
        raise WeatherProxyError("city must not be blank")
    url = GEOCODE_URL + "?name=" + urllib.parse.quote(safe) + "&count=1&language=zh&format=json"
    payload = _get_json(url)
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise WeatherCityNotFound(safe)
    first = results[0]
    if not isinstance(first, dict):
        raise WeatherProxyError("geocoder result is not an object")
    name = str(first.get("name") or "").strip()
    latitude, longitude = first.get("latitude"), first.get("longitude")
    if not name or not _valid_lat(latitude) or not _valid_lon(longitude):
        raise WeatherProxyError("geocoder result is incomplete")
    return {
        "name": name,
        "admin1": str(first.get("admin1") or "").strip(),
        "latitude": float(latitude),
        "longitude": float(longitude),
    }


def fetch_current(latitude: float, longitude: float) -> dict[str, Any]:
    """lat/lon -> {temperature_c, weather_code, label}; same query as the App."""
    if not _valid_lat(latitude) or not _valid_lon(longitude):
        raise WeatherProxyError("coordinates out of range")
    url = (
        f"{FORECAST_URL}?latitude={float(latitude)}&longitude={float(longitude)}"
        "&current=temperature_2m,weather_code&timezone=auto"
    )
    payload = _get_json(url)
    current = payload.get("current")
    if not isinstance(current, dict):
        raise WeatherProxyError("forecast payload has no current block")
    temperature = current.get("temperature_2m")
    if not isinstance(temperature, (int, float)):
        raise WeatherProxyError("forecast payload has no temperature")
    code = current.get("weather_code")
    code = int(code) if isinstance(code, (int, float)) else -1
    return {
        "temperature_c": int(round(float(temperature))),
        "weather_code": code,
        "label": weather_label(code),
    }


class WeatherProxy:
    """Tiny caching façade: one upstream call per rounded coordinate per TTL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}

    def forecast(
        self,
        *,
        city: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> dict[str, Any]:
        if city is not None and str(city).strip():
            place = geocode_city(city)
        elif latitude is not None and longitude is not None:
            if not _valid_lat(latitude) or not _valid_lon(longitude):
                raise WeatherProxyError("coordinates out of range")
            place = {"name": "", "admin1": "", "latitude": float(latitude), "longitude": float(longitude)}
        else:
            raise WeatherProxyError("city or latitude+longitude required")
        key = (round(place["latitude"], 2), round(place["longitude"], 2))
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
                return dict(cached[1])
        current = fetch_current(place["latitude"], place["longitude"])
        payload = {"place": place, **current}
        with self._lock:
            self._cache[key] = (now, payload)
            # Bound the cache: a handful of cities is expected, never hundreds.
            if len(self._cache) > 32:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)
        return dict(payload)
