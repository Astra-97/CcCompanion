#!/usr/bin/env python3
"""Durable, Xia-home-only native session state helpers for launcher.sh."""
from __future__ import annotations
import json, os, stat, sys
from pathlib import Path

def has_transcript(home: str | Path, session_id: str) -> bool:
    root = Path(home) / ".claude" / "projects"
    if not root.is_dir() or not session_id:
        return False
    for candidate in root.rglob(f"{session_id}.jsonl"):
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size > 0:
            return True
    return False

def write_marker(path: str | Path, generation: int, session_id: str, model: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump({"generation": int(generation), "session_id": session_id, "model": model}, handle)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.chmod(temp, 0o600); os.replace(temp, target); os.chmod(target, 0o600)
    fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(fd)
    finally: os.close(fd)

def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[1] == "mode":
        print("resume" if has_transcript(argv[2], argv[3]) else "fresh")
        return 0
    if len(argv) == 6 and argv[1] == "write-marker":
        write_marker(argv[2], int(argv[3]), argv[4], argv[5])
        return 0
    return 64

if __name__ == "__main__": raise SystemExit(main(sys.argv))
