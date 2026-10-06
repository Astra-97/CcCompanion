"""Targeted tests for the assistant-outbound [bqb:] token rescue pass.

The normalizer lives in sticker_catalog.normalize_bqb_tokens and is wired into
ChatHistory.append as the single choke point for contact assistant replies.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from chat_history import ChatHistory
from sticker_catalog import StickerCatalogService, normalize_bqb_tokens


def _write_manifest(root: Path, dirname: str, stickers: list[dict]) -> Path:
    directory = root / dirname
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "stickers.json"
    path.write_text(
        json.dumps({"stickers": stickers}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def _sticker(name: str, label: str | None = None, aliases: list[str] | None = None) -> dict:
    item = {"name": name, "file": f"{name}.gif"}
    if label is not None:
        item["label"] = label
    if aliases:
        item["aliases"] = aliases
    return item


class BqbTokenRescueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        zichao = _write_manifest(root, "zichao", [
            _sticker("自嘲熊·OK", label="OK"),
            _sticker("自嘲熊·困困", label="困困"),
            _sticker("自嘲熊·划水", label="摸鱼"),
        ])
        xiaodou = _write_manifest(root, "xiaodou", [
            _sticker("小黄豆·摸摸头", label="摸摸头"),
            _sticker("小黄豆·困困", label="困困"),
            _sticker("小黄豆·摸鱼", label="划水"),
            _sticker("小黄豆·抱抱", label="抱抱", aliases=["抱抱"]),
            _sticker("小黄豆·好累", label="好累"),
        ])
        gege = _write_manifest(root, "gege", [
            _sticker("哥哥熊·摸摸头", label="摸摸头"),
            _sticker("哥哥熊·撒娇", label="撒娇"),
            _sticker("哥哥熊·亲亲", label="亲亲", aliases=["亲亲"]),
        ])
        base = "https://assets.example/stickers"
        self.service = StickerCatalogService({"enabled": True, "sources": [
            {"manifest_path": str(zichao), "public_base_url": base,
             "category_id": "zichao", "category_name": "自嘲熊"},
            {"manifest_path": str(xiaodou), "public_base_url": base,
             "category_id": "xiaodou", "category_name": "小黄豆"},
            {"manifest_path": str(gege), "public_base_url": base,
             "category_id": "gege", "category_name": "哥哥熊"},
        ]})
        self.snapshot = self.service.snapshot()

    def tearDown(self):
        self._tmp.cleanup()

    def norm(self, text: str) -> str:
        return normalize_bqb_tokens(text, self.snapshot)

    def test_exact_name_unchanged(self):
        self.assertEqual("[bqb:自嘲熊·OK]", self.norm("[bqb:自嘲熊·OK]"))
        self.assertEqual(
            "早上好[bqb:自嘲熊·OK]！",
            self.norm("早上好[bqb:自嘲熊·OK]！"),
        )

    def test_alias_hit_rewrites_to_full_name(self):
        self.assertEqual("[bqb:小黄豆·抱抱]", self.norm("[bqb:抱抱]"))

    def test_wrong_prefix_unique_truename_match(self):
        self.assertEqual("[bqb:小黄豆·好累]", self.norm("[bqb:自嘲熊·好累]"))

    def test_multi_match_picks_catalog_order_first(self):
        # 困困 exists in 自嘲熊 and 小黄豆 with identical labels: the first
        # catalog entry wins, and the outcome is stable across runs.
        self.assertEqual("[bqb:自嘲熊·困困]", self.norm("[bqb:困困]"))
        self.assertEqual("[bqb:自嘲熊·困困]", self.norm("[bqb:困困]"))

    def test_multi_match_prefers_exact_label(self):
        # 摸鱼 matches 小黄豆·摸鱼 via name-truename but 自嘲熊·划水 via label;
        # the exact-label entry wins even though it sorts earlier anyway.
        self.assertEqual("[bqb:自嘲熊·划水]", self.norm("[bqb:摸鱼]"))

    def test_zero_match_preserved_verbatim(self):
        self.assertEqual("[bqb:不存在的表情]", self.norm("[bqb:不存在的表情]"))
        self.assertEqual("[bqb：不存在的表情]", self.norm("[bqb：不存在的表情]"))

    def test_fullwidth_colon_exact_name_normalized(self):
        self.assertEqual("[bqb:自嘲熊·OK]", self.norm("[bqb：自嘲熊·OK]"))

    def test_fullwidth_colon_bare_truename_rescued(self):
        self.assertEqual("[bqb:小黄豆·好累]", self.norm("[bqb：好累]"))

    def test_plain_fullwidth_colon_outside_token_untouched(self):
        text = "现在是：晚上八点，记得吃饭："
        self.assertEqual(text, self.norm(text))
        self.assertEqual("说说：而已", self.norm("说说：而已"))

    def test_malformed_or_nested_text_untouched(self):
        self.assertEqual("[bqb：好累", self.norm("[bqb：好累"))  # no closing ]
        self.assertEqual("[bqb:]", self.norm("[bqb:]"))
        self.assertEqual("xbqb:抱抱]", self.norm("xbqb:抱抱]"))

    def test_multiple_tokens_in_one_message(self):
        self.assertEqual(
            "早[bqb:自嘲熊·OK]，昨晚[bqb:小黄豆·好累]",
            self.norm("早[bqb：自嘲熊·OK]，昨晚[bqb：好累]"),
        )

    def test_exclusive_gege_bear_never_rescued_from_other_packs(self):
        # Bare / wrong-prefix tokens resolve away from 哥哥熊 when another
        # category also has the true name.
        self.assertEqual("[bqb:小黄豆·摸摸头]", self.norm("[bqb:摸摸头]"))
        self.assertEqual("[bqb:小黄豆·摸摸头]", self.norm("[bqb:自嘲熊·摸摸头]"))
        # True names that exist ONLY in 哥哥熊 stay dead text — a misfired
        # exclusive sticker is worse than plain text.
        self.assertEqual("[bqb:撒娇]", self.norm("[bqb:撒娇]"))
        self.assertEqual("[bqb:自嘲熊·撒娇]", self.norm("[bqb:自嘲熊·撒娇]"))
        # Even an exact alias of a 哥哥熊 entry is off-limits without the
        # explicit 哥哥熊· prefix.
        self.assertEqual("[bqb:亲亲]", self.norm("[bqb:亲亲]"))

    def test_explicit_gege_prefix_unchanged_or_colon_fixed(self):
        self.assertEqual("[bqb:哥哥熊·摸摸头]", self.norm("[bqb:哥哥熊·摸摸头]"))
        self.assertEqual("[bqb:哥哥熊·摸摸头]", self.norm("[bqb：哥哥熊·摸摸头]"))
        # Explicit prefix rescues inside the exclusive category itself.
        self.assertEqual("[bqb:哥哥熊·撒娇]", self.norm("[bqb：哥哥熊·撒娇]"))

    def test_empty_or_disabled_catalog_keeps_text(self):
        self.assertEqual("[bqb:抱抱]", normalize_bqb_tokens("[bqb:抱抱]", {}))
        self.assertEqual(
            "[bqb:抱抱]",
            normalize_bqb_tokens("[bqb:抱抱]", {"stickers": [], "categories": []}),
        )
        disabled = StickerCatalogService({"enabled": False})
        self.assertEqual("[bqb:抱抱]", disabled.normalize_outgoing_text("[bqb:抱抱]"))
        self.assertEqual("no tokens", disabled.normalize_outgoing_text("no tokens"))


class BqbTokenRescueHistoryWiringTests(unittest.TestCase):
    """ChatHistory is the persistence choke point: only assistant role passes
    through the normalizer, and a broken normalizer never eats a reply."""

    def test_assistant_normalized_user_untouched(self):
        with TemporaryDirectory() as tmp:
            chat = ChatHistory(
                Path(tmp) / "history.jsonl",
                assistant_text_normalizer=lambda text: text.replace(
                    "[bqb：抱抱]", "[bqb:小黄豆·抱抱]"
                ).replace("[bqb:抱抱]", "[bqb:小黄豆·抱抱]"),
            )
            user_rec = chat.append(role="user", text="我要[bqb：抱抱]", source="ios-app")
            self.assertEqual("我要[bqb：抱抱]", user_rec["text"])
            assistant_rec = chat.append(
                role="assistant", text="给你[bqb：抱抱]和[bqb:抱抱]", source="claude"
            )
            self.assertEqual("给你[bqb:小黄豆·抱抱]和[bqb:小黄豆·抱抱]", assistant_rec["text"])
            task_rec = chat.append(role="task", text="任务[bqb:抱抱]", source="scheduler")
            self.assertEqual("任务[bqb:抱抱]", task_rec["text"])

    def test_raising_normalizer_fails_open(self):
        def boom(text):
            raise RuntimeError("catalog exploded")

        with TemporaryDirectory() as tmp:
            chat = ChatHistory(
                Path(tmp) / "history.jsonl", assistant_text_normalizer=boom
            )
            rec = chat.append(role="assistant", text="原样[bqb：抱抱]", source="claude")
            self.assertEqual("原样[bqb：抱抱]", rec["text"])

    def test_no_normalizer_default_unchanged(self):
        with TemporaryDirectory() as tmp:
            chat = ChatHistory(Path(tmp) / "history.jsonl")
            rec = chat.append(role="assistant", text="[bqb:抱抱]", source="claude")
            self.assertEqual("[bqb:抱抱]", rec["text"])


if __name__ == "__main__":
    unittest.main()
