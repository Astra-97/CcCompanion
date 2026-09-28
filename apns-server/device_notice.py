"""手机感知事件 → 小克 Claude Code 会话（cctg）的低调注入（2026-09-28）。

形态照抄 roborock-watch / mihome watch.py 的「知悉级」注入：单行
``【<emoji> <主语>·自动触发】HH:MM <人话>（低调模式：…立刻结束）``，
用 tmux 注入 cctg，让小克本体记在心里、之后聊天自然提到，不回复不写日记。

和外部 watcher 的差别只有一点：它们是独立进程直接 ``send-keys``，这里跑在
apns-server 进程内，和 App 私聊的 exact-turn 注入共用同一个 tmux 会话。
为了不把设备通知粘进她正在发送/生成中的那一轮，注入走单一后台 worker：
小克忙（App 回合进行中 / Stop 收尾中 / 发送预留中）时按 FIFO 排队等空闲，
空闲判定与真正注入在同一把 xiaoke_stop_lock 内完成（App 路径也在这把锁里
paste+Enter），等待有上限——超时仍照注（等价于 watcher 的直接 send-keys，
由 Claude Code 自身的输入队列兜底），宁可晚到不可漏报。

隐私红线：注入文案只含当下这一条事件，本模块不落盘、不留历史。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger("device_notice")

TZ_BEIJING = ZoneInfo("Asia/Shanghai")

# 与 roborock-watch 的 SILENT_SUFFIX 逐字一致（小克本体已经认识这句话）。
SILENT_SUFFIX = (
    "（低调模式：本条不用回复、不用专门发消息、不写日记不记账；但记在心里，"
    "之后和方小南聊天时可以自然提到。本回合禁用工具，立刻结束）"
)

NOTICE_KIND_BATTERY = "battery"
NOTICE_KIND_APP = "app"
NOTICE_TAGS = {
    NOTICE_KIND_BATTERY: "🔋 手机电量",
    NOTICE_KIND_APP: "📱 手机应用",
}

BUSY_POLL_SECONDS = 2.0
BUSY_MAX_WAIT_SECONDS = 600.0
QUEUE_MAX = 50


def build_device_notice(kind: str, text: str, *, now: datetime | None = None) -> str:
    """拼一条单行注入文案；未知 kind 按电量处理（不因主语缺失丢事件）。"""
    tag = NOTICE_TAGS.get(kind) or NOTICE_TAGS[NOTICE_KIND_BATTERY]
    moment = (now or datetime.now(TZ_BEIJING)).astimezone(TZ_BEIJING)
    body = " ".join(str(text or "").split())
    return f"【{tag}·自动触发】{moment.strftime('%H:%M')} {body}{SILENT_SUFFIX}"


class XiaokeLowKeyNotifier:
    """单 worker、FIFO、忙时等待的低调注入器。

    ``is_busy()`` 与 ``inject(text)`` 都在 ``lock`` 内调用；``inject`` 返回
    真值视为投递成功。所有依赖可注入，便于测试。
    """

    def __init__(
        self,
        *,
        is_busy: Callable[[], bool],
        inject: Callable[[str], Any],
        lock: Any,
        poll_seconds: float = BUSY_POLL_SECONDS,
        max_wait_seconds: float = BUSY_MAX_WAIT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        start_worker: bool = True,
    ) -> None:
        self._is_busy = is_busy
        self._inject = inject
        self._lock = lock
        self._poll_seconds = poll_seconds
        self._max_wait_seconds = max_wait_seconds
        self._sleep = sleep
        self._clock = clock
        self._start_worker = start_worker
        self._queue: queue.Queue[str] = queue.Queue(maxsize=QUEUE_MAX)
        self._worker: threading.Thread | None = None
        self._worker_guard = threading.Lock()

    def enqueue(self, text: str) -> bool:
        text = " ".join(str(text or "").split())
        if not text:
            return False
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            logger.warning("device notice queue full; dropping one notice")
            return False
        if self._start_worker:
            self._ensure_worker()
        return True

    def pending(self) -> int:
        return self._queue.qsize()

    def _ensure_worker(self) -> None:
        with self._worker_guard:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run, name="xiaoke-device-notice", daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                self.deliver(text)
            except Exception:
                logger.warning("device notice delivery crashed", exc_info=True)
            finally:
                self._queue.task_done()

    def drain_once(self) -> bool:
        """同步投递队首一条（测试/无 worker 模式用）。队列空返回 False。"""
        try:
            text = self._queue.get_nowait()
        except queue.Empty:
            return False
        try:
            return self.deliver(text)
        finally:
            self._queue.task_done()

    def deliver(self, text: str) -> bool:
        """等小克空闲（有上限）后在锁内注入一条；返回是否投递成功。"""
        deadline = self._clock() + self._max_wait_seconds
        while True:
            with self._lock:
                busy = False
                try:
                    busy = bool(self._is_busy())
                except Exception:
                    logger.debug("device notice busy probe failed", exc_info=True)
                timed_out = self._clock() >= deadline
                if not busy or timed_out:
                    if busy:
                        logger.warning(
                            "device notice waited %.0fs for xiaoke idle; injecting anyway",
                            self._max_wait_seconds,
                        )
                    try:
                        ok = bool(self._inject(text))
                    except Exception:
                        logger.warning("device notice inject raised", exc_info=True)
                        ok = False
                    if not ok:
                        logger.warning("device notice inject failed")
                    return ok
            self._sleep(self._poll_seconds)
