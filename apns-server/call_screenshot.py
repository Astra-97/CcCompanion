#!/usr/bin/env python3
"""call-screenshot — grab one frame of 方小南's phone screen during a call.

Installed as /usr/local/bin/call-screenshot (symlink to this file).  Prints the
absolute path of the saved image on success (exit 0).  Failures print a clear
reason to stderr:

  exit 2  no_active_call   没有进行中的通话 / App 太旧
  exit 3  not_authorized   她没开屏幕共享
  exit 4  timeout          10 秒内没收到图
  exit 1  anything else

See screen_capture.py for the mailbox protocol.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import screen_capture  # noqa: E402

EXIT_CODES = {"no_active_call": 2, "not_authorized": 3, "timeout": 4}


def run(mailbox: screen_capture.Mailbox, timeout: float, *, sleep=time.sleep, clock=time.monotonic) -> tuple[int, str]:
    request_id = mailbox.new_request()
    deadline = clock() + timeout
    while clock() < deadline:
        result = mailbox.poll_result(request_id)
        if result is not None:
            mailbox.abandon(request_id)
            if result.get("ok") and result.get("path"):
                return 0, str(result["path"])
            code = str(result.get("error") or "unknown")
            message = screen_capture.ERROR_MESSAGES.get(code, code)
            detail = str(result.get("detail") or "")
            return EXIT_CODES.get(code, 1), f"{code}: {message}" + (f" ({detail})" if detail else "")
        sleep(0.2)
    mailbox.abandon(request_id)
    return EXIT_CODES["timeout"], f"timeout: {screen_capture.ERROR_MESSAGES['timeout']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通话中抓一帧方小南的手机屏幕，打印图片绝对路径")
    parser.add_argument("--timeout", type=float, default=10.0, help="最多等几秒（默认 10）")
    args = parser.parse_args(argv)
    code, text = run(screen_capture.Mailbox(), max(1.0, min(60.0, args.timeout)))
    print(text, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
