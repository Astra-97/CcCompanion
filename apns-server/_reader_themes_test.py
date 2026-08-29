"""Tests for the server-owned reader theme palette (GET /kimi/reader-themes)."""
from __future__ import annotations

import io
import json
import tempfile
import types
import unittest
from email.message import Message
from pathlib import Path

from push import PushHandler
from reader_themes import (
    DEFAULT_READER_THEMES,
    READER_THEME_IDS,
    load_reader_themes,
    reader_themes_config_path,
)


class ReaderThemesLoaderTest(unittest.TestCase):
    def _write(self, payload) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="reader-themes-"))
        path = directory / "reader-themes.json"
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(raw, encoding="utf-8")
        return path

    def test_missing_file_falls_back_to_built_in_defaults(self) -> None:
        payload = load_reader_themes("/nonexistent/reader-themes.json")
        self.assertEqual(payload, DEFAULT_READER_THEMES)
        self.assertEqual(payload["default_theme"], "green")

    def test_malformed_json_falls_back_to_defaults(self) -> None:
        self.assertEqual(load_reader_themes(self._write("{not json")), DEFAULT_READER_THEMES)

    def test_wrong_version_falls_back_to_defaults(self) -> None:
        self.assertEqual(load_reader_themes(self._write({"version": 2, "themes": {}})), DEFAULT_READER_THEMES)

    def test_valid_override_recolors_one_theme_and_keeps_the_rest(self) -> None:
        path = self._write({
            "version": 1,
            "default_theme": "brown",
            "themes": {
                "green": {
                    "label": "护眼绿",
                    "paper": [[1, 2, 3], [4, 5, 6]],
                    "paper_border": [7, 8, 9, 0.5],
                    "ink": [10, 11, 12],
                    "dim": [13, 14, 15],
                    "faint": [16, 17, 18],
                    "link": [19, 20, 21],
                },
            },
        })
        payload = load_reader_themes(path)
        self.assertEqual(payload["default_theme"], "brown")
        self.assertEqual(payload["themes"]["green"]["ink"], [10, 11, 12])
        self.assertEqual(payload["themes"]["glass"], DEFAULT_READER_THEMES["themes"]["glass"])
        self.assertEqual(payload["themes"]["brown"], DEFAULT_READER_THEMES["themes"]["brown"])

    def test_invalid_theme_and_unknown_keys_are_dropped(self) -> None:
        path = self._write({
            "version": 1,
            "default_theme": "nope",
            "themes": {
                "green": {"label": "坏", "paper": [[999, 0, 0]], "ink": [1, 2, 3]},
                "mystery": {"label": "未知"},
            },
            "unexpected": True,
        })
        payload = load_reader_themes(path)
        self.assertEqual(payload["default_theme"], "green")
        self.assertEqual(payload["themes"]["green"], DEFAULT_READER_THEMES["themes"]["green"])
        self.assertNotIn("mystery", payload["themes"])
        self.assertNotIn("unexpected", payload)

    def test_config_path_override_must_be_absolute(self) -> None:
        self.assertEqual(
            reader_themes_config_path({"reader_themes_config_path": "/tmp/custom-themes.json"}),
            Path("/tmp/custom-themes.json"),
        )
        self.assertNotEqual(
            str(reader_themes_config_path({"reader_themes_config_path": "relative.json"})),
            "relative.json",
        )
        self.assertEqual(reader_themes_config_path(None), reader_themes_config_path({}))


class ReaderThemesRouteTest(unittest.TestCase):
    def handler(self, path: str, *, token: str = "native-secret") -> PushHandler:
        handler = object.__new__(PushHandler)
        handler.path = path
        handler.command = "GET"
        handler.state = types.SimpleNamespace(
            shared_secret="native-secret",
            strict_auth=True,
            reader_themes_config_path="/nonexistent/reader-themes.json",
        )
        handler.responses = []
        handler._send_json = lambda status, payload, **_kwargs: handler.responses.append((status, payload))
        handler._check_ip_allowed = lambda: True
        headers = Message()
        if token:
            headers["X-Auth-Token"] = token
        handler.headers = headers
        handler.rfile = io.BytesIO()
        return handler

    def test_missing_native_auth_is_rejected_before_dispatch(self) -> None:
        handler = self.handler("/kimi/reader-themes", token="")
        handler.do_GET()
        self.assertEqual(handler.responses, [(401, {"ok": False, "error": "unauthorized"})])

    def test_authenticated_get_returns_the_full_palette(self) -> None:
        handler = self.handler("/kimi/reader-themes")
        handler.do_GET()
        self.assertEqual(len(handler.responses), 1)
        status, payload = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["default_theme"], "green")
        self.assertEqual(tuple(payload["themes"].keys()), READER_THEME_IDS)
        green = payload["themes"]["green"]
        self.assertEqual(green["paper"][0], [217, 242, 214])
        self.assertEqual(green["ink"], [45, 70, 49])
        self.assertEqual(green["link"], [11, 125, 196])
        self.assertIsNone(payload["themes"]["glass"]["paper"])


if __name__ == "__main__":
    unittest.main()
