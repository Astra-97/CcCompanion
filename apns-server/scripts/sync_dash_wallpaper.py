#!/usr/bin/env python3
"""把当前聊天壁纸同步成 xiaonan-dash 插件的内置静态壁纸 (2026-10-10)。

背景: 插件 WebView 的代取通道偶发吞单请求, 壁纸走 appearance-asset 端点
会偶发长时间不出图。把壁纸压缩成插件目录下的 wallpaper.jpg 后, 它随
app.js/style.css 同一批静态请求到达, 一点开就有。本脚本由 cron 每分钟
跑一次, 只在源文件更新时重写目标; 壁纸换成新图后约 1 分钟内面板跟上。

只读 user_settings.json 和 appearance_assets, 目标只写一个文件。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SETTINGS = HERE / "state" / "user_settings.json"
ASSETS = HERE / "state" / "appearance_assets"
TARGET = HERE / "plugins-builtin" / "xiaonan-dash" / "wallpaper.jpg"

MAX_DIM = 1440
JPEG_QUALITY = 82


def current_bg_file() -> Path | None:
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return None
    bg = str(((d.get("appearance") or {}).get("bgUri")) or "").strip()
    prefix = "/appearance-assets/"
    if not bg.startswith(prefix):
        return None
    name = bg[len(prefix):]
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return None
    path = ASSETS / name
    return path if path.is_file() else None


def main() -> int:
    src = current_bg_file()
    if src is None:
        if TARGET.exists():
            TARGET.unlink()
            print("no wallpaper configured -> removed bundled copy")
        return 0
    if TARGET.exists() and TARGET.stat().st_mtime >= src.stat().st_mtime:
        return 0  # 已是最新
    from PIL import Image
    img = Image.open(src)
    img.load()  # 截断文件在这里抛错, 不会写出半个图
    img = img.convert("RGB")
    if max(img.size) > MAX_DIM:
        scale = MAX_DIM / max(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    tmp = TARGET.with_suffix(".tmp")
    img.save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    os.replace(tmp, TARGET)
    print(f"synced {src.name} ({src.stat().st_size}B) -> {TARGET.name} ({TARGET.stat().st_size}B, {img.size})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
