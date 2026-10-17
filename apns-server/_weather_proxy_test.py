"""open-meteo 天气只读代理回归测试 (2026-10-17, offline; mock 上游 + stub handler).

Covers:
1. label        — weather_label 与 Android homeWeatherLabel() 逐档一致
2. geocode      — count=1 取首条; 空结果 -> CityNotFound; 畸形 -> ProxyError
3. current      — current 块缺失/温度缺失 -> ProxyError; 温度四舍五入
4. cache        — 同取整坐标 15 分钟内只打一次上游; 越界坐标拒绝
5. handler      — manifest 404; 参数互斥/成对/必填 400; CityNotFound 404;
                  上游失败 502; 成功 200 带 place/temperature_c/weather_code/label
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from plugins_store import PluginStore  # noqa: E402
from push import PushHandler  # noqa: E402
from weather_proxy import (  # noqa: E402
    WeatherCityNotFound,
    WeatherProxy,
    WeatherProxyError,
    fetch_current,
    geocode_city,
    weather_label,
)

SECRET = "weather-proxy-test-secret"
DASH_ID = "xiaonan-dash"


class WeatherLabelTest(unittest.TestCase):
    def test_label_table_matches_android(self):
        for code in (0,):
            self.assertEqual("晴朗", weather_label(code))
        for code in (1, 2):
            self.assertEqual("少云", weather_label(code))
        self.assertEqual("阴", weather_label(3))
        for code in (45, 48):
            self.assertEqual("有雾", weather_label(code))
        for code in (51, 53, 55, 56, 57):
            self.assertEqual("毛毛雨", weather_label(code))
        for code in (61, 63, 65, 66, 67, 80, 81, 82):
            self.assertEqual("有雨", weather_label(code))
        for code in (71, 73, 75, 77, 85, 86):
            self.assertEqual("有雪", weather_label(code))
        for code in (95, 96, 99):
            self.assertEqual("雷雨", weather_label(code))
        self.assertEqual("天气数据", weather_label(-1))
        self.assertEqual("天气数据", weather_label(4))


class GeocodeTest(unittest.TestCase):
    def test_first_result_and_admin1(self):
        with mock.patch("weather_proxy._get_json", return_value={
            "results": [{"name": "北京", "admin1": "北京市", "latitude": 39.9, "longitude": 116.4}],
        }) as get_json:
            place = geocode_city("北京")
        self.assertEqual(place, {"name": "北京", "admin1": "北京市", "latitude": 39.9, "longitude": 116.4})
        url = get_json.call_args.args[0]
        self.assertIn("count=1", url)
        self.assertIn("language=zh", url)
        self.assertIn("%E5%8C%97%E4%BA%AC", url)  # 北京 quote 后

    def test_no_result_is_city_not_found(self):
        with mock.patch("weather_proxy._get_json", return_value={"results": []}):
            with self.assertRaises(WeatherCityNotFound):
                geocode_city("不存在的城市")
        with mock.patch("weather_proxy._get_json", return_value={}):
            with self.assertRaises(WeatherCityNotFound):
                geocode_city("不存在的城市")

    def test_malformed_result_is_proxy_error(self):
        with mock.patch("weather_proxy._get_json", return_value={"results": [{"name": "x", "latitude": 999, "longitude": 0}]}):
            with self.assertRaises(WeatherProxyError):
                geocode_city("x")
        with mock.patch("weather_proxy._get_json", return_value={"results": ["nope"]}):
            with self.assertRaises(WeatherProxyError):
                geocode_city("x")

    def test_blank_city_rejected_before_upstream(self):
        with mock.patch("weather_proxy._get_json") as get_json:
            with self.assertRaises(WeatherProxyError):
                geocode_city("   ")
        get_json.assert_not_called()


class FetchCurrentTest(unittest.TestCase):
    def test_current_rounds_temperature_and_labels(self):
        with mock.patch("weather_proxy._get_json", return_value={
            "current": {"temperature_2m": 15.5, "weather_code": 82},
        }) as get_json:
            current = fetch_current(39.9, 116.4)
        self.assertEqual(current, {"temperature_c": 16, "weather_code": 82, "label": "有雨"})
        url = get_json.call_args.args[0]
        self.assertIn("current=temperature_2m,weather_code", url)
        self.assertIn("latitude=39.9", url)

    def test_missing_blocks_are_proxy_errors(self):
        with mock.patch("weather_proxy._get_json", return_value={}):
            with self.assertRaises(WeatherProxyError):
                fetch_current(39.9, 116.4)
        with mock.patch("weather_proxy._get_json", return_value={"current": {"weather_code": 0}}):
            with self.assertRaises(WeatherProxyError):
                fetch_current(39.9, 116.4)

    def test_out_of_range_coordinates_rejected(self):
        with mock.patch("weather_proxy._get_json") as get_json:
            for lat, lon in ((91, 0), (-91, 0), (0, 181), (0, -181)):
                with self.assertRaises(WeatherProxyError):
                    fetch_current(lat, lon)
        get_json.assert_not_called()


class WeatherProxyCacheTest(unittest.TestCase):
    def test_rounded_coordinate_cache_hits_within_ttl(self):
        proxy = WeatherProxy()
        calls = []

        def fake_get_json(url):
            calls.append(url)
            if "geocoding" in url:
                return {"results": [{"name": "北京", "admin1": "北京市", "latitude": 39.9042, "longitude": 116.4074}]}
            return {"current": {"temperature_2m": 15.3, "weather_code": 82}}

        with mock.patch("weather_proxy._get_json", side_effect=fake_get_json):
            first = proxy.forecast(city="北京")
            second = proxy.forecast(latitude=39.90419, longitude=116.40741)  # 同取整键
        self.assertEqual(first, second)
        self.assertEqual(first["place"]["name"], "北京")
        self.assertEqual(first["temperature_c"], 15)
        self.assertEqual(first["label"], "有雨")
        # geocode 1 次 + forecast 1 次; 缓存命中不再打上游
        self.assertEqual(len(calls), 2)

    def test_city_and_latlon_and_blank_inputs(self):
        proxy = WeatherProxy()
        with mock.patch("weather_proxy._get_json") as get_json:
            with self.assertRaises(WeatherProxyError):
                proxy.forecast()
            with self.assertRaises(WeatherProxyError):
                proxy.forecast(latitude=95.0, longitude=0.0)
        get_json.assert_not_called()


class WeatherHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="_weather_proxy_test_"))
        self.builtin_dir = self.tmp / "plugins-builtin"
        for pid in (DASH_ID, "other-plugin"):
            root = self.builtin_dir / pid
            root.mkdir(parents=True)
            (root / "manifest.json").write_text(json.dumps({
                "id": pid, "name": pid, "version": "0.1.0",
                "description": "test", "entry": "index.html",
            }), encoding="utf-8")
            (root / "index.html").write_text(f"<html>{pid}</html>", encoding="utf-8")
        self.store = PluginStore(data_dir=self.tmp / "plugins", builtin_dir=self.builtin_dir)
        original = PushHandler._weather_proxy_service
        PushHandler._weather_proxy_service = None
        self.addCleanup(setattr, PushHandler, "_weather_proxy_service", original)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def handler(self, path: str, headers: dict | None = None) -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
        handler.headers = dict(headers or {})
        handler.client_address = ("127.0.0.1", 1)
        handler.responses = []
        handler._send_json = lambda status, payload, **kw: handler.responses.append(
            (status, payload, kw)
        )
        handler.state = types.SimpleNamespace(
            allowed_ips=[],
            shared_secret=SECRET,
            strict_auth=True,
            web_session_enabled=False,
        )
        handler._plugins_store = self.store
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        return handler

    def auth(self):
        return {"X-Auth-Token": SECRET}

    def last(self, handler):
        return handler.responses[-1]

    def test_unknown_plugin_and_no_auth_fail_closed(self):
        h = self.handler(f"/plugins/nonexistent/weather?city=北京", headers=self.auth())
        h.do_GET()
        self.assertEqual(self.last(h)[0], 404)

        h = self.handler(f"/plugins/{DASH_ID}/weather?city=北京")
        h.do_GET()
        self.assertEqual(self.last(h)[0], 401)

    def test_query_validation_400(self):
        for path in (
            f"/plugins/{DASH_ID}/weather",                        # 缺参数
            f"/plugins/{DASH_ID}/weather?city=北京&lat=39.9&lon=116.4",  # 互斥
            f"/plugins/{DASH_ID}/weather?lat=39.9",               # 不成对
            f"/plugins/{DASH_ID}/weather?lon=116.4",              # 不成对
            f"/plugins/{DASH_ID}/weather?lat=abc&lon=116.4",      # 非数字
        ):
            h = self.handler(path, headers=self.auth())
            h.do_GET()
            status, payload, _ = self.last(h)
            self.assertEqual(status, 400, path)
            self.assertFalse(payload["ok"], path)

    def test_city_not_found_404_and_upstream_502(self):
        with mock.patch.object(WeatherProxy, "forecast", side_effect=WeatherCityNotFound("x")):
            h = self.handler(f"/plugins/{DASH_ID}/weather?city=不存在的城市", headers=self.auth())
            h.do_GET()
            self.assertEqual(self.last(h)[0], 404)

        with mock.patch.object(WeatherProxy, "forecast", side_effect=WeatherProxyError("boom")):
            h = self.handler(f"/plugins/{DASH_ID}/weather?city=北京", headers=self.auth())
            h.do_GET()
            status, payload, _ = self.last(h)
            self.assertEqual(status, 502)
            self.assertNotIn("boom", json.dumps(payload))  # 上游细节不外泄

    def test_success_payload_shape(self):
        fake = {
            "place": {"name": "北京", "admin1": "北京市", "latitude": 39.9, "longitude": 116.4},
            "temperature_c": 15, "weather_code": 82, "label": "有雨",
        }
        with mock.patch.object(WeatherProxy, "forecast", return_value=dict(fake)) as forecast:
            h = self.handler(f"/plugins/{DASH_ID}/weather?city=%E5%8C%97%E4%BA%AC", headers=self.auth())
            h.do_GET()
            status, payload, _ = self.last(h)
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["place"]["name"], "北京")
            self.assertEqual(payload["temperature_c"], 15)
            self.assertEqual(payload["weather_code"], 82)
            self.assertEqual(payload["label"], "有雨")
            self.assertEqual(forecast.call_args.kwargs["city"], "北京")
            self.assertIsNone(forecast.call_args.kwargs["latitude"])

        with mock.patch.object(WeatherProxy, "forecast", return_value=dict(fake)) as forecast:
            h = self.handler(f"/plugins/{DASH_ID}/weather?lat=39.9042&lon=116.4074", headers=self.auth())
            h.do_GET()
            self.assertEqual(self.last(h)[0], 200)
            self.assertIsNone(forecast.call_args.kwargs["city"])
            self.assertAlmostEqual(forecast.call_args.kwargs["latitude"], 39.9042)
            self.assertAlmostEqual(forecast.call_args.kwargs["longitude"], 116.4074)


if __name__ == "__main__":
    unittest.main()
