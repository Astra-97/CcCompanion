"""End-to-end-ish test of apples dispatch guards (offline; mocks tmux + codex).

Exercises 4 guard branches + astra-exemption + backward-compat path,
asserting the system messages land in chat_history_apples.jsonl.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Use a temp chat history so we don't pollute the real one.
TEST_CHAT_PATH = HERE / "tokens" / "_test_apples_history.jsonl"
if TEST_CHAT_PATH.exists():
    TEST_CHAT_PATH.unlink()

import push  # noqa: E402
from chat_history import ChatHistory  # noqa: E402


def make_handler():
    """Build a minimal Handler-like object that has the methods we need from push.PushHandler.

    We can't easily instantiate push.PushHandler (needs socket). Instead we bind the
    methods directly to a SimpleNamespace-style object.
    """
    class Stub:
        pass

    stub = Stub()
    stub._test_chat = ChatHistory(TEST_CHAT_PATH)

    # Bind unbound methods from push.PushHandler onto the stub via descriptors.
    H = push.PushHandler

    def _chat_for_contact(self, contact_id):
        return self._test_chat

    def _set_typing_for_contact(self, contact_id, state):
        # no-op for tests
        pass

    def _source_for_request(self, contact_id):
        return "test"

    def _inject_to_session(self, session, text, source="ios-app", sender="iphone"):
        # mock — pretend tmux inject ok, don't actually fire
        return True, None

    def _start_group_kairos_reply(self, chat, text, sender_name="Astra", hop_count=0):
        # mock — pretend codex inject ok
        return None

    def _remember_group_reply(self, member_id, ts, source_member=None):
        return None

    def _group_reply_marker(self, member_id, user_ts):
        return f"[{member_id}|{user_ts}]"

    # Wire methods
    bound = {
        "_chat_for_contact": _chat_for_contact,
        "_set_typing_for_contact": _set_typing_for_contact,
        "_source_for_request": _source_for_request,
        "_inject_to_session": _inject_to_session,
        "_start_group_kairos_reply": _start_group_kairos_reply,
        "_remember_group_reply": _remember_group_reply,
        "_group_reply_marker": _group_reply_marker,
    }
    # Grab actual Handler methods we want to test
    for name in [
        "_dispatch_apples_mentions",
        "_apples_dispatch_allowed",
        "_apples_sender_global_allowed",
        "_apples_room_global_allowed",
        "_apples_record_global",
        "_apples_is_human_sender",
        "_apples_emit_drop_system_msg",
        "_apples_member_name",
        "_apples_members",
        "_apples_self_id",
        "_apples_member_ids",
    ]:
        bound[name] = getattr(H, name)

    # Bind as instance methods
    import types
    for name, fn in bound.items():
        setattr(stub, name, types.MethodType(fn, stub))

    # Copy class constants
    for const in [
        "APPLES_HOP_LIMIT",
        "APPLES_HOP_LIMIT_DEBUG",
        "APPLES_PAIR_RATE_LIMIT_SEC",
        "APPLES_SENDER_GLOBAL_LIMIT",
        "APPLES_ROOM_GLOBAL_LIMIT",
        "APPLES_GLOBAL_WINDOW_SEC",
        "APPLES_AGENT_SENDERS",
    ]:
        setattr(stub, const, getattr(H, const))

    return stub


def reset_class_caches(handler=None):
    """Clear class-level rate caches so each test starts fresh.

    Caches live on type(self) inside the helpers. For Stub-bound methods that
    means they live on Stub (per make_handler() call). We clear both the
    Stub's class and PushHandler for safety.
    """
    targets = [push.PushHandler]
    if handler is not None:
        targets.append(type(handler))
    for tgt in targets:
        for attr in ("_apples_dispatch_rate", "_apples_sender_global", "_apples_room_global"):
            if hasattr(tgt, attr):
                delattr(tgt, attr)


def clear_pair_cache(handler):
    """Clear only the pair cache on the stub's class."""
    for tgt in (type(handler), push.PushHandler):
        if hasattr(tgt, "_apples_dispatch_rate"):
            getattr(tgt, "_apples_dispatch_rate").clear()


def read_history():
    if not TEST_CHAT_PATH.exists():
        return []
    out = []
    for line in TEST_CHAT_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


def truncate_history():
    if TEST_CHAT_PATH.exists():
        TEST_CHAT_PATH.unlink()


def make_rec(sender_id, text, mentions=None):
    """Append a fake assistant rec to history and return it."""
    h = ChatHistory(TEST_CHAT_PATH)
    rec = h.append(
        role="assistant",
        text=text,
        source=f"test:{sender_id}",
        sender_id=sender_id,
        sender_name=sender_id,
        mentions=mentions or [],
    )
    return rec


def get_last_system_msg():
    hist = read_history()
    for r in reversed(hist):
        if r.get("role") == "system":
            return r
    return None


# === TEST a: hop=2 → hop_limit drop ===
def test_hop_limit():
    print("\n=== TEST a: hop_limit (hop_count=2) ===")
    reset_class_caches()
    truncate_history()
    handler = make_handler()
    rec = make_rec("xiaoke", "@Kairos 测试 hop limit")
    routed, errors = handler._dispatch_apples_mentions(
        rec, "apples", {"kairos"}, "小克", hop_count=2, sender_id="xiaoke",
    )
    sys_msg = get_last_system_msg()
    print(f"routed={routed} errors={errors}")
    print(f"system msg text={sys_msg['text'] if sys_msg else None}")
    print(f"system meta={sys_msg.get('metadata') if sys_msg else None}")
    assert routed == [], f"expected no routing, got {routed}"
    assert sys_msg is not None, "expected system msg"
    assert "hop limit" in sys_msg["text"], f"text mismatch: {sys_msg['text']}"
    assert sys_msg["metadata"]["drop_reason"] == "hop_limit"
    assert sys_msg["metadata"]["original_sender_id"] == "xiaoke"
    assert sys_msg["sender_name"] == "系统"
    assert sys_msg["sender_id"] == "system"
    print("PASS")


# === TEST b: per-pair rate limit (xiaoke->kairos twice in <60s) ===
def test_pair_rate_limit():
    print("\n=== TEST b: per-pair 60s limit (xiaoke->kairos 2nd call) ===")
    reset_class_caches()
    truncate_history()
    handler = make_handler()
    # First call: should succeed
    rec1 = make_rec("xiaoke", "@Kairos 第1条")
    routed1, errors1 = handler._dispatch_apples_mentions(
        rec1, "apples", {"kairos"}, "小克", hop_count=1, sender_id="xiaoke",
    )
    print(f"first  routed={routed1} errors={errors1}")
    assert "kairos" in routed1
    # Second call (immediately): should hit pair rate limit
    rec2 = make_rec("xiaoke", "@Kairos 第2条")
    routed2, errors2 = handler._dispatch_apples_mentions(
        rec2, "apples", {"kairos"}, "小克", hop_count=1, sender_id="xiaoke",
    )
    sys_msg = get_last_system_msg()
    print(f"second routed={routed2} errors={errors2}")
    print(f"system msg text={sys_msg['text'] if sys_msg else None}")
    assert routed2 == [], f"expected no routing, got {routed2}"
    assert "per-pair rate limit" in sys_msg["text"]
    assert sys_msg["metadata"]["drop_reason"] == "pair_rate_limit"
    print("PASS")


# === TEST c: per-sender global limit (xiaoke 4 times in 60s) ===
def test_sender_global():
    print("\n=== TEST c: per-sender global 3/60s (4th call drops) ===")
    reset_class_caches()
    truncate_history()
    handler = make_handler()
    # We need 4 calls from sender=xiaoke. To avoid pair-limit collision,
    # alternate targets... but only 2 valid targets exist (kairos/xiaoke).
    # Easier: monkey-patch pair-limit cache to allow all pair calls,
    # OR target kairos each time but bypass pair limit by manually resetting.
    # Strategy: send 4 messages, after each reset just the pair cache, leaving
    # sender_global counter accumulating. The 4th should hit sender_rate_limit.
    for i in range(3):
        rec = make_rec("xiaoke", f"@Kairos 第{i+1}条")
        routed, errors = handler._dispatch_apples_mentions(
            rec, "apples", {"kairos"}, "小克", hop_count=1, sender_id="xiaoke",
        )
        print(f"call#{i+1} routed={routed} errors={errors}")
        # Clear pair cache so next call doesn't hit pair rate limit
        clear_pair_cache(handler)
    # 4th call — should hit sender_rate_limit (because sender_global allows 3)
    rec = make_rec("xiaoke", "@Kairos 第4条")
    routed, errors = handler._dispatch_apples_mentions(
        rec, "apples", {"kairos"}, "小克", hop_count=1, sender_id="xiaoke",
    )
    sys_msg = get_last_system_msg()
    print(f"call#4 routed={routed} errors={errors}")
    print(f"system msg text={sys_msg['text'] if sys_msg else None}")
    assert routed == []
    assert "sender rate limit" in sys_msg["text"]
    assert sys_msg["metadata"]["drop_reason"] == "sender_rate_limit"
    print("PASS")


# === TEST d: per-room global limit (mixed senders 7 calls; 7th drops) ===
def test_room_global():
    print("\n=== TEST d: per-room global 6/60s (7th call drops) ===")
    reset_class_caches()
    truncate_history()
    handler = make_handler()
    # Need 7 successful-or-attempted dispatches by agent senders to fill the
    # room counter. Alternate between kairos and xiaoke as sender to bypass
    # per-sender (3 each) limits — 3 from each = 6 successful. 7th fails.
    senders = ["kairos", "xiaoke", "kairos", "xiaoke", "kairos", "xiaoke", "kairos"]
    targets_map = {"kairos": "xiaoke", "xiaoke": "kairos"}
    last_routed = None
    for i, s in enumerate(senders):
        t = targets_map[s]
        rec = make_rec(s, f"@{t} 第{i+1}条")
        routed, errors = handler._dispatch_apples_mentions(
            rec, "apples", {t}, s, hop_count=1, sender_id=s,
        )
        print(f"call#{i+1} sender={s}->{t} routed={routed} errors={errors}")
        # clear pair cache to avoid pair-limit blocking
        clear_pair_cache(handler)
        # clear per-sender global so room limit is the only barrier
        for tgt in (type(handler), push.PushHandler):
            if hasattr(tgt, "_apples_sender_global"):
                getattr(tgt, "_apples_sender_global").clear()
        last_routed = routed
    sys_msg = get_last_system_msg()
    print(f"final routed={last_routed}")
    print(f"system msg text={sys_msg['text'] if sys_msg else None}")
    print(f"system meta={sys_msg.get('metadata') if sys_msg else None}")
    assert last_routed == [], f"7th should be dropped, got {last_routed}"
    assert "room rate limit" in sys_msg["text"]
    assert sys_msg["metadata"]["drop_reason"] == "room_rate_limit"
    print("PASS")


# === TEST e: astra exemption (5 rapid dispatches, all succeed) ===
def test_astra_exemption():
    print("\n=== TEST e: astra exempted from all limits (5 rapid) ===")
    reset_class_caches()
    truncate_history()
    handler = make_handler()
    for i in range(5):
        rec = make_rec("astra", f"@Kairos 第{i+1}条 (astra)")
        routed, errors = handler._dispatch_apples_mentions(
            rec, "apples", {"kairos"}, "Astra", hop_count=0, sender_id="astra",
        )
        print(f"call#{i+1} routed={routed} errors={errors}")
        assert routed == ["kairos"], f"astra call#{i+1} should route, got {routed}"
        assert errors == {}, f"unexpected errors {errors}"
    # No system messages should be emitted (no drops)
    sys_msgs = [r for r in read_history() if r.get("role") == "system"]
    assert sys_msgs == [], f"expected no system msgs, got {sys_msgs}"
    print("PASS")


# === TEST f: backward compat (astra single @kairos) ===
def test_backward_compat():
    print("\n=== TEST f: astra 1x @kairos (backward compat) ===")
    reset_class_caches()
    truncate_history()
    handler = make_handler()
    rec = make_rec("astra", "@Kairos 你好")
    routed, errors = handler._dispatch_apples_mentions(
        rec, "apples", {"kairos"}, "Astra", hop_count=0, sender_id="astra",
    )
    print(f"routed={routed} errors={errors}")
    assert routed == ["kairos"]
    assert errors == {}
    sys_msgs = [r for r in read_history() if r.get("role") == "system"]
    assert sys_msgs == []
    print("PASS")


if __name__ == "__main__":
    test_hop_limit()
    test_pair_rate_limit()
    test_sender_global()
    test_room_global()
    test_astra_exemption()
    test_backward_compat()
    print("\n=== ALL TESTS PASSED ===")
    print(f"\nConstants: HOP_LIMIT={push.PushHandler.APPLES_HOP_LIMIT}")
    print(f"PAIR_LIMIT_SEC={push.PushHandler.APPLES_PAIR_RATE_LIMIT_SEC}")
    print(f"SENDER_GLOBAL={push.PushHandler.APPLES_SENDER_GLOBAL_LIMIT}/60s")
    print(f"ROOM_GLOBAL={push.PushHandler.APPLES_ROOM_GLOBAL_LIMIT}/60s")
    # cleanup
    if TEST_CHAT_PATH.exists():
        TEST_CHAT_PATH.unlink()
