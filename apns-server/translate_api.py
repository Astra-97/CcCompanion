"""思考链翻译 via OpenRouter 千问 (2026-09-08).

小克 (CC 通道) 的思考链是英文。两个入口：
- 服务端自动预翻译 :func:`translate_thinking_auto` —— 思考链写入聊天记录
  之前同步翻成中文（push.py /chat/append 接入），记录里直接存中文，
  英文原文留 metadata.thinking_original，app 零改动直接显示。
- ``POST /chat/translate`` 手动翻译接口（保留，供旧记录按需补译）。
本模块把原文发给千问做忠实直译并返回简体中文。结果按
sha256(PROMPT_VERSION + text) 落盘缓存（``tokens/translate_cache/``），
同一思考链重复翻译不再烧钱；prompt 版本变化即自动失效旧缓存。

模型选型 (2026-10-18 升级，实测 OpenRouter /models 在售价格)：
``qwen/qwen3-235b-a22b-2507`` —— $0.09/M 输入 + $0.55/M 输出，262k 上下文，
instruct 版不输出思考标签浪费 output token。此前默认
``qwen/qwen3-30b-a3b-instruct-2507``（MoE 仅激活 3B）被 Astra 实测翻译
质量差、身份自述混乱（自称 ChatGPT/GPT-4.1 横跳）、译文 tab 偶发残留英文
原文；235b-a22b 激活 22B 是前者 7 倍+，输入同价、输出 ~1.8 倍，一段
~2000 词的思考链单次约 $0.001，属「稍微聪明」档里性价比最高的一步。

两条独立翻译路 (2026-10-18 Astra 拍板拆分，互不干扰)：
- 思维链预翻译 :func:`translate_thinking_auto` 用 :func:`thinking_translate_model`
  （env/drop-in ``TRANSLATE_MODEL_THINKING`` 覆盖，缺省回落共享默认）。
- 语音翻译 ``POST /chat/translate`` + 后台预热用 :func:`voice_translate_model`
  （env/drop-in ``TRANSLATE_MODEL_VOICE`` 覆盖，缺省同上）。
两路模型可各自独立换，缓存条目带 model 字段，换模型旧缓存自动失效、
互不串味；同模型时缓存照常共享（语音预热与手动「译」必须同模型才能命中）。

任何失败（无 key / 超时 / 402 / 网络）都抛 :class:`TranslateError`，
调用方返回错误码让 app 静默降级，绝不影响聊天主流程。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx


logger = logging.getLogger(__name__)

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
TRANSLATE_MODEL = "qwen/qwen3-235b-a22b-2507"
# 两路独立覆盖项：env 优先，其次 cc-companion.service 的 systemd drop-in
# （与 OPENROUTER_API_KEY 同一读取口径）。只设 env 名，值绝不入库。
TRANSLATE_MODEL_THINKING_ENV = "TRANSLATE_MODEL_THINKING"
TRANSLATE_MODEL_VOICE_ENV = "TRANSLATE_MODEL_VOICE"
MAX_TEXT_CHARS = 20_000
DEFAULT_TIMEOUT_SEC = 90.0
# 服务端自动预翻译 (2026-09-08) 同步阻塞聊天入库，超时要短，宁可存原文不拖累消息。
AUTO_TIMEOUT_SEC = 30.0
# OpenRouter 侧按 UA 拦截，伪装成 curl（同 voice_gemini）。
REQUEST_USER_AGENT = "curl/7.81.0"

SERVICE_ENV_FILE = Path("/etc/systemd/system/cc-companion.service.d/openrouter.conf")
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "tokens" / "translate_cache"

TRANSLATE_SYSTEM_PROMPT = (
    "你是一名专业翻译。把用户给出的内容忠实直译为简体中文。"
    "若输入已经是简体中文（或以中文为主），逐字原样返回输入内容，"
    "不得做任何改动，尤其不得把中文翻译成英文或其他任何语言。"
    "代码、shell 命令、文件路径、API 名等专有名词保持原文不译；"
    "[laughs]、[softly]、[pause] 等方括号语气/情绪标签是 TTS 控制标记，"
    "逐字原样保留在译文对应位置，不得翻译、增删；"
    "完整保留 markdown 结构（标题、列表、代码块、加粗等）。"
    "不要解释、不要评注、不要输出原文，只输出译文。"
)

# 缓存键纳入 prompt 版本（2026-09-11）：prompt 一旦改动，旧 prompt 产出的译文
# 不得再被同文命中——2026-09-11 的事故就是旧 prompt 把中文思维链反向翻成英文，
# 若缓存键不含 prompt 版本，同一条文本会永久命中那条错误译文。
# 版本直接从 prompt 内容派生（独立审核建议）：改 prompt 自动换版本，
# 不存在"改了 prompt 忘 bump 版本"导致陈旧缓存命中的可能。
PROMPT_VERSION = hashlib.sha256(TRANSLATE_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


class TranslateError(RuntimeError):
    """The translate request failed; carries a stable machine-readable code."""


_cache_lock = threading.Lock()
# 在途合并注册表：cache key -> 「在途那次翻译完成」事件（见 translate_text）。
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def _service_env_value(name: str) -> str:
    """Env first, then the cc-companion.service systemd override file."""

    value = str(os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        for line in SERVICE_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("Environment="):
                continue
            assignment = line[len("Environment="):].strip().strip('"').strip("'")
            if assignment.startswith(f"{name}="):
                return assignment[len(name) + 1:].strip()
    except OSError:
        pass
    return ""


def openrouter_api_key() -> str:
    """Env first, then the cc-companion.service systemd override file."""

    return _service_env_value("OPENROUTER_API_KEY")


def thinking_translate_model() -> str:
    """思维链预翻译路模型：``TRANSLATE_MODEL_THINKING`` 覆盖，缺省共享默认。"""

    return _service_env_value(TRANSLATE_MODEL_THINKING_ENV) or TRANSLATE_MODEL


def voice_translate_model() -> str:
    """语音翻译路模型：``TRANSLATE_MODEL_VOICE`` 覆盖，缺省共享默认。"""

    return _service_env_value(TRANSLATE_MODEL_VOICE_ENV) or TRANSLATE_MODEL


def _cache_key(text: str, salt: str = "") -> str:
    # salt 仅在有值时参与键派生，保证既有缓存条目（无 salt）键值不变。
    base = f"{PROMPT_VERSION}\n{text}"
    if salt:
        base = f"{salt}\n{base}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _cache_read(cache_dir: Path, key: str, model: str) -> str:
    try:
        data = json.loads((cache_dir / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict) or data.get("model") != model:
        return ""
    return str(data.get("translated") or "")


def _cache_write(cache_dir: Path, key: str, model: str, translated: str) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"model": model, "translated": translated, "ts": time.time()}
        fd, tmp_name = tempfile.mkstemp(dir=str(cache_dir), prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_name, cache_dir / f"{key}.json")
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        # 缓存写不进去只是多烧一次 API，不影响本次应答。
        pass


def translate_text(
    text: str,
    *,
    api_key: str | None = None,
    cache_dir: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    url: str = OPENROUTER_CHAT_URL,
    model: str = TRANSLATE_MODEL,
    max_chars: int = MAX_TEXT_CHARS,
    cache_salt: str = "",
) -> dict[str, Any]:
    """Translate one thinking chain to Simplified Chinese.

    Returns ``{"translated": str, "cached": bool, "truncated": bool,
    "usage": dict}``. Overlong input is truncated to ``max_chars`` before
    sending (the cache key is taken over the text actually sent).
    中文为主的输入恒等短路、直接原样返回（见 :func:`_is_chinese_dominant`）。
    同一文本已有翻译在途（如语音后台预热）时搭车等待其结果，不重复发起
    OpenRouter 请求（2026-10-02 在途合并）。
    ``cache_salt`` 非空时参与缓存键派生，用于缩水质检重试与首次翻译
    使用不同缓存条目（见 :func:`translate_thinking_auto`）。
    """

    source = str(text or "")
    if not source.strip():
        raise TranslateError("empty_text")
    truncated = len(source) > max_chars
    if truncated:
        source = source[:max_chars]

    # 中文为主恒等短路（2026-09-11，独立审核建议下沉到本函数）：中译中恒等于
    # 原文，走模型只有被改动/反向翻成英文的风险，零收益。/chat/translate 手动
    # 入口（push.py 直调本函数）与 translate_thinking_auto 两个调用点一并覆盖；
    # 不查缓存、不需要 API key、不写缓存，零成本。
    if _is_chinese_dominant(source):
        return {"translated": source, "cached": False, "truncated": truncated, "usage": {}}

    key = _cache_key(source, cache_salt)
    cache_path = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    with _cache_lock:
        hit = _cache_read(cache_path, key, model)
    if hit:
        return {"translated": hit, "cached": True, "truncated": truncated, "usage": {}}

    # 在途合并（2026-10-02 语音「译」慢的根因）：语音入库的后台预热与 App 点
    # 「译」常撞进同一文本的翻译窗口——缓存还没写好，点的人另起一次冷调，
    # 同一段文字烧两份钱、先点的人干等一整次。同 key 已有翻译在跑时搭车等
    # 待，在途那次写完缓存即命中返回；等待上限 timeout，超时抛 TranslateError
    # 走调用方既有失败降级。在途那次失败没落缓存时绕回争取自己当 owner，
    # 不因为别人失败而放弃本次翻译。
    deadline = time.monotonic() + timeout
    while True:
        with _inflight_lock:
            event = _inflight.get(key)
            if event is None:
                event = threading.Event()
                _inflight[key] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not event.wait(remaining):
            raise TranslateError("translate_inflight_timeout")
        with _cache_lock:
            hit = _cache_read(cache_path, key, model)
        if hit:
            return {"translated": hit, "cached": True, "truncated": truncated, "usage": {}}

    try:
        return _translate_network(
            source,
            cache_path=cache_path,
            key=key,
            api_key=api_key,
            timeout=timeout,
            url=url,
            model=model,
            truncated=truncated,
        )
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
            event.set()


def _translate_network(
    source: str,
    *,
    cache_path: Path,
    key: str,
    api_key: str | None,
    timeout: float,
    url: str,
    model: str,
    truncated: bool,
) -> dict[str, Any]:
    """translate_text 的网络段：唯一发起 OpenRouter 请求的地方。

    调用前必须已持有 key 的在途 owner 位（或确认无在途），返回前由
    translate_text 的 finally 释放 owner 并唤醒搭车者。
    """

    effective_key = api_key if api_key is not None else openrouter_api_key()
    if not effective_key:
        raise TranslateError("openrouter_api_key_missing")

    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
            {"role": "user", "content": source},
        ],
    }
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {effective_key}",
                "User-Agent": REQUEST_USER_AGENT,
            },
            json=payload,
            timeout=timeout,
        )
    except Exception as exc:
        raise TranslateError(f"openrouter_request_failed: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise TranslateError(f"openrouter_http_{response.status_code}")
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise TranslateError("openrouter_bad_payload") from exc
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    translated = str(content or "").strip()
    if not translated:
        raise TranslateError("openrouter_empty_reply")

    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    with _cache_lock:
        _cache_write(cache_path, key, model, translated)
    return {
        "translated": translated,
        "cached": False,
        "truncated": truncated,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
    }


# ---------------------------------------------------------------------------
# 思考链服务端自动预翻译 (2026-09-08)
# 小克 (CC 通道) 的思考链到达服务端、写聊天记录之前同步翻成中文，记录里直接
# 存中文，app 零改动；英文原文由调用方留进 metadata.thinking_original。
# ---------------------------------------------------------------------------


def _is_chinese_dominant(text: str) -> bool:
    """中文为主判定：剔除 ``` 代码围栏后，CJK 表意字符数 >= 其他字母数。

    代码/shell 是语言中立的，不计入"正文语言"——否则一段中文 prose 带一个
    英文代码块就会被误判成英文重新过模型。

    这是恒等短路，不是语言预判跳翻：Astra 2026-09-08 拍板"全部翻译、别有预置
    语言门控"针对的是需要翻译的输入；中文为主的输入"中译中"恒等于原文，走模型
    只有风险零收益——2026-09-11 实测旧 prompt 下 qwen 把整条中文思维链反向翻成
    英文，新 prompt 下仍会把半角逗号改成全角。故中文为主直接原样返回，与拍板
    意图一致（该翻的全翻，不该动的绝不动）。
    """

    prose = _CODE_FENCE_RE.sub(" ", text)
    cjk = 0
    other_letters = 0
    for ch in prose:
        if (
            ("一" <= ch <= "鿿")  # CJK 统一表意文字 U+4E00-U+9FFF
            or ("㐀" <= ch <= "䶿")  # 扩展 A U+3400-U+4DBF
            or ("豈" <= ch <= "﫿")  # 兼容表意文字 U+F900-U+FAFF
        ):
            cjk += 1
        elif ch.isalpha():
            other_letters += 1
    return cjk > 0 and cjk >= other_letters


# 译文缩水质检 (2026-10-16)：模型偶发"摸鱼"——5 句英文长段只翻出一句俏皮话
# （tokens/chat_history.jsonl ts=2026-10-16T08:11:00.199Z 实例，后半段整体没翻，
# 管线无检出直接入库）。判定启发式：原文较长 + 译文字符数不足原文 ~45%。
# EN→ZH 本身有字符压缩（正常译文常见 40%-70%），阈值偏宽，误报代价只是多一次
# 重试并取较长版，不会改坏正常译文。
ABRIDGED_MIN_SOURCE_CHARS = 200
ABRIDGED_MIN_RATIO = 0.45
# 重试走独立缓存键（salt），而不是绕开缓存：首次的缩水译文已占正常键，重试
# 必须换键才可能拿到新结果；重试结果缓存在 salt 键下，同一文本再次入库时
# 正常键 + salt 键两次查找全部命中、零 API。不覆盖正常键——/chat/translate
# 手动入口与语音预热共用正常键，语义保持不变。
ABRIDGED_RETRY_CACHE_SALT = "abridged-retry-v1"


def _looks_abridged(source: str, translated: str) -> bool:
    """长原文 + 译文字符数不足原文 ~45% → 疑似缩水。"""

    if len(source) < ABRIDGED_MIN_SOURCE_CHARS:
        return False
    return len(translated) < len(source) * ABRIDGED_MIN_RATIO


def translate_thinking_auto(
    text: str,
    *,
    timeout: float = AUTO_TIMEOUT_SEC,
    **kwargs: Any,
) -> tuple[str, str | None, bool]:
    """Auto-translate one thinking chain before it lands in chat history.

    Returns ``(display_text, original_or_None, translation_suspect)``:
    译成中文时给出原文让调用方存进 metadata.thinking_original；失败 / 超时
    一律静默返回 ``(原文, None, False)``，消息绝不因翻译丢失或久等。
    重复内容走磁盘缓存零成本。``translation_suspect`` 为缩水质检标记：
    长原文两次翻译都缩水（或重试失败仍只有缩水版）时为 True，调用方写进
    metadata.thinking_translation_suspect 落数据，App 侧暂不消费。

    中文为主的输入直接原样返回（见 :func:`_is_chinese_dominant`）；其余输入
    不设语言预判门控、全部走模型翻译（Astra 2026-09-08 拍板）。
    这里的短路让调用方能断言"中文不发模型"；:func:`translate_text` 入口也
    内置同一短路，覆盖 /chat/translate 手动入口，两处重复无害、行为一致。
    """

    source = str(text or "")
    if not source.strip():
        return text, None, False
    if _is_chinese_dominant(source):
        return text, None, False
    # 思维链路独立模型 (2026-10-18 分路)：调用方未显式给 model 时走
    # thinking_translate_model()，与语音路 voice_translate_model() 互不干扰。
    kwargs.setdefault("model", thinking_translate_model())
    try:
        result = translate_text(source, timeout=timeout, **kwargs)
    except Exception as exc:
        logger.warning("thinking auto-translate failed, keeping original: %s", exc)
        return text, None, False
    translated = str(result.get("translated") or "").strip()
    if not translated or translated == source.strip():
        return text, None, False
    suspect = False
    if _looks_abridged(source, translated):
        # 疑似缩水：同一 prompt 重试一次（temperature=0 但模型采样仍有方差），
        # 两版取较长；重试失败保留首版，仍按疑似缩水标记落库。
        retry_kwargs = dict(kwargs)
        retry_kwargs["cache_salt"] = ABRIDGED_RETRY_CACHE_SALT
        retry_translated = ""
        try:
            retry = translate_text(source, timeout=timeout, **retry_kwargs)
            retry_translated = str(retry.get("translated") or "").strip()
        except Exception as exc:
            logger.warning("thinking abridged-retry failed, keeping first pass: %s", exc)
        if (
            retry_translated
            and retry_translated != source.strip()
            and len(retry_translated) > len(translated)
        ):
            translated = retry_translated
        suspect = _looks_abridged(source, translated)
    return translated, text, suspect
