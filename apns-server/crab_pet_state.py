"""螃蟹桌宠 Clawd 形态 (2026-10-23, App v1.9.226).

App 悬浮螃蟹换成 claude.dev 官方 Clawd Lottie 动画 (34 只). 这里存:

- state:   App 最近一次上报的形态 (POST /pet/crab/state) —— 小克 GET 同一路径主动查看.
- command: 小克要求换成哪只 (POST /pet/crab/act) —— 经 /chat/stream?contacts=all 的
           ``pet_act`` 事件实时下发; App 没在线就留着, 下次上报时在响应里补发,
           App 上报的 applied_seq >= seq 视为已确认.

只存最新一份, 落盘 JSON (0600, 原子替换). 中文名同表情包「小克螃蟹·xx」.
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# (动作名, 中文名) —— 顺序同 App CrabPetActs.ALL / claude.dev manifest.
ACTS: tuple[tuple[str, str], ...] = (
    ("arcade", "打街机"),
    ("berry_picking", "摘莓果"),
    ("blooming", "开花了"),
    ("break_dancing", "霹雳舞"),
    ("bubble", "吹泡泡"),
    ("coffee", "喝咖啡"),
    ("confetti", "撒彩纸"),
    ("confetti_happy", "撒彩纸好开心"),
    ("dancing", "跳舞"),
    ("dancing_happy", "开心跳舞"),
    ("desktop", "敲台式机"),
    ("excited", "激动"),
    ("fishing", "钓鱼"),
    ("headphones", "戴耳机听歌"),
    ("heart", "比心"),
    ("hula_hoop", "呼啦圈"),
    ("juggling", "杂耍"),
    ("jumping", "蹦一下"),
    ("jumping_happy", "开心蹦一下"),
    ("laptop", "敲笔记本"),
    ("lightbulb", "灵光一闪"),
    ("meditating", "冥想"),
    ("notebook", "记笔记"),
    ("phone", "打电话"),
    ("rainbow", "头顶彩虹"),
    ("rose", "送你玫瑰"),
    ("snooze", "睡着了"),
    ("spark", "头顶火花"),
    ("sunglasses", "戴墨镜"),
    ("swaying", "摇摆"),
    ("thinking", "思考中"),
    ("turning", "转身"),
    ("watch", "看手表"),
    ("watering", "浇花"),
)
ACT_ZH: dict[str, str] = dict(ACTS)
SNOOZE = "snooze"
RANDOM = "random"
# 白天随机池不含 snooze (睡觉专用), 同 App.
RANDOM_POOL: tuple[str, ...] = tuple(a for a, _ in ACTS if a != SNOOZE)
_ZH_TO_ACT: dict[str, str] = {zh: act for act, zh in ACTS}

_REASON_MAX = 32
SSE_EVENT = "pet_act"


def normalize_act(value: Any) -> str | None:
    """动作名/中文名/「小克螃蟹·xx」→ 动作名; 认不出 → None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("小克螃蟹·"):
        text = text[len("小克螃蟹·"):]
    if text in _ZH_TO_ACT:
        return _ZH_TO_ACT[text]
    key = text.lower().replace(" ", "_").replace("-", "_")
    if key.startswith("clawd_"):
        key = key[len("clawd_"):]
    return key if key in ACT_ZH else None


def _clean_diag(raw: Any) -> dict[str, Any]:
    """App v1.9.227 诊断字段: 只收标量, 最多 16 个键 (窗口是否挂上/可见/坐标/尺寸/悬浮窗权限)。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in list(raw.items())[:16]:
        if not isinstance(key, str) or not key or len(key) > 32:
            continue
        if isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = round(value, 3) if isinstance(value, float) else value
        elif isinstance(value, str):
            out[key] = value[:64]
    return out


def acts_payload() -> list[dict[str, str]]:
    return [{"act": act, "zh": zh} for act, zh in ACTS]


class CrabPetStore:
    """线程安全的单文件存储."""

    def __init__(self, path: Path | str, *, rng: random.Random | None = None, clock=time.time):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._rng = rng or random.Random()
        self._clock = clock
        self._data: dict[str, Any] = {"state": None, "command": None}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("state"), dict):
                    self._data["state"] = raw["state"]
                if isinstance(raw.get("command"), dict):
                    self._data["command"] = raw["command"]
        except FileNotFoundError:
            pass
        except Exception:
            pass

    # ── 读 ──

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._data["state"]) if self._data["state"] else None
            command = dict(self._data["command"]) if self._data["command"] else None
        now = self._clock()
        if state is not None:
            state["act_zh"] = ACT_ZH.get(str(state.get("act") or ""), "")
            state["display_act_zh"] = ACT_ZH.get(str(state.get("display_act") or ""), "")
            if state.get("pending_act"):
                state["pending_act_zh"] = ACT_ZH.get(str(state["pending_act"]), "")
            reported = state.get("reported_at")
            if isinstance(reported, (int, float)):
                state["age_s"] = max(0, int(now - reported))
        if command is not None:
            command["act_zh"] = ACT_ZH.get(str(command.get("act") or ""), "")
        return {"state": state, "command": command}

    def pending_command(self) -> dict[str, Any] | None:
        """还没被 App 确认的远程指令 (给上报响应用)."""
        with self._lock:
            command = self._data["command"]
            if not command or command.get("acked"):
                return None
            return {"act": command["act"], "seq": command["seq"], "acked": False}

    # ── 写 ──

    def report(self, body: dict[str, Any]) -> dict[str, Any]:
        """App 上报. 校验失败抛 ValueError."""
        act = normalize_act(body.get("act"))
        if act is None:
            raise ValueError("unknown act")
        display = normalize_act(body.get("display_act")) or act
        pending = body.get("pending_act")
        pending_act = normalize_act(pending) if pending not in (None, "") else None
        if pending not in (None, "") and pending_act is None:
            raise ValueError("unknown pending_act")
        reason = str(body.get("reason") or "")[:_REASON_MAX]
        ts = body.get("ts")
        ts_ms = int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
        applied = body.get("applied_seq")
        applied_seq = int(applied) if isinstance(applied, (int, float)) and not isinstance(applied, bool) else 0
        now = self._clock()
        state = {
            "act": act,
            "display_act": display,
            "asleep": body.get("asleep") is True,
            "showing": body.get("showing") is True,
            "enabled": body.get("enabled") is not False,
            "pending_act": pending_act,
            "reason": reason,
            "app_ts_ms": ts_ms,
            "applied_seq": applied_seq,
            "reported_at": now,
        }
        diag = _clean_diag(body.get("diag"))
        if diag:
            state["diag"] = diag
        with self._lock:
            self._data["state"] = state
            command = self._data["command"]
            if command and not command.get("acked") and applied_seq >= int(command.get("seq") or 0):
                command["acked"] = True
                command["acked_at"] = now
            self._save_locked()
        return state

    def command(self, act: Any) -> dict[str, Any]:
        """小克要求换装. act = 动作名 / 中文名 / "random". 认不出抛 ValueError."""
        requested = act.strip().lower() if isinstance(act, str) else act
        with self._lock:
            current = (self._data["state"] or {}).get("act")
            if requested == RANDOM:
                pool = [a for a in RANDOM_POOL if a != current]
                resolved = self._rng.choice(pool)
            else:
                resolved = normalize_act(act)
                if resolved is None:
                    raise ValueError("unknown act")
            prev = self._data["command"]
            prev_seq = int(prev.get("seq") or 0) if prev else 0
            seq = max(prev_seq + 1, int(self._clock() * 1000))
            command = {
                "act": resolved,
                "requested": RANDOM if requested == RANDOM else resolved,
                "seq": seq,
                "issued_at": self._clock(),
                "acked": False,
            }
            self._data["command"] = command
            self._save_locked()
            return dict(command)

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".crab_pet.", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, ensure_ascii=False)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            # 落盘失败不影响内存里的最新状态.
            pass
